"""
db.py  —  asyncpg connection pool
Shared across all routes. Initialised once at startup via lifespan.
"""

import asyncpg
print("asyncpg installed")

_pool: asyncpg.Pool | None = None

DB_DSN = "postgresql://traffic_user:traffic_pass@localhost:5433/traffic_db"

async def init_pool():
    global _pool
    _pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=10)


async def close_pool():
    if _pool:
        await _pool.close()


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    return _pool
