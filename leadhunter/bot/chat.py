from html import escape

import structlog
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from leadhunter.agent import assistant
from leadhunter.agent import conversation as talk
from leadhunter.agent.report import start_of_day
from leadhunter.bot import deals, formatting, handlers
from leadhunter.bot.delivery import reply_html
from leadhunter.db.models import Lead, LeadStatus
from leadhunter.llm.providers import LLMError
from leadhunter.llm.selection import active_llm

log = structlog.get_logger()

NEEDS_LEAD = ("lead", "tell_client", "client_said", "won", "lost")
NEEDS_TEXT = ("tell_client", "client_said")
WHICH_LEAD = "Which lead? Give me its number, for example “lead 12”. /leads lists them."


async def _show_lead(update: Update, context, session, lead_id: int) -> None:
    lead = await session.get(Lead, lead_id)
    if lead is None:
        await reply_html(update, f"There is no lead #{lead_id}.")
        return
    conversation = await talk.conversation_for(session, lead.id)
    messages = await talk.messages_of(session, conversation.id) if conversation else []
    can_email = context.bot_data.get("mailer") is not None
    await reply_html(
        update, formatting.format_conversation(lead, conversation, messages, can_email)
    )


async def _act(update: Update, context, session, intent: assistant.Intent) -> None:
    action, lead_id, text = intent.action, intent.lead_id, intent.text.strip()
    if action == "out_of_scope":
        await reply_html(update, formatting.REFUSAL)
    elif action == "answer":
        await reply_html(update, escape(text[:3500]) if text else formatting.REFUSAL)
    elif action == "leads":
        await handlers.leads_command(update, context)
    elif action == "stats":
        await handlers.stats_command(update, context)
    elif action == "report":
        await deals.report_command(update, context)
    elif action in ("pause", "resume"):
        await handlers.set_paused(update, context, action == "pause")
    elif action in NEEDS_LEAD and lead_id is None:
        await reply_html(update, WHICH_LEAD)
    elif action in NEEDS_TEXT and not text:
        await reply_html(update, "What should I pass on? Write it after the lead number.")
    elif action == "lead":
        await _show_lead(update, context, session, lead_id)
    elif action == "tell_client":
        await deals.instruct(update, context, session, lead_id, text)
    elif action == "client_said":
        await deals.client_said(update, context, session, lead_id, text)
    elif action == "won":
        await handlers.set_outcome(
            update, context, lead_id, LeadStatus.WON, "marked as won. Nice work"
        )
    elif action == "lost":
        await handlers.set_outcome(update, context, lead_id, LeadStatus.LOST, "marked as lost")


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.bot_data
    settings = data["settings"]
    text = update.effective_message.text.strip()
    async with data["sessionmaker"]() as session:
        try:
            llm = await active_llm(session)
        except LLMError:
            await reply_html(
                update,
                "No model is connected, so I only understand commands. Send /connect, or "
                "/help for the commands.",
            )
            return
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        try:
            intent = await assistant.understand(
                session,
                llm,
                deals.desk_for(context).profile,
                settings.knowledge_path,
                text,
                since=start_of_day(settings.timezone),
                default_threshold=settings.score_threshold,
            )
        except LLMError as exc:
            log.error("assistant_failed", error=str(exc)[:300])
            await reply_html(
                update, "The model call failed, so I could not read that. /help lists the commands."
            )
            return
        log.info("assistant_intent", action=intent.action, lead_id=intent.lead_id)
        await _act(update, context, session, intent)


async def on_text_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await handlers.on_text_reply(update, context):
        await on_message(update, context)
