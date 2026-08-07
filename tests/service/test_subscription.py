"""П. 5.6 — личный кабинет: человек управляет своими подписками сам."""

import uuid

import pytest

from app.domain.notification import RoutingChannel
from app.domain.severity import Severity
from app.service.errors import NotFound, PermissionDenied
from app.service.subscription import SubscriptionService


@pytest.fixture
def service(subscriptions, users, audit, uow) -> SubscriptionService:
    return SubscriptionService(subscriptions, users, audit, uow)


class TestSelfService:
    async def test_create(self, service, l1, subscriptions):
        created = await service.create(
            l1.id,
            channel=RoutingChannel.MATTERMOST,
            address="@ivanov",
            min_severity=Severity.MAJOR,
            tags=["prod"],
        )
        assert created.id in subscriptions.rows
        assert created.tags == frozenset({"prod"})

    async def test_created_subscription_is_enabled(self, service, l1):
        created = await service.create(
            l1.id, channel=RoutingChannel.MAIL, address="a@b.ru"
        )
        assert created.is_enabled is True

    async def test_address_is_trimmed(self, service, l1):
        created = await service.create(
            l1.id, channel=RoutingChannel.MAIL, address="  a@b.ru  "
        )
        assert created.address == "a@b.ru"

    async def test_default_threshold_is_warning(self, service, l1):
        created = await service.create(
            l1.id, channel=RoutingChannel.MAIL, address="a@b.ru"
        )
        assert created.min_severity is Severity.WARNING

    async def test_list_mine(self, service, l1, sub_l1):
        assert [s.id for s in await service.list_mine(l1.id)] == [sub_l1.id]

    async def test_list_mine_is_isolated(self, service, l1, sub_l2):
        assert sub_l2.id not in {s.id for s in await service.list_mine(l1.id)}

    async def test_unknown_user(self, service):
        with pytest.raises(NotFound):
            await service.create(
                uuid.uuid4(), channel=RoutingChannel.MAIL, address="a@b.ru"
            )


class TestUpdate:
    async def test_change_threshold_and_tags(self, service, l1, sub_l1):
        updated = await service.update(
            sub_l1.id, l1.id, min_severity=Severity.CRITICAL, tags=["msk-dc1"]
        )
        assert updated.min_severity is Severity.CRITICAL
        assert updated.tags == frozenset({"msk-dc1"})

    async def test_mute_keeps_the_address(self, service, l1, sub_l1):
        """Отдельный сценарий, а не удаление: заглушить шумный канал на время и
        вернуть обратно, не набирая адрес заново."""
        muted = await service.mute(sub_l1.id, l1.id)
        assert muted.is_enabled is False
        assert muted.address == "tg:111"

    async def test_partial_update_leaves_the_rest_alone(self, service, l1, sub_l1):
        await service.update(sub_l1.id, l1.id, address="tg:222")
        assert sub_l1.min_severity is Severity.WARNING
        assert sub_l1.is_enabled is True

    async def test_delete(self, service, l1, sub_l1, subscriptions):
        await service.delete(sub_l1.id, l1.id)
        assert sub_l1.id not in subscriptions.rows

    async def test_unknown_subscription(self, service, l1):
        with pytest.raises(NotFound):
            await service.update(uuid.uuid4(), l1.id, address="x")


class TestOwnership:
    """Чужую подписку не трогает никто, включая администратора: иначе
    самообслуживание превращается в заявку админу."""

    async def test_foreign_subscription_cannot_be_updated(self, service, l1, sub_l2):
        with pytest.raises(PermissionDenied):
            await service.update(sub_l2.id, l1.id, address="tg:999")

    async def test_foreign_subscription_cannot_be_deleted(
        self, service, l1, sub_l2, subscriptions
    ):
        with pytest.raises(PermissionDenied):
            await service.delete(sub_l2.id, l1.id)
        assert sub_l2.id in subscriptions.rows

    async def test_admin_has_no_special_rights_here(self, service, admin, sub_l1):
        with pytest.raises(PermissionDenied):
            await service.update(sub_l1.id, admin.id, address="tg:999")

    async def test_error_does_not_leak_the_foreign_settings(self, service, l1, sub_l2):
        """Отказ не должен рассказывать, что именно там настроено: иначе
        перебором id можно составить карту чужих каналов и адресов."""
        with pytest.raises(PermissionDenied) as exc:
            await service.update(sub_l2.id, l1.id, address="x")
        text = str(exc.value)
        assert sub_l2.address not in text
        assert sub_l2.channel.value not in text
        assert str(sub_l2.user_id) not in text


class TestAudit:
    async def test_every_change_is_journalled(self, service, l1, sub_l1, audit):
        await service.update(sub_l1.id, l1.id, address="tg:222")
        await service.delete(sub_l1.id, l1.id)
        actions = [r.details["action"] for r in audit.rows]
        assert actions == ["updated", "deleted"]

    async def test_actor_is_recorded(self, service, l1, sub_l1, audit):
        await service.update(sub_l1.id, l1.id, address="tg:222")
        assert audit.rows[0].actor_id == l1.id
