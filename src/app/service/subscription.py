"""П. 5.6 — личный кабинет: человек сам управляет своими подписками.

Ключевое ограничение сценария: подписка принадлежит пользователю, и трогать
чужую нельзя никому, включая администратора. Админ заводит людей и источники,
но не решает за инженера, в какой канал тому удобнее получать алерты, — иначе
самообслуживание, которого требует ТЗ, превращается в заявку администратору.
"""

import uuid
from collections.abc import Sequence

from app.domain.audit import AuditAction, AuditRecord
from app.domain.notification import RoutingChannel
from app.domain.severity import Severity
from app.domain.user import Subscription
from app.repository._contracts import (
    AuditRepository,
    SubscriptionRepository,
    UnitOfWork,
    UserRepository,
)
from app.service.errors import NotFound, PermissionDenied


class SubscriptionService:
    def __init__(
        self,
        subscriptions: SubscriptionRepository,
        users: UserRepository,
        audit: AuditRepository,
        uow: UnitOfWork,
    ) -> None:
        self._subscriptions = subscriptions
        self._users = users
        self._audit = audit
        self._uow = uow

    async def list_mine(self, user_id: uuid.UUID) -> list[Subscription]:
        return await self._subscriptions.list_for_user(user_id)

    async def create(
        self,
        user_id: uuid.UUID,
        *,
        channel: RoutingChannel,
        address: str,
        min_severity: Severity = Severity.WARNING,
        tags: Sequence[str] = (),
    ) -> Subscription:
        user = await self._users.get_by_id(user_id)
        if user is None:
            raise NotFound(f"user {user_id} not found")

        subscription = Subscription(
            id=uuid.uuid4(),
            user_id=user_id,
            channel=channel,
            address=address.strip(),
            min_severity=min_severity,
            tags=frozenset(tags),
        )
        await self._subscriptions.add(subscription)
        await self._journal(user_id, subscription, "created")
        await self._uow.commit()
        return subscription

    async def update(
        self,
        subscription_id: uuid.UUID,
        actor_id: uuid.UUID,
        *,
        address: str | None = None,
        min_severity: Severity | None = None,
        tags: Sequence[str] | None = None,
        is_enabled: bool | None = None,
    ) -> Subscription:
        subscription = await self._own(subscription_id, actor_id)

        if address is not None:
            subscription.address = address.strip()
        if min_severity is not None:
            subscription.min_severity = min_severity
        if tags is not None:
            subscription.tags = frozenset(tags)
        if is_enabled is not None:
            subscription.is_enabled = is_enabled

        await self._subscriptions.save(subscription)
        await self._journal(actor_id, subscription, "updated")
        await self._uow.commit()
        return subscription

    async def delete(self, subscription_id: uuid.UUID, actor_id: uuid.UUID) -> None:
        subscription = await self._own(subscription_id, actor_id)
        await self._subscriptions.delete(subscription.id)
        await self._journal(actor_id, subscription, "deleted")
        await self._uow.commit()

    async def mute(self, subscription_id: uuid.UUID, actor_id: uuid.UUID) -> Subscription:
        """Отдельный сценарий, а не удаление: заглушить шумный канал на время и
        вернуть обратно, не набирая адрес заново."""
        return await self.update(subscription_id, actor_id, is_enabled=False)

    async def _own(self, subscription_id: uuid.UUID, actor_id: uuid.UUID) -> Subscription:
        subscription = await self._subscriptions.get(subscription_id)
        if subscription is None:
            raise NotFound(f"subscription {subscription_id} not found")
        if subscription.user_id != actor_id:
            # Сообщение намеренно не уточняет, что подписка существует, но чужая:
            # иначе перебором id можно составить карту чужих настроек.
            raise PermissionDenied("subscription belongs to another user")
        return subscription

    async def _journal(
        self, actor_id: uuid.UUID, subscription: Subscription, what: str
    ) -> None:
        await self._audit.add(
            AuditRecord.create(
                AuditAction.SUBSCRIPTION_CHANGED,
                actor_id=actor_id,
                details={
                    "subscription": str(subscription.id),
                    "channel": subscription.channel.value,
                    "action": what,
                },
            )
        )
