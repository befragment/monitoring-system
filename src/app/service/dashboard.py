"""П. 7.3 — дашборд администратора системы оповещений.

Считает по `Delivery`, а не по отправкам «на лету»: ради этого сущность и
заведена. Ключевая тонкость всего раздела — подавленные сообщения не входят в
недоставленные. Слив их в одну корзину, панель показывала бы деградацию ровно
там, где платформа отработала как задумано: во время объявленного регламента.
"""

from dataclasses import dataclass
from datetime import timedelta

from app.domain.notification import DeliveryStatus
from app.lib.clock import ClockInterface
from app.lib.queue import EventQueueInterface
from app.repository._contracts import (
    DeliveryRepository,
    EventRepository,
    ProblemRepository,
)


@dataclass(frozen=True, slots=True)
class DeliveryStats:
    delivered: int
    failed: int
    suppressed: int
    pending: int

    @property
    def attempted(self) -> int:
        """Знаменатель доли успеха — без подавленных: их не пытались вручить."""
        return self.delivered + self.failed

    @property
    def success_rate(self) -> float:
        """Доля успешно доставленных. При нуле попыток — 1.0, а не деление на
        ноль: «ничего не отправляли» это не «всё потеряли»."""
        return 1.0 if self.attempted == 0 else self.delivered / self.attempted


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    events_processed: int
    open_problems: int
    queue_depth: int
    deliveries: DeliveryStats
    window: timedelta


class DashboardService:
    def __init__(
        self,
        events: EventRepository,
        problems: ProblemRepository,
        deliveries: DeliveryRepository,
        queue: EventQueueInterface,
        clock: ClockInterface,
    ) -> None:
        self._events = events
        self._problems = problems
        self._deliveries = deliveries
        self._queue = queue
        self._clock = clock

    async def snapshot(self, window: timedelta = timedelta(days=1)) -> DashboardSnapshot:
        since = self._clock.now() - window
        counts = await self._deliveries.count_by_status_since(since)

        return DashboardSnapshot(
            events_processed=await self._events.count_since(since),
            open_problems=await self._problems.count_open(),
            # Глубина очереди — показатель того, справляется ли обработка.
            # Растущая очередь при живом приёме означает, что воркеров не хватает,
            # и это видно раньше, чем начнут опаздывать оповещения.
            queue_depth=await self._queue.size(),
            deliveries=DeliveryStats(
                delivered=counts.get(DeliveryStatus.DELIVERED, 0),
                failed=counts.get(DeliveryStatus.FAILED, 0),
                suppressed=counts.get(DeliveryStatus.SUPPRESSED, 0),
                pending=counts.get(DeliveryStatus.PENDING, 0),
            ),
            window=window,
        )
