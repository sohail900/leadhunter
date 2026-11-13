from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram import Bot

from leadhunter.agent import conversation as talk
from leadhunter.agent.outreach import auto_contact
from leadhunter.agent.report import build_report, start_of_day
from leadhunter.bot import delivery, formatting
from leadhunter.collectors.base import BaseCollector
from leadhunter.config import Profile, Settings
from leadhunter.db.models import Conversation, ConversationState, Lead
from leadhunter.inbox import InboundEmail, InboxReader
from leadhunter.llm.client import LLMClient
from leadhunter.llm.providers import LLMError
from leadhunter.llm.selection import active_llm
from leadhunter.mailer import Mailer
from leadhunter.pipeline.drafter import draft_scored_leads
from leadhunter.pipeline.runner import run_collector
from leadhunter.pipeline.scorer import score_new_leads
from leadhunter.state import is_auto, is_paused
from leadhunter.tracker.followups import draft_due_followups
from leadhunter.tracker.stats import build_guidance, collect_stats

log = structlog.get_logger()

INBOX_LOOKBACK_DAYS = 14


@dataclass
class Deps:
    settings: Settings
    profile: Profile
    sessionmaker: async_sessionmaker[AsyncSession]
    bot: Bot
    can_email: bool
    mailer: Mailer | None = None
    inbox: InboxReader | None = None

    @property
    def chat_id(self) -> int:
        return self.settings.telegram_chat_id

    @property
    def desk(self) -> talk.Desk:
        return talk.Desk(
            bot=self.bot,
            chat_id=self.chat_id,
            mailer=self.mailer,
            profile=self.profile,
            knowledge_path=self.settings.knowledge_path,
        )


async def _paused(deps: Deps) -> bool:
    async with deps.sessionmaker() as session:
        return await is_paused(session)


async def _llm(deps: Deps) -> LLMClient | None:
    async with deps.sessionmaker() as session:
        try:
            return await active_llm(session)
        except LLMError as exc:
            log.info("llm_not_connected", reason=str(exc))
            return None


async def collect(deps: Deps, collector: BaseCollector) -> None:
    if await _paused(deps):
        return
    await run_collector(
        collector, deps.sessionmaker, deps.profile, deps.settings.max_lead_age_hours
    )


async def pipeline(deps: Deps) -> None:
    if await _paused(deps):
        return
    llm = await _llm(deps)
    if llm is None:
        return
    try:
        async with deps.sessionmaker() as session:
            scorer_hint, drafter_hint = build_guidance(await collect_stats(session))

        await score_new_leads(
            deps.sessionmaker,
            llm,
            deps.profile,
            default_threshold=deps.settings.score_threshold,
            max_age_hours=deps.settings.max_lead_age_hours,
            batch_size=deps.settings.score_batch_size,
            guidance=scorer_hint,
        )
        await draft_scored_leads(
            deps.sessionmaker,
            llm,
            deps.profile,
            batch_size=deps.settings.draft_batch_size,
            guidance=drafter_hint,
            knowledge_path=deps.settings.knowledge_path,
        )
        await auto_contact(
            deps.sessionmaker,
            deps.desk,
            daily_limit=deps.settings.auto_daily_limit,
            timezone=deps.settings.timezone,
        )
        await delivery.send_drafted_leads(
            deps.bot,
            deps.chat_id,
            deps.sessionmaker,
            can_email=deps.can_email,
            min_score=deps.settings.urgent_score,
        )
    except Exception:
        log.exception("pipeline_failed")


async def digest(deps: Deps) -> None:
    if await _paused(deps):
        return
    try:
        await delivery.send_drafted_leads(
            deps.bot, deps.chat_id, deps.sessionmaker, can_email=deps.can_email, digest=True
        )
    except Exception:
        log.exception("digest_failed")


async def followups(deps: Deps) -> None:
    if await _paused(deps):
        return
    llm = await _llm(deps)
    if llm is None:
        return
    try:
        async with deps.sessionmaker() as session:
            drafts = await draft_due_followups(session, llm, deps.profile)
            await session.commit()
            for draft in drafts:
                lead = await session.get(Lead, draft.lead_id)
                await delivery.send_followup_card(
                    deps.bot, deps.chat_id, session, lead, draft, deps.can_email
                )
                await session.commit()
        if drafts:
            log.info("followups_sent", count=len(drafts))
    except Exception:
        log.exception("followups_failed")


async def weekly_summary(deps: Deps) -> None:
    try:
        since = datetime.now(UTC) - timedelta(days=7)
        async with deps.sessionmaker() as session:
            week = await collect_stats(session, since=since)
            all_time = await collect_stats(session)
        text = (
            formatting.format_stats(week, "Weekly summary: leads from the last 7 days")
            + "\n\n"
            + formatting.format_stats(all_time, "All time")
        )
        await delivery.send_html(deps.bot, deps.chat_id, text[: formatting.TELEGRAM_LIMIT])
    except Exception:
        log.exception("weekly_summary_failed")


async def _conversation_for_email(
    session: AsyncSession, email: InboundEmail
) -> Conversation | None:
    candidates = (
        await session.scalars(
            select(Conversation)
            .where(Conversation.channel == talk.EMAIL, Conversation.address == email.sender)
            .order_by(Conversation.id.desc())
        )
    ).all()
    for conversation in candidates:
        sent_ids = {m.external_id for m in await talk.messages_of(session, conversation.id)}
        if sent_ids.intersection(email.references):
            return conversation
    open_ones = [c for c in candidates if c.state != ConversationState.CLOSED]
    return (open_ones or candidates or [None])[0]


async def inbox(deps: Deps) -> None:
    if deps.inbox is None or await _paused(deps):
        return
    try:
        async with deps.sessionmaker() as session:
            senders = (
                await session.scalars(
                    select(Conversation.address)
                    .where(Conversation.channel == talk.EMAIL, Conversation.address.is_not(None))
                    .distinct()
                )
            ).all()
        if not senders:
            return
        since = datetime.now(UTC) - timedelta(days=INBOX_LOOKBACK_DAYS)
        emails = await deps.inbox.fetch(list(senders), since)
        llm = await _llm(deps) if emails else None

        handled = 0
        for email in sorted(emails, key=lambda e: e.received_at or datetime.now(UTC)):
            async with deps.sessionmaker() as session:
                conversation = await _conversation_for_email(session, email)
                if conversation is None:
                    continue
                if email.received_at and email.received_at < conversation.created_at:
                    continue
                handled += await talk.client_message(
                    session,
                    llm,
                    deps.desk,
                    conversation,
                    email.text,
                    external_id=email.message_id,
                    auto=await is_auto(session),
                )
                await session.commit()
        if handled:
            log.info("client_replies", count=handled)
    except Exception:
        log.exception("inbox_failed")


async def daily_report(deps: Deps) -> None:
    try:
        timezone = deps.settings.timezone
        since = start_of_day(timezone)
        async with deps.sessionmaker() as session:
            messages = await build_report(session, since=since, day=since.strftime("%a %d %b"))
        for text in messages:
            await delivery.send_html(deps.bot, deps.chat_id, text)
    except Exception:
        log.exception("daily_report_failed")
