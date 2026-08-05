from redis.asyncio import ConnectionPool, Redis

from app.lib.config import settings

pool = ConnectionPool.from_url(
    settings.redis_url,
    max_connections=settings.redis_max_connections,
    decode_responses=True,
    health_check_interval=30,
)

# redis-py's asyncio client is safe to share across tasks: every command
# checks a connection out of the pool for its duration.
client = Redis(connection_pool=pool)


async def get_redis() -> Redis:
    """FastAPI dependency."""
    return client


async def ping() -> bool:
    """Liveness probe for Redis."""
    return await client.ping()


async def close() -> None:
    """Release the connection pool. Called on shutdown."""
    await client.aclose()
    await pool.aclose()
