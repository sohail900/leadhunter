import re
from datetime import UTC, datetime, timedelta

from leadhunter.config import Profile

_OFFERING = re.compile(
    r"""
      \[\s*(for\s*hire|offer|offering)\s*\]         # [For Hire], [OFFER]
    | \bseeking\s+work\b                             # HN "SEEKING WORK"
    | \b(available|open)\s+for\s+(hire|work|freelance|new\s+(projects|clients|work))\b
    | \blooking\s+for\s+(work|a\s+job|clients|new\s+clients|gigs|projects\s+to\s+work)\b
    | \bhire\s+me\b
    | \bi\s+(will|can|could)\s+(build|create|design|develop|make|write|code|fix)\b
    | \bi(\s+am|'m)\s+(an?\s+)?[\w\s/+.-]{0,40}?(developer|engineer|designer|freelancer)\b
      .{0,60}?\b(available|for\s+hire|looking|offering|seeking)\b
    | \bmy\s+services\b
    | \boffering\s+(my\s+)?[\w\s-]{0,30}?services\b
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)
_HIRING_TAG = re.compile(r"^\s*\[\s*(hiring|task)\s*\]", re.IGNORECASE)
_OPENING_CHARS = 300


def is_offering_services(title: str, body: str) -> bool:
    if _HIRING_TAG.match(title):
        return False
    return bool(_OFFERING.search(f"{title}\n{body[:_OPENING_CHARS]}"))


def deal_breaker(text: str, keywords: list[str]) -> str | None:
    for keyword in keywords:
        keyword = keyword.strip()
        if keyword and re.search(
            rf"(?<![\w+#]){re.escape(keyword)}(?![\w+#])", text, re.IGNORECASE
        ):
            return keyword
    return None


def prefilter(
    title: str,
    body: str,
    posted_at: datetime | None,
    profile: Profile,
    max_age_hours: int = 48,
    now: datetime | None = None,
) -> str | None:
    now = now or datetime.now(UTC)
    if posted_at is not None and posted_at < now - timedelta(hours=max_age_hours):
        return f"older than {max_age_hours} hours"
    if is_offering_services(title, body):
        return "offering services, not hiring"
    keyword = deal_breaker(f"{title}\n{body}", profile.preferences.deal_breaker_keywords)
    if keyword:
        return f"deal-breaker keyword: {keyword}"
    return None
