import re
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from telegram import ForceReply, Update
from telegram.ext import ContextTypes

from leadhunter import state
from leadhunter.agent import conversation as talk
from leadhunter.agent.report import build_report, start_of_day
from leadhunter.bot.delivery import reply_html
from leadhunter.config import load_profile
from leadhunter.db.models import EventType, Lead, Message, MessageKind
from leadhunter.llm.client import LLMClient
from leadhunter.llm.providers import LLMError
from leadhunter.llm.selection import active_llm
from leadhunter.tracker.events import record_event

log = structlog.get_logger()

CLIENT_PROMPT = re.compile(
    r"^(Client question · lead|Reply to client · lead|Answer for lead|Your reply for lead) #(\d+)"
)
OWN_REPLY = "Your reply for lead"


def desk_for(context: ContextTypes.DEFAULT_TYPE) -> talk.Desk:
    data = context.bot_data
    if data.get("profile") is None:
        data["profile"] = load_profile(data["settings"].profile_path)
    return talk.Desk(
        bot=context.bot,
        chat_id=data["owner_chat_id"],
        mailer=data.get("mailer"),
        profile=data["profile"],
        knowledge_path=data["settings"].knowledge_path,
    )


async def connected_llm(session: AsyncSession) -> LLMClient | None:
    try:
        return await active_llm(session)
    except LLMError:
        return None


async def instruct(update: Update, context, session: AsyncSession, lead_id: int, text: str) -> None:
    conversation = await talk.conversation_for(session, lead_id)
    if conversation is None:
        await reply_html(
            update,
            f"Lead #{lead_id} has no conversation yet: nothing has been sent to that client.",
        )
        return
    await talk.owner_instruction(
        session,
        await connected_llm(session),
        desk_for(context),
        conversation,
        text,
        auto=await state.is_auto(session),
    )


async def client_said(
    update: Update, context, session: AsyncSession, lead_id: int, text: str
) -> None:
    lead = await session.get(Lead, lead_id)
    if lead is None:
        await reply_html(update, f"There is no lead #{lead_id}.")
        return
    conversation = await talk.ensure_conversation(session, lead, talk.MANUAL)
    await talk.client_message(
        session,
        await connected_llm(session),
        desk_for(context),
        conversation,
        text,
        auto=await state.is_auto(session),
    )


async def on_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt_text: str) -> bool:
    prompt = CLIENT_PROMPT.match(prompt_text)
    if not prompt:
        return False
    lead_id = int(prompt.group(2))
    text = update.effective_message.text.strip()

    async with context.bot_data["sessionmaker"]() as session:
        if prompt.group(1) != OWN_REPLY:
            await instruct(update, context, session, lead_id, text)
            return True
        conversation = await talk.conversation_for(session, lead_id)
        if conversation is None:
            await reply_html(update, f"Lead #{lead_id} has no conversation yet.")
            return True
        await reply_html(
            update, escape(await talk.owner_text(session, desk_for(context), conversation, text))
        )
    return True


async def ask_for_answer(update, context, session, lead_id) -> None:
    await update.callback_query.answer()
    await update.effective_message.reply_text(
        f"Answer for lead #{lead_id}: reply to this message with your answer, for example the "
        "price. I will write it to the client.",
        reply_markup=ForceReply(selective=True),
    )


async def ask_for_own_reply(update, context, session, lead_id) -> None:
    await update.callback_query.answer()
    await update.effective_message.reply_text(
        f"Your reply for lead #{lead_id}: reply to this message with the exact text for the "
        "client.",
        reply_markup=ForceReply(selective=True),
    )


async def _pending_draft(query, session: AsyncSession, message_id: str) -> Message | None:
    draft = await session.get(Message, int(message_id))
    if draft is None or draft.kind != MessageKind.DRAFT:
        await query.answer("This reply was already handled.", show_alert=True)
        await query.edit_message_reply_markup(None)
        return None
    return draft


async def send_reply(update, context, session, message_id) -> None:
    query = update.callback_query
    draft = await _pending_draft(query, session, message_id)
    if draft is None:
        return
    error = await talk.send_draft(session, desk_for(context), draft)
    if error:
        await query.answer()
        await reply_html(update, escape(error))
        return
    await query.answer("Done.")
    await query.edit_message_reply_markup(None)


async def skip_reply(update, context, session, message_id) -> None:
    query = update.callback_query
    draft = await _pending_draft(query, session, message_id)
    if draft is None:
        return
    await session.delete(draft)
    await session.commit()
    await query.answer("Skipped. Nothing was sent.")
    await query.edit_message_reply_markup(None)


BUTTONS = {
    "cans": ask_for_answer,
    "cown": ask_for_own_reply,
    "csend": send_reply,
    "cskip": skip_reply,
}


async def auto_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    choice = context.args[0].lower() if context.args else ""
    async with context.bot_data["sessionmaker"]() as session:
        if choice not in ("on", "off"):
            current = "on" if await state.is_auto(session) else "off"
            await reply_html(
                update, f"Automatic sending is {current}. Change it with /auto on|off."
            )
            return
        await state.set_state(session, state.AUTO, choice == "on")
        record_event(session, EventType.SETTING_CHANGED, key=state.AUTO, value=choice == "on")
        await session.commit()

    limit = context.bot_data["settings"].auto_daily_limit
    if choice == "off":
        await reply_html(update, "Automatic sending is off. I ask you before every message.")
    elif context.bot_data.get("mailer") is None:
        await reply_html(
            update,
            "Automatic sending is on, but email is not set up (SMTP_* in .env), so I still "
            "cannot send anything myself.",
        )
    else:
        await reply_html(
            update,
            f"Automatic sending is on. I email the first message to leads that give an "
            f"address (at most {limit} a day) and answer clients myself. Prices, dates and "
            "commitments still come to you first.",
        )


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    timezone = context.bot_data["settings"].timezone
    day = datetime.now(ZoneInfo(timezone)).strftime("%a %d %b")
    async with context.bot_data["sessionmaker"]() as session:
        messages = await build_report(session, since=start_of_day(timezone), day=day)
    for text in messages:
        await reply_html(update, text)


async def client_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if len(args) < 2 or not args[0].lstrip("#").isdigit():
        await reply_html(
            update, "Usage: /client &lt;lead id&gt; &lt;what the client wrote&gt;  (see /leads)"
        )
        return
    text = update.effective_message.text.split(None, 2)[2].strip()
    async with context.bot_data["sessionmaker"]() as session:
        await client_said(update, context, session, int(args[0].lstrip("#")), text)
