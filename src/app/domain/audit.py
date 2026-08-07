"""П. 6.3 ТЗ — журнал, допускающий только добавление."""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType


class AuditAction(StrEnum):
    """Закрытый словарь вместо свободного текста.

    П. 6.4 требует, чтобы каждое действие оставляло запись; фиксированный набор
    — это то, что делает журнал пригодным для запросов («покажи все закрытия за
    март»), а не грудой прозы, которая хорошо ищется разве что грепом.
    """

    PROBLEM_CREATED = "problem_created"
    PROBLEM_ACKNOWLEDGED = "problem_acknowledged"
    # Ручная передача L1 → L2. Отделена от ESCALATION_TRIGGERED намеренно: в
    # разборе инцидента «человек оценил и передал» и «никто не отреагировал,
    # сработал таймер» — принципиально разные события, и слив их в одно значение
    # сделал бы невозможным честный ответ на вопрос, работает ли L1.
    PROBLEM_HANDED_OVER = "problem_handed_over"
    PROBLEM_CLOSED = "problem_closed"
    PROBLEM_RESOLVED = "problem_resolved"
    NOTIFICATION_SENT = "notification_sent"
    NOTIFICATION_SUPPRESSED = "notification_suppressed"
    # Номер и цель ступени кладутся в `details`: заводить по значению на каждую
    # ступень означало бы править словарь при каждой правке лестницы.
    ESCALATION_TRIGGERED = "escalation_triggered"
    MAINTENANCE_STARTED = "maintenance_started"
    MAINTENANCE_ENDED = "maintenance_ended"
    INTEGRATION_CHANGED = "integration_changed"
    SETTINGS_CHANGED = "settings_changed"
    SUBSCRIPTION_CHANGED = "subscription_changed"


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """Одна неизменяемая запись.

    `frozen=True` и read-only отображение `details` выражают «только добавление»
    настолько, насколько это вообще доступно средствами Python. Настоящая
    гарантия должна прийти из базы: у роли приложения не должно быть грантов
    UPDATE и DELETE на эту таблицу. Датакласс не остановит
    `session.execute(update(...))`, так что считай это документацией намерения,
    которое обязана обеспечить миграция.

    `actor_id` допускает None, потому что действует и сама платформа:
    автоматическое закрытие по возврату объекта в норму и таймеры эскалации не
    имеют человека за спиной, а выдумывать для них фиктивный id пользователя
    означало бы испортить выборки «кто это сделал».
    """

    id: uuid.UUID
    occurred_at: datetime
    action: AuditAction
    actor_id: uuid.UUID | None
    problem_id: uuid.UUID | None
    # Хранится плоскими строками: журнал обязан оставаться читаемым и через
    # годы, не завися от того, существуют ли ещё сегодняшние формы объектов,
    # чтобы его десериализовать.
    details: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        action: AuditAction,
        *,
        actor_id: uuid.UUID | None = None,
        problem_id: uuid.UUID | None = None,
        details: Mapping[str, str] | None = None,
        at: datetime | None = None,
    ) -> "AuditRecord":
        return cls(
            id=uuid.uuid4(),
            occurred_at=at or datetime.now(UTC),
            action=action,
            actor_id=actor_id,
            problem_id=problem_id,
            # Защитная копия под read-only обёрткой: без неё у вызывающего
            # остаётся живая ссылка на словарь, и он смог бы править «замороженную»
            # запись.
            details=MappingProxyType(dict(details or {})),
        )
