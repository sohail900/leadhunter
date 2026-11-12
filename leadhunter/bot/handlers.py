import re
import smtplib

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from telegram import ForceReply, Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from leadhunter import state
from leadhunter.bot import connect, formatting
from leadhunter.bot.delivery import proposal_drafts
from leadhunter.bot.delivery import reply_html as _reply
from leadhunter.db.models import Draft, DraftVariant, EventType, Lead, LeadStatus
from leadhunter.tracker.events import record_event, set_status
from leadhunter.tracker.followups import FOLLOWUP_AFTER_DAYS, mark_contacted
from leadhunter.tracker.stats import collect_stats

log = structlog.get_logger()

AWAITING_ME = (LeadStatus.SENT_TO_ME, LeadStatus.APPROVED)
OPEN_STATUSES = (
    LeadStatus.SENT_TO_ME,
    LeadStatus.APPROVED,
    LeadStatus.CONTACTED,
    LeadStatus.REPLIED,
)
EDIT_PROMPT = re.compile(r"^(Proposal|Follow-up) for lead #(\d+)")


async def owner_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or chat.id != context.bot_data["owner_chat_id"]:
        log.warning("unauthorized_update", chat_id=chat.id if chat else None)
        raise ApplicationHandlerStop


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("telegram_handler_error", exc_info=context.error)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, formatting.HELP_TEXT)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with context.bot_data["sessionmaker"]() as session:
        stats = await collect_stats(session)
    await _reply(update, formatting.format_stats(stats))


async def leads_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with context.bot_data["sessionmaker"]() as session:
        leads = (
            await session.scalars(
                select(Lead)
                .where(Lead.status.in_(OPEN_STATUSES))
                .order_by(Lead.id.desc())
                .limit(30)
            )
        ).all()
        waiting = await session.scalar(
            select(func.count(Lead.id)).where(Lead.status == LeadStatus.DRAFTED)
        )
    await _reply(update, formatting.format_leads_list(list(leads), waiting or 0))


async def _set_outcome(update: Update, context, status: LeadStatus, done: str) -> None:
    command = update.effective_message.text.split()[0]
    if not context.args or not context.args[0].lstrip("#").isdigit():
        await _reply(update, f"Usage: {command} &lt;lead id&gt;  (see /leads)")
        return
    lead_id = int(context.args[0].lstrip("#"))
    async with context.bot_data["sessionmaker"]() as session:
        lead = await session.get(Lead, lead_id)
        if lead is None:
            await _reply(update, f"There is no lead #{lead_id}.")
            return
        if lead.status == status:
            await _reply(update, f"Lead #{lead_id} is already marked {status.value}.")
            return
        set_status(session, lead, status)
        await session.commit()
    await _reply(update, f"Lead #{lead_id} {done}.")


async def replied_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_outcome(update, context, LeadStatus.REPLIED, "marked as replied")


async def won_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_outcome(update, context, LeadStatus.WON, "marked as won. Nice work")


async def lost_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_outcome(update, context, LeadStatus.LOST, "marked as lost")


async def _set_paused(update: Update, context, paused: bool) -> None:
    async with context.bot_data["sessionmaker"]() as session:
        await state.set_state(session, state.PAUSED, paused)
        record_event(session, EventType.SETTING_CHANGED, key=state.PAUSED, value=paused)
        await session.commit()
    if paused:
        await _reply(update, "Paused. Nothing is collected, scored or sent until /resume.")
    else:
        await _reply(update, "Resumed.")


async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_paused(update, context, True)


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_paused(update, context, False)


async def threshold_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    default = context.bot_data["settings"].score_threshold
    async with context.bot_data["sessionmaker"]() as session:
        if not context.args:
            current = await state.get_threshold(session, default)
            await _reply(update, f"Threshold is {current}. Change it with /threshold &lt;0-10&gt;.")
            return
        value = context.args[0]
        if not value.isdigit() or not 0 <= int(value) <= 10:
            await _reply(update, "The threshold must be a whole number from 0 to 10.")
            return
        await state.set_state(session, state.THRESHOLD, int(value))
        record_event(session, EventType.SETTING_CHANGED, key=state.THRESHOLD, value=int(value))
        await session.commit()
    await _reply(update, f"Threshold set to {value}. Leads scoring below it are rejected.")


async def _deliver(
    update: Update, context, session: AsyncSession, lead: Lead, text: str, kind: str
) -> None:
    mailer = context.bot_data.get("mailer")
    email = formatting.email_contact(lead)
    note = ""

    if email and mailer:
        subject = formatting.email_subject(lead)
        try:
            await mailer.send(email, subject, text)
        except (smtplib.SMTPException, OSError) as exc:
            log.error("email_failed", lead_id=lead.id, error=str(exc)[:300])
            note = f"Email to {email} failed ({type(exc).__name__}), so nothing was sent."
        else:
            record_event(
                session,
                EventType.MESSAGE_SENT,
                lead.id,
                channel="smtp",
                kind=kind,
                to=email,
                subject=subject,
                text=text,
            )
            if kind == "proposal":
                mark_contacted(session, lead, via="smtp")
                await session.commit()
                await _reply(
                    update,
                    f"Emailed to {email}. Lead #{lead.id} is marked contacted; I will suggest "
                    f"a follow-up in {FOLLOWUP_AFTER_DAYS} days if there is no reply.",
                )
            else:
                await session.commit()
                await _reply(update, f"Follow-up emailed to {email} (lead #{lead.id}).")
            return

    await session.commit()
    await _reply(
        update,
        formatting.format_to_send_yourself(lead, text, note),
        formatting.mark_sent_keyboard(lead.id, kind),
    )


async def _choose(session: AsyncSession, lead: Lead, draft: Draft) -> None:
    await session.execute(
        update(Draft)
        .where(Draft.lead_id == lead.id, Draft.variant != DraftVariant.FOLLOWUP)
        .values(chosen=False)
    )
    draft.chosen = True
    if lead.status != LeadStatus.APPROVED:
        set_status(session, lead, LeadStatus.APPROVED, variant=str(draft.variant))


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action, _, rest = (query.data or "").partition(":")
    args = rest.split(":") if rest else []
    handler = BUTTONS.get(action)
    if handler is None or not args or not args[0].isdigit():
        await query.answer("Unknown button.")
        return
    async with context.bot_data["sessionmaker"]() as session:
        await handler(update, context, session, *args)


async def _lead_awaiting_me(query, session: AsyncSession, lead_id: str) -> Lead | None:
    lead = await session.get(Lead, int(lead_id))
    if lead is None:
        await query.answer(f"Lead #{lead_id} no longer exists.", show_alert=True)
        return None
    if lead.status not in AWAITING_ME:
        await query.answer(f"Lead #{lead.id} is already {lead.status.value}.", show_alert=True)
        await query.edit_message_reply_markup(None)
        return None
    return lead


async def use_draft(update, context, session, lead_id, number="1") -> None:
    query = update.callback_query
    lead = await _lead_awaiting_me(query, session, lead_id)
    if lead is None:
        return
    drafts = await proposal_drafts(session, lead.id)
    if not number.isdigit() or not 1 <= int(number) <= len(drafts):
        await query.answer("That draft no longer exists.", show_alert=True)
        return
    await query.answer()
    await query.edit_message_reply_markup(None)
    draft = drafts[int(number) - 1]
    await _choose(session, lead, draft)
    await _deliver(update, context, session, lead, draft.text, "proposal")


async def ask_for_edit(update, context, session, lead_id) -> None:
    query = update.callback_query
    lead = await _lead_awaiting_me(query, session, lead_id)
    if lead is None:
        return
    await query.answer()
    await update.effective_message.reply_text(
        f"Proposal for lead #{lead.id}: reply to this message with your own text.",
        reply_markup=ForceReply(selective=True),
    )


async def skip_lead(update, context, session, lead_id) -> None:
    query = update.callback_query
    lead = await _lead_awaiting_me(query, session, lead_id)
    if lead is None:
        return
    set_status(session, lead, LeadStatus.SKIPPED)
    await session.commit()
    await query.answer("Skipped.")
    await query.edit_message_reply_markup(None)


async def mark_sent(update, context, session, lead_id, kind="proposal") -> None:
    query = update.callback_query
    lead = await session.get(Lead, int(lead_id))
    if lead is None:
        await query.answer(f"Lead #{lead_id} no longer exists.", show_alert=True)
        return
    if kind == "proposal":
        if lead.status != LeadStatus.APPROVED:
            await query.answer(f"Lead #{lead.id} is already {lead.status.value}.", show_alert=True)
            await query.edit_message_reply_markup(None)
            return
        mark_contacted(session, lead, via="manual")
        answer = f"Marked contacted. Follow-up suggestion in {FOLLOWUP_AFTER_DAYS} days."
    else:
        answer = "Follow-up marked as sent."
    record_event(session, EventType.MESSAGE_SENT, lead.id, channel="manual", kind=kind)
    await session.commit()
    await query.answer(answer)
    await query.edit_message_reply_markup(None)


async def send_followup(update, context, session, draft_id) -> None:
    query = update.callback_query
    draft = await session.get(Draft, int(draft_id))
    if draft is None or draft.variant != DraftVariant.FOLLOWUP or draft.chosen:
        await query.answer("This follow-up was already handled.", show_alert=True)
        await query.edit_message_reply_markup(None)
        return
    lead = await session.get(Lead, draft.lead_id)
    await query.answer()
    await query.edit_message_reply_markup(None)
    draft.chosen = True
    await _deliver(update, context, session, lead, draft.text, "followup")


async def ask_for_followup_edit(update, context, session, lead_id) -> None:
    await update.callback_query.answer()
    await update.effective_message.reply_text(
        f"Follow-up for lead #{lead_id}: reply to this message with your own text.",
        reply_markup=ForceReply(selective=True),
    )


async def skip_followup(update, context, session, draft_id) -> None:
    query = update.callback_query
    await query.answer("Follow-up skipped.")
    await query.edit_message_reply_markup(None)


BUTTONS = {
    "use": use_draft,
    "edit": ask_for_edit,
    "skip": skip_lead,
    "sent": mark_sent,
    "fu": send_followup,
    "fuedit": ask_for_followup_edit,
    "fuskip": skip_followup,
}


async def on_text_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    prompt_text = message.reply_to_message.text or ""
    if await connect.on_reply(update, context, prompt_text):
        return
    prompt = EDIT_PROMPT.match(prompt_text)
    if not prompt:
        return
    kind = "proposal" if prompt.group(1) == "Proposal" else "followup"
    text = message.text.strip()

    async with context.bot_data["sessionmaker"]() as session:
        lead = await session.get(Lead, int(prompt.group(2)))
        if lead is None:
            await _reply(update, "That lead no longer exists.")
            return

        if kind == "proposal":
            if lead.status not in AWAITING_ME:
                await _reply(update, f"Lead #{lead.id} is already {lead.status.value}.")
                return
            draft = Draft(lead_id=lead.id, variant=DraftVariant.CUSTOM, text=text, edited_text=text)
            session.add(draft)
            await session.flush()
            await _choose(session, lead, draft)
        else:
            session.add(
                Draft(
                    lead_id=lead.id,
                    variant=DraftVariant.FOLLOWUP,
                    text=text,
                    edited_text=text,
                    chosen=True,
                )
            )
        await _deliver(update, context, session, lead, text, kind)
