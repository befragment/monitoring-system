import asyncio

import bcrypt

from app.lib.config import settings

# bcrypt only consumes the first 72 bytes of the password. Silently truncating
# would make "long password" and "same 72-byte prefix" equivalent, so we reject
# instead of hiding it.
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
    """Blocking. Prefer `hash_password_async` inside request handlers."""
    salt = bcrypt.gensalt(rounds=settings.bcrypt_rounds)
    return bcrypt.hashpw(_encode(password), salt).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """Blocking. Prefer `verify_password_async` inside request handlers.

    Returns False rather than raising on a malformed stored hash, so a corrupted
    row cannot be told apart from a wrong password by the caller.
    """
    try:
        return bcrypt.checkpw(_encode(password), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


async def hash_password_async(password: str) -> str:
    """bcrypt burns ~100ms of CPU; run it off the event loop.
    
    use them in handlers to not block event loop"""
    return await asyncio.to_thread(hash_password, password)


async def verify_password_async(password: str, hashed: str) -> bool:
    """bcrypt burns ~100ms of CPU; run it off the event loop."""
    return await asyncio.to_thread(verify_password, password, hashed)
