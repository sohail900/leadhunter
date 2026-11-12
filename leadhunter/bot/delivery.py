import asyncio

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram import Bot, LinkPreviewOptions, Update
from telegram.constants import ParseMode

from leadhunter.bot import formatting
from leadhunter.db.models import PROPOSAL_VARIANTS, Draft, EventType, Lead, LeadStatus
from leadhunter.tracker.events import record_event, set_status

log = structlog.get_logger()

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
SECONDS_BETWEEN_CARDS = 1.1


async def send_html(bot: Bot, chat_id: int, text: str, reply_markup=None) -> None:
    await bot.send_message(
        chat_id,
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
        link_preview_options=NO_PREVIEW,
    )


async def reply_html(update: Update, text: str, reply_markup=None) -> None:
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=reply_markup, link_preview_options=NO_PREVIEW
    )


async def proposal_drafts(session: AsyncSession, lead_id: int) -> list[Draft]:
    rows = await session.scalars(
        select(Draft)
        .where(Draft.lead_id == lead_id, Draft.variant.in_(PROPOSAL_VARIANTS))
        .order_by(Draft.id)
    )
    return list(rows)


async def send_lead_card(
    bot: Bot, chat_id: int, session: AsyncSession, lead: Lead, can_email: bool
) -> None:
    drafts = await proposal_drafts(session, lead.id)
    messages = formatting.format_lead_card(lead, drafts, can_email)
    for text in messages[:-1]:
        await send_html(bot, chat_id, text)
    await send_html(bot, chat_id, messages[-1], formatting.lead_keyboard(lead.id, len(drafts)))

    set_status(session, lead, LeadStatus.SENT_TO_ME)
    record_event(session, EventType.MESSAGE_SENT, lead.id, channel="telegram", kind="lead_card")


async def send_drafted_leads(
    bot: Bot,
    chat_id: int,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    can_email: bool,
    min_score: float | None = None,
    digest: bool = False,
) -> int:
    async with sessionmaker() as session:
        query = select(Lead).where(Lead.status == LeadStatus.DRAFTED)
        if min_score is not None:
            query = query.where(Lead.score >= min_score)
        leads = (await session.scalars(query.order_by(Lead.score.desc(), Lead.id))).all()
        if not leads:
            return 0

        if digest:
            noun = "lead" if len(leads) == 1 else "leads"
            await send_html(bot, chat_id, f"<b>Digest: {len(leads)} {noun} to review</b>")
        for i, lead in enumerate(leads):
            if i:
                await asyncio.sleep(SECONDS_BETWEEN_CARDS)
            await send_lead_card(bot, chat_id, session, lead, can_email)
            await session.commit()

    log.info("cards_sent", count=len(leads), digest=digest)
    return len(leads)


async def send_followup_card(
    bot: Bot, chat_id: int, session: AsyncSession, lead: Lead, draft: Draft, can_email: bool
) -> None:
    await send_html(
        bot,
        chat_id,
        formatting.format_followup_card(lead, draft, can_email),
        formatting.followup_keyboard(lead.id, draft.id),
    )
    record_event(session, EventType.MESSAGE_SENT, lead.id, channel="telegram", kind="followup_card")
