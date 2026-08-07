"""Шаги 3–5 — обработка события из очереди. Главный сценарий системы.

Классификация, дедупликация, решение о доставке. Всё остальное в приложении —
ветки и обвязка этого пути.

Транзакционная граница проведена так, чтобы сообщение нельзя было отправить
раньше, чем зафиксирована проблема: сначала коммитится проблема вместе с
событием, аудитом и записями `Delivery` в статусе PENDING, и только потом
отдельный воркер их разбирает. Обратный порядок дал бы алерт по проблеме,
которой нет в базе, а отправка внутри транзакции — потерю уведомления при
падении между коммитом и вызовом телеграма. `Delivery` в PENDING играет роль
outbox.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.domain.audit import AuditAction, AuditRecord
from app.domain.errors import ProblemClosed
from app.domain.event import Event, IncomingEvent
from app.domain.problem import Problem
from app.repository._contracts import (
    AuditRepository,
    EventRepository,
    IntegrationRepository,
    ProblemAlreadyOpen,
    ProblemRepository,
    UnitOfWork,
)
from app.service.errors import UnknownIntegration
from app.service.routing import RoutingOutcome, RoutingService


class ProcessingOutcome(StrEnum):
    """Что случилось с событием. Нужен не только логам: на этом же различии
    строится решение, слать ли оповещение."""

    DUPLICATE = "duplicate"
    """Событие уже обрабатывали — ретрай источника."""

    PROBLEM_OPENED = "problem_opened"
    REPEAT_REGISTERED = "repeat_registered"
    SEVERITY_CHANGED = "severity_changed"
    PROBLEM_RESOLVED = "problem_resolved"


@dataclass(frozen=True, slots=True)
class ProcessingResult:
    outcome: ProcessingOutcome
    problem: Problem | None = None
    routing: RoutingOutcome | None = None


class EventProcessingService:
    def __init__(
        self,
        integrations: IntegrationRepository,
        events: EventRepository,
        problems: ProblemRepository,
        audit: AuditRepository,
        routing: RoutingService,
        uow: UnitOfWork,
    ) -> None:
        self._integrations = integrations
        self._events = events
        self._problems = problems
        self._audit = audit
        self._routing = routing
        self._uow = uow

    async def process(self, incoming: IncomingEvent) -> ProcessingResult:
        integration = await self._integrations.get_by_slug(incoming.source_system)
        if integration is None or not integration.is_enabled:
            raise UnknownIntegration(
                f"unknown or disabled source system: {incoming.source_system!r}"
            )

        # Идемпотентность: источник ретраит webhook, и то же событие не должно
        # второй раз увеличить счётчик повторов проблемы.
        if await self._events.exists(incoming.source_system, incoming.source_event_id):
            return ProcessingResult(outcome=ProcessingOutcome.DUPLICATE)

        # Шаг 3 целиком — одним вызовом домена.
        event = incoming.classify(integration.severity_mapping)

        problem, outcome = await self._apply(event)
        await self._events.add(event, problem_id=problem.id)

        routing = await self._route_if_needed(problem, outcome)
        await self._uow.commit()
        return ProcessingResult(outcome=outcome, problem=problem, routing=routing)

    async def _apply(self, event: Event) -> tuple[Problem, ProcessingOutcome]:
        """Шаг 4 — открыть новую проблему или свернуть событие в существующую."""
        problem = await self._problems.find_open_by_fingerprint(event.fingerprint)

        if problem is None:
            return await self._open(event)

        # Снимок до записи. `problem.severity_changed` для этого не годится: он
        # отвечает на другой вопрос — «отличается ли текущая критичность от
        # последней отличавшейся», и с момента создания проблемы истинен всегда
        # (CRITICAL против стартового OK). Нам же нужно, сдвинуло ли уровень
        # именно это событие.
        severity_before = problem.severity

        try:
            problem.register(event)
        except ProblemClosed:
            # Проблему закрыли между поиском и записью. Это не сбой: закрытая не
            # воскресает, значит событие открывает новый эпизод.
            return await self._open(event)

        await self._problems.save(problem)

        if not problem.is_open:
            # `register` сам вызвал `resolve()` — объект вернулся в норму (п. 4.3).
            await self._audit.add(
                AuditRecord.create(
                    AuditAction.PROBLEM_RESOLVED,
                    problem_id=problem.id,
                    at=problem.closed_at,
                    details={"reason": problem.close_reason or ""},
                )
            )
            return problem, ProcessingOutcome.PROBLEM_RESOLVED

        if problem.severity is not severity_before:
            return problem, ProcessingOutcome.SEVERITY_CHANGED
        return problem, ProcessingOutcome.REPEAT_REGISTERED

    async def _open(self, event: Event) -> tuple[Problem, ProcessingOutcome]:
        """П. 4.2 — fingerprint без активной проблемы открывает новую."""
        problem = Problem.open_from(event)
        try:
            await self._problems.add(problem)
        except ProblemAlreadyOpen:
            # Гонка двух воркеров: пока мы собирали проблему, её создал сосед.
            # При 50 событиях/сек это штатный исход, а не ошибка, — перечитываем
            # и регистрируем повтор. Частичный уникальный индекс по fingerprint
            # для незакрытых проблем существует ровно ради этой развилки.
            existing = await self._problems.find_open_by_fingerprint(event.fingerprint)
            if existing is None:
                raise
            existing.register(event)
            await self._problems.save(existing)
            return existing, ProcessingOutcome.REPEAT_REGISTERED

        await self._audit.add(
            AuditRecord.create(
                AuditAction.PROBLEM_CREATED,
                problem_id=problem.id,
                at=problem.first_seen_at,
                details={
                    "fingerprint": problem.fingerprint.value,
                    "asset": problem.asset.name,
                    "monitor": problem.monitor_name,
                    "severity": problem.severity.name,
                },
            )
        )
        return problem, ProcessingOutcome.PROBLEM_OPENED

    async def _route_if_needed(
        self, problem: Problem, outcome: ProcessingOutcome
    ) -> RoutingOutcome | None:
        """Молчание на повторах — это и есть дедупликация.

        Оповещение уходит на трёх исходах: проблема появилась, критичность
        сдвинулась, объект вернулся в норму. На обычном повторе не уходит ничего
        — иначе счётчик `event_count` терял бы смысл, а критерий п. 4.7
        («повторная отправка не создаёт новых проблем») выполнялся бы формально
        при том же потоке сообщений человеку.
        """
        if outcome is ProcessingOutcome.REPEAT_REGISTERED:
            return None
        if outcome is ProcessingOutcome.DUPLICATE:
            return None

        routing = await self._routing.route(problem)

        action = (
            AuditAction.NOTIFICATION_SUPPRESSED
            if routing.suppression is not None
            else AuditAction.NOTIFICATION_SENT
        )
        await self._audit.add(
            AuditRecord.create(
                action,
                problem_id=problem.id,
                details={
                    "recipients": str(len(routing.deliveries)),
                    "decision": routing.decision.value,
                    "reason": routing.suppression.value if routing.suppression else "",
                },
            )
        )
        return routing
