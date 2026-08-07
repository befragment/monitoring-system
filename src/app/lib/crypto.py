import asyncio

import bcrypt

from app.lib.config import settings

# bcrypt читает только первые 72 байта пароля. Молчаливое обрезание сделало бы
# длинный пароль эквивалентным любому другому с тем же 72-байтным префиксом,
# поэтому такой пароль отклоняется, а не подрезается втихую.
MAX_PASSWORD_BYTES = 72


class PasswordTooLongError(ValueError):
    def __init__(self) -> None:
        super().__init__(
            f"password must be at most {MAX_PASSWORD_BYTES} bytes when UTF-8 encoded"
        )


def _encode(password: str) -> bytes:
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise PasswordTooLongError
    return encoded


def hash_password(password: str) -> str:
    """Блокирующая. В хендлерах используй `hash_password_async`."""
    salt = bcrypt.gensalt(rounds=settings.bcrypt_rounds)
    return bcrypt.hashpw(_encode(password), salt).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """Блокирующая. В хендлерах используй `verify_password_async`.

    На испорченном хэше из БД возвращает False, а не бросает исключение, чтобы
    вызывающий код не мог отличить битую строку от неверного пароля.
    """
    try:
        return bcrypt.checkpw(_encode(password), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


async def hash_password_async(password: str) -> str:
    """bcrypt сжигает ~100 мс CPU — уводим его с event loop.

    Именно эти обёртки нужно звать из хендлеров, чтобы не блокировать цикл.
    """
    return await asyncio.to_thread(hash_password, password)


async def verify_password_async(password: str, hashed: str) -> bool:
    """bcrypt сжигает ~100 мс CPU — уводим его с event loop."""
    return await asyncio.to_thread(verify_password, password, hashed)
