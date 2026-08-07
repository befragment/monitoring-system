import logging

from fastapi import FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware

from app.handler.middleware import AuthMiddleware
from app.lib import postgres, redis
from app.lib.config import settings
from app.lib.shutdown import lifespan, shutdown

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title=settings.app_name,
    debug=settings.debug,
    lifespan=lifespan,
    # Схема перечисляет все роуты и структуру их payload'ов — в проде выключаем.
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
    openapi_url="/openapi.json" if settings.debug else None,
)

# Порядок регистрации обратен порядку выполнения: AuthMiddleware добавлен
# первым, поэтому CORSMiddleware оказывается снаружи и даже 401 возвращается с
# CORS-заголовками, а не выглядит в браузере непрозрачной сетевой ошибкой.
app.add_middleware(AuthMiddleware)
if settings.cors_origin_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.get("/health", tags=["ops"], summary="Liveness probe")
async def health() -> dict[str, str]:
    """Сообщает, что процесс жив. Сознательно не трогает зависимости: иначе
    кратковременный сбой БД приводил бы к убийству и перезапуску здорового
    контейнера."""
    return {"status": "ok"}


@app.get("/health/ready", tags=["ops"], summary="Readiness probe")
async def ready(response: Response) -> dict[str, object]:
    """Сообщает, что процесс готов обслуживать трафик. Во время остановки
    отдаёт 503 — именно это выводит инстанс из балансировщика при rolling-
    деплое.
    """
    if shutdown.is_shutting_down:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "shutting_down", "checks": {}}

    checks: dict[str, str] = {}
    for name, probe in (("postgres", postgres.ping), ("redis", redis.ping)):
        try:
            await probe()
            checks[name] = "ok"
        except Exception:
            logger.exception("readiness: %s probe failed", name)
            checks[name] = "error"

    healthy = all(state == "ok" for state in checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if healthy else "degraded", "checks": checks}


# Сюда подключаются роутеры по мере наполнения слоя handler, например:
# from app.handler import user
# app.include_router(user.router, prefix="/auth", tags=["auth"])
