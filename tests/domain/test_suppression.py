"""Пп. 4.4–4.6 — что не будет отправлено."""

import uuid
from datetime import timedelta

import pytest

from app.domain.errors import DomainError
from app.domain.suppression import (
    DEFAULT_RECIPIENT_RATE_LIMIT,
    DEFAULT_SOURCE_RATE_LIMIT,
    GroupingRule,
    MaintenanceWindow,
    RateLimit,
)


@pytest.fixture
def window(t0) -> MaintenanceWindow:
    return MaintenanceWindow(
        id=uuid.uuid4(),
        asset_name="db-prod-03",
        starts_at=t0,
        ends_at=t0 + timedelta(hours=2),
        created_by=uuid.uuid4(),
        reason="Плановая замена диска",
    )


class TestMaintenanceWindow:
    """П. 4.5 — плановые работы."""

    def test_covers_its_asset_during_the_window(self, window, t0):
        assert window.covers("db-prod-03", t0 + timedelta(minutes=30)) is True

    def test_does_not_cover_other_assets(self, window, t0):
        """Привязка к конкретному объекту, не ко всей системе: регламент на
        одной машине не должен глушить платформу целиком."""
        assert window.covers("db-prod-04", t0 + timedelta(minutes=30)) is False

    @pytest.mark.parametrize("name", ["DB-PROD-03", "  db-prod-03  "])
    def test_asset_match_ignores_case_and_spaces(self, window, t0, name: str):
        assert window.covers(name, t0 + timedelta(minutes=30)) is True

    def test_interval_is_half_open(self, window, t0):
        """Окно, кончающееся в 14:00, и окно, начинающееся в 14:00, не должны
        оба претендовать на этот момент."""
        assert window.covers("db-prod-03", window.starts_at) is True
        assert window.covers("db-prod-03", window.ends_at) is False

    def test_outside_the_window_nothing_is_muted(self, window, t0):
        assert window.covers("db-prod-03", t0 - timedelta(seconds=1)) is False
        assert window.covers("db-prod-03", t0 + timedelta(days=1)) is False

    @pytest.mark.parametrize("name", ["", "   "])
    def test_asset_name_is_required(self, t0, name: str):
        """Пустое значение заглушило бы всю платформу — ровно тот способ
        спрятать аварию, ради предотвращения которого ограничение и существует."""
        with pytest.raises(DomainError):
            MaintenanceWindow(
                id=uuid.uuid4(),
                asset_name=name,
                starts_at=t0,
                ends_at=t0 + timedelta(hours=1),
                created_by=uuid.uuid4(),
                reason="Регламент",
            )

    def test_reason_is_required(self, t0):
        """Ответ на вопрос «почему в три ночи не было ни одного алерта»."""
        with pytest.raises(DomainError):
            MaintenanceWindow(
                id=uuid.uuid4(),
                asset_name="db-prod-03",
                starts_at=t0,
                ends_at=t0 + timedelta(hours=1),
                created_by=uuid.uuid4(),
                reason="  ",
            )

    @pytest.mark.parametrize("delta", [timedelta(0), timedelta(hours=-1)])
    def test_must_end_after_it_starts(self, t0, delta: timedelta):
        """Главное свойство окна — оно само выключается. Бессрочное окно вернуло
        бы в обиход «заглушил и забыл включить обратно»."""
        with pytest.raises(DomainError):
            MaintenanceWindow(
                id=uuid.uuid4(),
                asset_name="db-prod-03",
                starts_at=t0,
                ends_at=t0 + delta,
                created_by=uuid.uuid4(),
                reason="Регламент",
            )

    def test_records_who_declared_it(self, window):
        """Заглушенный объект обязан быть отличим от сломанной доставки."""
        assert window.created_by is not None
        assert window.reason


class TestRateLimit:
    def test_within_budget(self):
        limit = RateLimit(max_events=20, per=timedelta(hours=1))
        assert limit.exceeded(20) is False

    def test_over_budget(self):
        limit = RateLimit(max_events=20, per=timedelta(hours=1))
        assert limit.exceeded(21) is True

    def test_must_allow_at_least_one_event(self):
        with pytest.raises(DomainError):
            RateLimit(max_events=0, per=timedelta(hours=1))

    def test_two_scales_same_arithmetic(self):
        """П. 2.5 ограничивает приём от интеграции, п. 4.6 — доставку одному
        получателю. Числа разные, правило одно."""
        assert DEFAULT_RECIPIENT_RATE_LIMIT.max_events == 20
        assert DEFAULT_RECIPIENT_RATE_LIMIT.per == timedelta(hours=1)
        assert DEFAULT_SOURCE_RATE_LIMIT.max_events == 100
        assert DEFAULT_SOURCE_RATE_LIMIT.per == timedelta(minutes=1)


class TestGroupingRule:
    """П. 4.4 — группировка помимо точного совпадения fingerprint."""

    def test_groups_by_declared_tags_only(self):
        rule = GroupingRule(frozenset({"prod", "msk-dc1"}))
        assert rule.key_for(frozenset({"disk", "prod", "msk-dc1"})) == ("msk-dc1", "prod")

    def test_key_is_stable_regardless_of_tag_order(self):
        rule = GroupingRule(frozenset({"prod", "msk-dc1"}))
        assert rule.key_for(frozenset({"msk-dc1", "prod"})) == rule.key_for(
            frozenset({"prod", "msk-dc1"})
        )

    def test_unknown_tags_do_not_create_new_groups(self):
        """Группировка по *всем* тегам поместила бы каждое событие в свою группу,
        как только источник добавит уникальную метку, — и молча отключила бы
        защиту от шторма."""
        rule = GroupingRule(frozenset({"prod"}))
        first = rule.key_for(frozenset({"prod", "trace-id-1"}))
        second = rule.key_for(frozenset({"prod", "trace-id-2"}))
        assert first == second == ("prod",)

    def test_no_declared_tags_means_one_group(self):
        assert GroupingRule().key_for(frozenset({"prod", "disk"})) == ()
