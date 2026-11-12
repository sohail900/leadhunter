import re
from datetime import UTC, datetime

import asyncpraw

from leadhunter.collectors.base import BaseCollector, RawLead
from leadhunter.text import find_email

_HIRING_TITLE = re.compile(r"^\s*\[\s*(hiring|task)\s*\]", re.IGNORECASE)


def is_hiring(title: str, flair: str | None) -> bool:
    if _HIRING_TITLE.match(title or ""):
        return True
    flair = (flair or "").lower()
    return "hiring" in flair or flair == "task"


def parse_submission(post: dict) -> RawLead | None:
    if not is_hiring(post.get("title", ""), post.get("link_flair_text")):
        return None
    body = post.get("selftext") or ""
    author = post.get("author")
    return RawLead(
        source="reddit",
        external_id=str(post["id"]),
        url=f"https://www.reddit.com{post['permalink']}",
        title=post["title"],
        body=body,
        author=author,
        posted_at=datetime.fromtimestamp(float(post["created_utc"]), UTC),
        contact_hint=find_email(body) or (f"u/{author}" if author else None),
        raw=post,
    )


def _submission_fields(submission) -> dict:
    return {
        "id": submission.id,
        "title": submission.title,
        "selftext": submission.selftext,
        "author": submission.author.name if submission.author else None,
        "created_utc": submission.created_utc,
        "permalink": submission.permalink,
        "link_flair_text": submission.link_flair_text,
        "subreddit": submission.subreddit.display_name,
        "url": submission.url,
    }


class RedditCollector(BaseCollector):
    name = "reddit"
    interval_minutes = 15

    def __init__(
        self, client_id: str, client_secret: str, user_agent: str, subreddits: list[str]
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_agent = user_agent
        self.subreddits = subreddits

    async def fetch(self) -> list[RawLead]:
        async with asyncpraw.Reddit(
            client_id=self.client_id,
            client_secret=self.client_secret,
            user_agent=self.user_agent,
        ) as reddit:
            subreddit = await reddit.subreddit("+".join(self.subreddits))
            leads = []
            async for submission in subreddit.new(limit=100):
                lead = parse_submission(_submission_fields(submission))
                if lead:
                    leads.append(lead)
        return leads
