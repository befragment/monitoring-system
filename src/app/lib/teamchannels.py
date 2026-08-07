"""Куда писать команде целиком.

Ступени лестницы эскалации адресованы роли, а не человеку, — значит им нужен
адрес общего канала. Это конфигурация развёртывания (id чата в Mattermost,
адрес рассылки), а не доменное знание, поэтому справочник отдельный: домен
рассуждает про «канал L2», а какой это чат — вопрос конкретной установки.

Когда появится таблица дежурств (п. 5.5), подменяться будет именно эта штука:
вместо адреса канала она начнёт отдавать адрес человека, дежурящего сейчас.
"""

from collections.abc import Mapping
from typing import Protocol

from app.domain.notification import RoutingChannel
from app.domain.role import Role


class TeamChannelDirectoryInterface(Protocol):
    def channel_for(self, role: Role) -> tuple[RoutingChannel, str] | None:
        """None означает «канал не настроен».

        Сервис эскалации обязан это заметить и посчитать отдельно: ненастроенный
        канал молча обрывает лестницу, и такая дыра должна быть видна в
        метриках, а не только в журнале.
        """
        ...


class TeamChannelDirectory:
    """Статическая карта из настроек развёртывания."""

    def __init__(self, channels: Mapping[Role, tuple[RoutingChannel, str]]) -> None:
        self._channels = dict(channels)

    def channel_for(self, role: Role) -> tuple[RoutingChannel, str] | None:
        return self._channels.get(role)

    @classmethod
    def from_pairs(cls, pairs: Mapping[str, str]) -> "TeamChannelDirectory":
        """Собрать из плоских строк вида {"l1": "mattermost:#l1-duty"}.

        Отдельный конструктор нужен, потому что переменные окружения плоские, а
        держать в `.env` JSON невыносимо — та же причина, по которой
        `cors_origins` в настройках лежит строкой через запятую.
        """
        parsed: dict[Role, tuple[RoutingChannel, str]] = {}
        for raw_role, destination in pairs.items():
            channel, _, address = destination.partition(":")
            if not address:
                raise ValueError(
                    f"team channel for {raw_role!r} must look like 'mattermost:#l1-duty'"
                )
            parsed[Role(raw_role)] = (RoutingChannel(channel), address)
        return cls(parsed)
