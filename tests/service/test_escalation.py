"""Тик лестницы эскалации."""

from datetime import timedelta

import pytest

from app.domain.escalation import (
    DEFAULT_ESCALATION_POLICY,
    EscalationPolicy,
    EscalationStep,
    EscalationTarget,
)
from app.domain.problem import Problem
from app.domain.severity import Severity
from app.service.escalation import EscalationService

from .conftest import FakeChannels


@pytest.fixture
def problem(incoming, zabbix, problems) -> Problem:
    p = Problem.open_from(incoming.classify(zabbix.severity_mapping))
    problems.rows[p.id] = p
    return p


@pytest.fixture
def service(problems, users, subscriptions, audit, routing, channels, clock, uow):
    return EscalationService(
        problems, users, subscriptions, audit, routing, channels,
        DEFAULT_ESCALATION_POLICY, clock, uow,
    )


class TestLadder:
    async def test_first_rung_fires_immediately(self, service, problem, deliveries):
        report = await service.tick()
        assert report.escalated == 1
        assert deliveries.by_step(0), "ступень L1 отработала"
        assert problem.escalation_steps_taken == 1

    async def test_second_rung_waits(self, service, problem, clock):
        await service.tick()
        clock.advance(timedelta(minutes=14))
        assert (await service.tick()).escalated == 0

    async def test_second_rung_fires_on_time(self, service, problem, clock, deliveries):
        await service.tick()
        clock.advance(timedelta(minutes=15))
        assert (await service.tick()).escalated == 1
        assert deliveries.by_step(1)

    async def test_one_rung_per_pass(self, service, problem, clock):
        """Если воркер лежал час, нельзя вывалить человеку всю лестницу разом."""
        clock.advance(timedelta(hours=1))
        assert (await service.tick()).escalated == 1
        assert problem.escalation_steps_taken == 1

    async def test_counter_grows_after_sending(self, service, problem, deliveries):
        """Увеличить счётчик авансом — значит рискнуть проглотить ступень, если
        создание доставок упадёт."""
        await service.tick()
        assert len(deliveries.rows) >= 1
        assert problem.escalation_steps_taken == 1

    async def test_exhausted_ladder_stops(self, service, problem, clock):
        clock.advance(timedelta(hours=1))
        for _ in range(3):
            await service.tick()
        report = await service.tick()
        assert report.exhausted == 1 and report.escalated == 0


class TestAcknowledgeStopsIt:
    async def test_acknowledged_problem_is_skipped(self, service, problem, clock, l1):
        problem.acknowledge(l1.id, l1.role)
        clock.advance(timedelta(hours=1))
        report = await service.tick()
        assert report.escalated == 0

    async def test_closed_problem_is_not_even_listed(self, service, problem, clock, l1):
        problem.close(reason="Почистили логи", closed_by=str(l1.id))
        clock.advance(timedelta(hours=1))
        assert (await service.tick()).escalated == 0

    async def test_service_rechecks_what_the_repository_returned(
        self, service, problem, problems, clock, l1, deliveries
    ):
        """Между выборкой и обработкой проблему могли взять в работу.

        Запрос `list_escalatable` отбирает активные, но между ним и циклом
        проходит время, и дежурный вполне может нажать Acknowledge именно в этот
        промежуток. Без повторной проверки внутри цикла человек получил бы
        эскалацию по проблеме, которую только что взял, — а это ровно тот шум,
        который учит игнорировать канал.

        Проверяется подстановкой репозитория, отдающего уже подтверждённую
        проблему: обычная подделка фильтрует так же, как настоящий SQL, и эту
        защиту не задевает.
        """
        problem.acknowledge(l1.id, l1.role)
        clock.advance(timedelta(hours=1))

        async def stale_listing(*, limit=100):
            return [problem]

        problems.list_escalatable = stale_listing

        report = await service.tick()
        assert report.escalated == 0
        assert report.skipped == 1
        assert deliveries.rows == {}
        assert problem.escalation_steps_taken == 0


class TestTargets:
    async def test_team_channel_delivery_has_no_personal_recipient(
        self, service, problem, deliveries
    ):
        await service.tick()
        delivery = deliveries.by_step(0)[0]
        assert delivery.recipient_id is None
        assert delivery.subscription_id is None

    async def test_broadcast_goes_to_every_active_l2_subscription(
        self, service, problem, clock, deliveries, l2
    ):
        """Последняя ступень отличается от предыдущей адресацией: персонально
        каждому, а не в общий канал."""
        clock.advance(timedelta(minutes=30))
        await service.tick()
        await service.tick()
        await service.tick()
        broadcast = deliveries.by_step(2)
        assert broadcast
        assert all(d.recipient_id == l2.id for d in broadcast)

    async def test_inactive_l2_is_not_woken(
        self, service, problem, clock, deliveries, l2
    ):
        l2.is_active = False
        clock.advance(timedelta(minutes=30))
        for _ in range(3):
            await service.tick()
        assert deliveries.by_step(2) == []

    async def test_manager_is_never_an_escalation_target(
        self, service, problem, clock, deliveries, manager
    ):
        """Manager получает рассылку сразу и в разборе не участвует —
        эскалировать к нему нечего."""
        clock.advance(timedelta(minutes=30))
        for _ in range(3):
            await service.tick()
        assert all(d.recipient_id != manager.id for d in deliveries.rows.values())


class TestMisconfiguration:
    async def test_missing_channel_is_counted_separately(
        self, problems, users, subscriptions, audit, routing, clock, uow, problem
    ):
        """Ненастроенный канал молча обрывает лестницу — такая дыра обязана быть
        видна в метриках, а не только в журнале."""
        service = EscalationService(
            problems, users, subscriptions, audit, routing,
            FakeChannels(configured=()), DEFAULT_ESCALATION_POLICY, clock, uow,
        )
        report = await service.tick()
        assert report.undeliverable == 1
        assert report.escalated == 0

    async def test_step_still_advances(
        self, problems, users, subscriptions, audit, routing, clock, uow, problem
    ):
        """Иначе лестница застрянет на ненастроенной ступени навсегда и никогда
        не дойдёт до broadcast."""
        service = EscalationService(
            problems, users, subscriptions, audit, routing,
            FakeChannels(configured=()), DEFAULT_ESCALATION_POLICY, clock, uow,
        )
        await service.tick()
        assert problem.escalation_steps_taken == 1

    async def test_it_is_audited(
        self, problems, users, subscriptions, audit, routing, clock, uow, problem
    ):
        service = EscalationService(
            problems, users, subscriptions, audit, routing,
            FakeChannels(configured=()), DEFAULT_ESCALATION_POLICY, clock, uow,
        )
        await service.tick()
        record = next(r for r in audit.rows if r.action.value == "escalation_triggered")
        assert record.details["recipients"] == "0"


class TestSeverityThreshold:
    async def test_below_threshold_is_not_escalated(
        self, problems, users, subscriptions, audit, routing, channels, clock, uow, problem
    ):
        """Отключается эскалация, а не учёт: проблема живёт и рассылается по
        подпискам, просто никого не будят."""
        strict = EscalationPolicy(
            steps=DEFAULT_ESCALATION_POLICY.steps, min_severity=Severity.CRITICAL
        )
        problem.severity = Severity.WARNING
        service = EscalationService(
            problems, users, subscriptions, audit, routing, channels, strict, clock, uow
        )
        report = await service.tick()
        assert report.escalated == 0
        assert problem.is_open


class TestAudit:
    async def test_every_rung_leaves_a_record(self, service, problem, clock, audit):
        clock.advance(timedelta(minutes=30))
        for _ in range(3):
            await service.tick()
        triggered = [r for r in audit.rows if r.action.value == "escalation_triggered"]
        assert len(triggered) == 3

    async def test_record_names_the_target(self, service, problem, audit):
        await service.tick()
        record = next(r for r in audit.rows if r.action.value == "escalation_triggered")
        assert record.details["target"] == EscalationTarget.L1_CHANNEL.value


async def test_policy_shape_is_not_hardcoded(
    problems, users, subscriptions, audit, routing, channels, clock, uow, problem, deliveries
):
    """Лестница в проекте ещё пересматривается. Сервис обязан работать с любой:
    он спрашивает у политики, что пора, и не знает, сколько в ней ступеней."""
    single = EscalationPolicy(
        steps=(EscalationStep(EscalationTarget.L1_CHANNEL, timedelta(0)),)
    )
    service = EscalationService(
        problems, users, subscriptions, audit, routing, channels, single, clock, uow
    )
    assert (await service.tick()).escalated == 1
    assert (await service.tick()).exhausted == 1
