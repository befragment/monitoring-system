"""Supression - подавление


Правила, решающие, что *не* будет отправлено (пп. 4.4–4.6 ТЗ).

Здесь живут только политики. Сам подсчёт — забота Redis на сервисном слое:
доменный объект, хранящий скользящее окно, был бы состоянием, которое не
переживает перезапуск и не разделяется между воркерами. Всё, что ниже,
принимает уже измеренное число и отвечает на вопрос «да или нет».
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from app.domain.asset import normalize
from app.domain.errors import DomainError


class SuppressionReason(StrEnum):
    """Почему оповещение было придержано.

    Записывается, а не выбрасывается: п. 7.3 требует причины по недоставленным
    сообщениям, и «мы подавили его намеренно» обязано отличаться от «доставка
    не удалась». Без этого различия подавленное по регламенту сообщение попадёт
    в недоставленные, и доля успешной доставки покажет деградацию там, где всё
    отработало как задумано.
    """

    NONE = "none"
    MAINTENANCE = "maintenance"
    RECIPIENT_RATE_LIMIT = "recipient_rate_limit"
    SOURCE_RATE_LIMIT = "source_rate_limit"


class DeliveryDecision(StrEnum):
    """Исход стадии подавления для одного оповещения."""

    DELIVER = "deliver"
    # П. 4.6: свернуть в одно сводное сообщение вместо отправки каждого алерта.
    DIGEST = "digest"
    # П. 4.5: проглотить целиком — плановые работы, никому знать не нужно.
    DROP = "drop"


@dataclass(frozen=True, slots=True)
class MaintenanceWindow:
    """П. 4.5 — плановые работы, объявляемые инженером перед регламентом.

    Зачем это вообще нужно: инженер в 23:00 останавливает объект на регламент,
    мониторинг честно видит недоступность и выдаёт CRITICAL, а дальше по
    лестнице будят L1, через 15 минут L2, через 30 — всех L2 разом. Ночью.
    Из-за работ, которые сами же и запланировали. Без окна остаётся либо терпеть
    (и приучать команду, что ночной CRITICAL — «наверное, опять регламент»,
    после чего настоящую аварию тоже пролистают), либо глушить всё целиком (и
    пропустить аварию на соседнем объекте).

    Глушим у себя, а не в источнике: п. 0.2 ТЗ требует, чтобы настройки
    источников — правила и триггеры — оставались без изменений.

    Привязка к конкретному объекту, а не ко всей системе, — прямое требование
    п. 4.5, поэтому `asset_name` проверяется на непустоту: пустое значение
    заглушило бы всю платформу, то есть ровно тот способ спрятать аварию, ради
    предотвращения которого ограничение и существует.

    `created_by` и `reason` — не бюрократия, а ответ на вопрос, который задают
    уже после инцидента: «почему в три ночи не было ни одного алерта по этому
    объекту?». Без них заглушенный объект неотличим от сломанной доставки.
    """

    id: uuid.UUID
    asset_name: str
    starts_at: datetime
    ends_at: datetime
    created_by: uuid.UUID
    reason: str

    def __post_init__(self) -> None:
        if not self.asset_name.strip():
            raise DomainError("a maintenance window must name a specific asset")
        if not self.reason.strip():
            raise DomainError("a maintenance window must state a reason")
        # Конец обязателен и обязан быть позже начала: главное свойство окна в
        # том, что оно само выключается. «Заглушил и забыл включить обратно» —
        # самый частый способ пропустить аварию, и бессрочное окно вернуло бы
        # эту ошибку в обиход.
        if self.ends_at <= self.starts_at:
            raise DomainError("a maintenance window must end after it starts")

    def covers(self, asset_name: str, at: datetime) -> bool:
        # Полуоткрытый интервал: окно, кончающееся в 14:00, и окно, начинающееся
        # в 14:00, не должны оба претендовать на этот момент.
        return (
            normalize(asset_name) == normalize(self.asset_name)
            and self.starts_at <= at < self.ends_at
        )


@dataclass(frozen=True, slots=True)
class RateLimit:
    """Бюджет из N событий за окно.

    Используется в двух разных масштабах, поэтому это value object, а не
    константа: п. 2.5 ограничивает приём от интеграции, п. 4.6 — доставку одному
    получателю. Арифметика та же, числа совершенно разные.
    """

    max_events: int
    per: timedelta

    def __post_init__(self) -> None:
        if self.max_events < 1:
            raise DomainError("a rate limit must allow at least one event")

    def exceeded(self, observed: int) -> bool:
        return observed > self.max_events


@dataclass(frozen=True, slots=True)
class GroupingRule:
    """П. 4.4 — группировка помимо точного совпадения fingerprint.

    Теги в payload из п. 2.1 — просто строки, а не пары ключ/значение, поэтому
    ключом служит пересечение тегов события с тегами, объявленными значимыми
    здесь. Ограничение объявленным набором принципиально: группировка по *всем*
    тегам поместила бы каждое событие в собственную группу, как только источник
    добавит уникальную метку, а это молча отключает построенную сверху защиту от
    шторма.

    Пример: при tags={"prod", "msk-dc1"} событие с тегами
    ["disk", "prod", "msk-dc1"] попадёт в группу ("msk-dc1", "prod") — одна
    сводка на дата-центр при падении стойки вместо сообщения на каждый диск.
    """

    tags: frozenset[str] = field(default_factory=frozenset)

    def key_for(self, event_tags: frozenset[str]) -> tuple[str, ...]:
        # Сортировка делает ключ стабильным независимо от порядка тегов в payload.
        return tuple(sorted(event_tags & self.tags))


# Значения по умолчанию для п. 4.6. Намеренно консервативные: 20 сообщений в час
# одному человеку — уже на грани того, что вообще читают, а 100 событий в минуту
# от одной интеграции заметно выше пика из п. 2.3 (~50/сек) как *нормального*
# трафика — за этой чертой идёт каскад, и одна сводка полезнее 3000 алертов.
DEFAULT_RECIPIENT_RATE_LIMIT = RateLimit(max_events=20, per=timedelta(hours=1))
DEFAULT_SOURCE_RATE_LIMIT = RateLimit(max_events=100, per=timedelta(minutes=1))
