import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

import jwt

from app.lib.config import settings

TokenType = Literal["access", "refresh"]

REQUIRED_CLAIMS = ("sub", "role", "type", "jti", "iat", "exp")


class AuthError(Exception):
    """Базовый класс проблем с токеном, которые бросают функции ниже."""


class TokenExpiredError(AuthError):
    pass


class InvalidTokenError(AuthError):
    pass


@dataclass(frozen=True, slots=True)
class TokenPayload:
    sub: str
    role: str
    type: TokenType
    jti: str
    iat: datetime
    exp: datetime


def _create_token(
    subject: str | uuid.UUID,
    role: str,
    token_type: TokenType,
    lifetime: timedelta,
) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(subject),
        "role": role,
        "type": token_type,
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + lifetime,
    }
    return jwt.encode(
        payload,
        settings.secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


def create_access_token(subject: str | uuid.UUID, role: str) -> str:
    return _create_token(
        subject,
        role,
        "access",
        timedelta(minutes=settings.access_token_expire_minutes),
    )


def create_refresh_token(subject: str | uuid.UUID, role: str) -> str:
    return _create_token(
        subject,
        role,
        "refresh",
        timedelta(days=settings.refresh_token_expire_days),
    )


def _payload_from_claims(raw: dict[str, Any]) -> TokenPayload:
    """Наличие claims уже проверил PyJWT через опцию `require`, поэтому здесь
    остаётся только зафиксировать типы."""
    sub, role, token_type, jti = (raw["sub"], raw["role"], raw["type"], raw["jti"])
    if not all(isinstance(value, str) for value in (sub, role, token_type, jti)):
        raise InvalidTokenError("malformed token payload")

    try:
        iat = datetime.fromtimestamp(raw["iat"], UTC)
        exp = datetime.fromtimestamp(raw["exp"], UTC)
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        raise InvalidTokenError("malformed token timestamps") from exc

    return TokenPayload(sub=sub, role=role, type=token_type, jti=jti, iat=iat, exp=exp)


def decode_token(token: str, expected_type: TokenType = "access") -> TokenPayload:
    """Проверяет подпись и срок, затем сверяет, что тип токена тот самый.

    Без проверки типа долгоживущий refresh-токен принимался бы везде, где
    ожидается access.
    """
    try:
        raw = jwt.decode(
            token,
            settings.secret_key.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            options={"require": list(REQUIRED_CLAIMS)},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError("token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise InvalidTokenError(str(exc)) from exc

    payload = _payload_from_claims(raw)
    if payload.type != expected_type:
        raise InvalidTokenError(f"expected a {expected_type} token")
    return payload


class TokenIssuerInterface(Protocol):
    """Выдача токенов как порт.

    Нужен, чтобы сервис входа проверялся без разбора JWT: подделка возвращает
    строку «token-for-<id>», и тест читает её глазами вместо того, чтобы
    декодировать подпись ради проверки, что роль легла в нужный claim.
    """

    def issue_access(self, subject: str, role: str) -> str: ...

    def issue_refresh(self, subject: str, role: str) -> str: ...


class TokenIssuer:
    """Рабочая реализация — тонкая обёртка над функциями выше.

    Класс, а не сами функции, потому что сервису передают объект: подменить
    модульную функцию можно только патчингом, а объект — просто аргументом.
    """

    def issue_access(self, subject: str, role: str) -> str:
        return create_access_token(subject, role)

    def issue_refresh(self, subject: str, role: str) -> str:
        return create_refresh_token(subject, role)
