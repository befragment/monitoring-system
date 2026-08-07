"""Шаг 1 — подключённая система мониторинга."""

import uuid
from datetime import timedelta

import pytest

from app.domain.integration import ConnectionType, Integration
from app.domain.severity import ZABBIX_SEVERITY_MAPPING, Severity, SeverityMapping
from app.domain.suppression import DEFAULT_SOURCE_RATE_LIMIT, RateLimit


def make(**overrides) -> Integration:
    defaults = dict(
        id=uuid.uuid4(),
        slug="zabbix",
        title="Zabbix прод",
        connection=ConnectionType.PUSH,
        severity_mapping=ZABBIX_SEVERITY_MAPPING,
    )
    return Integration(**{**defaults, **overrides})


def test_slug_must_not_be_empty():
    """Slug — это то, что приходит в `source_system` и входит в fingerprint.
    Пустой означал бы, что события разных систем схлопываются в одну проблему."""
    with pytest.raises(ValueError):
        make(slug="   ")


def test_push_integration_needs_no_poller(zabbix: Integration):
    assert zabbix.is_polled is False


def test_pull_integration_needs_a_poller():
    """П. 1.2.2 — платформа сама опрашивает источник по расписанию."""
    assert make(slug="snmp", connection=ConnectionType.PULL).is_polled is True


def test_intake_limit_defaults_to_the_conservative_one():
    """П. 2.5 — лимит принадлежит интеграции, а не объекту: «Zabbix залил нас»
    это свойство источника."""
    assert make().intake_limit is DEFAULT_SOURCE_RATE_LIMIT


def test_intake_limit_is_per_integration():
    """Разным источникам — разные бюджеты."""
    quiet = make(slug="snmp", intake_limit=RateLimit(max_events=5, per=timedelta(minutes=1)))
    assert quiet.intake_limit.exceeded(6) is True
    assert make().intake_limit.exceeded(6) is False


def test_severity_mapping_belongs_to_the_integration():
    """П. 3.1 — таблица своя на каждый источник: «average» означает вполне
    конкретную вещь в Zabbix и ничего в Prometheus."""
    prometheus = make(
        slug="prometheus",
        severity_mapping=SeverityMapping(
            source_system="prometheus",
            table={"page": Severity.CRITICAL},
        ),
    )
    assert prometheus.severity_mapping.classify("page") is Severity.CRITICAL
    # То же слово в чужой таблице ничего не значит и уедет в fallback.
    assert make().severity_mapping.classify("page") is Severity.WARNING


def test_credentials_are_stored_by_reference_only():
    """Домен не должен уметь предъявить токен — только знать, как его спросить."""
    integration = make(credentials_ref="vault://monitoring/zabbix")
    assert integration.credentials_ref == "vault://monitoring/zabbix"


def test_is_enabled_by_default_and_immutable(zabbix: Integration):
    """Конфигурация правится заменой записи целиком, а не мутацией на месте."""
    assert zabbix.is_enabled is True
    with pytest.raises((AttributeError, TypeError)):
        zabbix.is_enabled = False  # type: ignore[misc]
