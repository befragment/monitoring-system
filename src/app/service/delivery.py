"""Отправка того, что решила маршрутизация.

Отдельный воркер, а не продолжение обработки события: доставка ходит по сети к
чужим сервисам, и её отказ не должен откатывать транзакцию, в которой уже
зафиксирована проблема. Записи `Delivery` в статусе PENDING — очередь на
отправку, то есть outbox.
"""

from dataclasses import dataclass

from app.domain.notification import Delivery, DeliveryStatus, NotificationMessage
from app.domain.problem import Problem
from app.lib.clock import ClockInterface
from app.lib.notification import ChannelDeliveryError, ChannelGatewayInterface
from app.repository._contracts import (
    DeliveryRepository,
    ProblemRepository,
    UnitOfWork,
    UserRepository,
)

# Сколько раз пытаемся вручить одно сообщение. Предел нужен: без него упавший
# канал будет вечно перемалывать одну и ту же запись, вытесняя из очереди свежие
# алерты, — то есть один сломанный вебхук глушит всю доставку.
MAX_ATTEMPTS = 5


@dataclass(frozen=True, slots=True)
class DispatchReport:
    delivered: int
    failed: int
    skipped: int


class DeliveryService:
    def __init__(
        self,
        deliveries: DeliveryRepository,
        problems: ProblemRepository,
        users: UserRepository,
        gateway: ChannelGatewayInterface,
        clock: ClockInterface,
        uow: UnitOfWork,
    ) -> None:
        self._deliveries = deliveries
        self._problems = problems
        self._users = users
        self._gateway = gateway
        self._clock = clock
        self._uow = uow

    async def dispatch_pending(self, *, limit: int = 50) -> DispatchReport:
        """Один проход воркера доставки."""
        batch = await self._deliveries.claim_pending(limit=limit)
        delivered = failed = skipped = 0

        for delivery in batch:
            problem = await self._problems.get(delivery.problem_id)
            if problem is None:
                delivery.mark_failed("problem is gone", at=self._clock.now())
                await self._deliveries.save(delivery)
                failed += 1
                continue

            if self._is_stale(delivery, problem):
                # Проблему уже закрыли, пока сообщение лежало в очереди. Слать
                # алерт про починенное — это шум, который учит игнорировать
                # канал; фиксируем причину и идём дальше.
                delivery.mark_failed("skipped: problem already closed", at=self._clock.now())
                await self._deliveries.save(delivery)
                skipped += 1
                continue

            owner = await self._owner_name(problem)
            message = NotificationMessage.from_problem(problem, owner=owner)

            try:
                await self._gateway.send(delivery.channel, delivery.address, message)
            except ChannelDeliveryError as exc:
                delivery.mark_failed(str(exc), at=self._clock.now())
                failed += 1
            else:
                delivery.mark_delivered(at=self._clock.now())
                delivered += 1
            await self._deliveries.save(delivery)

        await self._uow.commit()
        return DispatchReport(delivered=delivered, failed=failed, skipped=skipped)

    async def retry(self, delivery: Delivery) -> bool:
        """Вернуть неудачную доставку в очередь, если попытки не исчерпаны.

        Возвращает False, когда предел достигнут: тогда запись остаётся в
        FAILED с причиной, и её видно в отчёте п. 7.3, а не теряется молча.
        """
        if delivery.attempts >= MAX_ATTEMPTS:
            return False
        delivery.status = DeliveryStatus.PENDING
        delivery.settled_at = None
        await self._deliveries.save(delivery)
        await self._uow.commit()
        return True

    def _is_stale(self, delivery: Delivery, problem: Problem) -> bool:
        """Актуально ли ещё сообщение.

        Сообщение о закрытии слать нужно — именно оно снимает тревогу; поэтому
        устаревшими считаются только те, что были поставлены в очередь *до*
        закрытия.
        """
        if problem.is_open or problem.closed_at is None:
            return False
        return delivery.created_at < problem.closed_at

    async def _owner_name(self, problem: Problem) -> str | None:
        """Домен хранит идентификатор, сообщению нужно отображаемое имя —
        сопоставлять их работа сервисного слоя, а не агрегата."""
        if problem.owner_id is None:
            return None
        owner = await self._users.get_by_id(problem.owner_id)
        return owner.full_name if owner else None
