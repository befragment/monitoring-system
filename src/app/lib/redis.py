from redis.asyncio import ConnectionPool, Redis

from app.lib.config import settings

pool = ConnectionPool.from_url(
    settings.redis_url,
    max_connections=settings.redis_max_connections,
    decode_responses=True,
    health_check_interval=30,
)

# Асинхронный клиент redis-py безопасно шарить между задачами: на время каждой
# команды соединение берётся из пула и возвращается обратно.
client = Redis(connection_pool=pool)


async def get_redis() -> Redis:
    """Зависимость FastAPI."""
    return client


async def ping() -> bool:
    """Проба доступности Redis."""
    return await client.ping()


async def close() -> None:
    """Освобождает пул соединений. Вызывается при остановке приложения."""
    await client.aclose()
    await pool.aclose()
