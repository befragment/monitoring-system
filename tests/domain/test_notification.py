"""Шаг 5 — исходящее сообщение и судьба каждой отправки."""

import uuid
from dataclasses import replace
from datetime import timedelta

import pytest

from app.domain.notification import (
    Delivery,
    DeliveryStatus,
    Digest,
    NotificationMessage,
    RoutingChannel,
)
from app.domain.problem import Problem
from app.domain.severity import Severity
from app.domain.suppression import SuppressionReason

PAYLOAD_KEYS = {
    "subject",
    "text",
    "priority",
    "monitor_name",
    "state",
    "prev_state",
    "object_id",
    "resolved",
    "owner",
    "created_at",
    "count",
}


class TestRoutingChannel:
    def test_webhook_exists_as_the_universal_channel(self):
        """П. 5.2: через webhook достижима любая внешняя интеграция — это
        удерживает слой доставки от частного случая на каждого вендора."""
        assert RoutingChannel.WEBHOOK in RoutingChannel

    def test_trueconf_is_a_separate_channel(self):
        """П. 5.3.3: требует постоянно живущего WebSocket-бота, поэтому не может
        доставляться тем же HTTP-воркером «отправил и забыл»."""
        assert RoutingChannel.TRUECONF != RoutingChannel.WEBHOOK


class TestNotificationMessage:
    """Внешний контракт платформы — формат п. 5.1."""

    def test_payload_has_exactly_the_agreed_keys(self, problem: Problem):
        """Переименование поля молча сломало бы каждую подписанную внешнюю
        систему, поэтому набор ключей зафиксирован тестом."""
        assert set(NotificationMessage.from_problem(problem).as_payload()) == PAYLOAD_KEYS

    def test_subject_names_the_asset_and_the_monitor(self, problem: Problem):
        subject = NotificationMessage.from_problem(problem).subject
        assert "db-prod-03" in subject
        assert "Свободное место на диске" in subject
        assert subject.startswith("[CRITICAL]")

    def test_resolved_problem_is_marked_in_the_subject(self, problem: Problem):
        problem.resolve()
        message = NotificationMessage.from_problem(problem)
        assert message.subject.startswith("[RESOLVED]")
        assert message.resolved is True

    @pytest.mark.parametrize(
        ("severity", "priority"),
        [
            (Severity.CRITICAL, 0),
            (Severity.MAJOR, 1),
            (Severity.WARNING, 2),
            (Severity.INFO, 3),
            (Severity.OK, 4),
        ],
    )
    def test_priority_is_numeric_and_inverted(
        self, problem: Problem, severity: Severity, priority: int
    ):
        """Меньше — срочнее, как в примере ТЗ, где у CRITICAL стоит priority 0."""
        problem.severity = severity
        assert NotificationMessage.from_problem(problem).priority == priority

    def test_state_transition_is_visible_without_asking_the_source(
        self, problem: Problem, incoming, zabbix
    ):
        """П. 5.1 — `prev_state` нужен, чтобы получатель видел переход."""
        problem.register(
            replace(incoming, raw_severity="average").classify(zabbix.severity_mapping)
        )
        message = NotificationMessage.from_problem(problem)
        assert message.state == "MAJOR"
        assert message.prev_state == "CRITICAL"

    def test_repeat_count_reaches_the_human_readable_body(
        self, problem: Problem, event
    ):
        """«повторов: 47» — это разница между кратковременным сбоем и аварией."""
        for _ in range(46):
            problem.register(event)
        message = NotificationMessage.from_problem(problem)
        assert message.count == 47
        assert "47" in message.text

    def test_single_event_has_no_repeat_suffix(self, problem: Problem):
        assert NotificationMessage.from_problem(problem).text == problem.last_message

    def test_owner_comes_from_outside(self, problem: Problem):
        """Домен хранит идентификатор, а сообщению нужно отображаемое имя —
        сопоставлять их работа сервисного слоя, а не агрегата."""
        assert NotificationMessage.from_problem(problem).owner is None
        assert NotificationMessage.from_problem(problem, owner="Иванов").owner == "Иванов"

    def test_is_a_snapshot(self, problem: Problem):
        """При смене критичности собирается новое сообщение, а не переписывается
        лежащее в очереди."""
        message = NotificationMessage.from_problem(problem)
        with pytest.raises((AttributeError, TypeError)):
            message.priority = 9  # type: ignore[misc]

    def test_created_at_is_serialized_as_iso(self, problem: Problem):
        assert NotificationMessage.from_problem(problem).as_payload()[
            "created_at"
        ] == problem.last_seen_at.isoformat()


class TestDigest:
    """П. 4.6 — одно сводное сообщение вместо тысячи алертов."""

    @pytest.fixture
    def digest(self, t0) -> Digest:
        return Digest(
            subject="Каскадный сбой в msk-dc1",
            text="47 проблем за минуту",
            problem_ids=(uuid.uuid4(), uuid.uuid4()),
            suppressed_count=45,
            created_at=t0,
        )

    def test_addresses_many_problems_at_once(self, digest: Digest):
        assert len(digest.problem_ids) == 2
        assert digest.suppressed_count == 45

    def test_storm_is_top_priority(self, digest: Digest):
        """Шторм как минимум не менее срочен, чем отдельные его алерты."""
        assert digest.priority == 0

    def test_has_no_prev_state_or_object_id(self, digest: Digest):
        """Отдельный тип, а не флаг на NotificationMessage: впихивание сводки в
        форму п. 5.1 означало бы враньё в этих полях."""
        assert not hasattr(digest, "prev_state")
        assert not hasattr(digest, "object_id")


class TestDelivery:
    """Факт отправки — то, чем отвечают на «почему мне не пришло»."""

    @pytest.fixture
    def delivery(self, problem: Problem) -> Delivery:
        return Delivery(
            id=uuid.uuid4(),
            problem_id=problem.id,
            channel=RoutingChannel.TELEGRAM,
            address="12345",
        )

    def test_starts_pending(self, delivery: Delivery):
        assert delivery.status is DeliveryStatus.PENDING
        assert delivery.is_settled is False
        assert delivery.attempts == 0

    def test_delivered(self, delivery: Delivery, t0):
        delivery.mark_delivered(at=t0)
        assert delivery.status is DeliveryStatus.DELIVERED
        assert delivery.attempts == 1
        assert delivery.settled_at == t0
        assert delivery.is_settled is True

    def test_failure_records_the_reason(self, delivery: Delivery):
        """«Не дошло» без объяснения не отвечает ни на один вопрос п. 7.3."""
        delivery.mark_failed("telegram: 429 too many requests")
        assert delivery.status is DeliveryStatus.FAILED
        assert delivery.failure_reason == "telegram: 429 too many requests"

    def test_retries_accumulate(self, delivery: Delivery):
        delivery.mark_failed("сеть недоступна")
        delivery.mark_delivered()
        assert delivery.attempts == 2
        assert delivery.status is DeliveryStatus.DELIVERED

    def test_suppression_is_not_a_failure(self, delivery: Delivery):
        """Подавленное по регламенту сообщение не должно портить долю успешной
        доставки на дашборде п. 7.3."""
        delivery.mark_suppressed(SuppressionReason.MAINTENANCE)
        assert delivery.status is DeliveryStatus.SUPPRESSED
        assert delivery.status is not DeliveryStatus.FAILED
        assert delivery.suppression_reason is SuppressionReason.MAINTENANCE

    def test_suppression_costs_no_attempt(self, delivery: Delivery):
        """Попытки не было — её придержали."""
        delivery.mark_suppressed(SuppressionReason.RECIPIENT_RATE_LIMIT)
        assert delivery.attempts == 0

    def test_subscription_send_is_distinguishable_from_escalation_send(
        self, problem: Problem, l1
    ):
        """«Не совпало правило подписки» и «лестница до тебя не дошла» —
        разные ответы на «почему мне не пришло»."""
        by_subscription = Delivery(
            id=uuid.uuid4(),
            problem_id=problem.id,
            channel=RoutingChannel.MAIL,
            address="l1@example.ru",
            recipient_id=l1.id,
            subscription_id=uuid.uuid4(),
        )
        by_escalation = Delivery(
            id=uuid.uuid4(),
            problem_id=problem.id,
            channel=RoutingChannel.MATTERMOST,
            address="#duty-l2",
            escalation_step=1,
        )
        assert by_subscription.escalation_step is None
        assert by_escalation.subscription_id is None
        # Командный канал не имеет персонального получателя.
        assert by_escalation.recipient_id is None

    def test_address_is_copied_not_referenced(self, delivery: Delivery):
        """Если человек завтра сменит адрес, история обязана помнить, куда
        сообщение ушло на самом деле."""
        delivery.mark_delivered()
        assert delivery.address == "12345"

    def test_settled_at_is_recorded(self, delivery: Delivery, t0):
        delivery.mark_failed("таймаут", at=t0 + timedelta(seconds=30))
        assert delivery.settled_at == t0 + timedelta(seconds=30)
