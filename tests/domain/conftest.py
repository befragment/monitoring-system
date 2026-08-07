"""Общие фикстуры для тестов домена.

Всё время в тестах отсчитывается от фиксированного `T0`, а мутаторы домена
принимают `at` явно — поэтому ни один тест здесь не патчит часы и не спит.
Это и было причиной, по которой время передаётся аргументом.
"""

import uuid
from datetime import UTC, datetime

import pytest

from app.domain.asset import Asset
from app.domain.event import Event, IncomingEvent
from app.domain.integration import ConnectionType, Integration
from app.domain.problem import Problem
from app.domain.role import Role
from app.domain.severity import ZABBIX_SEVERITY_MAPPING
from app.domain.user import User

T0 = datetime(2026, 8, 6, 2, 14, tzinfo=UTC)


@pytest.fixture
def t0() -> datetime:
    return T0


@pytest.fixture
def zabbix() -> Integration:
    return Integration(
        id=uuid.uuid4(),
        slug="zabbix",
        title="Zabbix прод",
        connection=ConnectionType.PUSH,
        severity_mapping=ZABBIX_SEVERITY_MAPPING,
    )


@pytest.fixture
def asset() -> Asset:
    return Asset(name="db-prod-03", type="server")


@pytest.fixture
def incoming(asset: Asset) -> IncomingEvent:
    """Ровно пример payload'а из п. 2.1 ТЗ."""
    return IncomingEvent(
        source_system="zabbix",
        source_event_id="8841203",
        monitor_name="Свободное место на диске",
        raw_severity="high",
        asset=asset,
        message="Диск заполнен на 94%",
        occurred_at=T0,
        received_at=T0,
        tags=frozenset({"disk", "prod", "msk-dc1"}),
    )


@pytest.fixture
def event(incoming: IncomingEvent, zabbix: Integration) -> Event:
    return incoming.classify(zabbix.severity_mapping)


@pytest.fixture
def problem(event: Event) -> Problem:
    return Problem.open_from(event)


def make_user(role: Role, *, is_active: bool = True) -> User:
    return User(
        id=uuid.uuid4(),
        external_id=f"cn={role.value}",
        email=f"{role.value}@example.ru",
        full_name=f"Пользователь {role.value}",
        role=role,
        is_active=is_active,
    )


@pytest.fixture
def l1() -> User:
    return make_user(Role.L1)


@pytest.fixture
def l2() -> User:
    return make_user(Role.L2)


@pytest.fixture
def manager() -> User:
    return make_user(Role.MANAGER)
