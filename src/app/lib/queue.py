"""Очередь входящих событий (п. 2.4 ТЗ).

Приём кладёт событие сюда и сразу отвечает источнику; обработка идёт отдельным
воркером. Это прямое требование ТЗ, и оно же — то, что позволяет пережить пик из
п. 2.3: всплеск упирается в длину списка в Redis, а не в число одновременных
транзакций к постгресу.

Клиент Redis передаётся аргументом, а не берётся из `lib.redis`: модуль там
поднимает пул соединений на импорте, и сервис, которому нужен только протокол,
не должен этого тянуть.
"""

import json
from collections.abc import Mapping
from typing import Any, Protocol

from redis.asyncio import Redis

DEFAULT_STREAM = "events:incoming"


class EventQueueInterface(Protocol):
    """Payload переносится словарём, а не доменным `IncomingEvent`.

    Между приёмом и обработкой лежит сериализация, и делать вид, что объект
    переживает её неизменным, значит врать в типах: разбор полей — работа
    обработчика, и падать он должен там, где есть контекст для разбора.
    """

    async def publish(self, payload: Mapping[str, Any]) -> None: ...

    async def consume(self, *, limit: int = 100) -> list[Mapping[str, Any]]: ...

    async def size(self) -> int: ...


class EventQueue:
    """Список в Redis: `LPUSH` на приёме, `RPOP` пачкой на обработке.

    Список, а не Pub/Sub: подписчик Pub/Sub теряет всё, что пришло, пока он был
    отключён, — то есть ровно во время перезапуска воркера события исчезали бы
    бесследно. Список переживает и перезапуск, и отсутствие читателя.
    """

    def __init__(self, client: Redis, stream: str = DEFAULT_STREAM) -> None:
        self._client = client
        self._stream = stream

    async def publish(self, payload: Mapping[str, Any]) -> None:
        await self._client.lpush(self._stream, json.dumps(payload, ensure_ascii=False))

    async def consume(self, *, limit: int = 100) -> list[Mapping[str, Any]]:
        raw = await self._client.rpop(self._stream, count=limit)
        if not raw:
            return []
        if isinstance(raw, str | bytes):
            raw = [raw]

        payloads: list[Mapping[str, Any]] = []
        for item in raw:
            try:
                payloads.append(json.loads(item))
            except json.JSONDecodeError:
                # Битую запись пропускаем, а не роняем весь батч: одно
                # испорченное сообщение не должно останавливать обработку
                # остальных. Вызывающий увидит расхождение в счётчиках.
                continue
        return payloads

    async def size(self) -> int:
        """Глубина очереди. Растущая при живом приёме означает, что воркеров не
        хватает, — и это видно раньше, чем начнут опаздывать оповещения."""
        return int(await self._client.llen(self._stream))
