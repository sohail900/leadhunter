from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from leadhunter.collectors import build_collectors
from leadhunter.collectors import discord as discord_collector
from leadhunter.collectors import telegram as telegram_collector
from leadhunter.collectors.imap_upwork import job_url, parse_upwork_email
from leadhunter.config import Settings
from leadhunter.pipeline.normalize import normalize

FIXTURES = Path(__file__).parent / "fixtures"
SENT = datetime(2026, 11, 12, 9, 30, tzinfo=UTC)


def upwork_leads():
    html = (FIXTURES / "upwork_alert.html").read_text()
    return parse_upwork_email("New jobs for you", html, "", SENT)


def test_upwork_email_yields_one_lead_per_job_not_per_link():
    leads = upwork_leads()

    assert [lead.title for lead in leads] == [
        "Build a booking dashboard in Next.js and Supabase",
        "Fix Django REST API performance issues",
    ]
    assert all(lead.source == "upwork" for lead in leads)


def test_upwork_job_link_is_unwrapped_from_tracking_redirects_and_cleaned():
    first, second = upwork_leads()

    assert first.url == (
        "https://www.upwork.com/jobs/Build-booking-dashboard-Next-Supabase_~021987654321098765"
    )
    assert second.url == (
        "https://www.upwork.com/jobs/Fix-Django-REST-API-performance_~01aaaabbbbccccdddd"
    )
    assert first.external_id == "~021987654321098765"
    assert second.external_id == "~01aaaabbbbccccdddd"


def test_upwork_description_and_budget_are_extracted():
    first, second = (normalize(lead) for lead in upwork_leads())

    assert "yoga studio chain" in first.body
    assert "Skills: Next.js, React, Supabase, Stripe" in first.body
    assert "Django" not in first.body
    assert (first.budget_min, first.budget_max) == (1500, 1500)
    assert "N+1 queries" in second.body
    assert (second.budget_min, second.budget_max) == (30, 50)
    assert "saved searches" not in first.body
    assert first.posted_at == SENT


def test_upwork_single_job_email_uses_the_subject_when_the_link_is_only_a_button():
    html = (
        "<p>Client needs a Shopify theme fixed. Budget: $200</p>"
        '<a href="https://www.upwork.com/jobs/~01ffffeeeeddddcccc">View job</a>'
    )

    [lead] = parse_upwork_email("New job: Fix Shopify theme bug", html, "", SENT)

    assert lead.title == "Fix Shopify theme bug"
    assert lead.external_id == "~01ffffeeeeddddcccc"


def test_upwork_plain_text_email():
    text = (
        "New job: API integration\n"
        "https://www.upwork.com/jobs/~01abcdefabcdefabcd\n"
        "Connect our CRM to Stripe. Fixed price $400.\n"
    )

    [lead] = parse_upwork_email("New job: API integration", "", text, SENT)

    assert lead.title == "API integration"
    assert "Connect our CRM to Stripe" in lead.body
    assert normalize(lead).budget_min == 400


def test_upwork_email_without_job_links_yields_nothing():
    assert parse_upwork_email("Your weekly summary", "<p>No jobs here</p>", "", SENT) == []


@pytest.mark.parametrize(
    "href",
    [
        "https://www.upwork.com/nx/find-work/saved-searches",
        "https://www.upwork.com/legal",
        "https://example.com/jobs/123",
        "",
    ],
)
def test_non_job_links_are_ignored(href):
    assert job_url(href) is None


NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
CUTOFF = NOW - timedelta(hours=48)
TG_POST = (
    "#hiring Senior React developer (contract, remote)\n\n"
    "3 month contract, $40-55/hr. Stack: React, TypeScript, Node.\n"
    "Send your CV to jobs@acme.example"
)


def test_telegram_message_becomes_a_lead():
    lead = telegram_collector.parse_message(
        "@remote_dev_jobs", 4812, TG_POST, NOW - timedelta(hours=2), CUTOFF
    )

    assert lead.source == "telegram"
    assert lead.external_id == "remote_dev_jobs:4812"
    assert lead.url == "https://t.me/remote_dev_jobs/4812"
    assert lead.title == "#hiring Senior React developer (contract, remote)"
    assert lead.body == TG_POST
    assert lead.contact_hint == "jobs@acme.example"
    assert lead.posted_at == NOW - timedelta(hours=2)
    assert (normalize(lead).budget_min, normalize(lead).budget_max) == (40, 55)


def test_telegram_skips_old_short_and_media_only_messages():
    def parse(text, age_hours=1):
        return telegram_collector.parse_message(
            "jobs", 1, text, NOW - timedelta(hours=age_hours), CUTOFF
        )

    assert parse(TG_POST, age_hours=49) is None
    assert parse("Good morning everyone!") is None
    assert parse(None) is None
    assert parse(TG_POST) is not None


def test_telegram_naive_dates_are_treated_as_utc():
    naive = (NOW - timedelta(hours=1)).replace(tzinfo=None)
    lead = telegram_collector.parse_message("jobs", 1, TG_POST, naive, CUTOFF)
    assert lead.posted_at.tzinfo is UTC


@pytest.mark.parametrize(
    "text",
    [
        "[HIRING] Need a full-stack dev for a 2 week MVP build, budget $2k, DM me",
        "We're hiring a part-time React engineer for our startup, remote, paid weekly.",
        "Looking for a Python developer to build a scraping pipeline. Paid gig.",
        "Need someone who knows Stripe webhooks, paying $50/hr for about 10 hours.",
        "Anyone available? I need a senior Next.js developer for a contract role starting Monday.",
    ],
)
def test_discord_recognises_job_posts(text):
    assert discord_collector.looks_like_job_post(text)


@pytest.mark.parametrize(
    "text",
    [
        "lol same, my build has been broken all morning and I can't figure out why",
        "Has anyone tried the new React compiler yet? Curious how it handles memoization.",
        "hiring?",
        "",
    ],
)
def test_discord_ignores_chatter(text):
    assert not discord_collector.looks_like_job_post(text)


def test_discord_message_becomes_a_lead():
    content = (
        "[HIRING] Need a full-stack dev for a 2 week MVP build, budget $2k.\n"
        "Email hi@startup.example"
    )

    lead = discord_collector.parse_message(
        1234567890,
        content,
        "founder_jane",
        NOW,
        "https://discord.com/channels/1/2/1234567890",
        "job-board",
    )

    assert lead.source == "discord"
    assert lead.external_id == "1234567890"
    assert lead.url == "https://discord.com/channels/1/2/1234567890"
    assert lead.title == "[HIRING] Need a full-stack dev for a 2 week MVP build, budget $2k."
    assert lead.author == "founder_jane"
    assert lead.contact_hint == "hi@startup.example"
    assert normalize(lead).budget_min == 2000


def test_discord_contact_falls_back_to_the_author():
    lead = discord_collector.parse_message(
        1, "We are hiring a contract backend engineer, DM me for details.", "bob", NOW, "u", "jobs"
    )
    assert lead.contact_hint == "discord:bob"


async def test_discord_collector_buffers_matching_messages_from_configured_channels(monkeypatch):
    collector = discord_collector.DiscordCollector("token", [42])

    async def never_connect(token):
        return None

    monkeypatch.setattr(collector._client, "start", never_connect)

    class FakeMessage:
        def __init__(self, channel_id, content, bot=False):
            self.id = hash(content) & 0xFFFF
            self.content = content
            self.created_at = NOW
            self.jump_url = "https://discord.com/channels/1/2/3"
            self.channel = type("Channel", (), {"id": channel_id, "name": "jobs"})()
            self.author = type("Author", (), {"bot": bot, "__str__": lambda self: "jane"})()

    job = "[HIRING] Need a full-stack dev for a 2 week MVP build, budget $2k"
    await collector.on_message(FakeMessage(42, job))
    await collector.on_message(FakeMessage(99, job))
    await collector.on_message(FakeMessage(42, job, bot=True))
    await collector.on_message(FakeMessage(42, "anyone seen the new release notes yet? wild"))

    first = await collector.fetch()
    second = await collector.fetch()

    assert [lead.body for lead in first] == [job]
    assert second == []


def test_new_collectors_are_enabled_only_when_configured():
    names = [c.name for c in build_collectors(Settings(_env_file=None))]
    assert names == ["remoteok", "weworkremotely", "hackernews"]

    configured = Settings(
        _env_file=None,
        imap_host="imap.example.test",
        imap_username="me@example.test",
        imap_password="app-password",
        telegram_api_id=12345,
        telegram_api_hash="hash",
        telegram_session="session",
        telegram_channels="remote_dev_jobs,freelance_feed",
        discord_bot_token="token",
        discord_channel_ids="111,222",
    )
    names = [c.name for c in build_collectors(configured)]
    assert names == ["remoteok", "weworkremotely", "hackernews", "upwork", "telegram", "discord"]


def test_telegram_needs_channels_as_well_as_a_session():
    settings = Settings(
        _env_file=None,
        collectors="telegram",
        telegram_api_id=1,
        telegram_api_hash="h",
        telegram_session="s",
    )
    assert build_collectors(settings) == []
