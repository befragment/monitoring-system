"""П. 7.1 — вход через корпоративный каталог и управление людьми."""

import uuid

import pytest

from app.domain.role import Role
from app.lib.directory import DirectoryProfile
from app.service.errors import AuthenticationFailed, NotFound, PermissionDenied
from app.service.user import DEFAULT_ROLE, UserService

from .conftest import FakeDirectory, FakeTokens, FakeUsers

PROFILE = DirectoryProfile(
    external_id="cn=petrov,ou=eng", email="petrov@example.ru", full_name="Пётр Петров"
)


@pytest.fixture
def directory() -> FakeDirectory:
    return FakeDirectory({"petrov": ("secret", PROFILE)})


@pytest.fixture
def service(users, directory, audit, uow) -> UserService:
    return UserService(users, directory, FakeTokens(), audit, uow)


class TestLogin:
    async def test_first_login_creates_the_user(self, service, users):
        session = await service.login("petrov", "secret")
        assert session.user.external_id == PROFILE.external_id
        assert session.user.id in users.rows

    async def test_new_user_gets_the_quietest_role(self, service):
        """Неизвестный сотрудник не должен по факту первого входа получить право
        закрывать проблемы; повышает администратор осознанно."""
        session = await service.login("petrov", "secret")
        assert session.user.role is DEFAULT_ROLE

    async def test_tokens_carry_the_id_and_the_role(self, service):
        session = await service.login("petrov", "secret")
        assert session.access_token == f"access:{session.user.id}:{DEFAULT_ROLE.value}"
        assert session.refresh_token.startswith("refresh:")

    async def test_profile_is_refreshed_on_every_login(
        self, service, users, directory
    ):
        """Так почта и ФИО не расходятся с каталогом без отдельной
        синхронизации."""
        first = await service.login("petrov", "secret")
        directory.people["petrov"] = (
            "secret",
            DirectoryProfile(PROFILE.external_id, "p.petrov@example.ru", "Пётр Петров-Водкин"),
        )
        second = await service.login("petrov", "secret")
        assert second.user.id == first.user.id
        assert second.user.email == "p.petrov@example.ru"
        assert second.user.full_name == "Пётр Петров-Водкин"

    async def test_role_is_not_reset_by_login(self, service, users):
        """Каталог отвечает, кто это, а что ему можно — решает платформа."""
        first = await service.login("petrov", "secret")
        users.rows[first.user.id].role = Role.L2
        second = await service.login("petrov", "secret")
        assert second.user.role is Role.L2


class TestAuthenticationFailures:
    async def test_wrong_password(self, service):
        with pytest.raises(AuthenticationFailed):
            await service.login("petrov", "wrong")

    async def test_unknown_login(self, service):
        with pytest.raises(AuthenticationFailed):
            await service.login("sidorov", "secret")

    async def test_both_look_identical(self, service):
        """Иначе форма входа превращается в оракул для перебора логинов."""
        with pytest.raises(AuthenticationFailed) as unknown:
            await service.login("sidorov", "secret")
        with pytest.raises(AuthenticationFailed) as wrong:
            await service.login("petrov", "wrong")
        assert str(unknown.value) == str(wrong.value)

    async def test_disabled_account_cannot_log_in(self, service, users):
        """Уволенный числится в базе ради истории подписок, но входить не должен."""
        session = await service.login("petrov", "secret")
        users.rows[session.user.id].is_active = False
        with pytest.raises(AuthenticationFailed):
            await service.login("petrov", "secret")

    async def test_no_user_is_created_on_failure(self, service, users):
        before = len(users.rows)
        with pytest.raises(AuthenticationFailed):
            await service.login("petrov", "wrong")
        assert len(users.rows) == before


class TestRoles:
    async def test_admin_changes_a_role(self, service, admin, l1):
        updated = await service.set_role(l1.id, admin.id, Role.L2)
        assert updated.role is Role.L2

    @pytest.mark.parametrize("role_fixture", ["l1", "l2", "manager"])
    async def test_others_cannot(self, service, request, role_fixture, l1):
        actor = request.getfixturevalue(role_fixture)
        with pytest.raises(PermissionDenied):
            await service.set_role(l1.id, actor.id, Role.ADMIN)

    async def test_unknown_target(self, service, admin):
        with pytest.raises(NotFound):
            await service.set_role(uuid.uuid4(), admin.id, Role.L2)

    async def test_subscriptions_are_untouched(self, service, admin, l1, subscriptions):
        """Роль даёт умолчания, но не определяет подписки жёстко (п. 5.6):
        инженер, ставший менеджером, теряет кнопку, но не рассылку."""
        before = dict(subscriptions.rows)
        await service.set_role(l1.id, admin.id, Role.MANAGER)
        assert subscriptions.rows == before

    async def test_change_is_audited(self, service, admin, l1, audit):
        await service.set_role(l1.id, admin.id, Role.L2)
        record = audit.rows[-1]
        assert record.actor_id == admin.id
        assert record.details["role_from"] == Role.L1.value
        assert record.details["role_to"] == Role.L2.value


class TestDirectorySync:
    async def test_missing_people_are_deactivated(self, service, users, l1):
        """П. 5.6 — автоотписка при увольнении без ручных действий."""
        disabled = await service.sync_with_directory()
        assert disabled >= 1
        assert l1.is_active is False

    async def test_nobody_is_deleted(self, service, users, l1):
        """Удаление унесло бы вместе с человеком и подписки, и владение
        проблемами, а история обязана оставаться читаемой."""
        before = set(users.rows)
        await service.sync_with_directory()
        assert set(users.rows) == before

    async def test_present_people_stay_active(self, service, users, directory):
        session = await service.login("petrov", "secret")
        await service.sync_with_directory()
        assert users.rows[session.user.id].is_active is True

    async def test_sync_is_audited_once(self, service, audit):
        await service.sync_with_directory()
        records = [r for r in audit.rows if "directory_sync" in r.details]
        assert len(records) == 1

    async def test_no_audit_when_nothing_changed(self, service, users, audit):
        users.rows = {}
        await service.sync_with_directory()
        assert [r for r in audit.rows if "directory_sync" in r.details] == []


class TestListing:
    async def test_admin_lists_everyone(self, service, admin, users):
        assert len(await service.list_all(admin.id)) == len(users.rows)

    async def test_engineer_cannot(self, service, l1):
        with pytest.raises(PermissionDenied):
            await service.list_all(l1.id)


async def test_password_never_reaches_the_user_record(service, users):
    """Платформа не хранит учётные данные: пароль проходит через каталог и
    нигде не задерживается."""
    session = await service.login("petrov", "secret")
    stored = users.rows[session.user.id]
    assert not any("password" in field for field in type(stored).__slots__)
    assert "secret" not in repr(stored)
