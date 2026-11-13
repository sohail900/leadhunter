from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from leadhunter.agent.conversation import last_text, messages_of
from leadhunter.agent.report import day_counts
from leadhunter.bot import formatting
from leadhunter.config import Profile
from leadhunter.db.models import Conversation, ConversationState, Lead, LeadStatus, MessageKind
from leadhunter.knowledge import reference_for
from leadhunter.llm.client import LLMClient
from leadhunter.llm.prompts import knowledge_section, load_prompt, render_profile
from leadhunter.state import get_threshold, is_auto, is_paused

MAX_LEADS = 25
MAX_CONVERSATIONS = 15
SHOWN_STATUSES = (
    LeadStatus.DRAFTED,
    LeadStatus.SENT_TO_ME,
    LeadStatus.APPROVED,
    LeadStatus.CONTACTED,
    LeadStatus.REPLIED,
    LeadStatus.WON,
)

Action = Literal[
    "answer",
    "leads",
    "stats",
    "report",
    "lead",
    "tell_client",
    "client_said",
    "won",
    "lost",
    "pause",
    "resume",
    "out_of_scope",
]


class Intent(BaseModel):
    action: Action
    lead_id: int | None = Field(None, description="The lead number, when the action needs one")
    text: str = Field("", description="The answer, the instruction or the client's words")


async def snapshot(session: AsyncSession, *, since: datetime, default_threshold: int) -> str:
    counts = await day_counts(session, since)
    lines = [
        f"Automatic sending: {'on' if await is_auto(session) else 'off'}. "
        f"Paused: {'yes' if await is_paused(session) else 'no'}. "
        f"Minimum score: {await get_threshold(session, default_threshold)}.",
        f"Today: {counts['found']} projects found, {counts['qualified']} passed scoring, "
        f"{counts['sent']} messages sent to clients, {counts['replies']} client replies, "
        f"{counts['deals']} deals agreed.",
        "",
        "Leads (number, status, score, source, title, contact, summary):",
    ]
    leads = (
        await session.scalars(
            select(Lead)
            .where(Lead.status.in_(SHOWN_STATUSES))
            .order_by(Lead.id.desc())
            .limit(MAX_LEADS)
        )
    ).all()
    for lead in leads:
        score = "?" if lead.score is None else f"{lead.score:g}"
        summary = (lead.score_reasons or {}).get("summary") or ""
        lines.append(
            f"#{lead.id} | {lead.status.replace('_', ' ')} | {score}/10 | {lead.source} | "
            f"{formatting.shorten(lead.title, 90)} | {lead.contact_hint or 'no contact'} | "
            f"{formatting.shorten(summary, 120)}"
        )
    if not leads:
        lines.append("none")

    lines += ["", "Conversations with clients (lead number, client, state, details):"]
    rows = (
        await session.execute(
            select(Conversation)
            .where(Conversation.state != ConversationState.CLOSED)
            .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
            .limit(MAX_CONVERSATIONS)
        )
    ).scalars()
    listed = False
    for conversation in rows:
        listed = True
        messages = await messages_of(session, conversation.id)
        client = " ".join(p for p in (conversation.client_name, conversation.address) if p)
        details = [f"project: {formatting.shorten(conversation.brief or '', 200)}"]
        if conversation.question:
            details.append(
                f"waiting for the owner on: {formatting.shorten(conversation.question, 200)}"
            )
        received = last_text(messages, MessageKind.CLIENT)
        if received:
            details.append(f"client last wrote: {formatting.shorten(received, 250)}")
        sent = last_text(messages, MessageKind.SENT)
        if sent:
            details.append(f"last message sent: {formatting.shorten(sent, 250)}")
        lines.append(
            f"#{conversation.lead_id} | {client or 'unknown'} | "
            f"{formatting.STATE_NAMES[conversation.state]} | " + " | ".join(details)
        )
    if not listed:
        lines.append("none")
    return "\n".join(lines)


async def understand(
    session: AsyncSession,
    llm: LLMClient,
    profile: Profile,
    knowledge_path: Path,
    message: str,
    *,
    since: datetime,
    default_threshold: int,
) -> Intent:
    system = load_prompt("assistant_system").format(
        snapshot=await snapshot(session, since=since, default_threshold=default_threshold),
        profile=render_profile(profile),
        knowledge=knowledge_section(await reference_for(session, knowledge_path, message)),
    )
    return await llm.complete_json(system, f"The owner's message:\n{message}", Intent)
