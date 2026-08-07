"""Лестница эскалации: кого будят, когда и в каком порядке."""

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


@pytest.fixture
def policy() -> EscalationPolicy:
    return DEFAULT_ESCALATION_POLICY


class TestDefaultLadder:
    def test_has_three_rungs(self, policy: EscalationPolicy):
        assert [step.target for step in policy.steps] == [
            EscalationTarget.L1_CHANNEL,
            EscalationTarget.L2_CHANNEL,
            EscalationTarget.L2_BROADCAST,
        ]

    def test_timings(self, policy: EscalationPolicy):
        assert [step.after for step in policy.steps] == [
            timedelta(0),
            timedelta(minutes=15),
            timedelta(minutes=30),
        ]

    def test_offsets_are_absolute(self, policy: EscalationPolicy):
        """Отсчёт от появления проблемы, а не от предыдущего шага: задержавшийся
        шаг не сдвигает все последующие."""
        assert policy.steps[2].after == timedelta(minutes=30)

    def test_last_rung_is_a_broadcast(self, policy: EscalationPolicy):
        """Ступени 2 и 3 различаются адресацией, а не получателем: сначала одно
        сообщение в общий канал L2, затем персонально каждому. Без этого
        различия третья ступень была бы повтором второй."""
        assert policy.steps[1].target is not policy.steps[2].target


class TestDueStep:
    def test_first_rung_fires_immediately(self, policy: EscalationPolicy, t0):
        step = policy.due_step(opened_at=t0, now=t0, steps_taken=0)
        assert step.target is EscalationTarget.L1_CHANNEL

    @pytest.mark.parametrize("minutes", [0, 1, 14])
    def test_second_rung_waits(self, policy: EscalationPolicy, t0, minutes: int):
        assert policy.due_step(t0, t0 + timedelta(minutes=minutes), steps_taken=1) is None

    def test_second_rung_fires_at_fifteen(self, policy: EscalationPolicy, t0):
        step = policy.due_step(t0, t0 + timedelta(minutes=15), steps_taken=1)
        assert step.target is EscalationTarget.L2_CHANNEL

    def test_broadcast_fires_at_thirty(self, policy: EscalationPolicy, t0):
        step = policy.due_step(t0, t0 + timedelta(minutes=30), steps_taken=2)
        assert step.target is EscalationTarget.L2_BROADCAST

    def test_returns_one_rung_at_a_time(self, policy: EscalationPolicy, t0):
        """Если воркер лежал час, при возобновлении нельзя вывалить человеку в
        телефон всю лестницу разом — каждая отправка получает свой проход."""
        much_later = t0 + timedelta(hours=1)
        first = policy.due_step(t0, much_later, steps_taken=0)
        assert first.target is EscalationTarget.L1_CHANNEL
        second = policy.due_step(t0, much_later, steps_taken=1)
        assert second.target is EscalationTarget.L2_CHANNEL

    def test_nothing_left_after_the_last_rung(self, policy: EscalationPolicy, t0):
        assert policy.due_step(t0, t0 + timedelta(days=1), steps_taken=3) is None
        assert policy.is_exhausted(3) is True
        assert policy.is_exhausted(2) is False


class TestLadderAfterHandover:
    """Ручная передача съедает первые две ступени, но не последнюю."""

    def test_channel_rung_does_not_fire_again(self, policy: EscalationPolicy, problem: Problem, t0):
        problem.hand_over(at=t0 + timedelta(minutes=3))
        assert (
            policy.due_step(t0, t0 + timedelta(minutes=16), problem.escalation_steps_taken)
            is None
        )

    def test_broadcast_still_fires(self, policy: EscalationPolicy, problem: Problem, t0):
        """Иначе передача была бы способом похоронить проблему."""
        problem.hand_over(at=t0 + timedelta(minutes=3))
        step = policy.due_step(t0, t0 + timedelta(minutes=30), problem.escalation_steps_taken)
        assert step.target is EscalationTarget.L2_BROADCAST


class TestSeverityThreshold:
    """Открытый вопрос проекта: порог числом ещё не назван, но форма готова."""

    def test_no_threshold_means_every_problem_escalates(self, policy: EscalationPolicy):
        assert policy.min_severity is None
        assert policy.applies_to(Severity.INFO) is True

    def test_threshold_filters_by_severity(self):
        """Смысл решённый: warning про диск никого не будит, падение прода будит."""
        strict = EscalationPolicy(
            steps=(EscalationStep(EscalationTarget.L1_CHANNEL, timedelta(0)),),
            min_severity=Severity.MAJOR,
        )
        assert strict.applies_to(Severity.WARNING) is False
        assert strict.applies_to(Severity.MAJOR) is True
        assert strict.applies_to(Severity.CRITICAL) is True


def test_policy_holds_no_state(policy: EscalationPolicy, t0):
    """Счётчик пройденных ступеней живёт на Problem: иначе воркер после
    перезапуска либо разослал бы всю лестницу заново, либо не разослал ничего."""
    assert policy.due_step(t0, t0, 0) == policy.due_step(t0, t0, 0)
    assert not hasattr(policy, "steps_taken")
