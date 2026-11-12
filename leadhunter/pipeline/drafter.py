import structlog
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from leadhunter.config import Profile
from leadhunter.db.models import Draft, DraftVariant, Lead, LeadStatus
from leadhunter.llm.client import LLMClient
from leadhunter.llm.prompts import load_prompt, render_profile
from leadhunter.llm.providers import LLMError, LLMInvalidReply
from leadhunter.pipeline.scorer import MAX_BODY_CHARS, format_budget
from leadhunter.tracker.events import set_status

log = structlog.get_logger()

MAX_WORDS = 150


def _within_word_limit(text: str) -> str:
    text = text.strip()
    words = len(text.split())
    if not text:
        raise ValueError("draft is empty")
    if words > MAX_WORDS:
        raise ValueError(f"draft is {words} words; the limit is {MAX_WORDS}")
    return text


class DraftSet(BaseModel):
    short: str = Field(description="Short and direct, 40-70 words")
    portfolio_led: str = Field(description="Leads with the most relevant past project")
    problem_led: str = Field(description="Leads with an observation about their problem")

    _check = field_validator("short", "portfolio_led", "problem_led")(_within_word_limit)


class FollowupDraft(BaseModel):
    text: str = Field(description="The follow-up message, 25-60 words")

    _check = field_validator("text")(_within_word_limit)


def _lead_block(lead: Lead) -> str:
    reasons = lead.score_reasons or {}
    return (
        f"Source: {lead.source}\n"
        f"Posted by: {lead.author or 'unknown'}\n"
        f"Amounts found in the post (unit unknown): "
        f"{format_budget(lead.budget_min, lead.budget_max)}\n"
        f"Screening summary: {reasons.get('summary', '')}\n"
        f"Title: {lead.title}\n\n"
        f"Post:\n{lead.body[:MAX_BODY_CHARS]}"
    )


def build_drafter_prompts(profile: Profile, lead: Lead, guidance: str = "") -> tuple[str, str]:
    system = load_prompt("drafter_system").format(
        profile=render_profile(profile),
        guidance=f"\n## What has worked so far\n\n{guidance}\n" if guidance else "",
    )
    return system, _lead_block(lead)


def build_followup_prompts(profile: Profile, lead: Lead, sent_text: str) -> tuple[str, str]:
    system = load_prompt("followup_system").format(profile=render_profile(profile))
    user = f"{_lead_block(lead)}\n\n---\nThe proposal that was sent:\n{sent_text}"
    return system, user


async def draft_scored_leads(
    sessionmaker: async_sessionmaker[AsyncSession],
    llm: LLMClient,
    profile: Profile,
    *,
    batch_size: int = 10,
    guidance: str = "",
) -> dict[str, int]:
    counts = {"drafted": 0, "failed": 0}
    async with sessionmaker() as session:
        leads = (
            await session.scalars(
                select(Lead)
                .where(Lead.status == LeadStatus.SCORED)
                .order_by(Lead.score.desc(), Lead.fetched_at)
                .limit(batch_size)
            )
        ).all()

        for lead in leads:
            system, user = build_drafter_prompts(profile, lead, guidance)
            try:
                drafts = await llm.complete_json(system, user, DraftSet)
            except LLMInvalidReply as exc:
                set_status(session, lead, LeadStatus.REJECTED, stage="draft", reason=str(exc)[:200])
                counts["failed"] += 1
                await session.commit()
                continue
            except LLMError as exc:
                counts["failed"] += 1
                log.error("draft_failed", lead_id=lead.id, error=str(exc)[:300])
                break

            for variant in (
                DraftVariant.SHORT,
                DraftVariant.PORTFOLIO_LED,
                DraftVariant.PROBLEM_LED,
            ):
                session.add(Draft(lead_id=lead.id, variant=variant, text=getattr(drafts, variant)))
            set_status(session, lead, LeadStatus.DRAFTED)
            counts["drafted"] += 1
            await session.commit()

    if any(counts.values()):
        log.info("draft_run", **counts)
    return counts
