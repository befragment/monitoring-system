"""П. 4.5 — плановые работы."""

import uuid
from datetime import timedelta

import pytest

from app.domain.errors import DomainError
from app.service.errors import NotFound, PermissionDenied
from app.service.maintenance import MaintenanceService


@pytest.fixture
def service(maintenance, users, audit, clock, uow) -> MaintenanceService:
    return MaintenanceService(maintenance, users, audit, clock, uow)


class TestDeclare:
    async def test_engineer_declares_a_window(self, service, l1, t0, maintenance):
        window = await service.declare(
            l1.id,
            asset_name="db-prod-03",
            starts_at=t0,
            ends_at=t0 + timedelta(hours=2),
            reason="Замена диска",
        )
        assert window in maintenance.rows
        assert window.created_by == l1.id

    async def test_l2_can_declare_too(self, service, l2, t0):
        window = await service.declare(
            l2.id,
            asset_name="db-prod-03",
            starts_at=t0,
            ends_at=t0 + timedelta(hours=1),
            reason="Регламент",
        )
        assert window.created_by == l2.id

    async def test_admin_cannot(self, service, admin, t0):
        """Регламент ведёт тот, кто его выполняет. Заявка админу перед каждой
        ночной заменой диска гарантирует, что окном перестанут пользоваться."""
        with pytest.raises(PermissionDenied):
            await service.declare(
                admin.id,
                asset_name="db-prod-03",
                starts_at=t0,
                ends_at=t0 + timedelta(hours=1),
                reason="Регламент",
            )

    async def test_manager_cannot(self, service, manager, t0):
        with pytest.raises(PermissionDenied):
            await service.declare(
                manager.id,
                asset_name="db-prod-03",
                starts_at=t0,
                ends_at=t0 + timedelta(hours=1),
                reason="Регламент",
            )

    async def test_unknown_actor(self, service, t0):
        with pytest.raises(NotFound):
            await service.declare(
                uuid.uuid4(),
                asset_name="db-prod-03",
                starts_at=t0,
                ends_at=t0 + timedelta(hours=1),
                reason="Регламент",
            )


class TestDomainRulesHold:
    """Ограничения проверяет домен — сервис их не дублирует."""

    @pytest.mark.parametrize("asset_name", ["", "   "])
    async def test_blank_asset_is_rejected(self, service, l1, t0, asset_name):
        """Пустое имя заглушило бы платформу целиком — ровно тот способ спрятать
        аварию, ради предотвращения которого ограничение и заведено."""
        with pytest.raises(DomainError):
            await service.declare(
                l1.id,
                asset_name=asset_name,
                starts_at=t0,
                ends_at=t0 + timedelta(hours=1),
                reason="Регламент",
            )

    async def test_blank_reason_is_rejected(self, service, l1, t0):
        """Причина — ответ на вопрос «почему в три ночи не было алерта»."""
        with pytest.raises(DomainError):
            await service.declare(
                l1.id,
                asset_name="db-prod-03",
                starts_at=t0,
                ends_at=t0 + timedelta(hours=1),
                reason="  ",
            )

    async def test_window_must_end(self, service, l1, t0):
        """Бессрочное окно вернуло бы в обиход «заглушил и забыл включить»."""
        with pytest.raises(DomainError):
            await service.declare(
                l1.id,
                asset_name="db-prod-03",
                starts_at=t0,
                ends_at=t0,
                reason="Регламент",
            )

    async def test_nothing_is_committed_on_rejection(self, service, l1, t0, uow):
        with pytest.raises(DomainError):
            await service.declare(
                l1.id,
                asset_name="",
                starts_at=t0,
                ends_at=t0 + timedelta(hours=1),
                reason="Регламент",
            )
        assert uow.commits == 0


class TestEndEarly:
    async def test_window_is_removed(self, service, l1, maintenance, window_now):
        maintenance.rows.append(window_now)
        await service.end_early(window_now.id, l1.id)
        assert window_now not in maintenance.rows

    async def test_admin_cannot(self, service, admin, maintenance, window_now):
        maintenance.rows.append(window_now)
        with pytest.raises(PermissionDenied):
            await service.end_early(window_now.id, admin.id)

    async def test_it_is_audited(self, service, l1, maintenance, window_now, audit):
        maintenance.rows.append(window_now)
        await service.end_early(window_now.id, l1.id)
        assert "maintenance_ended" in audit.actions()


class TestVisibility:
    async def test_active_windows_are_listed(
        self, service, maintenance, window_now, clock
    ):
        """Список отвечает на вопрос, почему по объекту не было алертов."""
        maintenance.rows.append(window_now)
        assert list(await service.active()) == [window_now]

    async def test_expired_window_is_not_listed(
        self, service, maintenance, window_now, clock
    ):
        maintenance.rows.append(window_now)
        clock.advance(timedelta(days=1))
        assert list(await service.active()) == []


async def test_declaration_is_audited_with_the_reason(service, l1, t0, audit):
    await service.declare(
        l1.id,
        asset_name="db-prod-03",
        starts_at=t0,
        ends_at=t0 + timedelta(hours=2),
        reason="Замена диска",
    )
    record = next(r for r in audit.rows if r.action.value == "maintenance_started")
    assert record.actor_id == l1.id
    assert record.details["reason"] == "Замена диска"
    assert record.details["asset"] == "db-prod-03"
