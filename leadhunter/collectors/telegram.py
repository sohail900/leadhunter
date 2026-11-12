from datetime import UTC, datetime, timedelta

from telethon import TelegramClient
from telethon.sessions import StringSession

from leadhunter.collectors.base import BaseCollector, RawLead
from leadhunter.text import find_email, first_line

MIN_LENGTH = 40
MESSAGES_PER_CHANNEL = 50


def parse_message(
    channel: str, message_id: int, text: str, date: datetime, cutoff: datetime
) -> RawLead | None:
    text = (text or "").strip()
    if len(text) < MIN_LENGTH:
        return None
    if date.tzinfo is None:
        date = date.replace(tzinfo=UTC)
    if date < cutoff:
        return None
    channel = channel.lstrip("@")
    return RawLead(
        source="telegram",
        external_id=f"{channel}:{message_id}",
        url=f"https://t.me/{channel}/{message_id}",
        title=first_line(text),
        body=text,
        author=channel,
        posted_at=date,
        contact_hint=find_email(text),
        raw={"channel": channel, "id": message_id, "text": text},
    )


class TelegramChannelCollector(BaseCollector):
    name = "telegram"
    interval_minutes = 30

    def __init__(
        self, api_id: int, api_hash: str, session: str, channels: list[str], max_age_hours: int
    ) -> None:
        self.api_id = api_id
        self.api_hash = api_hash
        self.session = session
        self.channels = channels
        self.max_age_hours = max_age_hours

    async def fetch(self) -> list[RawLead]:
        cutoff = datetime.now(UTC) - timedelta(hours=self.max_age_hours)
        client = TelegramClient(StringSession(self.session), self.api_id, self.api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError(
                    "Telegram session is not authorized; run `python -m leadhunter.telegram_login`"
                )
            leads = []
            for channel in self.channels:
                async for message in client.iter_messages(channel, limit=MESSAGES_PER_CHANNEL):
                    if message.date < cutoff:
                        break
                    lead = parse_message(channel, message.id, message.message, message.date, cutoff)
                    if lead:
                        leads.append(lead)
            return leads
        finally:
            await client.disconnect()
