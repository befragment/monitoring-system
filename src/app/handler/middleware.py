from collections.abc import Iterable
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.lib.auth import AuthError, TokenExpiredError, TokenPayload, decode_token

# Всё, чего нет в этом списке, требует валидного access-токена: политика
# «запрещено по умолчанию», поэтому забытый guard на новом роуте закрывает его,
# а не открывает наружу.
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
    """Чистый ASGI-middleware: превращает Bearer-токен в `request.state.principal`.

    Написан на голом ASGI, а не через `BaseHTTPMiddleware`, потому что тот
    поднимает anyio task group на каждый запрос — лишние накладные расходы на
    горячем пути, который всего лишь читает один заголовок.

    Подключать нужно *до* CORSMiddleware, чтобы CORS оказался снаружи и даже
    отклонённые запросы возвращались с правильными CORS-заголовками:

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
        # Префикс сверяется по границе сегмента пути, чтобы через "/health"
        # нельзя было проскочить в "/health-internal".
        return any(
            path == public or path.startswith(f"{public}/")
            for public in self.public_paths
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # CORS-preflight по спецификации ходит без учётных данных.
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
        except AuthError:
            await self._reject(scope, receive, send, "could not validate credentials")
            return

        # Starlette отдаёт scope["state"] вниз по стеку как request.state.
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
    """Зависимость FastAPI: читает принципала, положенного AuthMiddleware."""
    principal = request.scope.get("state", {}).get("principal")
    if principal is None:
        # Достижимо, только если роут попал в public_paths, но при этом просит
        # принципала, — то есть ошибка сборки приложения, а не клиента.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


CurrentPrincipal = Annotated[TokenPayload, Depends(get_principal)]


def require_role(*allowed_roles: str):
    """Guard авторизации для роута: `Depends(require_role("admin"))`.

    Аутентификацию уже сделал middleware, здесь проверяется только роль.
    """

    async def dependency(principal: CurrentPrincipal) -> TokenPayload:
        if principal.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="insufficient permissions",
            )
        return principal

    return dependency
