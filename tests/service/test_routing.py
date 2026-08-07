"""Шаг 5 — кому и куда уходит сообщение."""

import uuid
from datetime import timedelta

import pytest

from app.domain.notification import RoutingChannel
from app.domain.problem import Problem
from app.domain.severity import Severity
from app.domain.suppression import DeliveryDecision, RateLimit, SuppressionReason
from app.domain.user import Subscription


@pytest.fixture
def problem(incoming, zabbix) -> Problem:
    return Problem.open_from(incoming.classify(zabbix.severity_mapping))


class TestSubscriptionRouting:
    async def test_matching_subscriptions_get_a_delivery(self, routing, problem):
        outcome = await routing.route(problem)
        assert outcome.decision is DeliveryDecision.DELIVER
        assert len(outcome.deliveries) == 2

    async def test_final_decision_belongs_to_the_domain(
        self, routing, problem, subscriptions, l1
    ):
        """Репозиторий отбирает кандидатов индексами, но «кому это интересно»
        решает `Subscription.matches` — иначе на вопрос «почему мне не пришло»
        придётся отвечать чтением SQL, а не вызовом функции."""
        foreign = Subscription(
            id=uuid.uuid4(),
            user_id=l1.id,
            channel=RoutingChannel.MAIL,
            address="dev@example.ru",
            min_severity=Severity.INFO,
            tags=frozenset({"dev"}),
        )
        subscriptions.rows[foreign.id] = foreign
        outcome = await routing.route(problem)
        assert foreign.id not in {d.subscription_id for d in outcome.deliveries}

    async def test_disabled_subscription_is_skipped(self, routing, problem, sub_l1):
        sub_l1.is_enabled = False
        outcome = await routing.route(problem)
        assert sub_l1.id not in {d.subscription_id for d in outcome.deliveries}

    async def test_address_is_copied_not_referenced(self, routing, problem, sub_l1):
        """Если человек завтра сменит адрес, история обязана помнить, куда
        сообщение ушло на самом деле."""
        outcome = await routing.route(problem)
        delivery = next(d for d in outcome.deliveries if d.subscription_id == sub_l1.id)
        sub_l1.address = "tg:999"
        assert delivery.address == "tg:111"

    async def test_delivery_records_its_origin(self, routing, problem, sub_l1, l1):
        outcome = await routing.route(problem)
        delivery = next(d for d in outcome.deliveries if d.subscription_id == sub_l1.id)
        assert delivery.recipient_id == l1.id
        assert delivery.escalation_step is None

    async def test_deliveries_are_persisted(self, routing, problem, deliveries):
        await routing.route(problem)
        assert len(deliveries.rows) == 2


class TestMaintenanceSuppression:
    """П. 4.5 — объект под регламентом."""

    async def test_decision_is_drop(self, routing, problem, maintenance, window_now):
        maintenance.rows.append(window_now)
        outcome = await routing.route(problem)
        assert outcome.decision is DeliveryDecision.DROP
        assert outcome.suppression is SuppressionReason.MAINTENANCE

    async def test_records_are_still_created(
        self, routing, problem, maintenance, window_now
    ):
        """Без записи «почему в три ночи не было алерта» останется без ответа, а
        подавленное намеренно не отличится от потерянного."""
        maintenance.rows.append(window_now)
        outcome = await routing.route(problem)
        assert len(outcome.deliveries) == 2
        assert all(
            d.suppression_reason is SuppressionReason.MAINTENANCE
            for d in outcome.deliveries
        )

    async def test_other_assets_are_unaffected(
        self, routing, problem, maintenance, window_now
    ):
        from dataclasses import replace

        maintenance.rows.append(replace(window_now, asset_name="db-prod-99"))
        outcome = await routing.route(problem)
        assert outcome.decision is DeliveryDecision.DELIVER


class TestRecipientRateLimit:
    """П. 4.6 — лимит сообщений на одного получателя."""

    async def test_over_limit_is_suppressed(
        self, subscriptions, deliveries, maintenance, limiter, clock, problem, l1
    ):
        from app.service.routing import RoutingService

        service = RoutingService(
            subscriptions,
            deliveries,
            maintenance,
            limiter,
            clock,
            recipient_limit=RateLimit(max_events=1, per=timedelta(hours=1)),
        )
        limiter.preload(f"recipient:{l1.id}", 5)
        outcome = await service.route(problem)
        suppressed = [d for d in outcome.deliveries if d.recipient_id == l1.id]
        assert all(
            d.suppression_reason is SuppressionReason.RECIPIENT_RATE_LIMIT
            for d in suppressed
        )

    async def test_limit_is_per_recipient(self, routing, problem, limiter, l1, l2):
        """Один шумный получатель не должен глушить остальных."""
        await routing.route(problem)
        assert limiter.counts[f"recipient:{l1.id}"] == 1
        assert limiter.counts[f"recipient:{l2.id}"] == 1

    async def test_maintenance_does_not_burn_the_budget(
        self, routing, problem, maintenance, window_now, limiter, l1
    ):
        """Подавленное регламентом сообщение не отправлялось — тратить на него
        бюджет получателя значит наказать человека за чужой регламент."""
        maintenance.rows.append(window_now)
        await routing.route(problem)
        assert limiter.counts[f"recipient:{l1.id}"] == 0


class TestEscalationDelivery:
    """Адресная отправка мимо подписок."""

    async def test_delivers_to_a_team_channel(self, routing, problem):
        delivery = await routing.deliver_to_address(
            problem, RoutingChannel.MATTERMOST, "#l2-duty", escalation_step=1
        )
        assert delivery.escalation_step == 1
        assert delivery.subscription_id is None
        assert delivery.recipient_id is None, "у командного канала нет персонального адресата"

    async def test_escalation_ignores_the_recipient_limit(
        self, routing, problem, limiter, l1
    ):
        """Лестница дошла до ступени именно потому, что на предыдущие никто не
        отреагировал; подавить это счётчиком значит погасить единственное
        сообщение, которое обязано дойти."""
        limiter.preload(f"recipient:{l1.id}", 999)
        delivery = await routing.deliver_to_address(
            problem, RoutingChannel.TELEGRAM, "tg:111", escalation_step=2, recipient_id=l1.id
        )
        assert delivery.suppression_reason is None
        assert not delivery.is_settled

    async def test_broadcast_creates_one_delivery_per_target(self, routing, problem, l2):
        targets = [
            (RoutingChannel.MAIL, "a@example.ru", l2.id),
            (RoutingChannel.TELEGRAM, "tg:222", l2.id),
        ]
        created = await routing.deliver_to_many(problem, targets, escalation_step=2)
        assert len(created) == 2
        assert all(d.escalation_step == 2 for d in created)

    async def test_empty_target_list_creates_nothing(self, routing, problem, deliveries):
        assert await routing.deliver_to_many(problem, [], escalation_step=2) == []
        assert deliveries.rows == {}
