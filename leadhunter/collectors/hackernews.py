import re
from datetime import UTC, datetime, timedelta

from leadhunter.collectors.base import BaseCollector, RawLead
from leadhunter.http import get_with_retry, make_client
from leadhunter.text import find_email, first_line, html_to_text

API = "https://hn.algolia.com/api/v1/search_by_date"
THREAD_TITLE = "Ask HN: Who is hiring?"
PAGE_SIZE = 100
MAX_PAGES = 10


def find_hiring_story(hits: list[dict]) -> dict | None:
    for hit in hits:
        if (hit.get("title") or "").startswith(THREAD_TITLE):
            return hit
    return None


def keyword_pattern(keywords: list[str]) -> re.Pattern | None:
    if not keywords:
        return None
    alternatives = "|".join(re.escape(k).replace(r"\-", r"[- ]?") for k in keywords)
    return re.compile(rf"\b(?:{alternatives})\b", re.IGNORECASE)


def parse_comments(hits: list[dict], story_id: str, keywords: list[str]) -> list[RawLead]:
    pattern = keyword_pattern(keywords)
    leads = []
    for hit in hits:
        if str(hit.get("parent_id")) != str(story_id):
            continue
        html = hit.get("comment_text") or ""
        text = html_to_text(html)
        if not text or (pattern and not pattern.search(text)):
            continue
        leads.append(
            RawLead(
                source="hackernews",
                external_id=str(hit["objectID"]),
                url=f"https://news.ycombinator.com/item?id={hit['objectID']}",
                title=first_line(text),
                body=html,
                author=hit.get("author"),
                posted_at=datetime.fromtimestamp(int(hit["created_at_i"]), UTC),
                contact_hint=find_email(text),
                raw=hit,
            )
        )
    return leads


class HackerNewsCollector(BaseCollector):
    name = "hackernews"
    interval_minutes = 60

    def __init__(self, keywords: list[str], max_age_hours: int) -> None:
        self.keywords = keywords
        self.max_age_hours = max_age_hours

    async def fetch(self) -> list[RawLead]:
        async with make_client() as client:
            stories = await get_with_retry(
                client, API, params={"tags": "story,author_whoishiring", "hitsPerPage": 10}
            )
            story = find_hiring_story(stories.json()["hits"])
            if story is None:
                return []

            cutoff = int((datetime.now(UTC) - timedelta(hours=self.max_age_hours)).timestamp())
            hits: list[dict] = []
            for page in range(MAX_PAGES):
                response = await get_with_retry(
                    client,
                    API,
                    params={
                        "tags": f"comment,story_{story['objectID']}",
                        "numericFilters": f"created_at_i>{cutoff}",
                        "hitsPerPage": PAGE_SIZE,
                        "page": page,
                    },
                )
                data = response.json()
                hits.extend(data["hits"])
                if page + 1 >= data.get("nbPages", 0):
                    break
        return parse_comments(hits, story["objectID"], self.keywords)
