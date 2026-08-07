"""Шаги 4 и 6 — дедупликация и жизненный цикл проблемы."""

import uuid
from dataclasses import replace
from datetime import timedelta

import pytest

from app.domain.errors import (
    AcknowledgeNotPermitted,
    AlreadyAcknowledged,
    CloseReasonRequired,
    FingerprintMismatch,
    HandoverNotAllowed,
    ProblemClosed,
)
from app.domain.problem import SYSTEM_ACTOR, Problem, ProblemLevel, ProblemStatus
from app.domain.role import Role
from app.domain.severity import Severity


class TestOpening:
    def test_starts_active_on_l1(self, problem: Problem):
        """Любое уведомление сначала попадает на L1 — он прокси для всего."""
        assert problem.status is ProblemStatus.ACTIVE
        assert problem.level is ProblemLevel.L1

    def test_starts_from_ok(self, problem: Problem):
        """Как в примере п. 5.1, где `"prev_state": "OK"`."""
        assert problem.previous_severity is Severity.OK
        assert problem.severity is Severity.CRITICAL

    def test_copies_identity_from_event(self, problem: Problem, event):
        assert problem.fingerprint == event.fingerprint
        assert problem.source_system == event.source_system
        assert problem.asset == event.asset
        assert problem.monitor_name == event.monitor_name

    def test_first_event_is_counted(self, problem: Problem, t0):
        assert problem.event_count == 1
        assert problem.first_seen_at == t0
        assert problem.last_seen_at == t0

    def test_nothing_is_escalated_yet(self, problem: Problem):
        assert problem.escalation_steps_taken == 0
        assert problem.escalation_active is True


class TestDeduplication:
    """П. 4.2 — повтор сворачивается в существующую проблему."""

    def test_repeat_grows_the_counter(self, problem: Problem, event):
        problem.register(event)
        problem.register(event)
        assert problem.event_count == 3

    def test_repeat_keeps_identity(self, problem: Problem, event):
        before = problem.id
        problem.register(event)
        assert problem.id == before
        assert problem.fingerprint == event.fingerprint

    def test_latest_message_wins(self, problem: Problem, incoming, zabbix):
        later = replace(incoming, message="Диск заполнен на 97%").classify(
            zabbix.severity_mapping
        )
        problem.register(later)
        assert problem.last_message == "Диск заполнен на 97%"

    def test_tags_accumulate(self, problem: Problem, incoming, zabbix):
        """Источник может добавить метку только на поздних срабатываниях, а
        маршрутизация (п. 5.6) должна видеть всё, что известно о проблеме."""
        later = replace(incoming, tags=frozenset({"escalated"})).classify(
            zabbix.severity_mapping
        )
        problem.register(later)
        assert {"disk", "prod", "msk-dc1", "escalated"} <= problem.tags

    def test_out_of_order_delivery_does_not_rewind_last_seen(
        self, problem: Problem, incoming, zabbix, t0
    ):
        """Ретрай из очереди не должен тянуть last_seen_at назад и выставлять
        проблему протухшей для эскалации."""
        problem.register(
            replace(incoming, occurred_at=t0 + timedelta(minutes=5)).classify(
                zabbix.severity_mapping
            )
        )
        problem.register(
            replace(incoming, occurred_at=t0 - timedelta(minutes=5)).classify(
                zabbix.severity_mapping
            )
        )
        assert problem.last_seen_at == t0 + timedelta(minutes=5)

    def test_severity_change_records_the_previous_one(
        self, problem: Problem, incoming, zabbix
    ):
        worse = replace(incoming, raw_severity="average").classify(
            zabbix.severity_mapping
        )
        problem.register(worse)
        assert problem.severity is Severity.MAJOR
        assert problem.previous_severity is Severity.CRITICAL
        assert problem.severity_changed is True

    def test_repeat_at_same_severity_keeps_the_last_different_one(
        self, problem: Problem, incoming, zabbix
    ):
        """Хранится последняя *отличавшаяся* критичность: иначе повтор на том же
        уровне давал бы «CRITICAL -> CRITICAL» и терял контекст перехода."""
        problem.register(replace(incoming, raw_severity="high").classify(zabbix.severity_mapping))
        assert problem.previous_severity is Severity.OK

    def test_foreign_event_is_rejected(self, problem: Problem, incoming, zabbix):
        """Достижимо только через баг сервисного слоя, но агрегат отказывается
        молча всасывать чужие события в свои счётчики."""
        alien = replace(incoming, monitor_name="Сервис недоступен").classify(
            zabbix.severity_mapping
        )
        with pytest.raises(FingerprintMismatch):
            problem.register(alien)
        assert problem.event_count == 1


class TestAutoClose:
    """П. 4.3 — объект вернулся в норму."""

    @pytest.fixture
    def recovery(self, incoming, zabbix):
        return replace(incoming, raw_severity="resolved").classify(zabbix.severity_mapping)

    def test_recovery_event_closes_the_problem(self, problem: Problem, recovery):
        problem.register(recovery)
        assert problem.status is ProblemStatus.CLOSED

    def test_close_reason_is_generated(self, problem: Problem, recovery):
        """Правило «без причины не закрывать» остаётся ненарушенным и тогда,
        когда закрывает не человек."""
        problem.register(recovery)
        assert problem.close_reason
        assert problem.closed_by == SYSTEM_ACTOR

    def test_severity_drops_to_ok(self, problem: Problem, recovery):
        problem.register(recovery)
        assert problem.severity is Severity.OK
        assert problem.previous_severity is Severity.CRITICAL

    def test_recovery_uses_event_time(self, problem: Problem, incoming, zabbix, t0):
        """Воркер, разгребающий бэклог, обязан использовать время события,
        а не стенных часов."""
        late = replace(
            incoming, raw_severity="ok", occurred_at=t0 + timedelta(minutes=40)
        ).classify(zabbix.severity_mapping)
        problem.register(late)
        assert problem.closed_at == t0 + timedelta(minutes=40)

    def test_resolve_is_idempotent(self, problem: Problem):
        """События восстановления часто дублируются."""
        problem.resolve()
        first_closed_at = problem.closed_at
        problem.resolve()
        assert problem.closed_at == first_closed_at


class TestClosedIsTerminal:
    """Дедупликация ищет совпадение только среди незакрытых проблем."""

    def test_closed_problem_rejects_new_events(self, problem: Problem, event, l1):
        """Тот же fingerprint после закрытия обязан открыть новый эпизод —
        иначе «сколько раз это ломалось за неделю» останется без ответа."""
        problem.close(reason="Почистили логи", closed_by=str(l1.id))
        with pytest.raises(ProblemClosed):
            problem.register(event)

    def test_new_episode_has_the_same_fingerprint(self, problem: Problem, event, l1):
        problem.close(reason="Почистили логи", closed_by=str(l1.id))
        second = Problem.open_from(event)
        assert second.fingerprint == problem.fingerprint
        assert second.id != problem.id
        assert second.event_count == 1  # счётчик начинается заново

    def test_closed_problem_cannot_be_acknowledged(self, problem: Problem, l1):
        problem.close(reason="Почистили логи", closed_by=str(l1.id))
        with pytest.raises(ProblemClosed):
            problem.acknowledge(l1.id, l1.role)

    def test_closed_problem_cannot_be_handed_over(self, problem: Problem, l1):
        problem.close(reason="Почистили логи", closed_by=str(l1.id))
        with pytest.raises(ProblemClosed):
            problem.hand_over()


class TestAcknowledge:
    """П. 6.1 — обязательный шаг «взято в работу»."""

    @pytest.mark.parametrize("role", [Role.L1, Role.L2])
    def test_engineers_can_take_the_problem(self, problem: Problem, role: Role):
        problem.acknowledge(uuid.uuid4(), role)
        assert problem.status is ProblemStatus.ACKNOWLEDGED

    @pytest.mark.parametrize("role", [Role.MANAGER, Role.ADMIN])
    def test_others_cannot(self, problem: Problem, role: Role):
        """Manager видит проблему и получает ту же рассылку, что L2, но кнопки
        «взять в работу» у него нет; admin не участвует в разборе вовсе."""
        with pytest.raises(AcknowledgeNotPermitted):
            problem.acknowledge(uuid.uuid4(), role)
        assert problem.status is ProblemStatus.ACTIVE

    def test_owner_is_recorded(self, problem: Problem, l1, t0):
        problem.acknowledge(l1.id, l1.role, at=t0 + timedelta(minutes=2))
        assert problem.owner_id == l1.id
        assert problem.acknowledged_at == t0 + timedelta(minutes=2)

    def test_second_acknowledge_is_rejected(self, problem: Problem, l1, l2):
        """Не молчаливый no-op: второму нужно сообщить, кто уже взял проблему,
        иначе оба решат, что чинит другой."""
        problem.acknowledge(l1.id, l1.role)
        with pytest.raises(AlreadyAcknowledged):
            problem.acknowledge(l2.id, l2.role)
        assert problem.owner_id == l1.id

    def test_acknowledge_stops_escalation(self, problem: Problem, l1):
        """Реакция — это только ack; с этого момента лестница останавливается."""
        assert problem.escalation_active is True
        problem.acknowledge(l1.id, l1.role)
        assert problem.escalation_active is False


class TestHandover:
    """Передача L1 → L2."""

    def test_moves_the_problem_to_l2(self, problem: Problem, t0):
        problem.hand_over(at=t0 + timedelta(minutes=3))
        assert problem.level is ProblemLevel.L2
        assert problem.handed_over_at == t0 + timedelta(minutes=3)

    def test_is_not_a_reaction(self, problem: Problem):
        """В работу проблему всё ещё никто не взял, поэтому статус прежний, а
        таймер последней ступени продолжает идти. Иначе передача была бы
        способом похоронить проблему."""
        problem.hand_over()
        assert problem.status is ProblemStatus.ACTIVE
        assert problem.escalation_active is True
        assert problem.owner_id is None

    def test_consumes_the_first_two_rungs(self, problem: Problem):
        """Ступень L1 отработала при создании, ступень «канал L2» — только что,
        вручную. Повторять их автоматика не должна."""
        problem.hand_over()
        assert problem.escalation_steps_taken == 2

    def test_cannot_hand_over_twice(self, problem: Problem):
        """Возврата L2 → L1 нет: он позволил бы гонять проблему между уровнями,
        размывая ответственность."""
        problem.hand_over()
        with pytest.raises(HandoverNotAllowed):
            problem.hand_over()

    def test_acknowledged_problem_can_still_be_handed_over(self, problem: Problem, l1):
        """L1 взял в работу, разобрался, что задача тяжёлая, и передал дальше —
        это основной сценарий из ролевой модели."""
        problem.acknowledge(l1.id, l1.role)
        problem.hand_over()
        assert problem.level is ProblemLevel.L2


class TestEscalationBookkeeping:
    def test_escalate_counts_the_rung(self, problem: Problem):
        problem.escalate()
        assert problem.escalation_steps_taken == 1

    def test_second_rung_moves_the_problem_to_l2(self, problem: Problem, t0):
        """Со второй ступени разбор официально на стороне L2, даже если никто
        ничего не передавал руками."""
        problem.escalate(at=t0)
        assert problem.level is ProblemLevel.L1
        problem.escalate(at=t0 + timedelta(minutes=15))
        assert problem.level is ProblemLevel.L2
        assert problem.handed_over_at == t0 + timedelta(minutes=15)

    def test_manual_handover_time_is_not_overwritten(self, problem: Problem, t0):
        problem.hand_over(at=t0 + timedelta(minutes=3))
        problem.escalate(at=t0 + timedelta(minutes=30))
        assert problem.handed_over_at == t0 + timedelta(minutes=3)


class TestClose:
    """П. 6.1 и 6.4 — закрытие без причины технически невозможно."""

    @pytest.mark.parametrize("reason", ["", "   ", "\n\t"])
    def test_reason_is_required(self, problem: Problem, reason: str, l1):
        with pytest.raises(CloseReasonRequired):
            problem.close(reason=reason, closed_by=str(l1.id))
        assert problem.status is ProblemStatus.ACTIVE

    def test_reason_is_stripped(self, problem: Problem, l1):
        problem.close(reason="  Заменили диск  ", closed_by=str(l1.id))
        assert problem.close_reason == "Заменили диск"

    def test_records_who_and_when(self, problem: Problem, l1, t0):
        problem.close(
            reason="Заменили диск", closed_by=str(l1.id), at=t0 + timedelta(hours=1)
        )
        assert problem.closed_by == str(l1.id)
        assert problem.closed_at == t0 + timedelta(hours=1)
        assert problem.is_open is False

    def test_cannot_close_twice(self, problem: Problem, l1):
        problem.close(reason="Заменили диск", closed_by=str(l1.id))
        with pytest.raises(ProblemClosed):
            problem.close(reason="ещё раз", closed_by=str(l1.id))

    def test_manual_close_is_distinguishable_from_automatic(self, problem: Problem, l1):
        """Журнал аудита обязан отличать решение человека от автоматического
        восстановления."""
        problem.close(reason="Заменили диск", closed_by=str(l1.id))
        assert problem.closed_by != SYSTEM_ACTOR
