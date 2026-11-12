from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from leadhunter.db.models import AppState

THRESHOLD = "score_threshold"
PAUSED = "paused"


async def get_state(session: AsyncSession, key: str, default: Any = None) -> Any:
    row = await session.get(AppState, key)
    return default if row is None else row.value


async def set_state(session: AsyncSession, key: str, value: Any) -> None:
    statement = insert(AppState).values(key=key, value=value)
    await session.execute(
        statement.on_conflict_do_update(index_elements=["key"], set_={"value": value})
    )


async def get_threshold(session: AsyncSession, default: int) -> int:
    return int(await get_state(session, THRESHOLD, default))


async def is_paused(session: AsyncSession) -> bool:
    return bool(await get_state(session, PAUSED, False))
