from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from leadhunter.db.models import AppState

THRESHOLD = "score_threshold"
PAUSED = "paused"
AUTO = "auto_send"


async def get_state(session: AsyncSession, key: str, default: Any = None) -> Any:
    row = await session.get(AppState, key)
    return default if row is None else row.value


async def set_state(session: AsyncSession, key: str, value: Any) -> None:
    row = await session.get(AppState, key)
    if row is None:
        session.add(AppState(key=key, value=value))
    else:
        row.value = value
    await session.flush()


async def get_threshold(session: AsyncSession, default: int) -> int:
    return int(await get_state(session, THRESHOLD, default))


async def is_paused(session: AsyncSession) -> bool:
    return bool(await get_state(session, PAUSED, False))


async def is_auto(session: AsyncSession) -> bool:
    return bool(await get_state(session, AUTO, False))
