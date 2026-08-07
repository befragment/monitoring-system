"""Шаги 3–5 — обработка события. Главный сценарий системы."""

from dataclasses import replace
from datetime import timedelta

import pytest

from app.domain.problem import Problem, ProblemStatus
from app.domain.severity import Severity
from app.domain.suppression import SuppressionReason
from app.service.errors import UnknownIntegration
from app.service.event_processing import ProcessingOutcome


class TestNewProblem:
    async def test_first_event_opens_a_problem(self, processing, incoming):
        result = await processing.process(incoming)
        assert result.outcome is ProcessingOutcome.PROBLEM_OPENED
        assert result.problem.status is ProblemStatus.ACTIVE

    async def test_event_is_journalled_with_its_problem(
        self, processing, incoming, events
    ):
        """Связь события с эпизодом проставляет сервис: по одному fingerprint
        может пройти несколько эпизодов, и выборка «события этой проблемы» без
        ссылки смешала бы их."""
        result = await processing.process(incoming)
        assert events.rows[0][1] == result.problem.id

    async def test_creation_is_audited(self, processing, incoming, audit):
        await processing.process(incoming)
        assert "problem_created" in audit.actions()

    async def test_notification_goes_out(self, processing, incoming):
        result = await processing.process(incoming)
        assert result.routing is not None
        assert len(result.routing.deliveries) == 2

    async def test_everything_lands_in_one_transaction(self, processing, incoming, uow):
        """Проблема, событие, аудит и записи доставки коммитятся вместе: иначе
        возможно состояние, где действие произошло, а журнал о нём молчит."""
        await processing.process(incoming)
        assert uow.commits == 1


class TestIdempotency:
    async def test_retried_event_changes_nothing(self, processing, incoming, problems):
        await processing.process(incoming)
        result = await processing.process(incoming)
        assert result.outcome is ProcessingOutcome.DUPLICATE
        assert len(problems.rows) == 1

    async def test_retried_event_does_not_grow_the_counter(self, processing, incoming):
        first = await processing.process(incoming)
        await processing.process(incoming)
        assert first.problem.event_count == 1

    async def test_retried_event_sends_nothing(self, processing, incoming, deliveries):
        await processing.process(incoming)
        before = len(deliveries.rows)
        await processing.process(incoming)
        assert len(deliveries.rows) == before


class TestDeduplication:
    """П. 4.2 и критерий п. 4.7."""

    @pytest.fixture
    def repeat(self, incoming):
        return replace(incoming, source_event_id="8841204", message="Диск заполнен на 96%")

    async def test_repeat_grows_the_counter(self, processing, incoming, repeat):
        await processing.process(incoming)
        result = await processing.process(repeat)
        assert result.outcome is ProcessingOutcome.REPEAT_REGISTERED
        assert result.problem.event_count == 2

    async def test_repeat_creates_no_new_problem(self, processing, incoming, repeat, problems):
        await processing.process(incoming)
        await processing.process(repeat)
        assert len(problems.rows) == 1

    async def test_repeat_notifies_nobody(self, processing, incoming, repeat, deliveries):
        """Молчание на повторах и есть дедупликация. Слать на каждый повтор —
        значит выполнить критерий п. 4.7 формально, оставив человеку тот же
        поток сообщений."""
        await processing.process(incoming)
        before = len(deliveries.rows)
        result = await processing.process(repeat)
        assert result.routing is None
        assert len(deliveries.rows) == before


class TestSeverityChange:
    async def test_change_is_detected(self, processing, incoming):
        await processing.process(incoming)
        worse = replace(incoming, source_event_id="2", raw_severity="average")
        result = await processing.process(worse)
        assert result.outcome is ProcessingOutcome.SEVERITY_CHANGED
        assert result.problem.severity is Severity.MAJOR

    async def test_change_notifies(self, processing, incoming, deliveries):
        await processing.process(incoming)
        before = len(deliveries.rows)
        await processing.process(replace(incoming, source_event_id="2", raw_severity="average"))
        assert len(deliveries.rows) > before

    async def test_same_severity_is_not_a_change(self, processing, incoming):
        """Ловушка: `Problem.severity_changed` отвечает на другой вопрос — он
        истинен с рождения проблемы (CRITICAL против стартового OK). Здесь нужно
        сравнение до и после конкретного события."""
        await processing.process(incoming)
        result = await processing.process(replace(incoming, source_event_id="2"))
        assert result.outcome is ProcessingOutcome.REPEAT_REGISTERED


class TestRecovery:
    @pytest.fixture
    def recovery(self, incoming):
        return replace(incoming, source_event_id="9", raw_severity="resolved")

    async def test_recovery_closes_the_problem(self, processing, incoming, recovery):
        await processing.process(incoming)
        result = await processing.process(recovery)
        assert result.outcome is ProcessingOutcome.PROBLEM_RESOLVED
        assert result.problem.status is ProblemStatus.CLOSED

    async def test_closed_by_the_platform(self, processing, incoming, recovery):
        await processing.process(incoming)
        result = await processing.process(recovery)
        assert result.problem.closed_by == "system"
        assert result.problem.close_reason

    async def test_recovery_is_audited_without_an_actor(
        self, processing, incoming, recovery, audit
    ):
        """Автозакрытие не имеет человека за спиной, и фиктивный id испортил бы
        выборки «кто это сделал»."""
        await processing.process(incoming)
        await processing.process(recovery)
        record = next(r for r in audit.rows if r.action.value == "problem_resolved")
        assert record.actor_id is None

    async def test_recovery_notifies(self, processing, incoming, recovery):
        """Сообщение о восстановлении снимает тревогу — его шлём всегда."""
        await processing.process(incoming)
        result = await processing.process(recovery)
        assert result.routing is not None


class TestClosedIsTerminal:
    async def test_same_fingerprint_opens_a_new_episode(
        self, processing, incoming, problems
    ):
        first = await processing.process(incoming)
        await processing.process(replace(incoming, source_event_id="9", raw_severity="ok"))

        second = await processing.process(replace(incoming, source_event_id="10"))
        assert second.outcome is ProcessingOutcome.PROBLEM_OPENED
        assert second.problem.id != first.problem.id
        assert second.problem.fingerprint == first.problem.fingerprint
        assert len(problems.rows) == 2, "два эпизода, а не один воскресший"

    async def test_new_episode_counts_from_scratch(self, processing, incoming):
        await processing.process(incoming)
        await processing.process(replace(incoming, source_event_id="9", raw_severity="ok"))
        second = await processing.process(replace(incoming, source_event_id="10"))
        assert second.problem.event_count == 1


class TestConcurrency:
    async def test_race_falls_back_to_registering_a_repeat(
        self, processing, incoming, problems, zabbix
    ):
        """Гонка двух воркеров: пока мы собирали проблему, её создал сосед.

        При 50 событиях/сек это штатный исход, а не сбой. Частичный уникальный
        индекс по fingerprint существует ровно ради этой развилки.
        """
        rival = Problem.open_from(incoming.classify(zabbix.severity_mapping))
        problems.rows[rival.id] = rival

        # Прячем существующую проблему от поиска, чтобы сервис пошёл в ветку
        # создания и упёрся в конфликт на вставке — ровно как в базе.
        original_find = problems.find_open_by_fingerprint
        calls = {"n": 0}

        async def find_once(fingerprint):
            calls["n"] += 1
            return None if calls["n"] == 1 else await original_find(fingerprint)

        problems.find_open_by_fingerprint = find_once

        result = await processing.process(incoming)
        assert result.outcome is ProcessingOutcome.REPEAT_REGISTERED
        assert result.problem.id == rival.id
        assert rival.event_count == 2
        assert len(problems.rows) == 1


class TestSuppression:
    async def test_maintenance_stops_delivery_but_not_accounting(
        self, processing, incoming, maintenance, window_now, problems
    ):
        maintenance.rows.append(window_now)
        result = await processing.process(incoming)
        assert result.outcome is ProcessingOutcome.PROBLEM_OPENED
        assert len(problems.rows) == 1, "проблема создаётся всё равно"
        assert all(
            d.suppression_reason is SuppressionReason.MAINTENANCE
            for d in result.routing.deliveries
        )

    async def test_suppression_is_audited_separately(
        self, processing, incoming, maintenance, window_now, audit
    ):
        maintenance.rows.append(window_now)
        await processing.process(incoming)
        assert "notification_suppressed" in audit.actions()
        assert "notification_sent" not in audit.actions()


class TestUnknownSource:
    async def test_rejected(self, processing, incoming):
        with pytest.raises(UnknownIntegration):
            await processing.process(replace(incoming, source_system="nagios"))

    async def test_disabled_integration_rejected(
        self, processing, incoming, integrations, zabbix
    ):
        integrations.rows["zabbix"] = replace(zabbix, is_enabled=False)
        with pytest.raises(UnknownIntegration):
            await processing.process(incoming)


class TestOutOfOrderDelivery:
    async def test_late_event_does_not_rewind_last_seen(self, processing, incoming, t0):
        """Ретрай из очереди не должен выставлять проблему протухшей для
        эскалации."""
        await processing.process(replace(incoming, occurred_at=t0 + timedelta(minutes=5)))
        result = await processing.process(
            replace(incoming, source_event_id="2", occurred_at=t0 - timedelta(minutes=5))
        )
        assert result.problem.last_seen_at == t0 + timedelta(minutes=5)
