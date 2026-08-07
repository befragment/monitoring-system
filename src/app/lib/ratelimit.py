"""Счётчики скользящего окна для лимитов приёма (п. 2.5) и доставки (п. 4.6).

Разделение обязанностей с доменом: здесь считают «сколько», а решает «много ли
это» доменный `RateLimit.exceeded`. Поэтому пороги в этот модуль не попадают —
он не знает ни про источники, ни про получателей.

Состояние живёт в Redis, а не в объекте: скользящее окно в памяти процесса не
переживает перезапуск и не разделяется между воркерами, то есть при двух
экземплярах приёма лимит стал бы вдвое мягче, чем написано в настройках.
"""

import time
import uuid
from datetime import timedelta
from typing import Protocol

from redis.asyncio import Redis


class RateLimiterInterface(Protocol):
    async def hit(self, key: str, window: timedelta) -> int:
        """Отметить событие и вернуть их число в окне, включая текущее."""
        ...

    async def count(self, key: str, window: timedelta) -> int:
        """Посмотреть, не отмечая."""
        ...


class RateLimiter:
    """Скользящее окно на sorted set: метки времени как score.

    Не «счётчик с TTL»: тот сбрасывается скачком, и на границе окна пропускает
    двойную норму — сто событий в последнюю секунду старого окна плюс сто в
    первую секунду нового. Для защиты от шторма это худший момент, чтобы
    ошибиться вдвое.
    """

    def __init__(self, client: Redis, prefix: str = "ratelimit") -> None:
        self._client = client
        self._prefix = prefix

    async def hit(self, key: str, window: timedelta) -> int:
        now = time.time()
        cutoff = now - window.total_seconds()
        redis_key = self._key(key)

        pipe = self._client.pipeline()
        pipe.zremrangebyscore(redis_key, 0, cutoff)
        # Член множества обязан быть уникальным, иначе два события в одну
        # миллисекунду схлопнутся в одну запись и лимит окажется мягче.
        pipe.zadd(redis_key, {uuid.uuid4().hex: now})
        pipe.zcard(redis_key)
        # Ключ живёт чуть дольше окна: без TTL молчащие источники навсегда
        # оставляли бы в Redis мусор.
        pipe.expire(redis_key, int(window.total_seconds()) + 60)
        _, _, total, _ = await pipe.execute()
        return int(total)

    async def count(self, key: str, window: timedelta) -> int:
        now = time.time()
        return int(
            await self._client.zcount(self._key(key), now - window.total_seconds(), now)
        )

    def _key(self, key: str) -> str:
        return f"{self._prefix}:{key}"
