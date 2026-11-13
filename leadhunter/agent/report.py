from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from leadhunter.agent.conversation import last_text, messages_of
from leadhunter.bot import formatting
from leadhunter.db.models import (
    Conversation,
    ConversationState,
    Event,
    EventType,
    Lead,
    LeadStatus,
    Message,
    MessageKind,
)

MAX_CONVERSATIONS = 25
MAX_NEW_LEADS = 10
NOT_CONTACTED = (
    LeadStatus.SCORED,
    LeadStatus.DRAFTED,
    LeadStatus.SENT_TO_ME,
    LeadStatus.APPROVED,
)


def start_of_day(timezone: str) -> datetime:
    return datetime.now(ZoneInfo(timezone)).replace(hour=0, minute=0, second=0, microsecond=0)


def pack(blocks: list[str], limit: int = formatting.TELEGRAM_LIMIT) -> list[str]:
    messages, current = [], ""
    for block in blocks:
        if current and len(current) + len(block) + 2 > limit:
            messages.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
    return [*messages, current] if current else messages


async def _count(session: AsyncSession, column, *conditions) -> int:
    return await session.scalar(select(func.count(column)).where(*conditions)) or 0


async def day_counts(session: AsyncSession, since: datetime) -> dict[str, int]:
    return {
        "found": await _count(session, Lead.id, Lead.fetched_at >= since),
        "qualified": await _count(
            session,
            Lead.id,
            Lead.fetched_at >= since,
            Lead.score.is_not(None),
            Lead.status != LeadStatus.REJECTED,
        ),
        "sent": await _count(
            session, Message.id, Message.kind == MessageKind.SENT, Message.created_at >= since
        ),
        "replies": await _count(
            session, Message.id, Message.kind == MessageKind.CLIENT, Message.created_at >= since
        ),
        "deals": await _count(
            session,
            Event.id,
            Event.type == EventType.STATUS_CHANGED.value,
            Event.created_at >= since,
            Event.payload["to"].as_string() == LeadStatus.WON.value,
        ),
        "questions": await _count(
            session, Conversation.id, Conversation.state == ConversationState.WAITING_OWNER
        ),
        "cards": await _count(session, Lead.id, Lead.status == LeadStatus.SENT_TO_ME),
    }


def _summary_block(day: str, counts: dict[str, int], by_source: list[tuple[str, int]]) -> str:
    sources = ", ".join(f"{formatting.source_name(name)} {count}" for name, count in by_source)
    waiting = counts["questions"] + counts["cards"]
    lines = [
        f"<b>Daily report · {escape(day)}</b>",
        "",
        f"Projects found today: <b>{counts['found']}</b>"
        + (f" ({escape(sources)})" if sources else ""),
        f"Passed scoring: <b>{counts['qualified']}</b>",
        f"Messages sent to clients: <b>{counts['sent']}</b>",
        f"Client replies: <b>{counts['replies']}</b>",
        f"Deals agreed: <b>{counts['deals']}</b>",
        f"Waiting for you: <b>{waiting}</b>"
        + (
            f" ({counts['questions']} client questions, {counts['cards']} lead cards)"
            if waiting
            else ""
        ),
    ]
    return "\n".join(lines)


def _conversation_block(lead: Lead, conversation: Conversation, messages: list[Message]) -> str:
    lines = [
        f"<b>#{lead.id} · {escape(formatting.shorten(lead.title, 120))}</b>",
        formatting.client_line(conversation),
        f"Status: {formatting.STATE_NAMES[conversation.state]}",
        "Project: "
        + escape(
            formatting.shorten(
                conversation.brief or (lead.score_reasons or {}).get("summary") or lead.title, 350
            )
        ),
    ]
    sent = last_text(messages, MessageKind.SENT)
    lines.append(
        "Last message sent: " + (f"“{escape(formatting.shorten(sent, 400))}”" if sent else "none")
    )
    received = last_text(messages, MessageKind.CLIENT)
    if received:
        lines.append(f"Client's last message: “{escape(formatting.shorten(received, 300))}”")
    if conversation.state == ConversationState.WAITING_OWNER and conversation.question:
        lines.append(f"Needs your answer: {escape(formatting.shorten(conversation.question, 300))}")
    return "\n".join(lines)


def _new_lead_line(lead: Lead) -> str:
    score = "?" if lead.score is None else f"{lead.score:g}"
    summary = (lead.score_reasons or {}).get("summary") or ""
    contact = lead.contact_hint or lead.author or "reply on the post"
    return (
        f"#{lead.id} · {score}/10 · {escape(formatting.shorten(lead.title, 90))}\n"
        f"Contact: {escape(contact)}"
        + (f"\n{escape(formatting.shorten(summary, 160))}" if summary else "")
    )


async def build_report(session: AsyncSession, *, since: datetime, day: str) -> list[str]:
    by_source = (
        await session.execute(
            select(Lead.source, func.count(Lead.id))
            .where(Lead.fetched_at >= since)
            .group_by(Lead.source)
            .order_by(Lead.source)
        )
    ).all()
    blocks = [_summary_block(day, await day_counts(session, since), [tuple(r) for r in by_source])]

    touched_today = select(Message.conversation_id).where(Message.created_at >= since)
    rows = (
        await session.execute(
            select(Conversation, Lead)
            .join(Lead, Lead.id == Conversation.lead_id)
            .where(
                or_(
                    Conversation.id.in_(touched_today),
                    Conversation.state.in_(
                        (ConversationState.ACTIVE, ConversationState.WAITING_OWNER)
                    ),
                )
            )
            .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
            .limit(MAX_CONVERSATIONS)
        )
    ).all()
    if rows:
        blocks.append("<b>Clients I am talking to</b>")
        for conversation, lead in rows:
            messages = await messages_of(session, conversation.id)
            blocks.append(_conversation_block(lead, conversation, messages))

    new_leads = (
        await session.scalars(
            select(Lead)
            .where(Lead.fetched_at >= since, Lead.status.in_(NOT_CONTACTED))
            .order_by(Lead.score.desc(), Lead.id)
            .limit(MAX_NEW_LEADS)
        )
    ).all()
    if new_leads:
        blocks.append("<b>Found today, not contacted yet</b>")
        blocks += [_new_lead_line(lead) for lead in new_leads]
    return pack(blocks)
