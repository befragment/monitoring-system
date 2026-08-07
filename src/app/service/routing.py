"""Шаг 5 — кому и куда уходит сообщение о проблеме.

Сервис отвечает на один вопрос: какие записи `Delivery` должны появиться. Сама
отправка — работа `DeliveryService`, и это разделение не косметическое: решение
о доставке принимается в транзакции обработки события, а отправка идёт отдельным
воркером. Иначе падение телеграма откатывало бы транзакцию вместе с проблемой.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.domain.notification import Delivery, RoutingChannel
from app.domain.problem import Problem
from app.domain.suppression import (
    DEFAULT_RECIPIENT_RATE_LIMIT,
    DeliveryDecision,
    RateLimit,
    SuppressionReason,
)
from app.lib.clock import ClockInterface
from app.lib.ratelimit import RateLimiterInterface
from app.repository._contracts import (
    DeliveryRepository,
    MaintenanceRepository,
    SubscriptionRepository,
)


@dataclass(frozen=True, slots=True)
class RoutingOutcome:
    """Что решили сделать с оповещением по проблеме."""

    decision: DeliveryDecision
    deliveries: list[Delivery] = field(default_factory=list)
    suppression: SuppressionReason | None = None

    @property
    def pending_count(self) -> int:
        return sum(1 for d in self.deliveries if not d.is_settled)


class RoutingService:
    def __init__(
        self,
        subscriptions: SubscriptionRepository,
        deliveries: DeliveryRepository,
        maintenance: MaintenanceRepository,
        limiter: RateLimiterInterface,
        clock: ClockInterface,
        recipient_limit: RateLimit = DEFAULT_RECIPIENT_RATE_LIMIT,
    ) -> None:
        self._subscriptions = subscriptions
        self._deliveries = deliveries
        self._maintenance = maintenance
        self._limiter = limiter
        self._clock = clock
        self._recipient_limit = recipient_limit

    async def route(self, problem: Problem) -> RoutingOutcome:
        """Разослать по подпискам (п. 5.6) с учётом подавления (пп. 4.5–4.6)."""
        now = self._clock.now()

        window = await self._maintenance.find_active_for_asset(problem.asset.name, now)

        candidates = await self._subscriptions.list_candidates(
            severity=problem.severity, tags=sorted(problem.tags)
        )
        # Окончательное решение принимает домен, а не SQL: правило «кому это
        # интересно» обязано жить в одном месте, иначе на вопрос «почему мне не
        # пришло» придётся отвечать чтением запроса, а не вызовом функции.
        matched = [s for s in candidates if s.matches(problem)]

        deliveries: list[Delivery] = []
        for subscription in matched:
            delivery = Delivery(
                id=uuid.uuid4(),
                problem_id=problem.id,
                channel=subscription.channel,
                # Адрес копируется на момент отправки: если человек завтра сменит
                # его, история обязана помнить, куда сообщение ушло на самом деле.
                address=subscription.address,
                recipient_id=subscription.user_id,
                subscription_id=subscription.id,
                created_at=now,
            )

            if window is not None:
                # П. 4.5 — объект под регламентом. Запись всё равно создаётся:
                # без неё «почему в три ночи не было алерта» останется без
                # ответа, а подавленное намеренно не отличится от потерянного.
                delivery.mark_suppressed(SuppressionReason.MAINTENANCE, at=now)
            elif await self._recipient_over_limit(subscription.user_id):
                delivery.mark_suppressed(SuppressionReason.RECIPIENT_RATE_LIMIT, at=now)

            deliveries.append(delivery)

        if deliveries:
            await self._deliveries.add_many(deliveries)

        if window is not None:
            return RoutingOutcome(
                decision=DeliveryDecision.DROP,
                deliveries=deliveries,
                suppression=SuppressionReason.MAINTENANCE,
            )
        return RoutingOutcome(decision=DeliveryDecision.DELIVER, deliveries=deliveries)

    async def deliver_to_address(
        self,
        problem: Problem,
        channel: RoutingChannel,
        address: str,
        *,
        escalation_step: int | None = None,
        recipient_id: uuid.UUID | None = None,
    ) -> Delivery:
        """Адресная отправка мимо подписок — так работают ступени эскалации.

        Лимит получателя здесь не применяется: лестница добралась до этой
        ступени именно потому, что на предыдущие никто не отреагировал, и
        глушить её тем же счётчиком, что и обычную рассылку, означало бы
        подавить единственное сообщение, которое обязано дойти.
        """
        delivery = Delivery(
            id=uuid.uuid4(),
            problem_id=problem.id,
            channel=channel,
            address=address,
            recipient_id=recipient_id,
            escalation_step=escalation_step,
            created_at=self._clock.now(),
        )
        await self._deliveries.add_many([delivery])
        return delivery

    async def deliver_to_many(
        self,
        problem: Problem,
        targets: Sequence[tuple[RoutingChannel, str, uuid.UUID | None]],
        *,
        escalation_step: int | None = None,
    ) -> list[Delivery]:
        """Персональная рассылка — последняя ступень лестницы (broadcast)."""
        now = self._clock.now()
        deliveries = [
            Delivery(
                id=uuid.uuid4(),
                problem_id=problem.id,
                channel=channel,
                address=address,
                recipient_id=recipient_id,
                escalation_step=escalation_step,
                created_at=now,
            )
            for channel, address, recipient_id in targets
        ]
        if deliveries:
            await self._deliveries.add_many(deliveries)
        return deliveries

    async def _recipient_over_limit(self, user_id: uuid.UUID) -> bool:
        """П. 4.6 — лимит сообщений на одного получателя.

        Счёт ведёт Redis, а решение принимает домен: `RateLimit.exceeded` — это
        то место, где записано, что считается «слишком много».
        """
        observed = await self._limiter.hit(
            f"recipient:{user_id}", self._recipient_limit.per
        )
        return self._recipient_limit.exceeded(observed)
