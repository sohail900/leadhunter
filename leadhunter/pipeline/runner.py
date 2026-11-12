from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from leadhunter.collectors.base import BaseCollector
from leadhunter.config import Profile
from leadhunter.db.models import Lead, LeadStatus
from leadhunter.pipeline.dedupe import is_duplicate
from leadhunter.pipeline.normalize import normalize
from leadhunter.pipeline.prefilter import prefilter
from leadhunter.tracker.events import set_status

log = structlog.get_logger()


@dataclass
class RunResult:
    fetched: int = 0
    new: int = 0
    duplicates: int = 0
    rejected: int = 0
    failed: bool = False


async def run_collector(
    collector: BaseCollector,
    sessionmaker: async_sessionmaker[AsyncSession],
    profile: Profile,
    max_age_hours: int = 48,
) -> RunResult:
    result = RunResult()
    try:
        raw_leads = await collector.fetch()
        result.fetched = len(raw_leads)

        async with sessionmaker() as session:
            for raw in raw_leads:
                lead = normalize(raw)
                if await is_duplicate(session, lead):
                    result.duplicates += 1
                    continue
                row = Lead(**lead.model_dump(), status=LeadStatus.NEW)
                session.add(row)
                await session.flush()
                result.new += 1

                reason = prefilter(lead.title, lead.body, lead.posted_at, profile, max_age_hours)
                if reason:
                    set_status(session, row, LeadStatus.REJECTED, stage="prefilter", reason=reason)
                    result.rejected += 1
            await session.commit()
    except Exception:
        result.failed = True
        log.exception("collector_failed", source=collector.name)
        return result

    log.info(
        "collector_run",
        source=collector.name,
        fetched=result.fetched,
        new=result.new,
        duplicates=result.duplicates,
        rejected=result.rejected,
    )
    return result
