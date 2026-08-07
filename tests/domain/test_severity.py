"""Шаг 3 — приведение severity источников к единой шкале."""

import pytest

from app.domain.severity import (
    ZABBIX_SEVERITY_MAPPING,
    Severity,
    SeverityMapping,
)


def test_scale_is_ordered():
    """Шкала сравнима: на этом держатся «не ниже WARNING» в подписке и
    «стало хуже» в жизненном цикле проблемы."""
    assert Severity.OK < Severity.INFO < Severity.WARNING
    assert Severity.WARNING < Severity.MAJOR < Severity.CRITICAL


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("not classified", Severity.INFO),
        ("information", Severity.INFO),
        ("warning", Severity.WARNING),
        ("average", Severity.MAJOR),
        ("high", Severity.CRITICAL),
        ("disaster", Severity.CRITICAL),
        ("ok", Severity.OK),
        ("resolved", Severity.OK),
    ],
)
def test_zabbix_table(raw: str, expected: Severity):
    assert ZABBIX_SEVERITY_MAPPING.classify(raw) is expected


@pytest.mark.parametrize("raw", ["HIGH", "  high  ", "High"])
def test_lookup_ignores_case_and_spaces(raw: str):
    """Источники не согласованы в регистре, и это не должно менять класс события."""
    assert ZABBIX_SEVERITY_MAPPING.classify(raw) is Severity.CRITICAL


def test_unknown_severity_falls_back_to_warning():
    """Незнакомое значение — дыра в конфигурации. INFO похоронил бы аварию,
    CRITICAL поднимал бы людей ночью из-за опечатки; WARNING заметен, но не будит."""
    assert ZABBIX_SEVERITY_MAPPING.classify("катастрофа") is Severity.WARNING
    assert ZABBIX_SEVERITY_MAPPING.fallback is Severity.WARNING


def test_fallback_is_configurable_per_source():
    mapping = SeverityMapping(
        source_system="custom",
        table={"panic": Severity.CRITICAL},
        fallback=Severity.INFO,
    )
    assert mapping.classify("panic") is Severity.CRITICAL
    assert mapping.classify("что-то ещё") is Severity.INFO


def test_not_classified_ranks_below_warning():
    """Zabbix ставит «not classified» триггерам, которые ещё никто не разобрал.
    Это информация, а не инцидент."""
    assert ZABBIX_SEVERITY_MAPPING.classify("not classified") < Severity.WARNING


def test_recovery_values_map_to_ok():
    """Именно эти значения запускают автозакрытие проблемы."""
    assert ZABBIX_SEVERITY_MAPPING.classify("ok") is Severity.OK
    assert ZABBIX_SEVERITY_MAPPING.classify("resolved") is Severity.OK
