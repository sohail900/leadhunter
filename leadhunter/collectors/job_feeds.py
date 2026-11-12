import calendar
from datetime import UTC, datetime

import feedparser

from leadhunter.collectors.base import BaseCollector, RawLead
from leadhunter.http import get_with_retry, make_client

REMOTEOK_URL = "https://remoteok.com/api"


def _money(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def parse_remoteok(payload: list[dict], tags: list[str]) -> list[RawLead]:
    wanted = {t.lower() for t in tags}
    leads = []
    for job in payload:
        if "id" not in job or "position" not in job:
            continue
        job_tags = [str(t).lower() for t in job.get("tags") or []]
        if wanted and not wanted.intersection(job_tags):
            continue

        header = []
        if job.get("location"):
            header.append(f"Location: {job['location']}")
        low, high = _money(job.get("salary_min")), _money(job.get("salary_max"))
        if 10_000 <= low <= high <= 1_000_000:
            header.append(f"Salary: ${low:,} - ${high:,} per year")
        if job_tags:
            header.append(f"Tags: {', '.join(job_tags)}")

        company = job.get("company") or ""
        title = f"{company}: {job['position']}" if company else job["position"]
        posted_at = datetime.fromtimestamp(int(job["epoch"]), UTC) if job.get("epoch") else None
        leads.append(
            RawLead(
                source="remoteok",
                external_id=str(job["id"]),
                url=job.get("url") or job.get("apply_url"),
                title=title,
                body="\n".join(header) + "\n\n" + (job.get("description") or ""),
                author=company or None,
                posted_at=posted_at,
                raw=job,
            )
        )
    return leads


def parse_wwr(xml: str) -> list[RawLead]:
    feed = feedparser.parse(xml)
    leads = []
    for entry in feed.entries:
        link = entry.get("link")
        external_id = entry.get("id") or link
        if not external_id or not entry.get("title"):
            continue

        header = []
        for label, key in (("Region", "region"), ("Skills", "skills"), ("Category", "category")):
            if entry.get(key):
                header.append(f"{label}: {entry[key]}")

        company = entry.title.split(":", 1)[0].strip() if ":" in entry.title else None
        published = entry.get("published_parsed")
        leads.append(
            RawLead(
                source="weworkremotely",
                external_id=str(external_id),
                url=link,
                title=entry.title,
                body="\n".join(header) + "\n\n" + (entry.get("summary") or ""),
                author=company,
                posted_at=datetime.fromtimestamp(calendar.timegm(published), UTC)
                if published
                else None,
                raw={k: entry.get(k) for k in ("id", "link", "title", "published", "summary")},
            )
        )
    return leads


class RemoteOKCollector(BaseCollector):
    name = "remoteok"
    interval_minutes = 60

    def __init__(self, tags: list[str]) -> None:
        self.tags = tags

    async def fetch(self) -> list[RawLead]:
        async with make_client() as client:
            response = await get_with_retry(client, REMOTEOK_URL)
        return parse_remoteok(response.json(), self.tags)


class WWRCollector(BaseCollector):
    name = "weworkremotely"
    interval_minutes = 60

    def __init__(self, feed_urls: list[str]) -> None:
        self.feed_urls = feed_urls

    async def fetch(self) -> list[RawLead]:
        leads: dict[str, RawLead] = {}
        async with make_client() as client:
            for url in self.feed_urls:
                response = await get_with_retry(client, url)
                for lead in parse_wwr(response.text):
                    leads[lead.external_id] = lead
        return list(leads.values())
