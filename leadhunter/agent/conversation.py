import smtplib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Bot

from leadhunter.agent.guard import unapproved_amounts
from leadhunter.bot import formatting
from leadhunter.bot.delivery import send_html
from leadhunter.config import Profile
from leadhunter.db.models import (
    Conversation,
    ConversationState,
    EventType,
    Lead,
    LeadStatus,
    Message,
    MessageKind,
)
from leadhunter.knowledge import reference_for
from leadhunter.llm.client import LLMClient
from leadhunter.llm.prompts import knowledge_section, load_prompt, render_profile
from leadhunter.llm.providers import LLMError
from leadhunter.mailer import Mailer
from leadhunter.pipeline.drafter import lead_query
from leadhunter.pipeline.scorer import MAX_BODY_CHARS
from leadhunter.tracker.events import record_event, set_status

log = structlog.get_logger()

EMAIL, MANUAL = "email", "manual"
MAX_TRANSCRIPT_MESSAGES = 30
MAX_MESSAGE_CHARS = 3000
_LABELS = {
    MessageKind.CLIENT: "Client",
    MessageKind.SENT: "Freelancer",
    MessageKind.OWNER: "Owner instruction (private, the client never sees this)",
}


@dataclass
class Desk:
    bot: Bot
    chat_id: int
    mailer: Mailer | None
    profile: Profile
    knowledge_path: Path


class Decision(BaseModel):
    action: Literal["reply", "ask_owner", "deal", "lost"]
    message: str = Field("", description="The next message to the client, ready to send")
    owner_question: str = Field("", description="For ask_owner: what the owner must decide")
    brief: str = Field("", description="What the client wants built, in 2 to 5 sentences")
    client_name: str = Field("", description="The client's name, if they gave it")


async def conversation_for(session: AsyncSession, lead_id: int) -> Conversation | None:
    return await session.scalar(select(Conversation).where(Conversation.lead_id == lead_id))


async def messages_of(session: AsyncSession, conversation_id: int) -> list[Message]:
    rows = await session.scalars(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.id)
    )
    return list(rows)


def last_text(messages: list[Message], kind: MessageKind) -> str:
    return next((m.text for m in reversed(messages) if m.kind == kind), "")


def sends_email(desk: Desk, conversation: Conversation) -> bool:
    return bool(conversation.channel == EMAIL and conversation.address and desk.mailer)


def _add(
    session: AsyncSession,
    conversation: Conversation,
    kind: MessageKind,
    text: str,
    external_id: str | None = None,
) -> Message:
    message = Message(
        conversation_id=conversation.id, kind=kind, text=text, external_id=external_id
    )
    session.add(message)
    conversation.updated_at = datetime.now(UTC)
    return message


async def _discard_drafts(session: AsyncSession, conversation: Conversation) -> None:
    await session.execute(
        delete(Message).where(
            Message.conversation_id == conversation.id, Message.kind == MessageKind.DRAFT
        )
    )


async def ensure_conversation(session: AsyncSession, lead: Lead, channel: str) -> Conversation:
    conversation = await conversation_for(session, lead.id)
    if conversation is None:
        email = formatting.email_contact(lead)
        conversation = Conversation(
            lead_id=lead.id,
            channel=channel,
            address=email.lower() if channel == EMAIL and email else lead.contact_hint,
            client_name=lead.author,
            subject=formatting.email_subject(lead),
        )
        session.add(conversation)
        await session.flush()
    return conversation


async def record_sent(
    session: AsyncSession, lead: Lead, text: str, *, channel: str, external_id: str | None = None
) -> Conversation:
    conversation = await ensure_conversation(session, lead, channel)
    _add(session, conversation, MessageKind.SENT, text, external_id)
    await session.flush()
    return conversation


def render_transcript(messages: list[Message]) -> str:
    shown = [m for m in messages if m.kind in _LABELS][-MAX_TRANSCRIPT_MESSAGES:]
    return "\n\n".join(f"{_LABELS[m.kind]}:\n{m.text[:MAX_MESSAGE_CHARS]}" for m in shown)


def _closing(messages: list[Message]) -> str:
    shown = [m for m in messages if m.kind in _LABELS]
    if shown and shown[-1].kind == MessageKind.OWNER:
        return (
            "The last entry is an instruction from the owner. Write the message to the client "
            'that carries it out now, with action "reply" or "deal". Do not ask the owner to '
            'confirm what they have just told you; choose "ask_owner" only if the instruction '
            "leaves the client's question unanswered."
        )
    return "Decide the next step."


def build_reply_prompts(
    profile: Profile, lead: Lead, messages: list[Message], reference: str = ""
) -> tuple[str, str]:
    system = load_prompt("reply_system").format(
        profile=render_profile(profile), knowledge=knowledge_section(reference)
    )
    user = (
        f"Source: {lead.source}\n"
        f"Title: {lead.title}\n\n"
        f"The job post:\n{lead.body[:MAX_BODY_CHARS]}\n\n"
        f"---\nThe conversation so far, oldest first:\n\n{render_transcript(messages)}\n\n"
        f"---\n{_closing(messages)}"
    )
    return system, user


def enforce(decision: Decision, messages: list[Message]) -> Decision:
    instructions = [m.text for m in messages if m.kind == MessageKind.OWNER]
    approved = instructions + [m.text for m in messages if m.kind == MessageKind.SENT]

    def ask(question: str) -> Decision:
        return decision.model_copy(
            update={"action": "ask_owner", "message": "", "owner_question": question}
        )

    if decision.action in ("reply", "deal"):
        if not decision.message.strip():
            return ask(
                decision.owner_question
                or "I could not write a reply to the client's last message. What should I say?"
            )
        figures = unapproved_amounts(decision.message, approved)
        if figures:
            return ask(
                f"My reply would have mentioned {', '.join(figures)}, a figure you have not "
                "given me. What price or terms should I give the client?"
            )
        if decision.action == "deal" and not instructions:
            return ask(
                "The client seems ready to go ahead, but you have not given me a price or "
                "terms yet. What should I confirm?"
            )
    if decision.action == "ask_owner" and not decision.owner_question.strip():
        return ask("The client's last message needs your decision.")
    return decision


async def decide(
    session: AsyncSession, llm: LLMClient, desk: Desk, lead: Lead, messages: list[Message]
) -> Decision:
    query = f"{last_text(messages, MessageKind.CLIENT)}\n{lead_query(lead)}"
    reference = await reference_for(session, desk.knowledge_path, query)
    system, user = build_reply_prompts(desk.profile, lead, messages, reference)
    return enforce(await llm.complete_json(system, user, Decision), messages)


async def ask_owner(
    session: AsyncSession,
    desk: Desk,
    lead: Lead,
    conversation: Conversation,
    client_text: str,
    question: str,
) -> None:
    if conversation.state not in (ConversationState.AGREED, ConversationState.CLOSED):
        conversation.state = ConversationState.WAITING_OWNER
    conversation.question = question
    await session.commit()
    await send_html(
        desk.bot,
        desk.chat_id,
        formatting.format_owner_question(lead, conversation, client_text, question),
        formatting.question_keyboard(lead.id),
    )


async def send_now(
    session: AsyncSession, desk: Desk, lead: Lead, conversation: Conversation, text: str
) -> str | None:
    messages = await messages_of(session, conversation.id)
    threaded = [m.external_id for m in messages if (m.external_id or "").startswith("<")]
    try:
        message_id = await desk.mailer.send(
            conversation.address,
            conversation.subject,
            text,
            reply_to=threaded[-1] if threaded else None,
        )
    except (smtplib.SMTPException, OSError) as exc:
        log.error("email_failed", lead_id=lead.id, error=str(exc)[:300])
        return (
            f"Email to {conversation.address} failed ({type(exc).__name__}), so nothing was sent."
        )
    _add(session, conversation, MessageKind.SENT, text, message_id)
    record_event(
        session,
        EventType.MESSAGE_SENT,
        lead.id,
        channel="smtp",
        kind="reply",
        to=conversation.address,
        text=text,
    )
    return None


async def deliver(
    session: AsyncSession,
    desk: Desk,
    lead: Lead,
    conversation: Conversation,
    text: str,
    client_text: str,
    *,
    auto: bool,
) -> None:
    by_email = sends_email(desk, conversation)
    note = ""
    if auto and by_email:
        note = await send_now(session, desk, lead, conversation, text) or ""
        if not note:
            await session.commit()
            await send_html(
                desk.bot,
                desk.chat_id,
                formatting.format_sent_notice(lead, conversation, text, first=False),
            )
            return

    draft = _add(session, conversation, MessageKind.DRAFT, text)
    await session.commit()
    await send_html(
        desk.bot,
        desk.chat_id,
        formatting.format_reply_draft(lead, conversation, client_text, text, by_email, note),
        formatting.draft_keyboard(draft.id, lead.id, by_email),
    )


async def advance(
    session: AsyncSession,
    llm: LLMClient | None,
    desk: Desk,
    conversation: Conversation,
    *,
    auto: bool,
) -> None:
    lead = await session.get(Lead, conversation.lead_id)
    messages = await messages_of(session, conversation.id)
    client_text = last_text(messages, MessageKind.CLIENT)

    if llm is None:
        await ask_owner(
            session,
            desk,
            lead,
            conversation,
            client_text,
            "No model is connected, so I cannot write a reply. Send /connect, or write the "
            "reply yourself.",
        )
        return
    try:
        decision = await decide(session, llm, desk, lead, messages)
    except LLMError as exc:
        log.error("reply_failed", lead_id=lead.id, error=str(exc)[:300])
        await ask_owner(
            session,
            desk,
            lead,
            conversation,
            client_text,
            "The model call failed, so I could not write a reply. What should I tell the client?",
        )
        return

    if decision.brief.strip():
        conversation.brief = decision.brief.strip()
    if decision.client_name.strip():
        conversation.client_name = decision.client_name.strip()[:255]

    if decision.action == "ask_owner":
        await ask_owner(session, desk, lead, conversation, client_text, decision.owner_question)
        return

    conversation.question = None
    if decision.action == "lost":
        conversation.state = ConversationState.CLOSED
        if lead.status != LeadStatus.LOST:
            set_status(session, lead, LeadStatus.LOST, via="conversation")
        await session.commit()
        await send_html(
            desk.bot, desk.chat_id, formatting.format_client_lost(lead, conversation, client_text)
        )
        return

    if decision.action == "deal":
        conversation.state = ConversationState.AGREED
        if lead.status != LeadStatus.WON:
            set_status(session, lead, LeadStatus.WON, via="conversation")
    elif conversation.state != ConversationState.AGREED:
        conversation.state = ConversationState.ACTIVE
    await deliver(
        session, desk, lead, conversation, decision.message.strip(), client_text, auto=auto
    )

    if decision.action == "deal":
        instructions = [m.text for m in messages if m.kind == MessageKind.OWNER]
        await send_html(
            desk.bot,
            desk.chat_id,
            formatting.format_deal(lead, conversation, instructions, client_text),
        )


async def client_message(
    session: AsyncSession,
    llm: LLMClient | None,
    desk: Desk,
    conversation: Conversation,
    text: str,
    *,
    external_id: str | None = None,
    auto: bool,
) -> bool:
    if external_id and await session.scalar(
        select(Message.id).where(
            Message.conversation_id == conversation.id, Message.external_id == external_id
        )
    ):
        return False

    await _discard_drafts(session, conversation)
    _add(session, conversation, MessageKind.CLIENT, text, external_id)
    lead = await session.get(Lead, conversation.lead_id)
    if lead.status in (LeadStatus.APPROVED, LeadStatus.CONTACTED):
        set_status(session, lead, LeadStatus.REPLIED, via=conversation.channel)
    await session.flush()

    if conversation.state in (ConversationState.AGREED, ConversationState.CLOSED):
        await ask_owner(
            session,
            desk,
            lead,
            conversation,
            text,
            f"This conversation is already marked {formatting.STATE_NAMES[conversation.state]}, "
            "and the client wrote again. Tell me what to answer, or write the reply yourself.",
        )
    else:
        await advance(session, llm, desk, conversation, auto=auto)
    return True


async def owner_instruction(
    session: AsyncSession,
    llm: LLMClient | None,
    desk: Desk,
    conversation: Conversation,
    text: str,
    *,
    auto: bool,
) -> None:
    await _discard_drafts(session, conversation)
    _add(session, conversation, MessageKind.OWNER, text)
    if conversation.state == ConversationState.CLOSED:
        conversation.state = ConversationState.ACTIVE
    await session.flush()
    await advance(session, llm, desk, conversation, auto=auto)


async def owner_text(
    session: AsyncSession, desk: Desk, conversation: Conversation, text: str
) -> str:
    lead = await session.get(Lead, conversation.lead_id)
    await _discard_drafts(session, conversation)
    if sends_email(desk, conversation):
        error = await send_now(session, desk, lead, conversation, text)
        if error:
            await session.commit()
            return error
        done = f"Sent to {conversation.address} (lead #{lead.id})."
    else:
        _add(session, conversation, MessageKind.SENT, text)
        record_event(session, EventType.MESSAGE_SENT, lead.id, channel="manual", kind="reply")
        done = (
            f"Recorded as sent for lead #{lead.id}. I cannot send on this channel, so send it "
            "yourself."
        )
    if conversation.state in (ConversationState.WAITING_OWNER, ConversationState.CLOSED):
        conversation.state = ConversationState.ACTIVE
    conversation.question = None
    await session.commit()
    return done


async def send_draft(session: AsyncSession, desk: Desk, draft: Message) -> str | None:
    conversation = await session.get(Conversation, draft.conversation_id)
    lead = await session.get(Lead, conversation.lead_id)
    if sends_email(desk, conversation):
        error = await send_now(session, desk, lead, conversation, draft.text)
        if error:
            await session.commit()
            return error
        await session.delete(draft)
    else:
        draft.kind = MessageKind.SENT
        record_event(session, EventType.MESSAGE_SENT, lead.id, channel="manual", kind="reply")
    await session.commit()
    return None
