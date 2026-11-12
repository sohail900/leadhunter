from typing import Annotated

import structlog
from pydantic import BaseModel, BeforeValidator, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from leadhunter.config import Profile
from leadhunter.db.models import Lead, LeadStatus
from leadhunter.llm.client import LLMClient
from leadhunter.llm.prompts import load_prompt, render_profile
from leadhunter.llm.providers import LLMError, LLMInvalidReply
from leadhunter.pipeline.prefilter import prefilter
from leadhunter.state import get_threshold
from leadhunter.tracker.events import set_status

log = structlog.get_logger()

MAX_BODY_CHARS = 6000


def _clamp(value):
    try:
        return max(0, min(10, round(float(value))))
    except (TypeError, ValueError):
        return value


Rating = Annotated[int, BeforeValidator(_clamp)]


class ScoreResult(BaseModel):
    score: Rating = Field(description="Overall recommendation, 0-10")
    stack_match: Rating = Field(description="0-10")
    budget_fit: Rating = Field(description="0-10")
    client_quality: Rating = Field(description="0-10")
    scam_risk: Rating = Field(description="0-10, where 10 is almost certainly a scam")
    tech_stack: list[str] = Field(description="Technologies the post names or clearly needs")
    summary: str = Field(description="One line, at most 20 words")
    reasons: list[str] = Field(description="Two to four short points explaining the score")


def format_budget(budget_min: int | None, budget_max: int | None) -> str:
    if budget_min is None:
        return "not stated"
    if budget_min == budget_max or budget_max is None:
        return f"${budget_min:,}"
    return f"${budget_min:,} - ${budget_max:,}"


def build_scorer_prompts(
    profile: Profile,
    *,
    source: str,
    title: str,
    body: str,
    budget_min: int | None = None,
    budget_max: int | None = None,
    guidance: str = "",
) -> tuple[str, str]:
    system = load_prompt("scorer_system").format(
        profile=render_profile(profile),
        guidance=f"\n## What has worked so far\n\n{guidance}\n" if guidance else "",
    )
    user = (
        f"Source: {source}\n"
        f"Amounts found in the post (unit unknown): {format_budget(budget_min, budget_max)}\n"
        f"Title: {title}\n\n"
        f"Post:\n{body[:MAX_BODY_CHARS]}"
    )
    return system, user


async def score_text(llm: LLMClient, profile: Profile, **lead_fields) -> ScoreResult:
    system, user = build_scorer_prompts(profile, **lead_fields)
    return await llm.complete_json(system, user, ScoreResult)


async def score_new_leads(
    sessionmaker: async_sessionmaker[AsyncSession],
    llm: LLMClient,
    profile: Profile,
    *,
    default_threshold: int,
    max_age_hours: int,
    batch_size: int = 20,
    guidance: str = "",
) -> dict[str, int]:
    counts = {"scored": 0, "accepted": 0, "rejected": 0, "prefiltered": 0, "failed": 0}
    async with sessionmaker() as session:
        threshold = await get_threshold(session, default_threshold)
        leads = (
            await session.scalars(
                select(Lead)
                .where(Lead.status == LeadStatus.NEW)
                .order_by(Lead.fetched_at)
                .limit(batch_size)
            )
        ).all()

        for lead in leads:
            reason = prefilter(lead.title, lead.body, lead.posted_at, profile, max_age_hours)
            if reason:
                set_status(session, lead, LeadStatus.REJECTED, stage="prefilter", reason=reason)
                counts["prefiltered"] += 1
                await session.commit()
                continue

            try:
                result = await score_text(
                    llm,
                    profile,
                    source=lead.source,
                    title=lead.title,
                    body=lead.body,
                    budget_min=lead.budget_min,
                    budget_max=lead.budget_max,
                    guidance=guidance,
                )
            except LLMInvalidReply as exc:
                set_status(session, lead, LeadStatus.REJECTED, stage="score", reason=str(exc)[:200])
                counts["failed"] += 1
                await session.commit()
                continue
            except LLMError as exc:
                counts["failed"] += 1
                log.error("score_failed", lead_id=lead.id, error=str(exc)[:300])
                break

            lead.score = result.score
            lead.score_reasons = result.model_dump(exclude={"score", "tech_stack"})
            lead.tech_stack = list(dict.fromkeys(result.tech_stack))
            set_status(session, lead, LeadStatus.SCORED, score=result.score)
            counts["scored"] += 1
            if result.score < threshold:
                set_status(session, lead, LeadStatus.REJECTED, stage="score", threshold=threshold)
                counts["rejected"] += 1
            else:
                counts["accepted"] += 1
            await session.commit()

    if any(counts.values()):
        log.info("score_run", **counts)
    return counts
