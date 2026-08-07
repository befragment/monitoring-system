"""Пп. 5.5, 5.6, 7.1 — люди, права, подписки и дежурства."""

import uuid
from datetime import timedelta

import pytest

from app.domain.notification import RoutingChannel
from app.domain.problem import Problem
from app.domain.role import Role
from app.domain.severity import Severity
from app.domain.user import DutyShift, Subscription, User

from .conftest import make_user


class TestRole:
    @pytest.mark.parametrize("role", [Role.L1, Role.L2])
    def test_engineers_can_acknowledge(self, role: Role):
        assert role.can_acknowledge is True

    @pytest.mark.parametrize("role", [Role.MANAGER, Role.ADMIN])
    def test_others_cannot_acknowledge(self, role: Role):
        """Manager получает ту же рассылку, что L2, но в разборе не участвует;
        admin не участвует тем более."""
        assert role.can_acknowledge is False

    @pytest.mark.parametrize("role", [Role.L1, Role.L2])
    def test_engineers_declare_maintenance(self, role: Role):
        """П. 4.5 — окно обслуживания объявляет тот, кто ведёт регламент."""
        assert role.is_engineer is True

    def test_admin_does_not_declare_maintenance(self):
        assert Role.ADMIN.is_engineer is False

    def test_the_model_stays_small(self):
        """Излишне усложнённая ролевая модель прямо названа риском (п. 10)."""
        assert len(Role) == 4


class TestUser:
    def test_has_no_password(self):
        """П. 7.1: платформа хранит, кто это и что ему можно видеть, но никогда —
        как он это доказывает."""
        assert not any("password" in field for field in User.__slots__)

    def test_identified_by_directory_id(self, l1: User):
        assert l1.external_id.startswith("cn=")

    def test_only_active_l2_receive_the_broadcast(self):
        assert make_user(Role.L2).receives_escalation is True
        assert make_user(Role.L2, is_active=False).receives_escalation is False

    @pytest.mark.parametrize("role", [Role.L1, Role.MANAGER, Role.ADMIN])
    def test_broadcast_does_not_reach_anyone_else(self, role: Role):
        """Broadcast на уволенного или на manager'а — это шум ровно тогда, когда
        его меньше всего можно себе позволить."""
        assert make_user(role).receives_escalation is False


class TestSubscription:
    """П. 5.6 — самостоятельное управление маршрутизацией."""

    def make(self, user_id: uuid.UUID, **overrides) -> Subscription:
        defaults = dict(
            id=uuid.uuid4(),
            user_id=user_id,
            channel=RoutingChannel.TELEGRAM,
            address="12345",
            min_severity=Severity.WARNING,
        )
        return Subscription(**{**defaults, **overrides})

    def test_matches_a_severe_enough_problem(self, problem: Problem, l1: User):
        assert self.make(l1.id).matches(problem) is True

    def test_disabled_subscription_never_matches(self, problem: Problem, l1: User):
        assert self.make(l1.id, is_enabled=False).matches(problem) is False

    def test_below_threshold_is_filtered_out(self, problem: Problem, l1: User):
        problem.severity = Severity.INFO
        assert self.make(l1.id, min_severity=Severity.WARNING).matches(problem) is False

    def test_threshold_is_inclusive(self, problem: Problem, l1: User):
        problem.severity = Severity.WARNING
        assert self.make(l1.id, min_severity=Severity.WARNING).matches(problem) is True

    def test_no_tags_means_no_tag_filter(self, problem: Problem, l1: User):
        assert self.make(l1.id, tags=frozenset()).matches(problem) is True

    def test_one_matching_tag_is_enough(self, problem: Problem, l1: User):
        assert self.make(l1.id, tags=frozenset({"prod", "spb-dc2"})).matches(problem) is True

    def test_foreign_tags_are_filtered_out(self, problem: Problem, l1: User):
        """Так выражается требование п. 7.1 «группа видит только относящиеся к
        ней показатели» — без отдельной системы ACL."""
        assert self.make(l1.id, tags=frozenset({"dev"})).matches(problem) is False

    def test_without_the_tag_filter_everyone_would_get_everything(
        self, problem: Problem, l1: User
    ):
        foreign = self.make(l1.id, tags=frozenset({"dev"}))
        unfiltered = self.make(l1.id, tags=frozenset())
        assert foreign.matches(problem) is not unfiltered.matches(problem)

    def test_matching_is_pure(self, problem: Problem, l1: User):
        """На вопрос «почему мне не пришло» отвечают вызовом этой функции — она
        обязана быть повторяемой и не менять проблему."""
        subscription = self.make(l1.id)
        before = (problem.severity, problem.tags, problem.event_count)
        assert subscription.matches(problem) == subscription.matches(problem)
        assert (problem.severity, problem.tags, problem.event_count) == before

    def test_bound_to_a_user_not_to_a_role(self, l1: User):
        """Роль даёт умолчания, но не должна жёстко определять подписки (п. 5.6):
        инженер может заглушить шумный тег без правки ролей администратором."""
        assert self.make(l1.id).user_id == l1.id
        assert not hasattr(self.make(l1.id), "role")


class TestDutyShift:
    """П. 5.5 — заготовка на перспективу."""

    @pytest.fixture
    def shift(self, l1: User, t0) -> DutyShift:
        return DutyShift(
            id=uuid.uuid4(),
            user_id=l1.id,
            starts_at=t0,
            ends_at=t0 + timedelta(hours=8),
        )

    def test_covers_its_interval(self, shift: DutyShift, t0):
        assert shift.covers(t0 + timedelta(hours=4)) is True

    def test_interval_is_half_open(self, shift: DutyShift):
        """При передаче смены дежурный ровно один — не двое и не ноль."""
        assert shift.covers(shift.starts_at) is True
        assert shift.covers(shift.ends_at) is False

    def test_is_a_historical_fact(self, shift: DutyShift):
        """Журнал аудита обязан уметь ответить, кто дежурил в момент эскалации,
        спустя долгое время после того, как график поменялся."""
        with pytest.raises((AttributeError, TypeError)):
            shift.user_id = uuid.uuid4()  # type: ignore[misc]
