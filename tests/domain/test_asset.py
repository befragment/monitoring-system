"""Объект мониторинга и каноническая форма его имени."""

import pytest

from app.domain.asset import Asset, normalize


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("DB-PROD-03", "db-prod-03"),
        ("  db-prod-03  ", "db-prod-03"),
        ("Скважина-12", "скважина-12"),
    ],
)
def test_normalize(raw: str, expected: str):
    """Одна нормализация на весь домен: fingerprint, окно обслуживания и поиск
    оператора обязаны сходиться в том, что «DB-PROD-03» и «db-prod-03» — одно."""
    assert normalize(raw) == expected


def test_key_uses_normalized_name():
    assert Asset(name="  DB-PROD-03 ", type="server").key == "db-prod-03"


def test_asset_is_immutable_value_object():
    """У объекта нет собственного жизненного цикла: платформа его не заводит,
    а узнаёт о нём из событий."""
    asset = Asset(name="db-prod-03", type="server")
    with pytest.raises((AttributeError, TypeError)):
        asset.name = "другой"  # type: ignore[misc]


def test_equality_is_by_value():
    assert Asset("db-prod-03", "server") == Asset("db-prod-03", "server")
    assert Asset("db-prod-03", "server") != Asset("db-prod-03", "vm")
