import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt

from app.lib.config import settings

TokenType = Literal["access", "refresh"]

REQUIRED_CLAIMS = ("sub", "role", "type", "jti", "iat", "exp")


class AuthError(Exception):
    """Base class for token problems raised by the pure functions below."""


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
    """Claim presence is already enforced by PyJWT's `require` option, so this
    only has to pin down the types."""
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
    """Verify signature and expiry, then check the token is of the right kind.

    Without the type check a long-lived refresh token would be accepted
    everywhere an access token is.
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
