import asyncio
import hashlib
import re
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlsplit

from imap_tools import AND, MailBox

from leadhunter.collectors.base import BaseCollector, RawLead
from leadhunter.text import clean_whitespace, html_to_text

_JOB_PATH = re.compile(r"upwork\.com/(?:jobs|freelance-jobs/apply|ab/proposals/job)/[^\s\"'<>]+")
_JOB_ID = re.compile(r"~0?[0-9a-f]{16,}")
_BUTTON_TEXT = re.compile(r"^(view|see|apply|open|submit|read)\b.{0,25}$|^more$", re.IGNORECASE)
_MIN_TITLE = 8


def job_url(href: str) -> str | None:
    href = unquote(href)
    if not _JOB_PATH.search(href):
        return None
    for values in parse_qs(urlsplit(href).query).values():
        for value in values:
            if _JOB_PATH.search(value):
                href = value
    match = _JOB_PATH.search(href)
    return "https://www." + match.group(0).split("?")[0].rstrip("/.,)") if match else None


class _JobLinkSplitter(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict] = []
        self._in_job_link = False
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag == "a":
            url = job_url(dict(attrs).get("href") or "")
            if url:
                self._in_job_link = True
                self.blocks.append({"url": url, "link_text": "", "html": ""})
        elif self.blocks and tag in ("p", "div", "br", "tr", "li", "td", "h1", "h2", "h3"):
            self.blocks[-1]["html"] += "\n"

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self._skip = max(0, self._skip - 1)
        elif tag == "a":
            self._in_job_link = False

    def handle_data(self, data):
        if self._skip or not self.blocks:
            return
        key = "link_text" if self._in_job_link else "html"
        self.blocks[-1][key] += data


def _external_id(url: str) -> str:
    match = _JOB_ID.search(url)
    return match.group(0) if match else hashlib.sha256(url.encode()).hexdigest()[:24]


def parse_upwork_email(subject: str, html: str, text: str, date: datetime | None) -> list[RawLead]:
    splitter = _JobLinkSplitter()
    if html:
        splitter.feed(html)
    else:
        position = 0
        for match in _JOB_PATH.finditer(text or ""):
            if splitter.blocks:
                splitter.blocks[-1]["html"] = text[position : match.start()]
            url = job_url("https://www." + match.group(0))
            splitter.blocks.append({"url": url, "link_text": "", "html": ""})
            position = match.end()
        if splitter.blocks:
            splitter.blocks[-1]["html"] = text[position:]

    jobs: dict[str, dict] = {}
    for block in splitter.blocks:
        job = jobs.setdefault(block["url"], {"title": "", "body": ""})
        link_text = clean_whitespace(block["link_text"])
        if len(link_text) >= _MIN_TITLE and not _BUTTON_TEXT.match(link_text) and not job["title"]:
            job["title"] = link_text
        job["body"] += "\n" + block["html"]

    subject_title = re.sub(
        r"^\s*(new job|job alert|new jobs?)\s*[:\-–]\s*", "", subject or "", flags=re.I
    )
    leads = []
    for url, job in jobs.items():
        body = html_to_text(job["body"])
        title = job["title"] or (subject_title.strip() if len(jobs) == 1 else "")
        if not title:
            continue
        leads.append(
            RawLead(
                source="upwork",
                external_id=_external_id(url),
                url=url,
                title=title,
                body=body,
                posted_at=date,
                raw={"subject": subject, "url": url},
            )
        )
    return leads


class UpworkEmailCollector(BaseCollector):
    name = "upwork"
    interval_minutes = 10

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        folder: str,
        sender: str,
        max_age_hours: int,
    ) -> None:
        self.host, self.port, self.username, self.password = host, port, username, password
        self.folder = folder
        self.sender = sender
        self.max_age_hours = max_age_hours

    def _fetch_blocking(self) -> list[RawLead]:
        since = (datetime.now(UTC) - timedelta(hours=self.max_age_hours + 24)).date()
        leads: dict[str, RawLead] = {}
        with MailBox(self.host, self.port).login(self.username, self.password, self.folder) as box:
            criteria = AND(from_=self.sender, date_gte=since)
            for message in box.fetch(criteria, mark_seen=False, bulk=True):
                date = message.date
                if date is not None and date.tzinfo is None:
                    date = date.replace(tzinfo=UTC)
                for lead in parse_upwork_email(message.subject, message.html, message.text, date):
                    leads[lead.external_id] = lead
        return list(leads.values())

    async def fetch(self) -> list[RawLead]:
        return await asyncio.to_thread(self._fetch_blocking)
