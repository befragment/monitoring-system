"""Тик лестницы эскалации.

Порядок операций здесь не произволен: счётчик пройденных ступеней растёт
*после* того, как записи доставки созданы, а не до. Увеличить его авансом
значит рискнуть молча проглотить ступень, если создание доставок упадёт, —
и тогда никто никогда не узнает, что до человека не дошло.

Лестница в проекте ещё пересматривается (тайминги по критичности, ручная
передача вместо автоматической для WARNING и MAJOR). Сервис от этого не зависит:
он спрашивает у политики, что пора, и не знает, из скольких ступеней она состоит.
"""

from dataclasses import dataclass

from app.domain.audit import AuditAction, AuditRecord
from app.domain.escalation import EscalationPolicy, EscalationTarget
from app.domain.problem import Problem
from app.domain.role import Role
from app.lib.clock import ClockInterface
from app.lib.teamchannels import TeamChannelDirectoryInterface
from app.repository._contracts import (
    AuditRepository,
    ProblemRepository,
    SubscriptionRepository,
    UnitOfWork,
    UserRepository,
)
from app.service.routing import RoutingService


@dataclass(frozen=True, slots=True)
class EscalationReport:
    escalated: int
    skipped: int
    exhausted: int
    undeliverable: int
    """Ступень пора отправить, но некому: канал не настроен или нет активных L2.
    Считается отдельно — это дыра в конфигурации, из-за которой лестница молча
    обрывается, и она обязана быть видна в метриках, а не только в журнале."""


class EscalationService:
    def __init__(
        self,
        problems: ProblemRepository,
        users: UserRepository,
        subscriptions: SubscriptionRepository,
        audit: AuditRepository,
        routing: RoutingService,
        channels: TeamChannelDirectoryInterface,
        policy: EscalationPolicy,
        clock: ClockInterface,
        uow: UnitOfWork,
    ) -> None:
        self._problems = problems
        self._users = users
        self._subscriptions = subscriptions
        self._audit = audit
        self._routing = routing
        self._channels = channels
        self._policy = policy
        self._clock = clock
        self._uow = uow

    async def tick(self, *, limit: int = 100) -> EscalationReport:
        """Один проход планировщика."""
        now = self._clock.now()
        escalated = skipped = exhausted = undeliverable = 0

        for problem in await self._problems.list_escalatable(limit=limit):
            if not problem.escalation_active:
                # Кто-то нажал Acknowledge — лестница останавливается.
                skipped += 1
                continue
            if not self._policy.applies_to(problem.severity):
                # Проблема учитывается и рассылается по подпискам, просто
                # никого не будят: отключается эскалация, а не учёт.
                skipped += 1
                continue
            if self._policy.is_exhausted(problem.escalation_steps_taken):
                exhausted += 1
                continue

            step = self._policy.due_step(
                opened_at=problem.first_seen_at,
                now=now,
                steps_taken=problem.escalation_steps_taken,
            )
            if step is None:
                skipped += 1
                continue

            recipients = await self._notify(
                problem, step.target, step_index=problem.escalation_steps_taken
            )

            problem.escalate(at=now)
            await self._problems.save(problem)
            await self._audit.add(
                AuditRecord.create(
                    AuditAction.ESCALATION_TRIGGERED,
                    problem_id=problem.id,
                    at=now,
                    details={
                        "step": str(problem.escalation_steps_taken),
                        "target": step.target.value,
                        "recipients": str(recipients),
                    },
                )
            )

            if recipients == 0:
                undeliverable += 1
            else:
                escalated += 1

        await self._uow.commit()
        return EscalationReport(
            escalated=escalated,
            skipped=skipped,
            exhausted=exhausted,
            undeliverable=undeliverable,
        )

    async def _notify(
        self, problem: Problem, target: EscalationTarget, *, step_index: int
    ) -> int:
        """Превратить роль в конкретные адреса.

        Единственное место, которое придётся тронуть, когда появится таблица
        дежурств: тогда командный канал сменится на конкретного человека, а всё
        остальное останется как есть.
        """
        if target is EscalationTarget.L1_CHANNEL:
            return await self._to_team(problem, Role.L1, step=step_index)
        if target is EscalationTarget.L2_CHANNEL:
            return await self._to_team(problem, Role.L2, step=step_index)
        return await self._broadcast(problem, step=step_index)

    async def _to_team(self, problem: Problem, role: Role, *, step: int) -> int:
        destination = self._channels.channel_for(role)
        if destination is None:
            return 0
        channel, address = destination
        await self._routing.deliver_to_address(
            problem, channel, address, escalation_step=step
        )
        return 1

    async def _broadcast(self, problem: Problem, *, step: int) -> int:
        """Последняя ступень: персонально каждому активному L2.

        Адреса берутся из личных подписок, а не из общего канала: тем и
        отличается broadcast от предыдущей ступени. Лимит получателя здесь не
        применяется — лестница дошла сюда именно потому, что на всё предыдущее
        никто не отреагировал, и подавить это сообщение счётчиком значило бы
        погасить единственное, что обязано дойти.
        """
        targets: list[tuple] = []
        for user in await self._users.list_by_role(Role.L2, only_active=True):
            if not user.receives_escalation:
                continue
            for subscription in await self._subscriptions.list_for_user(user.id):
                if subscription.is_enabled:
                    targets.append((subscription.channel, subscription.address, user.id))

        deliveries = await self._routing.deliver_to_many(
            problem, targets, escalation_step=step
        )
        return len(deliveries)
