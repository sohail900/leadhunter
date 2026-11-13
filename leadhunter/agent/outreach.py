import smtplib
from datetime import datetime

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from leadhunter.agent.conversation import EMAIL, Desk, record_sent
from leadhunter.agent.report import start_of_day
from leadhunter.bot import formatting
from leadhunter.bot.delivery import proposal_drafts, send_html
from leadhunter.db.models import Event, EventType, Lead, LeadStatus
from leadhunter.state import is_auto
from leadhunter.tracker.events import record_event, set_status
from leadhunter.tracker.followups import mark_contacted
from leadhunter.tracker.stats import best_variant, collect_stats

log = structlog.get_logger()

AUTO = "auto"


async def contacted_automatically(session: AsyncSession, since: datetime) -> int:
    return (
        await session.scalar(
            select(func.count(Event.id)).where(
                Event.type == EventType.STATUS_CHANGED.value,
                Event.created_at >= since,
                Event.payload["to"].as_string() == LeadStatus.CONTACTED.value,
                Event.payload["via"].as_string() == AUTO,
            )
        )
        or 0
    )


async def auto_contact(
    sessionmaker: async_sessionmaker[AsyncSession], desk: Desk, *, daily_limit: int, timezone: str
) -> int:
    sent = 0
    async with sessionmaker() as session:
        if desk.mailer is None or not await is_auto(session):
            return 0
        room = daily_limit - await contacted_automatically(session, start_of_day(timezone))
        if room <= 0:
            return 0
        preferred = best_variant(await collect_stats(session))
        leads = (
            await session.scalars(
                select(Lead)
                .where(Lead.status == LeadStatus.DRAFTED)
                .order_by(Lead.score.desc(), Lead.id)
            )
        ).all()

        for lead in leads:
            if sent >= room:
                break
            email = formatting.email_contact(lead)
            drafts = await proposal_drafts(session, lead.id)
            if not email or not drafts:
                continue
            draft = next((d for d in drafts if d.variant == preferred), drafts[0])
            subject = formatting.email_subject(lead)
            try:
                message_id = await desk.mailer.send(email, subject, draft.text)
            except (smtplib.SMTPException, OSError) as exc:
                log.error("email_failed", lead_id=lead.id, error=str(exc)[:300])
                continue

            draft.chosen = True
            set_status(session, lead, LeadStatus.APPROVED, variant=str(draft.variant))
            record_event(
                session,
                EventType.MESSAGE_SENT,
                lead.id,
                channel="smtp",
                kind="proposal",
                to=email,
                subject=subject,
                text=draft.text,
            )
            mark_contacted(session, lead, via=AUTO)
            conversation = await record_sent(
                session, lead, draft.text, channel=EMAIL, external_id=message_id
            )
            await session.commit()
            await send_html(
                desk.bot,
                desk.chat_id,
                formatting.format_sent_notice(lead, conversation, draft.text, first=True),
            )
            sent += 1

    if sent:
        log.info("auto_contacted", count=sent)
    return sent
