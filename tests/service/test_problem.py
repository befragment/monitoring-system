"""Шаг 6 — работа дежурного."""

import uuid

import pytest

from app.domain.errors import (
    AcknowledgeNotPermitted,
    AlreadyAcknowledged,
    CloseReasonRequired,
    ProblemClosed,
)
from app.domain.problem import Problem, ProblemLevel, ProblemStatus
from app.service.errors import NotFound, PermissionDenied
from app.service.problem import ProblemService


@pytest.fixture
def problem(incoming, zabbix, problems) -> Problem:
    p = Problem.open_from(incoming.classify(zabbix.severity_mapping))
    problems.rows[p.id] = p
    return p


@pytest.fixture
def service(problems, events, deliveries, audit, users, routing, clock, uow) -> ProblemService:
    return ProblemService(problems, events, deliveries, audit, users, routing, clock, uow)


class TestAcknowledge:
    async def test_engineer_takes_the_problem(self, service, problem, l1):
        result = await service.acknowledge(problem.id, l1.id)
        assert result.status is ProblemStatus.ACKNOWLEDGED
        assert result.owner_id == l1.id

    async def test_manager_cannot(self, service, problem, manager):
        """Правило живёт в агрегате, сервис его не дублирует — два места, где
        записано одно правило, рано или поздно разойдутся."""
        with pytest.raises(AcknowledgeNotPermitted):
            await service.acknowledge(problem.id, manager.id)
        assert problem.status is ProblemStatus.ACTIVE

    async def test_admin_cannot(self, service, problem, admin):
        with pytest.raises(AcknowledgeNotPermitted):
            await service.acknowledge(problem.id, admin.id)

    async def test_role_comes_from_the_directory_not_the_token(
        self, service, problem, l1
    ):
        """Claim в токене отражает состояние на момент выдачи: переведённый в
        manager час назад до истечения токена продолжал бы нажимать кнопку,
        которой у него уже нет."""
        from app.domain.role import Role

        l1.role = Role.MANAGER
        with pytest.raises(AcknowledgeNotPermitted):
            await service.acknowledge(problem.id, l1.id)

    async def test_second_acknowledge_is_rejected(self, service, problem, l1, l2):
        await service.acknowledge(problem.id, l1.id)
        with pytest.raises(AlreadyAcknowledged):
            await service.acknowledge(problem.id, l2.id)

    async def test_inactive_user_cannot_act(self, service, problem, l1):
        l1.is_active = False
        with pytest.raises(PermissionDenied):
            await service.acknowledge(problem.id, l1.id)

    async def test_it_is_audited(self, service, problem, l1, audit):
        await service.acknowledge(problem.id, l1.id)
        record = next(r for r in audit.rows if r.action.value == "problem_acknowledged")
        assert record.actor_id == l1.id

    async def test_it_stops_escalation(self, service, problem, l1):
        await service.acknowledge(problem.id, l1.id)
        assert problem.escalation_active is False

    async def test_unknown_problem(self, service, l1):
        with pytest.raises(NotFound):
            await service.acknowledge(uuid.uuid4(), l1.id)

    async def test_unknown_actor(self, service, problem):
        with pytest.raises(NotFound):
            await service.acknowledge(problem.id, uuid.uuid4())


class TestHandover:
    async def test_l1_hands_over(self, service, problem, l1):
        result = await service.hand_over(problem.id, l1.id)
        assert result.level is ProblemLevel.L2

    async def test_l2_cannot(self, service, problem, l2):
        """Для L2 это движение вбок, а не передача."""
        with pytest.raises(PermissionDenied):
            await service.hand_over(problem.id, l2.id)

    async def test_manager_cannot(self, service, problem, manager):
        with pytest.raises(PermissionDenied):
            await service.hand_over(problem.id, manager.id)

    async def test_it_notifies_l2_immediately(self, service, problem, l1, deliveries):
        """L2 обязан узнать о передаче сразу, а не на следующем тике
        планировщика."""
        before = len(deliveries.rows)
        await service.hand_over(problem.id, l1.id)
        assert len(deliveries.rows) > before

    async def test_it_is_audited_apart_from_escalation(self, service, problem, l1, audit):
        """«Человек оценил и передал» и «сработал таймер» в разборе инцидента —
        принципиально разные события."""
        await service.hand_over(problem.id, l1.id)
        assert "problem_handed_over" in audit.actions()
        assert "escalation_triggered" not in audit.actions()

    async def test_problem_stays_active(self, service, problem, l1):
        """Передача — не реакция вообще: в работу проблему всё ещё никто не взял,
        и таймер последней ступени продолжает идти."""
        await service.hand_over(problem.id, l1.id)
        assert problem.status is ProblemStatus.ACTIVE
        assert problem.escalation_active is True


class TestClose:
    async def test_engineer_closes_with_a_reason(self, service, problem, l1):
        result = await service.close(problem.id, l1.id, reason="Заменили диск")
        assert result.status is ProblemStatus.CLOSED
        assert result.close_reason == "Заменили диск"

    @pytest.mark.parametrize("reason", ["", "   ", "\n"])
    async def test_empty_reason_is_impossible(self, service, problem, l1, reason):
        """Критерий п. 6.4 требует, чтобы это было технически невозможно, а
        валидатор на одной точке входа такого не даёт."""
        with pytest.raises(CloseReasonRequired):
            await service.close(problem.id, l1.id, reason=reason)
        assert problem.status is ProblemStatus.ACTIVE

    async def test_manager_cannot_close(self, service, problem, manager):
        with pytest.raises(PermissionDenied):
            await service.close(problem.id, manager.id, reason="почему бы и нет")

    async def test_closing_twice_is_rejected(self, service, problem, l1):
        await service.close(problem.id, l1.id, reason="Заменили диск")
        with pytest.raises(ProblemClosed):
            await service.close(problem.id, l1.id, reason="ещё раз")

    async def test_it_notifies(self, service, problem, l1, deliveries):
        """Сообщение о закрытии снимает тревогу — его шлём всегда."""
        before = len(deliveries.rows)
        await service.close(problem.id, l1.id, reason="Заменили диск")
        assert len(deliveries.rows) > before

    async def test_human_close_is_distinguishable_from_automatic(
        self, service, problem, l1
    ):
        result = await service.close(problem.id, l1.id, reason="Заменили диск")
        assert result.closed_by == str(l1.id)
        assert result.closed_by != "system"


class TestCard:
    async def test_card_gathers_the_whole_context(
        self, service, problem, l1, audit, deliveries, routing
    ):
        await routing.route(problem)
        await service.acknowledge(problem.id, l1.id)

        card = await service.card(problem.id)
        assert card.problem.id == problem.id
        assert card.deliveries
        assert card.audit
        assert card.owner_name == l1.full_name

    async def test_card_answers_why_it_did_not_arrive(
        self, service, problem, routing, maintenance, window_now
    ):
        """Ради этого и заведена сущность доставки: видно и правило, и исход, и
        причину подавления."""
        maintenance.rows.append(window_now)
        await routing.route(problem)

        card = await service.card(problem.id)
        assert all(d.suppression_reason is not None for d in card.deliveries)

    async def test_unknown_problem(self, service):
        with pytest.raises(NotFound):
            await service.card(uuid.uuid4())


class TestTransaction:
    async def test_each_action_commits_once(self, service, problem, l1, uow):
        await service.acknowledge(problem.id, l1.id)
        assert uow.commits == 1
        await service.close(problem.id, l1.id, reason="Заменили диск")
        assert uow.commits == 2

    async def test_rejected_action_commits_nothing(self, service, problem, manager, uow):
        with pytest.raises(AcknowledgeNotPermitted):
            await service.acknowledge(problem.id, manager.id)
        assert uow.commits == 0
