"""Каналы доставки (п. 5.2–5.3 ТЗ).

Два уровня. `Notifier` — адаптер одного канала, знает про свой транспорт и
больше ни про что. `ChannelGateway` — единственная точка, куда обращается
сервис доставки: он не должен ветвиться по каналу, иначе почта и TrueConf
начнут расходиться в логике маршрутизации, а не только в способе отправки.
"""

from abc import ABC, abstractmethod
from typing import Protocol

from app.domain.notification import Digest, NotificationMessage, RoutingChannel


class ChannelDeliveryError(Exception):
    """Доставить не удалось.

    Текст попадает в `Delivery.failure_reason` и показывается в ответе на
    «почему мне не пришло», поэтому обязан быть пригодным для чтения человеком:
    «telegram: 429 too many requests», а не трейсбек.

    Живёт здесь, а не в `service/errors.py`, потому что бросают её адаптеры
    каналов: ошибка принадлежит порту, а не тому, кто его вызывает.
    """


class Notifier(ABC):
    """Адаптер одного канала доставки.

    Реализации отличаются не только транспортом, но и моделью работы: почта и
    вебхук — это «отправил и забыл», а TrueConf требует постоянно живущего
    процесса-бота на WebSocket (п. 5.3.3 ТЗ), поэтому его адаптер держит
    соединение, а не открывает его на каждое сообщение.
    """

    @abstractmethod
    async def notify(self, address: str, message: NotificationMessage) -> None:
        """Бросает `ChannelDeliveryError`, если доставить не удалось."""
        raise NotImplementedError("implement notify() in child class")

    async def notify_digest(self, address: str, digest: Digest) -> None:
        """Сводка при шторме (п. 4.6).

        Реализация по умолчанию есть, потому что для большинства каналов сводка
        — это обычное сообщение с другим текстом. Переопределяют её там, где у
        канала есть своя форма для списка (тред в Mattermost, вложение в почте).
        """
        raise NotImplementedError("implement notify_digest() to support digests")


class TrueConfNotifier(Notifier):
    pass


class TelegramNotifier(Notifier):
    pass


class PhoneNotifier(Notifier):
    pass


class EmailNotifier(Notifier):
    pass


class ChannelGatewayInterface(Protocol):
    """Одна точка отправки вместо метода на канал.

    `RoutingChannel` уже перечисляет варианты, и ветвление по нему принадлежит
    шлюзу, а не сервису доставки, который обязан оставаться одинаковым для
    почты и TrueConf.
    """

    async def send(
        self, channel: RoutingChannel, address: str, message: NotificationMessage
    ) -> None: ...

    async def send_digest(
        self, channel: RoutingChannel, address: str, digest: Digest
    ) -> None: ...


class ChannelGateway:
    """Раскладывает отправку по адаптерам.

    Ненастроенный канал — это `ChannelDeliveryError`, а не тихий пропуск:
    подписка на канал, для которого забыли поднять адаптер, обязана попасть в
    отчёт о недоставке, иначе человек будет считать, что подписан, и не получит
    ничего.
    """

    def __init__(self, notifiers: dict[RoutingChannel, Notifier]) -> None:
        self._notifiers = notifiers

    async def send(
        self, channel: RoutingChannel, address: str, message: NotificationMessage
    ) -> None:
        await self._for(channel).notify(address, message)

    async def send_digest(
        self, channel: RoutingChannel, address: str, digest: Digest
    ) -> None:
        await self._for(channel).notify_digest(address, digest)

    def _for(self, channel: RoutingChannel) -> Notifier:
        notifier = self._notifiers.get(channel)
        if notifier is None:
            raise ChannelDeliveryError(f"channel {channel.value} has no configured notifier")
        return notifier
