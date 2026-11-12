from functools import lru_cache
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from leadhunter.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    return make_engine(get_settings().database_url)


def make_engine(url: str) -> AsyncEngine:
    if not url.startswith("sqlite"):
        return create_async_engine(url, pool_pre_ping=True)

    path = url.split("///", 1)[-1]
    if path and path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(url, connect_args={"timeout": 30})

    @event.listens_for(engine.sync_engine, "connect")
    def _configure(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)
