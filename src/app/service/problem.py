"""Шаг 6 — работа дежурного.

Правила жизненного цикла живут в агрегате: право на Acknowledge, обязательность
причины закрытия, невозможность передать дважды. Сервис их не дублирует — он
подтягивает участников, ведёт транзакцию и пишет журнал. Дублирование проверок
здесь означало бы два места, где записано одно правило, и рано или поздно они
разойдутся.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from app.domain.audit import AuditAction, AuditRecord
from app.domain.event import Event
from app.domain.notification import Delivery
from app.domain.problem import Problem, ProblemStatus
from app.domain.role import Role
from app.domain.severity import Severity
from app.lib.clock import ClockInterface
from app.repository._contracts import (
    AuditRepository,
    DeliveryRepository,
    EventRepository,
    ProblemRepository,
    UnitOfWork,
    UserRepository,
)
from app.service.errors import NotFound, PermissionDenied
from app.service.routing import RoutingService


@dataclass(frozen=True, slots=True)
class ProblemCard:
    """Карточка проблемы: всё, что нужно показать дежурному на одном экране."""

    problem: Problem
    events: list[Event]
    deliveries: list[Delivery]
    audit: list[AuditRecord]
    owner_name: str | None


class ProblemService:
    def __init__(
        self,
        problems: ProblemRepository,
        events: EventRepository,
        deliveries: DeliveryRepository,
        audit: AuditRepository,
        users: UserRepository,
        routing: RoutingService,
        clock: ClockInterface,
        uow: UnitOfWork,
    ) -> None:
        self._problems = problems
        self._events = events
        self._deliveries = deliveries
        self._audit = audit
        self._users = users
        self._routing = routing
        self._clock = clock
        self._uow = uow

    async def acknowledge(self, problem_id: uuid.UUID, actor_id: uuid.UUID) -> Problem:
        """П. 6.1 — взять в работу.

        Роль берётся из справочника, а не из токена: claim в токене отражает
        состояние на момент выдачи, и человек, переведённый в manager час назад,
        до истечения токена продолжал бы нажимать кнопку, которой у него уже нет.
        """
        problem, actor = await self._load(problem_id, actor_id)
        problem.acknowledge(actor.id, actor.role, at=self._clock.now())
        await self._problems.save(problem)
        await self._audit.add(
            AuditRecord.create(
                AuditAction.PROBLEM_ACKNOWLEDGED,
                actor_id=actor.id,
                problem_id=problem.id,
                at=problem.acknowledged_at,
                details={"owner": actor.full_name},
            )
        )
        await self._uow.commit()
        return problem

    async def hand_over(self, problem_id: uuid.UUID, actor_id: uuid.UUID) -> Problem:
        """Передача L1 → L2.

        Передавать может только L1: для L2 это движение вбок, а manager с admin
        в разборе не участвуют вовсе.
        """
        problem, actor = await self._load(problem_id, actor_id)
        if actor.role is not Role.L1:
            raise PermissionDenied(f"role {actor.role} cannot hand a problem over to l2")

        problem.hand_over(at=self._clock.now())
        await self._problems.save(problem)
        await self._audit.add(
            AuditRecord.create(
                AuditAction.PROBLEM_HANDED_OVER,
                actor_id=actor.id,
                problem_id=problem.id,
                at=problem.handed_over_at,
                details={"from": Role.L1.value, "to": Role.L2.value},
            )
        )
        # Передача — это и уведомление тоже: L2 обязан узнать о ней сразу, а не
        # на следующем тике планировщика.
        await self._routing.route(problem)
        await self._uow.commit()
        return problem

    async def close(
        self, problem_id: uuid.UUID, actor_id: uuid.UUID, reason: str
    ) -> Problem:
        """П. 6.1 — закрытие с обязательной причиной.

        Пустая причина отсекается доменом (`CloseReasonRequired`), а не схемой
        запроса: критерий п. 6.4 требует, чтобы это было технически невозможно,
        а валидатор на одной точке входа такого не даёт.
        """
        problem, actor = await self._load(problem_id, actor_id)
        if not actor.role.can_acknowledge:
            raise PermissionDenied(f"role {actor.role} cannot close problems")

        problem.close(reason=reason, closed_by=str(actor.id), at=self._clock.now())
        await self._problems.save(problem)
        await self._audit.add(
            AuditRecord.create(
                AuditAction.PROBLEM_CLOSED,
                actor_id=actor.id,
                problem_id=problem.id,
                at=problem.closed_at,
                details={"reason": problem.close_reason or "", "by": actor.full_name},
            )
        )
        # Сообщение о закрытии снимает тревогу — его шлём всегда.
        await self._routing.route(problem)
        await self._uow.commit()
        return problem

    async def card(self, problem_id: uuid.UUID) -> ProblemCard:
        """Карточка со всем контекстом, включая ответ на «почему мне не пришло»:
        список доставок с их исходами и причинами."""
        problem = await self._problems.get(problem_id)
        if problem is None:
            raise NotFound(f"problem {problem_id} not found")

        owner_name = None
        if problem.owner_id is not None:
            owner = await self._users.get_by_id(problem.owner_id)
            owner_name = owner.full_name if owner else None

        return ProblemCard(
            problem=problem,
            events=await self._events.list_for_problem(problem_id),
            deliveries=await self._deliveries.list_for_problem(problem_id),
            audit=await self._audit.list_for_problem(problem_id),
            owner_name=owner_name,
        )

    async def search(
        self,
        *,
        statuses: Sequence[ProblemStatus] | None = None,
        min_severity: Severity | None = None,
        asset_name: str | None = None,
        tags: Sequence[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Problem]:
        return await self._problems.search(
            statuses=statuses,
            min_severity=min_severity,
            asset_name=asset_name,
            tags=tags,
            limit=limit,
            offset=offset,
        )

    async def _load(self, problem_id: uuid.UUID, actor_id: uuid.UUID):
        problem = await self._problems.get(problem_id)
        if problem is None:
            raise NotFound(f"problem {problem_id} not found")
        actor = await self._users.get_by_id(actor_id)
        if actor is None:
            raise NotFound(f"user {actor_id} not found")
        if not actor.is_active:
            raise PermissionDenied("inactive user cannot act on problems")
        return problem, actor
