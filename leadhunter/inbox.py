import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from imap_tools import AND, OR, MailBox

from leadhunter.config import Settings
from leadhunter.text import clean_whitespace, html_to_text

SENDERS_PER_QUERY = 20
_QUOTE_START = re.compile(
    r"^(On .{5,200} wrote:|-{2,}\s*Original Message\s*-{2,}|_{10,}|From: .+|Sent from my .+)$",
    re.IGNORECASE,
)
_AUTOMATIC_SUBJECT = re.compile(
    r"^\s*(automatic reply|auto(matic)?[- ]?reply|out of (the )?office|undeliverable|"
    r"delivery status notification|mail delivery)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class InboundEmail:
    sender: str
    message_id: str
    references: tuple[str, ...]
    text: str
    received_at: datetime | None


def strip_quoted(text: str) -> str:
    kept = []
    for line in clean_whitespace(text).split("\n"):
        if _QUOTE_START.match(line) or line == "--":
            break
        if line.startswith(">"):
            continue
        kept.append(line)
    return clean_whitespace("\n".join(kept))


def is_automatic(subject: str, headers: dict[str, tuple[str, ...]]) -> bool:
    auto_submitted = " ".join(headers.get("auto-submitted", ())).lower()
    if auto_submitted and auto_submitted != "no":
        return True
    if "x-autoreply" in headers or "x-autorespond" in headers:
        return True
    return bool(_AUTOMATIC_SUBJECT.match(subject or ""))


def _message_ids(headers: dict[str, tuple[str, ...]], *names: str) -> tuple[str, ...]:
    found = []
    for name in names:
        for value in headers.get(name, ()):
            found += re.findall(r"<[^<>\s]+>", value)
    return tuple(dict.fromkeys(found))


class InboxReader:
    def __init__(self, settings: Settings) -> None:
        self.host = settings.imap_host
        self.port = settings.imap_port
        self.username = settings.imap_username
        self.password = settings.imap_password
        self.folder = settings.imap_folder

    @staticmethod
    def is_configured(settings: Settings) -> bool:
        return bool(settings.imap_host and settings.imap_username and settings.imap_password)

    def _fetch_blocking(self, senders: list[str], since: datetime) -> list[InboundEmail]:
        emails = []
        with MailBox(self.host, self.port).login(self.username, self.password, self.folder) as box:
            for start in range(0, len(senders), SENDERS_PER_QUERY):
                group = senders[start : start + SENDERS_PER_QUERY]
                who = OR(from_=group) if len(group) > 1 else AND(from_=group[0])
                criteria = AND(who, date_gte=since.date())
                for message in box.fetch(criteria, mark_seen=False, bulk=True):
                    email = parse_message(message)
                    if email:
                        emails.append(email)
        return emails

    async def fetch(self, senders: list[str], since: datetime) -> list[InboundEmail]:
        if not senders:
            return []
        return await asyncio.to_thread(self._fetch_blocking, senders, since)


def parse_message(message) -> InboundEmail | None:
    headers = message.headers
    if is_automatic(message.subject, headers):
        return None
    body = message.text or html_to_text(message.html or "")
    text = strip_quoted(body) or clean_whitespace(body)
    ids = _message_ids(headers, "message-id")
    if not text or not message.from_:
        return None
    received_at = message.date
    if received_at is not None and received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=UTC)
    return InboundEmail(
        sender=message.from_.lower(),
        message_id=ids[0] if ids else f"uid:{message.uid}",
        references=_message_ids(headers, "in-reply-to", "references"),
        text=text,
        received_at=received_at,
    )
