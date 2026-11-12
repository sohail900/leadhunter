from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from leadhunter.db.models import Lead
from leadhunter.pipeline.normalize import NormalizedLead

DEDUPE_WINDOW_DAYS = 14


async def is_duplicate(
    session: AsyncSession, lead: NormalizedLead, window_days: int = DEDUPE_WINDOW_DAYS
) -> bool:
    same_post = (Lead.source == lead.source) & (Lead.external_id == lead.external_id)
    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    same_content = (Lead.content_hash == lead.content_hash) & (Lead.fetched_at >= cutoff)
    return bool(await session.scalar(select(exists().where(same_post | same_content))))
