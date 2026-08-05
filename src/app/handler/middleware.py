import logging
from collections.abc import Iterable
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.lib.auth import AuthError, TokenExpiredError, TokenPayload, decode_token

# Everything not listed here requires a valid access token: the default is deny,
# so forgetting to guard a new route fails closed instead of leaking it.
DEFAULT_PUBLIC_PATHS = (
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/auth/login",
    "/auth/register",
    "/auth/refresh",
)


class AuthMiddleware:
    """Pure ASGI middleware that turns a Bearer token into `request.state.principal`.

    Written against the raw ASGI interface rather than `BaseHTTPMiddleware`
    because that base class spawns an anyio task group per request — needless
    overhead on a hot path that only reads one header.

    Install it *before* CORSMiddleware so CORS ends up on the outside and even
    rejected requests come back with the right CORS headers:

        app.add_middleware(AuthMiddleware)
        app.add_middleware(CORSMiddleware, ...)
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        public_paths: Iterable[str] = DEFAULT_PUBLIC_PATHS,
    ) -> None:
        self.app = app
        self.public_paths = tuple(public_paths)

    def _is_public(self, path: str) -> bool:
        # Prefix match on a path segment boundary, so "/health" cannot be used
        # to slip past "/health-internal".
        return any(
            path == public or path.startswith(f"{public}/")
            for public in self.public_paths
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # CORS preflight carries no credentials by design.
        if scope["method"] == "OPTIONS" or self._is_public(scope["path"]):
            await self.app(scope, receive, send)
            return

        header = Headers(scope=scope).get("authorization")
        if header is None:
            await self._reject(scope, receive, send, "not authenticated")
            return

        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            await self._reject(scope, receive, send, "invalid authorization header")
            return

        try:
            principal = decode_token(token, expected_type="access")
        except TokenExpiredError:
            await self._reject(scope, receive, send, "token has expired")
            return
        except AuthError as exc:
            await self._reject(scope, receive, send, "could not validate credentials")
            return

        # Starlette exposes scope["state"] as request.state downstream.
        scope.setdefault("state", {})["principal"] = principal
        await self.app(scope, receive, send)

    async def _reject(
        self, scope: Scope, receive: Receive, send: Send, detail: str
    ) -> None:
        response = JSONResponse(
            {"detail": detail},
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)


async def get_principal(request: Request) -> TokenPayload:
    """FastAPI dependency reading the principal AuthMiddleware attached."""
    principal = request.scope.get("state", {}).get("principal")
    if principal is None:
        # Reachable only if the route is in public_paths but asks for a
        # principal anyway — a wiring mistake, not a client error.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


CurrentPrincipal = Annotated[TokenPayload, Depends(get_principal)]


def require_role(*allowed_roles: str):
    """Route guard for authorization: `Depends(require_role("admin"))`.

    Authentication is already done by the middleware; this only checks the role.
    """

    async def dependency(principal: CurrentPrincipal) -> TokenPayload:
        if principal.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="insufficient permissions",
            )
        return principal

    return dependency
