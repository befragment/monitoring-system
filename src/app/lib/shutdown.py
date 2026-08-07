import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.lib import postgres, redis
from app.lib.config import settings

logger = logging.getLogger(__name__)

CleanupCallback = Callable[[], Awaitable[None]]


class GracefulShutdown:
    """Упорядоченный реестр процедур очистки.

    Uvicorn по SIGTERM/SIGINT сам перестаёт принимать соединения и дожидается
    завершения запросов в работе, и только потом запускает shutdown у lifespan.
    Этот класс отвечает за то, что происходит после: освобождает внешние
    ресурсы в порядке, обратном регистрации, и укладывается в общий бюджет
    времени, чтобы зависшая зависимость не держала процесс живым вечно.
    """

    def __init__(self, timeout: float) -> None:
        self._timeout = timeout
        self._callbacks: list[tuple[str, CleanupCallback]] = []
        self.is_shutting_down = False

    def register(self, name: str, callback: CleanupCallback) -> None:
        self._callbacks.append((name, callback))

    async def run(self) -> None:
        self.is_shutting_down = True
        try:
            async with asyncio.timeout(self._timeout):
                for name, callback in reversed(self._callbacks):
                    try:
                        await callback()
                        logger.info("shutdown: closed %s", name)
                    except Exception:
                        # Падение одного ресурса не должно бросать остальные.
                        logger.exception("shutdown: failed to close %s", name)
        except TimeoutError:
            logger.error(
                "shutdown: exceeded %.1fs budget, exiting anyway", self._timeout
            )


shutdown = GracefulShutdown(timeout=settings.shutdown_timeout)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Подключается к FastAPI через `FastAPI(lifespan=lifespan)`.

    Зависимости проверяются на старте, чтобы процесс падал сразу при кривом
    конфиге, а не отдавал 500-е, и закрываются на выходе.
    """
    await postgres.ping()
    logger.info("startup: postgres reachable")
    await redis.ping()
    logger.info("startup: redis reachable")

    shutdown.register("postgres", postgres.close)
    shutdown.register("redis", redis.close)

    try:
        yield
    finally:
        await shutdown.run()
