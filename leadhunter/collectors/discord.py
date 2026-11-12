import asyncio
import re
from datetime import datetime

import discord
import structlog

from leadhunter.collectors.base import BaseCollector, RawLead
from leadhunter.text import find_email, first_line

log = structlog.get_logger()

MIN_LENGTH = 40
_ROLE = r"[\w\s/+.#-]{0,40}?(developer|dev|engineer|programmer|freelancer|coder)\b"
_JOB_SIGNALS = re.compile(
    rf"""
      \bhiring\b | \[\s*(hiring|paid|job|gig|task)\s*\]
    | \blooking\s+for\s+(an?\s+)?{_ROLE}
    | \bneed(ed|s)?\s+(an?\s+)?{_ROLE}
    | \b(budget|paid\s+(gig|project|work|task)|pay(ing|s)?\s*[:$]|per\s+hour|/\s?hr\b|/\s?hour\b)
    | \b(contract|freelance|part[- ]time)\s+(role|position|job|work|gig|opportunity)\b
    | [$€£]\s?\d
    """,
    re.IGNORECASE | re.VERBOSE,
)


def looks_like_job_post(text: str) -> bool:
    text = (text or "").strip()
    return len(text) >= MIN_LENGTH and bool(_JOB_SIGNALS.search(text))


def parse_message(
    message_id: int, content: str, author: str, created_at: datetime, jump_url: str, channel: str
) -> RawLead | None:
    if not looks_like_job_post(content):
        return None
    return RawLead(
        source="discord",
        external_id=str(message_id),
        url=jump_url,
        title=first_line(content),
        body=content,
        author=author,
        posted_at=created_at,
        contact_hint=find_email(content) or f"discord:{author}",
        raw={"id": message_id, "channel": channel, "author": author, "content": content},
    )


class DiscordCollector(BaseCollector):
    name = "discord"
    interval_minutes = 5

    def __init__(self, token: str, channel_ids: list[int]) -> None:
        self.token = token
        self.channel_ids = set(channel_ids)
        self._buffer: list[RawLead] = []
        self._task: asyncio.Task | None = None

        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True
        self._client = discord.Client(intents=intents)
        self._client.event(self.on_message)

    async def on_message(self, message: discord.Message) -> None:
        if message.channel.id not in self.channel_ids or message.author.bot:
            return
        lead = parse_message(
            message.id,
            message.content,
            str(message.author),
            message.created_at,
            message.jump_url,
            getattr(message.channel, "name", str(message.channel.id)),
        )
        if lead:
            self._buffer.append(lead)

    async def fetch(self) -> list[RawLead]:
        if self._task is None:
            self._task = asyncio.create_task(self._client.start(self.token))
        elif self._task.done():
            error = self._task.exception()
            self._task = None
            raise RuntimeError(f"Discord connection stopped: {error!r}")
        leads, self._buffer = self._buffer, []
        return leads
