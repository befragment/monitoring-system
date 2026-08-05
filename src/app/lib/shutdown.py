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
    """Ordered cleanup registry.

    Uvicorn already stops accepting connections and drains in-flight requests
    when it receives SIGTERM/SIGINT; only then does it run the lifespan
    shutdown. This class handles what comes after that: releasing external
    resources in reverse registration order, under a total time budget, so a
    hung dependency cannot keep the process alive forever.
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
                        # One resource failing must not strand the others.
                        logger.exception("shutdown: failed to close %s", name)
        except TimeoutError:
            logger.error(
                "shutdown: exceeded %.1fs budget, exiting anyway", self._timeout
            )


shutdown = GracefulShutdown(timeout=settings.shutdown_timeout)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Wire into FastAPI with `FastAPI(lifespan=lifespan)`.

    Dependencies are verified on the way up so the process fails fast on a bad
    config instead of serving 500s, and torn down on the way out.
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
