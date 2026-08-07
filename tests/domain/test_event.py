"""Шаги 2–4 — идентичность события и граница классификации."""

from dataclasses import replace
from datetime import timedelta

import pytest

from app.domain.asset import Asset
from app.domain.event import Event, Fingerprint, IncomingEvent
from app.domain.severity import Severity


class TestFingerprint:
    """П. 4.1 — то, что решает, одна это проблема или две."""

    def test_built_from_three_parts(self, incoming: IncomingEvent, zabbix):
        fp = incoming.classify(zabbix.severity_mapping).fingerprint
        assert fp == Fingerprint.build(
            "zabbix", "db-prod-03", "Свободное место на диске"
        )

    @pytest.mark.parametrize(
        ("source", "asset_name", "monitor"),
        [
            ("ZABBIX", "db-prod-03", "Свободное место на диске"),
            ("zabbix", "DB-PROD-03", "Свободное место на диске"),
            (" zabbix ", "  db-prod-03  ", " Свободное место на диске "),
        ],
    )
    def test_case_and_spaces_do_not_split_identity(self, source, asset_name, monitor):
        """Источники расходятся в написании хостнейма; это не должно
        расщеплять одну проблему на две."""
        assert Fingerprint.build(source, asset_name, monitor) == Fingerprint.build(
            "zabbix", "db-prod-03", "Свободное место на диске"
        )

    @pytest.mark.parametrize(
        ("source", "asset_name", "monitor"),
        [
            ("prometheus", "db-prod-03", "Свободное место на диске"),
            ("zabbix", "db-prod-04", "Свободное место на диске"),
            ("zabbix", "db-prod-03", "Сервис недоступен"),
        ],
    )
    def test_each_part_matters(self, source, asset_name, monitor):
        """Ни одно слагаемое не лишнее: две системы мониторинга, два хоста и две
        разные поломки одного хоста — это разные проблемы."""
        assert Fingerprint.build(source, asset_name, monitor) != Fingerprint.build(
            "zabbix", "db-prod-03", "Свободное место на диске"
        )

    def test_message_is_not_part_of_identity(self, incoming: IncomingEvent, zabbix):
        """«Диск заполнен на 94%» через минуту станет 95%. Включение текста в
        fingerprint означало бы отсутствие дедупликации."""
        other = replace(incoming, message="Диск заполнен на 95%")
        assert (
            other.classify(zabbix.severity_mapping).fingerprint
            == incoming.classify(zabbix.severity_mapping).fingerprint
        )

    def test_source_event_id_is_not_part_of_identity(self, incoming, zabbix):
        """Zabbix выдаёт новый id события на каждое срабатывание."""
        other = replace(incoming, source_event_id="8841999")
        assert (
            other.classify(zabbix.severity_mapping).fingerprint
            == incoming.classify(zabbix.severity_mapping).fingerprint
        )

    def test_severity_is_not_part_of_identity(self, incoming, zabbix):
        """П. 4.3: пока объект не вернулся в норму, проблема остаётся одной и
        той же — меняется только её критичность."""
        other = replace(incoming, raw_severity="average")
        assert (
            other.classify(zabbix.severity_mapping).fingerprint
            == incoming.classify(zabbix.severity_mapping).fingerprint
        )

    def test_has_fixed_width(self):
        """Фиксированная ширина важна: по этому значению идёт индексный поиск на
        каждом входящем событии (п. 2.3 — до 50 событий/сек на пике)."""
        short = Fingerprint.build("a", "b", "c")
        long = Fingerprint.build("zabbix", "очень-длинное-имя-хоста" * 20, "монитор")
        assert len(short.value) == len(long.value) == 64


class TestClassification:
    """Шаг 3 — граница между сырым payload'ом и записью журнала."""

    def test_classify_assigns_platform_severity(self, incoming, zabbix):
        assert incoming.classify(zabbix.severity_mapping).severity is Severity.CRITICAL

    def test_classify_keeps_raw_severity(self, incoming, zabbix):
        """Исходное значение сохраняется: без него не разобрать, почему
        событие получило именно такой класс."""
        assert incoming.classify(zabbix.severity_mapping).raw_severity == "high"

    def test_classify_gives_each_event_its_own_id(self, incoming, zabbix):
        first = incoming.classify(zabbix.severity_mapping)
        second = incoming.classify(zabbix.severity_mapping)
        assert first.id != second.id

    def test_classify_carries_payload_through(self, incoming, zabbix):
        event = incoming.classify(zabbix.severity_mapping)
        assert event.source_system == incoming.source_system
        assert event.asset == incoming.asset
        assert event.message == incoming.message
        assert event.tags == incoming.tags
        assert event.occurred_at == incoming.occurred_at
        assert event.received_at == incoming.received_at


class TestEvent:
    def test_is_immutable(self, event: Event):
        """П. 4.3: событие — неизменяемая запись. Исправленное событие это
        новое событие, а не правка старого."""
        with pytest.raises((AttributeError, TypeError)):
            event.message = "подправили"  # type: ignore[misc]

    def test_recovery_is_detected_by_ok_severity(self, incoming, zabbix):
        recovery = replace(incoming, raw_severity="resolved").classify(
            zabbix.severity_mapping
        )
        assert recovery.is_recovery is True

    def test_normal_event_is_not_recovery(self, event: Event):
        assert event.is_recovery is False

    def test_occurred_and_received_are_kept_apart(self, incoming, zabbix, t0):
        """Расходятся при аварии самой интеграции: события копятся у неё и
        приезжают пачкой с опозданием."""
        delayed = replace(incoming, received_at=t0 + timedelta(hours=3))
        event = delayed.classify(zabbix.severity_mapping)
        assert event.occurred_at == t0
        assert event.received_at == t0 + timedelta(hours=3)


def test_incoming_event_requires_monitor_name():
    """Расхождение с ТЗ, зафиксированное осознанно: п. 2.1 поля не перечисляет,
    но без него не построить fingerprint из п. 4.1."""
    with pytest.raises(TypeError):
        IncomingEvent(  # type: ignore[call-arg]
            source_system="zabbix",
            source_event_id="1",
            raw_severity="high",
            asset=Asset("db-prod-03", "server"),
            message="упало",
            occurred_at=None,
            received_at=None,
        )
