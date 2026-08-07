"""Шаг 2 — приём события.

Единственный синхронный участок пути. Здесь сознательно не делается ничего,
кроме проверки источника и постановки в очередь: п. 2.4 ТЗ требует, чтобы приём
и обработка не были одним вызовом, а критерий п. 2.6 — чтобы пакет в сотни
событий не ронял сервис и не съедал ответ на запрос.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.lib.queue import EventQueueInterface
from app.lib.ratelimit import RateLimiterInterface
from app.repository._contracts import IntegrationRepository
from app.service.errors import IntakeThrottled, UnknownIntegration


@dataclass(frozen=True, slots=True)
class IntakeResult:
    accepted: int
    throttled: int


class EventIntakeService:
    def __init__(
        self,
        integrations: IntegrationRepository,
        queue: EventQueueInterface,
        limiter: RateLimiterInterface,
    ) -> None:
        self._integrations = integrations
        self._queue = queue
        self._limiter = limiter

    async def accept(self, payload: Mapping[str, Any]) -> None:
        """Принять одно событие в формате п. 2.1.

        Payload проверяется на принадлежность известному источнику и уходит в
        очередь как есть. Разбор полей — работа обработчика: если валидировать
        структуру здесь, то приём начнёт падать на каждом расхождении с версией
        источника, а событие потеряется вместо того, чтобы попасть в разбор.
        """
        slug = str(payload.get("source_system", "")).strip()
        integration = await self._integrations.get_by_slug(slug)
        if integration is None or not integration.is_enabled:
            raise UnknownIntegration(f"unknown or disabled source system: {slug!r}")

        limit = integration.intake_limit
        observed = await self._limiter.hit(f"intake:{integration.slug}", limit.per)
        if limit.exceeded(observed):
            # П. 2.5: избыток уходит в отложенную обработку, а не отбрасывается.
            # Наружу это не должно превращаться в отказ — источник начнёт
            # ретраить и усилит ровно тот шторм, от которого мы защищаемся.
            raise IntakeThrottled(
                f"{integration.slug}: {observed} events in {limit.per}, limit is {limit.max_events}"
            )

        await self._queue.publish(payload)

    async def accept_batch(self, payloads: Sequence[Mapping[str, Any]]) -> IntakeResult:
        """Пакетный приём: источники при каскадном сбое шлют пачками.

        Одно отклонённое событие не отменяет остальные — иначе превышение лимита
        по одному источнику похоронило бы весь пакет, включая чужие события.
        """
        accepted = throttled = 0
        for payload in payloads:
            try:
                await self.accept(payload)
                accepted += 1
            except IntakeThrottled:
                throttled += 1
        return IntakeResult(accepted=accepted, throttled=throttled)
