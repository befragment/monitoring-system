"""П. 6.3 — журнал, допускающий только добавление."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.domain.audit import AuditAction, AuditRecord


class TestAuditRecord:
    def test_create_fills_id_and_time(self):
        record = AuditRecord.create(AuditAction.PROBLEM_CREATED)
        assert isinstance(record.id, uuid.UUID)
        assert record.occurred_at.tzinfo is not None

    def test_explicit_time_wins(self, t0):
        """Воркер, разгребающий бэклог, пишет время события, а не стенных часов."""
        record = AuditRecord.create(AuditAction.PROBLEM_CLOSED, at=t0)
        assert record.occurred_at == t0

    def test_record_is_immutable(self):
        """Редактирование и удаление записей запрещены."""
        record = AuditRecord.create(AuditAction.PROBLEM_CREATED)
        with pytest.raises((AttributeError, TypeError)):
            record.action = AuditAction.PROBLEM_CLOSED  # type: ignore[misc]

    def test_details_are_read_only(self):
        record = AuditRecord.create(
            AuditAction.PROBLEM_HANDED_OVER, details={"from": "l1", "to": "l2"}
        )
        with pytest.raises(TypeError):
            record.details["from"] = "подделка"  # type: ignore[index]

    def test_details_are_copied_defensively(self):
        """Без копии у вызывающего осталась бы живая ссылка на словарь, и он смог
        бы править «замороженную» запись."""
        source = {"reason": "Заменили диск"}
        record = AuditRecord.create(AuditAction.PROBLEM_CLOSED, details=source)
        source["reason"] = "подделка"
        assert record.details["reason"] == "Заменили диск"

    def test_platform_actions_have_no_actor(self):
        """Автозакрытие и таймеры эскалации не имеют человека за спиной, а
        фиктивный id пользователя испортил бы выборки «кто это сделал»."""
        record = AuditRecord.create(
            AuditAction.PROBLEM_RESOLVED, problem_id=uuid.uuid4()
        )
        assert record.actor_id is None

    def test_human_actions_record_the_actor(self, l1):
        record = AuditRecord.create(AuditAction.PROBLEM_ACKNOWLEDGED, actor_id=l1.id)
        assert record.actor_id == l1.id

    def test_details_default_to_empty(self):
        assert dict(AuditRecord.create(AuditAction.SETTINGS_CHANGED).details) == {}


class TestAuditAction:
    def test_manual_handover_differs_from_timer_escalation(self):
        """«Человек оценил и передал» и «никто не отреагировал, сработал таймер»
        в разборе инцидента — принципиально разные события."""
        assert AuditAction.PROBLEM_HANDED_OVER is not AuditAction.ESCALATION_TRIGGERED

    def test_automatic_close_differs_from_manual(self):
        assert AuditAction.PROBLEM_RESOLVED is not AuditAction.PROBLEM_CLOSED

    def test_suppression_differs_from_sending(self):
        """П. 7.3 требует различать причины по недоставленным сообщениям."""
        assert AuditAction.NOTIFICATION_SUPPRESSED is not AuditAction.NOTIFICATION_SENT

    def test_vocabulary_is_closed(self):
        """Фиксированный набор — это то, что делает журнал пригодным для
        запросов («покажи все закрытия за март»), а не грудой прозы под grep."""
        with pytest.raises(ValueError):
            AuditAction("что-то новенькое")

    def test_every_lifecycle_step_is_covered(self):
        """П. 6.4: каждое действие обязано оставлять запись."""
        required = {
            AuditAction.PROBLEM_CREATED,
            AuditAction.PROBLEM_ACKNOWLEDGED,
            AuditAction.PROBLEM_HANDED_OVER,
            AuditAction.PROBLEM_CLOSED,
            AuditAction.PROBLEM_RESOLVED,
            AuditAction.ESCALATION_TRIGGERED,
            AuditAction.MAINTENANCE_STARTED,
            AuditAction.MAINTENANCE_ENDED,
        }
        assert required <= set(AuditAction)


def test_journal_is_append_only_by_construction(l1):
    """Настоящая гарантия обязана прийти из базы — у роли приложения не должно
    быть грантов UPDATE и DELETE на эту таблицу. Здесь проверяется только то,
    что домен не предлагает мутаторов."""
    record = AuditRecord.create(AuditAction.PROBLEM_CLOSED, actor_id=l1.id)
    mutators = [
        name
        for name in dir(record)
        if name.startswith(("set_", "update_", "delete_", "mark_"))
    ]
    assert mutators == []


def test_records_are_ordered_by_time(l1, t0):
    """Журнал читают хронологически — на этом строится разбор инцидента."""
    earlier = AuditRecord.create(AuditAction.PROBLEM_CREATED, at=t0)
    later = AuditRecord.create(
        AuditAction.PROBLEM_CLOSED, at=t0 + timedelta(hours=1), actor_id=l1.id
    )
    assert earlier.occurred_at < later.occurred_at
    assert later.occurred_at < datetime.now(UTC)
