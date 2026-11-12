from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from leadhunter.config import Profile
from leadhunter.db.models import Draft, DraftVariant, Followup, Lead, LeadStatus
from leadhunter.llm.client import LLMClient
from leadhunter.llm.providers import LLMError, LLMInvalidReply
from leadhunter.pipeline.drafter import FollowupDraft, build_followup_prompts
from leadhunter.tracker.events import set_status

log = structlog.get_logger()

FOLLOWUP_AFTER_DAYS = 3


def mark_contacted(session: AsyncSession, lead: Lead, **event_fields) -> None:
    set_status(session, lead, LeadStatus.CONTACTED, **event_fields)
    due_at = datetime.now(UTC) + timedelta(days=FOLLOWUP_AFTER_DAYS)
    session.add(Followup(lead_id=lead.id, due_at=due_at))


async def sent_text_for(session: AsyncSession, lead_id: int) -> str:
    draft = await session.scalar(
        select(Draft)
        .where(Draft.lead_id == lead_id, Draft.chosen, Draft.variant != DraftVariant.FOLLOWUP)
        .order_by(Draft.id.desc())
    )
    return (draft.edited_text or draft.text) if draft else ""


async def draft_due_followups(
    session: AsyncSession, llm: LLMClient, profile: Profile, now: datetime | None = None
) -> list[Draft]:
    now = now or datetime.now(UTC)
    due = (
        await session.execute(
            select(Followup, Lead)
            .join(Lead, Lead.id == Followup.lead_id)
            .where(Followup.done.is_(False), Followup.due_at <= now)
            .order_by(Followup.due_at)
        )
    ).all()

    drafts = []
    for followup, lead in due:
        if lead.status != LeadStatus.CONTACTED:
            followup.done = True
            continue
        system, user = build_followup_prompts(profile, lead, await sent_text_for(session, lead.id))
        try:
            result = await llm.complete_json(system, user, FollowupDraft)
        except LLMInvalidReply as exc:
            followup.done = True
            log.error("followup_draft_invalid", lead_id=lead.id, error=str(exc)[:300])
            continue
        except LLMError as exc:
            log.error("followup_draft_failed", lead_id=lead.id, error=str(exc)[:300])
            break
        draft = Draft(lead_id=lead.id, variant=DraftVariant.FOLLOWUP, text=result.text)
        session.add(draft)
        followup.done = True
        drafts.append(draft)

    await session.flush()
    return drafts
