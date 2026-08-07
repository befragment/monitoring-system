"""Шаг 5 — то, что уходит из платформы, и что из этого вышло."""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from app.domain.problem import Problem, ProblemStatus
from app.domain.severity import Severity
from app.domain.suppression import SuppressionReason


class RoutingChannel(StrEnum):
    """П. 5.2 ТЗ. Webhook — обязательный: через него достижима любая другая
    интеграция, включая мессенджеры, и это удерживает слой доставки от
    обрастания отдельным частным случаем на каждого вендора."""

    WEBHOOK = "webhook"
    MAIL = "mail"
    MATTERMOST = "mattermost"
    TELEGRAM = "telegram"
    # П. 5.3: это не обычный webhook. TrueConf требует постоянно живущего
    # процесса-бота на WebSocket (п. 5.3.3), поэтому канал не может доставляться
    # тем же HTTP-воркером «отправил и забыл», что и остальные.
    TRUECONF = "trueconf"


# П. 5.1 передаёт `priority` числом для машинных потребителей. Меньше — срочнее,
# как в примере ТЗ, где у CRITICAL стоит `"priority": 0`.
_PRIORITY_BY_SEVERITY = {
    Severity.CRITICAL: 0,
    Severity.MAJOR: 1,
    Severity.WARNING: 2,
    Severity.INFO: 3,
    Severity.OK: 4,
}


@dataclass(frozen=True, slots=True)
class NotificationMessage:
    """Payload из п. 5.1 ТЗ — внешний контракт платформы.

    Frozen, потому что сообщение — это снимок проблемы на момент времени: если
    критичность сдвинулась после сборки, правильное поведение — новое сообщение,
    а не переписывание того, что уже лежит в очереди на доставку.
    """

    subject: str
    text: str
    priority: int
    monitor_name: str
    state: str
    prev_state: str
    object_id: str
    resolved: bool
    owner: str | None
    created_at: datetime
    count: int

    @classmethod
    def from_problem(
        cls,
        problem: Problem,
        *,
        owner: str | None = None,
        created_at: datetime | None = None,
    ) -> "NotificationMessage":
        """Отрисовывает проблему сразу для человека и для машины.

        `owner` передаётся снаружи, а не читается из проблемы, потому что домен
        хранит идентификатор, а сообщению нужно отображаемое имя, — сопоставлять
        одно с другим это работа сервисного слоя, а не агрегата.
        """
        resolved = problem.status is ProblemStatus.CLOSED
        prefix = "RESOLVED" if resolved else problem.severity.name
        subject = f"[{prefix}] {problem.asset.name} — {problem.monitor_name}"

        text = problem.last_message
        if problem.event_count > 1:
            # Счётчик повторов нужен и в человекочитаемом теле: «повторов: 47» —
            # это разница между кратковременным сбоем и текущей аварией.
            text = f"{text} (повторов: {problem.event_count})"

        return cls(
            subject=subject,
            text=text,
            priority=_PRIORITY_BY_SEVERITY[problem.severity],
            monitor_name=problem.monitor_name,
            state=problem.severity.name,
            prev_state=problem.previous_severity.name,
            object_id=problem.asset.name,
            resolved=resolved,
            owner=owner,
            created_at=created_at or problem.last_seen_at,
            count=problem.event_count,
        )

    def as_payload(self) -> dict[str, Any]:
        """Сериализация ровно в набор ключей из п. 5.1.

        Написано руками, а не через `asdict()`, чтобы внешний контракт был виден
        и стабилен: переименование поля здесь молча сломало бы каждую
        подписанную внешнюю систему, и это обязано быть осознанной правкой.
        """
        return {
            "subject": self.subject,
            "text": self.text,
            "priority": self.priority,
            "monitor_name": self.monitor_name,
            "state": self.state,
            "prev_state": self.prev_state,
            "object_id": self.object_id,
            "resolved": self.resolved,
            "owner": self.owner,
            "created_at": self.created_at.isoformat(),
            "count": self.count,
        }


@dataclass(frozen=True, slots=True)
class Digest:
    """П. 4.6: одно сводное сообщение вместо поштучной доставки, когда источник
    превысил порог.

    Сделан отдельным типом, а не флагом на NotificationMessage, потому что
    адресуется к множеству проблем и не имеет осмысленных `prev_state` и
    `object_id` — впихивание его в форму п. 5.1 означало бы враньё в этих полях.
    """

    subject: str
    text: str
    problem_ids: tuple[uuid.UUID, ...]
    suppressed_count: int
    created_at: datetime

    @property
    def priority(self) -> int:
        """Шторм как минимум не менее срочен, чем отдельные его алерты."""
        return _PRIORITY_BY_SEVERITY[Severity.CRITICAL]


class DeliveryStatus(StrEnum):
    """Что стало с одной попыткой доставки.

    SUPPRESSED отделён от FAILED намеренно: п. 7.3 требует показывать долю
    доставленных и причины по остальным, а подавленное по регламенту сообщение —
    это не сбой. Слив их в одно значение, дашборд показывал бы деградацию ровно
    там, где платформа отработала как задумано.
    """

    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    SUPPRESSED = "suppressed"


@dataclass(slots=True)
class Delivery:
    """Факт отправки: кому, куда, когда и с каким исходом.

    Сущность, без которой не отвечается ни один эксплуатационный вопрос:
    «почему мне не пришло» (правило совпало, но доставка упала? или подавлено, и
    чем именно?), «какая доля сообщений доставлена за сутки» (п. 7.3), «не
    слишком ли много ушло одному человеку за час» (лимит п. 4.6) и «какая
    ступень эскалации уже отработала».

    Изменяемая — в отличие от `NotificationMessage`. Само сообщение это
    неизменяемый снимок, а вот попытка его вручить проходит через состояния:
    поставлена в очередь, отправлена, не дошла. Пара «неизменяемый текст +
    изменяемый исход» — это то, что позволяет ретраить доставку, не искажая
    того, что было сказано.
    """

    id: uuid.UUID
    problem_id: uuid.UUID
    channel: RoutingChannel
    # Куда достучались: почтовый ящик, chat id, URL вебхука. Копируется в момент
    # отправки, а не берётся из подписки по ссылке: если человек завтра сменит
    # адрес, история обязана помнить, куда сообщение ушло на самом деле.
    address: str
    status: DeliveryStatus = DeliveryStatus.PENDING
    # Пусто, когда адресат — командный канал, а не человек: первые две ступени
    # лестницы пишут в канал L1 и канал L2, персональных получателей у них нет.
    recipient_id: uuid.UUID | None = None
    # Пусто, когда отправку инициировала эскалация, а не подписка (п. 5.6).
    # Разделение нужно, чтобы «мне не пришло» отвечалось по-разному: не совпало
    # правило подписки — это одно, лестница до тебя не дошла — совсем другое.
    subscription_id: uuid.UUID | None = None
    # Номер ступени лестницы, если отправку породила эскалация.
    escalation_step: int | None = None
    suppression_reason: SuppressionReason | None = None
    failure_reason: str | None = None
    attempts: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    settled_at: datetime | None = None

    @property
    def is_settled(self) -> bool:
        """Исход известен — ретраить больше нечего."""
        return self.status is not DeliveryStatus.PENDING

    def mark_delivered(self, at: datetime | None = None) -> None:
        self.status = DeliveryStatus.DELIVERED
        self.attempts += 1
        self.settled_at = at or datetime.now(UTC)

    def mark_failed(self, reason: str, at: datetime | None = None) -> None:
        """Доставка не удалась. Причина обязательна — ради неё запись и ведётся:
        «не дошло» без объяснения не отвечает ни на один вопрос п. 7.3."""
        self.status = DeliveryStatus.FAILED
        self.attempts += 1
        self.failure_reason = reason
        self.settled_at = at or datetime.now(UTC)

    def mark_suppressed(
        self,
        reason: SuppressionReason,
        at: datetime | None = None,
    ) -> None:
        """Отправку придержали намеренно. Попытки не считаем: её не было."""
        self.status = DeliveryStatus.SUPPRESSED
        self.suppression_reason = reason
        self.settled_at = at or datetime.now(UTC)
