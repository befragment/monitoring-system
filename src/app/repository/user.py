"""Доступ к пользователям.

ORM-модель переехала в `_ormmodels.py` — там живут все таблицы разом, потому
что alembic импортирует этот модуль целиком. Здесь остаётся то, ради чего
репозиторий и нужен: перевод строки таблицы в доменный объект и обратно.

Граница слоя: наружу отдаётся `app.domain.user.User`, а не `UserORM`. Иначе
сервисы начали бы принимать решения по полям строки таблицы, и доменные правила
разъехались бы со схемой — ровно то, что уже случилось с колонкой пароля,
которой в модели никогда не было.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.role import Role
from app.domain.user import User
from app.repository._ormmodels import UserORM


def _to_domain(row: UserORM) -> User:
    return User(
        id=row.id,
        external_id=row.external_id,
        email=row.email,
        full_name=row.full_name,
        role=row.role,
        is_active=row.is_active,
    )


class UserRepository:
    """Коммит намеренно не делается здесь — транзакцией владеет сервисный слой.

    Иначе одно действие дежурного (взять проблему, записать аудит, поставить
    доставки) распалось бы на несколько независимых транзакций, и падение
    посередине оставило бы журнал аудита без записи о том, что уже произошло.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        row = await self._session.get(UserORM, user_id)
        return _to_domain(row) if row else None

    async def get_by_external_id(self, external_id: str) -> User | None:
        """Точка входа аутентификации: каталог знает свой идентификатор, а не наш."""
        stmt = select(UserORM).where(UserORM.external_id == external_id)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_domain(row) if row else None

    async def get_by_email(self, email: str) -> User | None:
        stmt = select(UserORM).where(UserORM.email == email)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_domain(row) if row else None

    async def list_by_role(self, role: Role, *, only_active: bool = True) -> list[User]:
        """Адресация ступеней эскалации и рассылок по ролям.

        `only_active` по умолчанию включён: рассылка на уволенного — это шум
        ровно в тот момент, когда его меньше всего можно себе позволить.
        """
        stmt = select(UserORM).where(UserORM.role == role)
        if only_active:
            stmt = stmt.where(UserORM.is_active.is_(True))
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_domain(row) for row in rows]

    async def add(self, user: User) -> None:
        self._session.add(
            UserORM(
                id=user.id,
                external_id=user.external_id,
                email=user.email,
                full_name=user.full_name,
                role=user.role,
                is_active=user.is_active,
            )
        )

    async def save(self, user: User) -> None:
        """Обновляет изменяемую часть: роль и признак активности.

        `external_id` не трогаем — это ключ, по которому сходится синхронизация
        с каталогом, и его смена означала бы другого человека.
        """
        row = await self._session.get(UserORM, user.id)
        if row is None:
            raise LookupError(f"user {user.id} not found")
        row.email = user.email
        row.full_name = user.full_name
        row.role = user.role
        row.is_active = user.is_active
