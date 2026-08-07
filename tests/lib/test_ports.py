"""Реализации портов, которым не нужна внешняя система.

`EventQueue` и `RateLimiter` сюда не попали намеренно: их поведение — это
поведение Redis (атомарность пайплайна, семантика `RPOP count`, границы
скользящего окна), и подделка клиента проверяла бы мои представления о Redis,
а не сам код. Им нужен интеграционный тест с живым сервером.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.notification import Digest, NotificationMessage, RoutingChannel
from app.domain.role import Role
from app.lib.clock import Clock, FixedClock
from app.lib.notification import (
    ChannelDeliveryError,
    ChannelGateway,
    Notifier,
)
from app.lib.teamchannels import TeamChannelDirectory

T0 = datetime(2026, 8, 7, 2, 14, tzinfo=UTC)


class TestClock:
    def test_system_clock_is_timezone_aware(self):
        """Наивное время недопустимо: значения уходят в `timestamptz` и
        сравниваются со временем событий от источников, которые присылают
        смещение. Смешать наивное с aware — получить TypeError посреди шторма.
        """
        assert Clock().now().tzinfo is not None

    def test_system_clock_is_utc(self):
        assert Clock().now().utcoffset() == timedelta(0)

    def test_fixed_clock_stands_still(self):
        clock = FixedClock(T0)
        assert clock.now() == clock.now() == T0

    def test_fixed_clock_advances_on_demand(self):
        clock = FixedClock(T0)
        clock.advance(timedelta(minutes=15))
        assert clock.now() == T0 + timedelta(minutes=15)


class RecordingNotifier(Notifier):
    def __init__(self) -> None:
        self.sent: list[tuple] = []

    async def notify(self, address, message):
        self.sent.append((address, message))

    async def notify_digest(self, address, digest):
        self.sent.append((address, digest))


class BrokenNotifier(Notifier):
    async def notify(self, address, message):
        raise ChannelDeliveryError("smtp: connection refused")


@pytest.fixture
def message() -> NotificationMessage:
    return NotificationMessage(
        subject="[CRITICAL] db-prod-03 — диск",
        text="Диск заполнен на 94%",
        priority=0,
        monitor_name="диск",
        state="CRITICAL",
        prev_state="OK",
        object_id="db-prod-03",
        resolved=False,
        owner=None,
        created_at=T0,
        count=1,
    )


class TestChannelGateway:
    async def test_dispatches_to_the_right_notifier(self, message):
        mail, telegram = RecordingNotifier(), RecordingNotifier()
        gateway = ChannelGateway(
            {RoutingChannel.MAIL: mail, RoutingChannel.TELEGRAM: telegram}
        )
        await gateway.send(RoutingChannel.TELEGRAM, "tg:111", message)
        assert telegram.sent and not mail.sent

    async def test_unconfigured_channel_is_an_error_not_a_silent_skip(self, message):
        """Подписка на канал без адаптера обязана попасть в отчёт о недоставке,
        иначе человек считает, что подписан, и не получает ничего."""
        gateway = ChannelGateway({})
        with pytest.raises(ChannelDeliveryError) as exc:
            await gateway.send(RoutingChannel.TRUECONF, "@bot", message)
        assert "trueconf" in str(exc.value)

    async def test_adapter_failure_propagates(self, message):
        gateway = ChannelGateway({RoutingChannel.MAIL: BrokenNotifier()})
        with pytest.raises(ChannelDeliveryError):
            await gateway.send(RoutingChannel.MAIL, "a@b.ru", message)

    async def test_digest_goes_through_the_same_dispatch(self):
        mail = RecordingNotifier()
        gateway = ChannelGateway({RoutingChannel.MAIL: mail})
        digest = Digest(
            subject="Шторм", text="47 проблем", problem_ids=(), suppressed_count=46,
            created_at=T0,
        )
        await gateway.send_digest(RoutingChannel.MAIL, "a@b.ru", digest)
        assert mail.sent[0][1] is digest


class TestTeamChannelDirectory:
    def test_known_role_resolves(self):
        directory = TeamChannelDirectory(
            {Role.L2: (RoutingChannel.MATTERMOST, "#l2-duty")}
        )
        assert directory.channel_for(Role.L2) == (RoutingChannel.MATTERMOST, "#l2-duty")

    def test_unknown_role_returns_none(self):
        """None означает «не настроено», и сервис эскалации обязан это заметить:
        ненастроенный канал молча обрывает лестницу."""
        assert TeamChannelDirectory({}).channel_for(Role.L1) is None

    def test_parses_flat_pairs(self):
        """Переменные окружения плоские, а JSON в `.env` писать невыносимо — та
        же причина, по которой `cors_origins` лежит строкой через запятую."""
        directory = TeamChannelDirectory.from_pairs(
            {"l1": "mattermost:#l1-duty", "l2": "telegram:-100500"}
        )
        assert directory.channel_for(Role.L1) == (RoutingChannel.MATTERMOST, "#l1-duty")
        assert directory.channel_for(Role.L2) == (RoutingChannel.TELEGRAM, "-100500")

    def test_address_may_contain_colons(self):
        directory = TeamChannelDirectory.from_pairs(
            {"l1": "webhook:https://hooks.example.ru/x"}
        )
        assert directory.channel_for(Role.L1)[1] == "https://hooks.example.ru/x"

    @pytest.mark.parametrize("bad", ["mattermost", "", "#l1-duty"])
    def test_malformed_pair_is_rejected_loudly(self, bad):
        """Опечатка в конфигурации обязана падать при старте, а не оборачиваться
        молчащей лестницей эскалации в три часа ночи."""
        with pytest.raises(ValueError):
            TeamChannelDirectory.from_pairs({"l1": bad})

    def test_unknown_role_name_is_rejected(self):
        with pytest.raises(ValueError):
            TeamChannelDirectory.from_pairs({"l3": "mattermost:#x"})
