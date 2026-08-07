"""П. 4.5 — плановые работы.

Объявляет инженер, не администратор: регламент ведёт тот, кто его выполняет, и
заявка админу перед каждой ночной заменой диска гарантирует, что окном просто
перестанут пользоваться, а алерты начнут игнорировать вручную.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

from app.domain.audit import AuditAction, AuditRecord
from app.domain.suppression import MaintenanceWindow
from app.lib.clock import ClockInterface
from app.repository._contracts import (
    AuditRepository,
    MaintenanceRepository,
    UnitOfWork,
    UserRepository,
)
from app.service.errors import NotFound, PermissionDenied


class MaintenanceService:
    def __init__(
        self,
        maintenance: MaintenanceRepository,
        users: UserRepository,
        audit: AuditRepository,
        clock: ClockInterface,
        uow: UnitOfWork,
    ) -> None:
        self._maintenance = maintenance
        self._users = users
        self._audit = audit
        self._clock = clock
        self._uow = uow

    async def declare(
        self,
        actor_id: uuid.UUID,
        *,
        asset_name: str,
        starts_at: datetime,
        ends_at: datetime,
        reason: str,
    ) -> MaintenanceWindow:
        """Заглушить объект на время работ.

        Пустое имя объекта, пустая причина и окно без конца отсекаются доменом:
        первое заглушило бы платформу целиком, третье вернуло бы в обиход
        «заглушил и забыл включить обратно».
        """
        actor = await self._users.get_by_id(actor_id)
        if actor is None:
            raise NotFound(f"user {actor_id} not found")
        if not actor.role.is_engineer:
            raise PermissionDenied(f"role {actor.role} cannot declare maintenance")

        window = MaintenanceWindow(
            id=uuid.uuid4(),
            asset_name=asset_name,
            starts_at=starts_at,
            ends_at=ends_at,
            created_by=actor.id,
            reason=reason,
        )
        await self._maintenance.add(window)
        await self._audit.add(
            AuditRecord.create(
                AuditAction.MAINTENANCE_STARTED,
                actor_id=actor.id,
                at=self._clock.now(),
                details={
                    "asset": window.asset_name,
                    "from": window.starts_at.isoformat(),
                    "to": window.ends_at.isoformat(),
                    "reason": window.reason,
                },
            )
        )
        await self._uow.commit()
        return window

    async def end_early(self, window_id: uuid.UUID, actor_id: uuid.UUID) -> None:
        """Работы закончились раньше — оповещения должны пойти немедленно."""
        actor = await self._users.get_by_id(actor_id)
        if actor is None:
            raise NotFound(f"user {actor_id} not found")
        if not actor.role.is_engineer:
            raise PermissionDenied(f"role {actor.role} cannot end maintenance")

        now = self._clock.now()
        await self._maintenance.end_now(window_id, now)
        await self._audit.add(
            AuditRecord.create(
                AuditAction.MAINTENANCE_ENDED,
                actor_id=actor.id,
                at=now,
                details={"window": str(window_id), "ended": "early"},
            )
        )
        await self._uow.commit()

    async def active(self, at: datetime | None = None) -> Sequence[MaintenanceWindow]:
        """Что заглушено прямо сейчас. Нужно и интерфейсу, и разбору инцидента:
        именно этот список отвечает, почему по объекту не было алертов."""
        return await self._maintenance.list_active(at or self._clock.now())
