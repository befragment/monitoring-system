"""П. 7.3 — дашборд администратора системы оповещений."""

import uuid
from datetime import timedelta

import pytest

from app.domain.notification import Delivery, RoutingChannel
from app.domain.problem import Problem
from app.domain.suppression import SuppressionReason
from app.service.dashboard import DashboardService, DeliveryStats


@pytest.fixture
def service(events, problems, deliveries, queue, clock) -> DashboardService:
    return DashboardService(events, problems, deliveries, queue, clock)


def make_delivery(problem_id, **overrides) -> Delivery:
    defaults = dict(
        id=uuid.uuid4(),
        problem_id=problem_id,
        channel=RoutingChannel.MAIL,
        address="a@b.ru",
    )
    return Delivery(**{**defaults, **overrides})


class TestSuccessRate:
    def test_suppressed_are_not_failures(self):
        """Подавленное по регламенту сообщение — не сбой. Слив их в одну
        корзину, панель показывала бы деградацию ровно там, где платформа
        отработала как задумано."""
        stats = DeliveryStats(delivered=8, failed=0, suppressed=100, pending=0)
        assert stats.success_rate == 1.0

    def test_suppressed_are_not_in_the_denominator(self):
        stats = DeliveryStats(delivered=5, failed=5, suppressed=90, pending=0)
        assert stats.attempted == 10
        assert stats.success_rate == 0.5

    def test_pending_is_not_counted_either(self):
        """Ещё не отправленное не является ни успехом, ни провалом."""
        stats = DeliveryStats(delivered=1, failed=0, suppressed=0, pending=99)
        assert stats.attempted == 1
        assert stats.success_rate == 1.0

    def test_nothing_sent_is_not_total_failure(self):
        """«Ничего не отправляли» — это не «всё потеряли», и делить на ноль
        панель не должна."""
        assert DeliveryStats(0, 0, 0, 0).success_rate == 1.0


class TestSnapshot:
    async def test_counts_events_in_the_window(
        self, service, events, incoming, zabbix, clock
    ):
        events.rows.append((incoming.classify(zabbix.severity_mapping), None))
        snapshot = await service.snapshot()
        assert snapshot.events_processed == 1

    async def test_older_events_fall_out_of_the_window(
        self, service, events, incoming, zabbix, clock
    ):
        events.rows.append((incoming.classify(zabbix.severity_mapping), None))
        clock.advance(timedelta(days=2))
        assert (await service.snapshot()).events_processed == 0

    async def test_counts_open_problems_only(
        self, service, problems, incoming, zabbix, l1
    ):
        first = Problem.open_from(incoming.classify(zabbix.severity_mapping))
        second = Problem.open_from(incoming.classify(zabbix.severity_mapping))
        second.close(reason="Заменили диск", closed_by=str(l1.id))
        problems.rows = {first.id: first, second.id: second}
        assert (await service.snapshot()).open_problems == 1

    async def test_queue_depth_is_visible(self, service, queue):
        """Растущая очередь при живом приёме означает нехватку воркеров — и это
        видно раньше, чем начнут опаздывать оповещения."""
        await queue.publish({"any": "payload"})
        await queue.publish({"any": "payload"})
        assert (await service.snapshot()).queue_depth == 2

    async def test_delivery_breakdown(self, service, deliveries):
        problem_id = uuid.uuid4()
        delivered = make_delivery(problem_id)
        delivered.mark_delivered()
        failed = make_delivery(problem_id)
        failed.mark_failed("smtp: connection refused")
        suppressed = make_delivery(problem_id)
        suppressed.mark_suppressed(SuppressionReason.MAINTENANCE)
        pending = make_delivery(problem_id)
        await deliveries.add_many([delivered, failed, suppressed, pending])

        stats = (await service.snapshot()).deliveries
        assert (stats.delivered, stats.failed, stats.suppressed, stats.pending) == (1, 1, 1, 1)
        assert stats.success_rate == 0.5

    async def test_window_is_reported_back(self, service):
        snapshot = await service.snapshot(window=timedelta(hours=6))
        assert snapshot.window == timedelta(hours=6)
