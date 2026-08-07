"""Отправка того, что решила маршрутизация."""

import uuid
from datetime import timedelta

import pytest

from app.domain.notification import Delivery, DeliveryStatus, RoutingChannel
from app.domain.problem import Problem
from app.service.delivery import MAX_ATTEMPTS, DeliveryService


@pytest.fixture
def problem(incoming, zabbix, problems) -> Problem:
    p = Problem.open_from(incoming.classify(zabbix.severity_mapping))
    problems.rows[p.id] = p
    return p


@pytest.fixture
def service(deliveries, problems, users, gateway, clock, uow) -> DeliveryService:
    return DeliveryService(deliveries, problems, users, gateway, clock, uow)


def make_delivery(problem, **overrides) -> Delivery:
    defaults = dict(
        id=uuid.uuid4(),
        problem_id=problem.id,
        channel=RoutingChannel.TELEGRAM,
        address="tg:111",
    )
    return Delivery(**{**defaults, **overrides})


class TestDispatch:
    async def test_pending_is_sent(self, service, deliveries, problem, gateway):
        await deliveries.add_many([make_delivery(problem)])
        report = await service.dispatch_pending()
        assert report.delivered == 1
        assert len(gateway.sent) == 1

    async def test_message_is_built_from_the_problem(
        self, service, deliveries, problem, gateway
    ):
        await deliveries.add_many([make_delivery(problem)])
        await service.dispatch_pending()
        _, _, message = gateway.sent[0]
        assert message.subject.startswith("[CRITICAL]")
        assert "db-prod-03" in message.subject

    async def test_owner_name_is_resolved_by_the_service(
        self, service, deliveries, problem, gateway, l1
    ):
        """Домен хранит идентификатор, сообщению нужно отображаемое имя —
        сопоставлять их работа сервиса, а не агрегата."""
        problem.acknowledge(l1.id, l1.role)
        await deliveries.add_many([make_delivery(problem)])
        await service.dispatch_pending()
        _, _, message = gateway.sent[0]
        assert message.owner == l1.full_name

    async def test_status_becomes_delivered(self, service, deliveries, problem):
        delivery = make_delivery(problem)
        await deliveries.add_many([delivery])
        await service.dispatch_pending()
        assert delivery.status is DeliveryStatus.DELIVERED
        assert delivery.attempts == 1

    async def test_already_settled_is_not_resent(self, service, deliveries, problem, gateway):
        delivery = make_delivery(problem)
        delivery.mark_delivered()
        await deliveries.add_many([delivery])
        report = await service.dispatch_pending()
        assert report.delivered == 0
        assert gateway.sent == []


class TestFailure:
    async def test_channel_error_is_recorded_with_its_reason(
        self, service, deliveries, problem, gateway
    ):
        """«Не дошло» без объяснения не отвечает ни на один вопрос п. 7.3."""
        gateway.fail_on.add(RoutingChannel.TELEGRAM)
        delivery = make_delivery(problem)
        await deliveries.add_many([delivery])

        report = await service.dispatch_pending()
        assert report.failed == 1
        assert delivery.status is DeliveryStatus.FAILED
        assert "503" in delivery.failure_reason

    async def test_one_broken_channel_does_not_stop_the_rest(
        self, service, deliveries, problem, gateway
    ):
        gateway.fail_on.add(RoutingChannel.TELEGRAM)
        await deliveries.add_many(
            [
                make_delivery(problem),
                make_delivery(problem, channel=RoutingChannel.MAIL, address="a@b.ru"),
            ]
        )
        report = await service.dispatch_pending()
        assert report.delivered == 1 and report.failed == 1

    async def test_missing_problem_is_not_a_crash(self, service, deliveries, problem, problems):
        delivery = make_delivery(problem)
        await deliveries.add_many([delivery])
        problems.rows.clear()
        report = await service.dispatch_pending()
        assert report.failed == 1
        assert delivery.status is DeliveryStatus.FAILED


class TestStaleMessages:
    async def test_alert_queued_before_closing_is_skipped(
        self, service, deliveries, problem, gateway, clock, l1
    ):
        """Слать алерт про уже починенное — это шум, который учит игнорировать
        канал."""
        delivery = make_delivery(problem, created_at=clock.now())
        await deliveries.add_many([delivery])
        problem.close(reason="Почистили логи", closed_by=str(l1.id), at=clock.now() + timedelta(minutes=1))

        report = await service.dispatch_pending()
        assert report.skipped == 1
        assert gateway.sent == []

    async def test_closing_notification_is_still_delivered(
        self, service, deliveries, problem, gateway, clock, l1
    ):
        """Сообщение о закрытии снимает тревогу — его пропускать нельзя."""
        problem.close(reason="Почистили логи", closed_by=str(l1.id), at=clock.now())
        delivery = make_delivery(problem, created_at=clock.now() + timedelta(seconds=1))
        await deliveries.add_many([delivery])

        report = await service.dispatch_pending()
        assert report.delivered == 1
        _, _, message = gateway.sent[0]
        assert message.subject.startswith("[RESOLVED]")


class TestRetry:
    async def test_failed_delivery_returns_to_the_queue(
        self, service, deliveries, problem, gateway
    ):
        gateway.fail_on.add(RoutingChannel.TELEGRAM)
        delivery = make_delivery(problem)
        await deliveries.add_many([delivery])
        await service.dispatch_pending()

        assert await service.retry(delivery) is True
        assert delivery.status is DeliveryStatus.PENDING
        assert delivery.settled_at is None

    async def test_attempts_are_capped(self, service, deliveries, problem):
        """Без предела упавший канал будет вечно перемалывать одну запись,
        вытесняя из очереди свежие алерты."""
        delivery = make_delivery(problem)
        delivery.attempts = MAX_ATTEMPTS
        await deliveries.add_many([delivery])
        assert await service.retry(delivery) is False

    async def test_capped_delivery_keeps_its_reason(self, service, problem):
        """Исчерпавшая попытки запись остаётся в FAILED с причиной — её видно в
        отчёте, а не теряется молча."""
        delivery = make_delivery(problem)
        delivery.mark_failed("timeout")
        delivery.attempts = MAX_ATTEMPTS
        await service.retry(delivery)
        assert delivery.status is DeliveryStatus.FAILED
        assert delivery.failure_reason == "timeout"
