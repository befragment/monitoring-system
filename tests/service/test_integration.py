"""Настройка источников — раздел 8 ТЗ и критерий «конфигурируемое подключение»."""

import uuid
from datetime import timedelta

import pytest

from app.domain.integration import ConnectionType
from app.domain.severity import Severity
from app.domain.suppression import RateLimit
from app.service.errors import NotFound, PermissionDenied
from app.service.integration import IntegrationService


@pytest.fixture
def service(integrations, users, audit, uow) -> IntegrationService:
    return IntegrationService(integrations, users, audit, uow)


class TestCreate:
    async def test_admin_adds_a_source(self, service, admin, integrations):
        created = await service.create(
            admin.id,
            slug="prometheus",
            title="Prometheus",
            connection=ConnectionType.PULL,
            severity_table={"page": Severity.CRITICAL},
        )
        assert created.slug in integrations.rows
        assert created.is_polled is True

    async def test_second_source_needs_no_code(self, service, admin):
        """Смысл сущности: добавление источника — это запись в базе, а не
        правка кода."""
        await service.create(
            admin.id,
            slug="saymon",
            title="Saymon",
            connection=ConnectionType.PUSH,
            severity_table={"alarm": Severity.MAJOR},
        )
        assert len(await service.list_all()) == 2

    async def test_slug_is_normalized(self, service, admin):
        """Slug участвует в fingerprint — разнобой в регистре расщепил бы
        проблемы одного источника на два набора."""
        created = await service.create(
            admin.id,
            slug="  Prometheus  ",
            title="Prometheus",
            connection=ConnectionType.PULL,
            severity_table={},
        )
        assert created.slug == "prometheus"

    async def test_default_fallback_is_warning(self, service, admin):
        """Незнакомая severity — дыра в конфигурации, и ни одна крайность не
        безопасна: INFO похоронит аварию, CRITICAL поднимет ночью из-за
        опечатки."""
        created = await service.create(
            admin.id,
            slug="prometheus",
            title="Prometheus",
            connection=ConnectionType.PULL,
            severity_table={},
        )
        assert created.severity_mapping.classify("что угодно") is Severity.WARNING

    async def test_default_intake_limit_is_applied(self, service, admin):
        created = await service.create(
            admin.id,
            slug="prometheus",
            title="Prometheus",
            connection=ConnectionType.PULL,
            severity_table={},
        )
        assert created.intake_limit.max_events == 100


class TestPermissions:
    @pytest.mark.parametrize("role_fixture", ["l1", "l2", "manager"])
    async def test_only_admin_configures(self, service, request, role_fixture):
        actor = request.getfixturevalue(role_fixture)
        with pytest.raises(PermissionDenied):
            await service.create(
                actor.id,
                slug="prometheus",
                title="Prometheus",
                connection=ConnectionType.PULL,
                severity_table={},
            )

    async def test_unknown_actor(self, service):
        with pytest.raises(NotFound):
            await service.create(
                uuid.uuid4(),
                slug="x",
                title="X",
                connection=ConnectionType.PUSH,
                severity_table={},
            )

    async def test_unknown_integration(self, service, admin):
        with pytest.raises(NotFound):
            await service.set_enabled(uuid.uuid4(), admin.id, enabled=False)


class TestSeverityTable:
    async def test_table_is_replaced(self, service, admin, zabbix):
        updated = await service.update_severity_table(
            zabbix.id, admin.id, severity_table={"critical": Severity.CRITICAL}
        )
        assert updated.severity_mapping.classify("critical") is Severity.CRITICAL

    async def test_old_keys_disappear(self, service, admin, zabbix):
        updated = await service.update_severity_table(
            zabbix.id, admin.id, severity_table={"critical": Severity.CRITICAL}
        )
        assert updated.severity_mapping.classify("disaster") is Severity.WARNING

    async def test_source_system_stays_bound_to_the_slug(self, service, admin, zabbix):
        updated = await service.update_severity_table(
            zabbix.id, admin.id, severity_table={"critical": Severity.CRITICAL}
        )
        assert updated.severity_mapping.source_system == zabbix.slug

    async def test_history_is_not_reclassified(self, service, admin, zabbix, events):
        """Уже разобранные события — неизменяемые записи журнала: правка таблицы
        влияет только на то, что придёт после неё, иначе история перестанет
        воспроизводиться."""
        before = len(events.rows)
        await service.update_severity_table(
            zabbix.id, admin.id, severity_table={"critical": Severity.CRITICAL}
        )
        assert len(events.rows) == before


class TestLifecycle:
    async def test_disable_keeps_the_record(self, service, admin, zabbix, integrations):
        """Выключенный источник перестаёт приниматься, но его история остаётся —
        так же, как неактивный пользователь не теряет подписки."""
        updated = await service.set_enabled(zabbix.id, admin.id, enabled=False)
        assert updated.is_enabled is False
        assert updated.slug in integrations.rows

    async def test_disabled_source_is_filtered_out(self, service, admin, zabbix):
        await service.set_enabled(zabbix.id, admin.id, enabled=False)
        assert await service.list_all(only_enabled=True) == []

    async def test_intake_limit_is_configurable(self, service, admin, zabbix):
        updated = await service.set_intake_limit(
            zabbix.id, admin.id, limit=RateLimit(max_events=5, per=timedelta(seconds=10))
        )
        assert updated.intake_limit.max_events == 5

    async def test_update_replaces_the_record_wholesale(self, service, admin, zabbix):
        """`Integration` заморожен: конфигурация не может измениться
        «наполовину» под уже читающим её обработчиком событий."""
        updated = await service.set_enabled(zabbix.id, admin.id, enabled=False)
        assert updated is not zabbix
        assert zabbix.is_enabled is True


class TestAudit:
    async def test_every_change_is_journalled(self, service, admin, zabbix, audit):
        await service.set_enabled(zabbix.id, admin.id, enabled=False)
        record = next(r for r in audit.rows if r.action.value == "integration_changed")
        assert record.actor_id == admin.id
        assert record.details["integration"] == "zabbix"
        assert record.details["action"] == "disabled"

    async def test_rejected_change_commits_nothing(self, service, l1, zabbix, uow):
        with pytest.raises(PermissionDenied):
            await service.set_enabled(zabbix.id, l1.id, enabled=False)
        assert uow.commits == 0
