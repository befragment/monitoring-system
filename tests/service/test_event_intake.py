"""Шаг 2 — приём события."""

from dataclasses import replace
from datetime import timedelta

import pytest

from app.domain.suppression import RateLimit
from app.service.errors import IntakeThrottled, UnknownIntegration
from app.service.event_intake import EventIntakeService

PAYLOAD = {
    "source_system": "zabbix",
    "source_event_id": "8841203",
    "monitor_name": "Свободное место на диске",
    "severity": "high",
    "object": {"name": "db-prod-03", "type": "server"},
    "message": "Диск заполнен на 94%",
}


@pytest.fixture
def intake(integrations, queue, limiter) -> EventIntakeService:
    return EventIntakeService(integrations, queue, limiter)


async def test_event_goes_to_the_queue(intake, queue):
    await intake.accept(PAYLOAD)
    assert await queue.size() == 1


async def test_payload_is_passed_through_unchanged(intake, queue):
    """Разбор полей — работа обработчика: валидация структуры на приёме начнёт
    ронять его на каждом расхождении с версией источника, и событие потеряется
    вместо того, чтобы попасть в разбор."""
    await intake.accept(PAYLOAD)
    assert queue.items[0] == PAYLOAD


async def test_intake_does_not_process(intake, problems, events):
    """П. 2.4: приём и обработка не должны быть одним синхронным вызовом."""
    await intake.accept(PAYLOAD)
    assert problems.rows == {}
    assert events.rows == []


async def test_unknown_source_is_rejected(intake):
    """Неизвестный `source_system` — это либо опечатка в настройке источника,
    либо чужой трафик на эндпоинте. Оба случая надо видеть, а не глотать."""
    with pytest.raises(UnknownIntegration):
        await intake.accept({**PAYLOAD, "source_system": "nagios"})


async def test_disabled_integration_is_rejected(intake, integrations, zabbix):
    integrations.rows["zabbix"] = replace(zabbix, is_enabled=False)
    with pytest.raises(UnknownIntegration):
        await intake.accept(PAYLOAD)


async def test_missing_source_system_is_rejected(intake):
    with pytest.raises(UnknownIntegration):
        await intake.accept({"message": "что-то упало"})


class TestRateLimit:
    """П. 2.5 — лимит числа событий от одного источника."""

    async def test_within_budget_passes(self, intake, integrations, zabbix, queue):
        integrations.rows["zabbix"] = replace(
            zabbix, intake_limit=RateLimit(max_events=3, per=timedelta(minutes=1))
        )
        for _ in range(3):
            await intake.accept(PAYLOAD)
        assert await queue.size() == 3

    async def test_over_budget_is_throttled(self, intake, integrations, zabbix, queue):
        integrations.rows["zabbix"] = replace(
            zabbix, intake_limit=RateLimit(max_events=2, per=timedelta(minutes=1))
        )
        await intake.accept(PAYLOAD)
        await intake.accept(PAYLOAD)
        with pytest.raises(IntakeThrottled):
            await intake.accept(PAYLOAD)
        assert await queue.size() == 2, "лишнее в общий поток не попало"

    async def test_limit_is_counted_per_integration(self, intake, integrations, zabbix, limiter):
        """«Zabbix залил нас» — свойство источника, а не объекта: ключ счётчика
        обязан включать slug, иначе тихий источник накажут за шумный."""
        integrations.rows["prometheus"] = replace(zabbix, slug="prometheus")
        await intake.accept(PAYLOAD)
        await intake.accept({**PAYLOAD, "source_system": "prometheus"})
        assert limiter.counts["intake:zabbix"] == 1
        assert limiter.counts["intake:prometheus"] == 1


class TestBatch:
    """П. 2.6 — пакетная отправка не должна ронять сервис."""

    async def test_all_accepted(self, intake, queue):
        result = await intake.accept_batch([PAYLOAD] * 5)
        assert result.accepted == 5 and result.throttled == 0
        assert await queue.size() == 5

    async def test_throttled_one_does_not_kill_the_rest(
        self, intake, integrations, zabbix, queue
    ):
        """Превышение лимита по одному источнику не должно хоронить весь пакет,
        включая чужие события."""
        integrations.rows["zabbix"] = replace(
            zabbix, intake_limit=RateLimit(max_events=2, per=timedelta(minutes=1))
        )
        result = await intake.accept_batch([PAYLOAD] * 5)
        assert result.accepted == 2 and result.throttled == 3
        assert await queue.size() == 2

    async def test_unknown_source_still_raises(self, intake):
        """А вот неизвестный источник в пакете — не штатная ситуация, и она не
        должна тихо теряться в счётчике throttled."""
        with pytest.raises(UnknownIntegration):
            await intake.accept_batch([{**PAYLOAD, "source_system": "nagios"}])
