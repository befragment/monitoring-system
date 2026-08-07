"""Шаги 4 и 6 — изменяемый агрегат и его жизненный цикл.

Расхождение в ТЗ, которое стоит зафиксировать: п. 4.1 определяет fingerprint
через `monitor_name`, но payload из п. 2.1 такого поля не содержит. Поэтому
`monitor_name` сделан обязательным в `IncomingEvent`, и каждая интеграция
обязана его поставлять (для Zabbix это имя триггера). Альтернатива — выводить
его из текста сообщения — пересчитывала бы fingerprint при каждом изменении
процента в сообщении, то есть означала бы отсутствие дедупликации.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from app.domain.asset import Asset
from app.domain.errors import (
    AcknowledgeNotPermitted,
    AlreadyAcknowledged,
    CloseReasonRequired,
    FingerprintMismatch,
    HandoverNotAllowed,
    ProblemClosed,
)
from app.domain.event import Event, Fingerprint
from app.domain.role import Role
from app.domain.severity import Severity

# Пишется в `closed_by`, когда проблему закрывает сама платформа, чтобы журнал
# аудита отличал автоматическое восстановление от решения человека.
SYSTEM_ACTOR = "system"


class ProblemStatus(StrEnum):
    """П. 4.3 ТЗ: активна / в работе / закрыта.

    StrEnum (в отличие от Severity), потому что это неупорядоченные ярлыки —
    никто никогда не спросит, «больше» ли ACKNOWLEDGED, чем ACTIVE.

    Передача L1 → L2 сюда намеренно не добавлена четвёртым статусом: она не
    меняет состояние проблемы для внешнего мира (никто ещё не взял её в работу),
    меняется только адресат. Это `level`, а не статус.
    """

    ACTIVE = "active"
    ACKNOWLEDGED = "acknowledged"
    CLOSED = "closed"


class ProblemLevel(StrEnum):
    """На чьей стороне разбор прямо сейчас.

    L1 работает прокси для любого уведомления: разбирает сам либо передаёт
    дальше. Переход только вперёд — см. `HandoverNotAllowed`.
    """

    L1 = "l1"
    L2 = "l2"


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class Problem:
    """Единственная сущность модели, которая меняется во времени.

    Каждый мутатор принимает время `at` явно (по умолчанию — сейчас), чтобы
    тесты могли прогонять тайминги эскалации без патчинга часов, а воркер,
    разгребающий бэклог, использовал время самого события, а не стенных часов.

    Дедупликация ищет совпадение fingerprint **только среди незакрытых
    проблем**: закрытая не воскресает никогда (см. `ProblemClosed`). Слой
    хранения обязан это подкрепить — частичный уникальный индекс по
    `fingerprint` с условием `status <> 'closed'`, и поиск на горячем пути по
    тому же условию.
    """

    id: uuid.UUID
    fingerprint: Fingerprint
    # Slug интеграции — то же значение, что пришло в `source_system` payload'а и
    # что участвует в fingerprint. Хранится строкой, а не ссылкой на
    # `Integration`, чтобы приём события не требовал похода в справочник.
    source_system: str
    monitor_name: str
    asset: Asset

    severity: Severity
    # П. 5.1 ТЗ передаёт `prev_state`, чтобы получатель видел переход, не
    # обращаясь к системе-источнику. Здесь хранится последняя *отличавшаяся*
    # критичность, а не просто предыдущее значение: иначе повтор на том же
    # уровне давал бы «CRITICAL -> CRITICAL» и терял контекст.
    previous_severity: Severity

    status: ProblemStatus
    level: ProblemLevel
    first_seen_at: datetime
    last_seen_at: datetime
    # П. 4.2: счётчик, который растёт вместо порождения дублирующих проблем.
    event_count: int
    last_message: str
    tags: frozenset[str] = field(default_factory=frozenset)

    owner_id: uuid.UUID | None = None
    acknowledged_at: datetime | None = None
    handed_over_at: datetime | None = None
    # Сколько ступеней лестницы уже отработало. Состояние эскалации живёт здесь,
    # а не в политике: политика — чистый value object, и без записанного
    # счётчика воркер после перезапуска либо разослал бы всю лестницу заново,
    # либо не разослал бы ничего.
    escalation_steps_taken: int = 0
    closed_at: datetime | None = None
    closed_by: str | None = None
    close_reason: str | None = None

    @classmethod
    def open_from(cls, event: Event) -> "Problem":
        """П. 4.2: fingerprint без активной проблемы открывает новую."""
        return cls(
            id=uuid.uuid4(),
            fingerprint=event.fingerprint,
            source_system=event.source_system,
            monitor_name=event.monitor_name,
            asset=event.asset,
            severity=event.severity,
            # Всё начинается с «раньше было нормально» — как в примере п. 5.1,
            # где `"prev_state": "OK"`.
            previous_severity=Severity.OK,
            status=ProblemStatus.ACTIVE,
            # Любое уведомление сначала попадает на L1 — он прокси для всего.
            level=ProblemLevel.L1,
            first_seen_at=event.occurred_at,
            last_seen_at=event.occurred_at,
            event_count=1,
            last_message=event.message,
            tags=event.tags,
        )

    @property
    def is_open(self) -> bool:
        return self.status is not ProblemStatus.CLOSED

    @property
    def severity_changed(self) -> bool:
        """Сдвинуло ли последнее зарегистрированное событие критичность."""
        return self.severity is not self.previous_severity

    @property
    def escalation_active(self) -> bool:
        """Идут ли ещё часы эскалации.

        Реакцией считается только Acknowledge — ни просмотр карточки, ни ответ в
        чате, ни ручная передача на L2. Поэтому условие ровно одно: статус
        ACTIVE. Как только кто-то взял проблему в работу или она закрылась,
        лестница останавливается.
        """
        return self.status is ProblemStatus.ACTIVE

    def register(self, event: Event) -> None:
        """П. 4.2: свернуть повторное событие в уже существующую проблему.

        Проблема сохраняет идентичность — двигаются только счётчик, отметки
        времени и критичность (п. 4.3). Это самый горячий путь в системе: на
        пике из п. 2.3 (~50 событий/сек) почти все они попадают сюда, а не
        создают что-либо новое.
        """
        if event.fingerprint != self.fingerprint:
            raise FingerprintMismatch(
                f"event {event.id} does not belong to problem {self.id}"
            )
        if self.status is ProblemStatus.CLOSED:
            # Не воскрешаем: вызывающий обязан открыть новую проблему, и именно
            # это делает повторную поломку отдельным эпизодом в истории.
            raise ProblemClosed(f"problem {self.id} is closed")

        if event.is_recovery:
            self.resolve(at=event.occurred_at)
            return

        self.event_count += 1
        # Защита от доставки не по порядку: ретрай из очереди не должен тянуть
        # last_seen_at назад и выставлять проблему протухшей для эскалации.
        self.last_seen_at = max(self.last_seen_at, event.occurred_at)
        self.last_message = event.message
        # Теги накапливаются: источник может добавить «msk-dc1» только на
        # поздних срабатываниях, а маршрутизация (п. 5.6) должна видеть всё, что
        # известно о проблеме.
        self.tags |= event.tags

        if event.severity is not self.severity:
            self.previous_severity = self.severity
            self.severity = event.severity

    def acknowledge(
        self,
        user_id: uuid.UUID,
        role: Role,
        at: datetime | None = None,
    ) -> None:
        """П. 6.1 — обязательный шаг «взято в работу».

        `role` принимается явно, потому что право нажать эту кнопку есть не у
        всех: manager подписан на ту же рассылку, что L2, и видит проблему, но
        в разборе не участвует; admin не участвует тем более. Проверка живёт
        здесь, а не в схеме запроса, чтобы правило держалось для всех входов —
        HTTP-API, чат-бота из п. 6.2 и любого будущего CLI.
        """
        if not role.can_acknowledge:
            raise AcknowledgeNotPermitted(
                f"role {role} cannot acknowledge problems"
            )
        if self.status is ProblemStatus.CLOSED:
            raise ProblemClosed(f"problem {self.id} is closed")
        if self.status is ProblemStatus.ACKNOWLEDGED:
            raise AlreadyAcknowledged(
                f"problem {self.id} is already owned by {self.owner_id}"
            )

        self.status = ProblemStatus.ACKNOWLEDGED
        self.owner_id = user_id
        self.acknowledged_at = at or _utcnow()

    def hand_over(self, at: datetime | None = None) -> None:
        """L1 передаёт разбор на L2, не дожидаясь таймера.

        Ручная передача засчитывается как реакция L1 — но не как реакция
        вообще: проблема остаётся ACTIVE, потому что в работу её всё ещё никто
        не взял. Практическое следствие: ступень «L2, командный канал» больше не
        сработает (она только что произошла по инициативе человека), а таймер
        последней ступени продолжает идти. Иначе передача была бы способом
        похоронить проблему — отдал дальше, и больше про неё никто не вспомнит.

        Возврата L2 → L1 нет: он позволил бы гонять проблему между уровнями,
        размывая ответственность, а лестница эскалации всё равно не ходит назад.
        """
        if self.status is ProblemStatus.CLOSED:
            raise ProblemClosed(f"problem {self.id} is closed")
        if self.level is ProblemLevel.L2:
            raise HandoverNotAllowed(f"problem {self.id} is already on l2")

        self.level = ProblemLevel.L2
        self.handed_over_at = at or _utcnow()
        # Ступень «L1» и ступень «L2, командный канал» считаем пройденными:
        # первая отработала при создании, вторая — только что, вручную.
        self.escalation_steps_taken = max(self.escalation_steps_taken, 2)

    def escalate(self, at: datetime | None = None) -> None:
        """Отметить, что очередная ступень лестницы отработала.

        Вызывается воркером эскалации после фактической отправки, а не до неё:
        счётчик — это запись о случившемся, и увеличивать его авансом значит
        рисковать молча проглотить ступень при падении доставки.
        """
        self.escalation_steps_taken += 1
        # Со второй ступени разбор официально на стороне L2, даже если никто
        # ничего не передавал руками.
        if self.escalation_steps_taken >= 2:
            self.level = ProblemLevel.L2
            self.handed_over_at = self.handed_over_at or at or _utcnow()

    def close(
        self,
        reason: str,
        closed_by: str,
        at: datetime | None = None,
    ) -> None:
        """П. 6.1: закрытие без указания причины недопустимо.

        Проверка живёт здесь, а не в схеме запроса, по той же причине, что и в
        `acknowledge`. Критерий п. 6.4 сформулирован как «технически
        невозможно», а валидатор на одной точке входа этого не даёт.
        """
        if not reason or not reason.strip():
            raise CloseReasonRequired(
                f"problem {self.id} cannot be closed without a reason"
            )
        if self.status is ProblemStatus.CLOSED:
            raise ProblemClosed(f"problem {self.id} is already closed")

        self.status = ProblemStatus.CLOSED
        self.close_reason = reason.strip()
        self.closed_by = closed_by
        self.closed_at = at or _utcnow()

    def resolve(self, at: datetime | None = None) -> None:
        """Автоматическое закрытие, когда объект вернулся в норму (п. 4.3).

        Проходит через то же терминальное состояние, что и ручное закрытие, но с
        сгенерированной причиной и системным актором: так правило «нет закрытия
        без причины» остаётся ненарушенным и при отсутствии человека, а журнал
        аудита отличает автоматическое восстановление от решения дежурного.
        """
        if self.status is ProblemStatus.CLOSED:
            return  # События восстановления часто дублируются — остаёмся идемпотентными.

        self.previous_severity = self.severity
        self.severity = Severity.OK
        self.status = ProblemStatus.CLOSED
        self.close_reason = "resolved automatically: object returned to normal"
        self.closed_by = SYSTEM_ACTOR
        self.closed_at = at or _utcnow()
