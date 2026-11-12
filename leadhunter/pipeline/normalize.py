import hashlib
import re
from datetime import UTC, datetime

from pydantic import BaseModel

from leadhunter.collectors.base import RawLead
from leadhunter.text import clean_whitespace, find_email, html_to_text

_NUM = r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s?(k\b)?"
_RANGE = re.compile(rf"\$\s?{_NUM}\s*(?:-|–|—|to)\s*\$?\s?{_NUM}", re.IGNORECASE)
_SINGLE = re.compile(rf"\$\s?{_NUM}", re.IGNORECASE)
_USD_SUFFIX = re.compile(rf"\b{_NUM}\s*(?:usd|dollars)\b", re.IGNORECASE)


class NormalizedLead(BaseModel):
    source: str
    external_id: str
    url: str | None
    title: str
    body: str
    author: str | None
    contact_hint: str | None
    posted_at: datetime | None
    content_hash: str
    budget_min: int | None
    budget_max: int | None


def _amount(number: str, k: str | None) -> int:
    value = int(number.replace(",", ""))
    return value * 1000 if k else value


def parse_budget(text: str) -> tuple[int | None, int | None]:
    match = _RANGE.search(text)
    if match:
        low_n, low_k, high_n, high_k = match.groups()
        low, high = _amount(low_n, low_k), _amount(high_n, high_k)
        if high_k and not low_k and low < int(high_n.replace(",", "")):
            low *= 1000
        if low <= high:
            return low, high

    match = _SINGLE.search(text) or _USD_SUFFIX.search(text)
    if match:
        value = _amount(match.group(1), match.group(2))
        return value, value
    return None, None


def content_hash(title: str, body: str) -> str:
    words = re.findall(r"[a-z0-9]+", f"{title} {body}".lower())
    return hashlib.sha256(" ".join(words).encode()).hexdigest()


def normalize(raw: RawLead) -> NormalizedLead:
    title = clean_whitespace(html_to_text(raw.title))
    body = html_to_text(raw.body)
    budget_min, budget_max = parse_budget(f"{title}\n{body}")

    posted_at = raw.posted_at
    if posted_at is not None and posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=UTC)

    return NormalizedLead(
        source=raw.source,
        external_id=raw.external_id,
        url=raw.url,
        title=title,
        body=body,
        author=raw.author,
        contact_hint=raw.contact_hint or find_email(body),
        posted_at=posted_at,
        content_hash=content_hash(title, body),
        budget_min=budget_min,
        budget_max=budget_max,
    )
