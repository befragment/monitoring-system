"""Настройка источников — п. 1.1–1.3 и раздел 8 ТЗ.

Административный сценарий: завести Zabbix, задать способ подключения, таблицу
соответствий severity и лимит приёма. «Конфигурируемое подключение источников»
— отдельный пункт критериев приёмки, и он означает именно это: добавление
второго источника не требует правки кода.
"""

import uuid
from collections.abc import Mapping
from dataclasses import replace

from app.domain.audit import AuditAction, AuditRecord
from app.domain.integration import ConnectionType, Integration
from app.domain.role import Role
from app.domain.severity import Severity, SeverityMapping
from app.domain.suppression import DEFAULT_SOURCE_RATE_LIMIT, RateLimit
from app.repository._contracts import (
    AuditRepository,
    IntegrationRepository,
    UnitOfWork,
    UserRepository,
)
from app.service.errors import NotFound, PermissionDenied


class IntegrationService:
    def __init__(
        self,
        integrations: IntegrationRepository,
        users: UserRepository,
        audit: AuditRepository,
        uow: UnitOfWork,
    ) -> None:
        self._integrations = integrations
        self._users = users
        self._audit = audit
        self._uow = uow

    async def list_all(self, *, only_enabled: bool = False) -> list[Integration]:
        return await self._integrations.list_all(only_enabled=only_enabled)

    async def create(
        self,
        actor_id: uuid.UUID,
        *,
        slug: str,
        title: str,
        connection: ConnectionType,
        severity_table: Mapping[str, Severity],
        fallback: Severity = Severity.WARNING,
        intake_limit: RateLimit = DEFAULT_SOURCE_RATE_LIMIT,
        endpoint: str | None = None,
        credentials_ref: str | None = None,
    ) -> Integration:
        """Завести источник.

        `fallback` по умолчанию WARNING, а не CRITICAL и не INFO: незнакомая
        severity — это дыра в конфигурации, и ни одна крайность не безопасна.
        INFO похоронит реальную аварию, CRITICAL поднимет человека ночью из-за
        опечатки. WARNING заметен, но не будит.
        """
        await self._require_admin(actor_id)

        integration = Integration(
            id=uuid.uuid4(),
            slug=slug.strip().lower(),
            title=title,
            connection=connection,
            severity_mapping=SeverityMapping(
                source_system=slug.strip().lower(),
                table=dict(severity_table),
                fallback=fallback,
            ),
            intake_limit=intake_limit,
            endpoint=endpoint,
            credentials_ref=credentials_ref,
        )
        await self._integrations.add(integration)
        await self._journal(actor_id, integration, "created")
        await self._uow.commit()
        return integration

    async def update_severity_table(
        self,
        integration_id: uuid.UUID,
        actor_id: uuid.UUID,
        *,
        severity_table: Mapping[str, Severity],
        fallback: Severity | None = None,
    ) -> Integration:
        """Правка таблицы соответствий (п. 3.1).

        Задним числом ничего не переклассифицируется: уже разобранные события —
        неизменяемые записи журнала, и правка таблицы влияет только на то, что
        придёт после неё. Иначе история перестала бы воспроизводиться.
        """
        current = await self._require(integration_id, actor_id)
        updated = self._replace(
            current,
            severity_mapping=SeverityMapping(
                source_system=current.slug,
                table=dict(severity_table),
                fallback=fallback or current.severity_mapping.fallback,
            ),
        )
        await self._integrations.save(updated)
        await self._journal(actor_id, updated, "severity_table_changed")
        await self._uow.commit()
        return updated

    async def set_enabled(
        self, integration_id: uuid.UUID, actor_id: uuid.UUID, *, enabled: bool
    ) -> Integration:
        """Выключенный источник перестаёт приниматься, но его проблемы и история
        остаются — так же, как неактивный пользователь не теряет подписки."""
        current = await self._require(integration_id, actor_id)
        updated = self._replace(current, is_enabled=enabled)
        await self._integrations.save(updated)
        await self._journal(actor_id, updated, "enabled" if enabled else "disabled")
        await self._uow.commit()
        return updated

    async def set_intake_limit(
        self, integration_id: uuid.UUID, actor_id: uuid.UUID, *, limit: RateLimit
    ) -> Integration:
        current = await self._require(integration_id, actor_id)
        updated = self._replace(current, intake_limit=limit)
        await self._integrations.save(updated)
        await self._journal(actor_id, updated, "intake_limit_changed")
        await self._uow.commit()
        return updated

    @staticmethod
    def _replace(integration: Integration, **changes) -> Integration:
        """`Integration` заморожен: правка — это замена записи целиком.

        Так конфигурация не может измениться «наполовину» под уже читающим её
        обработчиком событий.
        """
        return replace(integration, **changes)

    async def _require(self, integration_id: uuid.UUID, actor_id: uuid.UUID) -> Integration:
        await self._require_admin(actor_id)
        integration = await self._integrations.get_by_id(integration_id)
        if integration is None:
            raise NotFound(f"integration {integration_id} not found")
        return integration

    async def _require_admin(self, actor_id: uuid.UUID) -> None:
        actor = await self._users.get_by_id(actor_id)
        if actor is None:
            raise NotFound(f"user {actor_id} not found")
        if actor.role is not Role.ADMIN:
            raise PermissionDenied(f"role {actor.role} cannot configure integrations")

    async def _journal(
        self, actor_id: uuid.UUID, integration: Integration, what: str
    ) -> None:
        await self._audit.add(
            AuditRecord.create(
                AuditAction.INTEGRATION_CHANGED,
                actor_id=actor_id,
                details={
                    "integration": integration.slug,
                    "action": what,
                    "enabled": str(integration.is_enabled),
                },
            )
        )
