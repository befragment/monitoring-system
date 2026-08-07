"""П. 7.1 — вход через корпоративный каталог и управление людьми.

Пароль в этом модуле встречается ровно один раз: как аргумент, который уходит в
`DirectoryGateway` и нигде не задерживается. Платформа не хранит учётные данные
и не умеет их проверять сама — этого требует ТЗ, и из этого следует, что в
`users` нет и не должно быть колонки с хэшем.
"""

import uuid
from dataclasses import dataclass

from app.domain.audit import AuditAction, AuditRecord
from app.domain.role import Role
from app.domain.user import User
from app.lib.auth import TokenIssuerInterface
from app.lib.directory import DirectoryGatewayInterface
from app.repository._contracts import AuditRepository, UnitOfWork, UserRepository
from app.service.errors import AuthenticationFailed, NotFound, PermissionDenied

# Роль, которую получает человек, впервые вошедший через каталог. Самая тихая из
# возможных: неизвестный сотрудник не должен по факту первого входа получить
# право закрывать проблемы. Повышает администратор осознанно.
DEFAULT_ROLE = Role.L1


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    user: User
    access_token: str
    refresh_token: str


class UserService:
    def __init__(
        self,
        users: UserRepository,
        directory: DirectoryGatewayInterface,
        tokens: TokenIssuerInterface,
        audit: AuditRepository,
        uow: UnitOfWork,
    ) -> None:
        self._users = users
        self._directory = directory
        self._tokens = tokens
        self._audit = audit
        self._uow = uow

    async def login(self, login: str, password: str) -> AuthenticatedSession:
        """Вход: каталог подтверждает, платформа выдаёт токен.

        Профиль подтягивается при каждом входе — так почта и ФИО не расходятся
        с каталогом без отдельной синхронизации.
        """
        profile = await self._directory.authenticate(login, password)
        if profile is None:
            # Одна ошибка на «нет такого» и «неверный пароль»: иначе форма входа
            # становится оракулом для перебора логинов.
            raise AuthenticationFailed("directory did not confirm the credentials")

        user = await self._users.get_by_external_id(profile.external_id)
        if user is None:
            user = User(
                id=uuid.uuid4(),
                external_id=profile.external_id,
                email=profile.email,
                full_name=profile.full_name,
                role=DEFAULT_ROLE,
            )
            await self._users.add(user)
        else:
            if not user.is_active:
                # Уволенный числится в базе ради истории подписок, но входить
                # больше не должен.
                raise AuthenticationFailed("account is disabled")
            user.email = profile.email
            user.full_name = profile.full_name
            await self._users.save(user)

        await self._uow.commit()
        return AuthenticatedSession(
            user=user,
            access_token=self._tokens.issue_access(str(user.id), user.role.value),
            refresh_token=self._tokens.issue_refresh(str(user.id), user.role.value),
        )

    async def set_role(
        self, user_id: uuid.UUID, actor_id: uuid.UUID, role: Role
    ) -> User:
        """Смена роли — единственное, чем администратор влияет на оповещения.

        Подписки при этом не трогаются: роль даёт умолчания, но не определяет
        подписки жёстко (п. 5.6). Инженер, ставший менеджером, перестаёт видеть
        кнопку «взять в работу», но продолжает получать то, на что подписался.
        """
        await self._require_admin(actor_id)
        user = await self._users.get_by_id(user_id)
        if user is None:
            raise NotFound(f"user {user_id} not found")

        previous = user.role
        user.role = role
        await self._users.save(user)
        await self._audit.add(
            AuditRecord.create(
                AuditAction.SETTINGS_CHANGED,
                actor_id=actor_id,
                details={
                    "user": str(user.id),
                    "role_from": previous.value,
                    "role_to": role.value,
                },
            )
        )
        await self._uow.commit()
        return user

    async def sync_with_directory(self) -> int:
        """П. 5.6 — автоматическое отключение при увольнении.

        Никого не удаляем: гасим флаг. Удаление унесло бы вместе с человеком его
        подписки и владение проблемами, а история обязана оставаться читаемой —
        кто дежурил, кто закрыл, кому уходили оповещения.
        """
        present = set(await self._directory.list_active_ids())
        disabled = 0
        for user in await self._users.list_all(only_active=True):
            if user.external_id not in present:
                user.is_active = False
                await self._users.save(user)
                disabled += 1
        if disabled:
            await self._audit.add(
                AuditRecord.create(
                    AuditAction.SETTINGS_CHANGED,
                    details={"directory_sync": "deactivated", "count": str(disabled)},
                )
            )
        await self._uow.commit()
        return disabled

    async def list_all(self, actor_id: uuid.UUID) -> list[User]:
        await self._require_admin(actor_id)
        return await self._users.list_all()

    async def _require_admin(self, actor_id: uuid.UUID) -> None:
        actor = await self._users.get_by_id(actor_id)
        if actor is None:
            raise NotFound(f"user {actor_id} not found")
        if actor.role is not Role.ADMIN:
            raise PermissionDenied(f"role {actor.role} cannot manage users")
