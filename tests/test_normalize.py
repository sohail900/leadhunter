from datetime import UTC, datetime

import pytest

from leadhunter.collectors.base import RawLead
from leadhunter.pipeline.normalize import content_hash, normalize, parse_budget
from leadhunter.text import find_email, html_to_text


def raw(**overrides) -> RawLead:
    fields = {"source": "test", "external_id": "1", "title": "A job", "body": "Details"}
    return RawLead(**{**fields, **overrides})


def test_html_is_stripped_and_entities_decoded():
    html = (
        "<p>We need a <strong>React</strong> dev &amp; a designer.</p>"
        "<ul><li>Next.js</li><li>Postgres</li></ul>"
        "<script>alert(1)</script><p>Pay: $2,000&#x2F;month</p>"
    )

    text = html_to_text(html)

    assert text == "We need a React dev & a designer.\n\n- Next.js\n- Postgres\n\nPay: $2,000/month"


def test_whitespace_is_trimmed_and_collapsed():
    lead = normalize(
        raw(title="  Senior   dev \n", body="line one   \n\n\n\n\n  line two\t\t end  ")
    )

    assert lead.title == "Senior dev"
    assert lead.body == "line one\n\nline two end"


def test_plain_text_passes_through_unchanged():
    assert html_to_text("Budget is < $500 and > $100") == "Budget is < $500 and > $100"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Budget: $500", (500, 500)),
        ("Paying $1,500 - $2,000 for this", (1500, 2000)),
        ("$50-70/hr depending on experience", (50, 70)),
        ("$3,000–4,000/month", (3000, 4000)),
        ("around $3-5k total", (3000, 5000)),
        ("$500 to $1k", (500, 1000)),
        ("$2k fixed", (2000, 2000)),
        ("$40k - $60k", (40000, 60000)),
        ("budget is 800 USD", (800, 800)),
        ("Salary: $60,000 - $90,000 per year", (60000, 90000)),
        ("$1500.50 for the lot", (1500, 1500)),
        ("no money mentioned, 3 months, 2024", (None, None)),
        ("", (None, None)),
    ],
)
def test_parse_budget(text, expected):
    assert parse_budget(text) == expected


def test_budget_is_read_from_title_and_body():
    assert normalize(raw(title="[Hiring] Fix my app ($300)", body="See title")).budget_min == 300
    lead = normalize(raw(title="Fix my app", body="<p>Budget $1,000-$1,200</p>"))
    assert (lead.budget_min, lead.budget_max) == (1000, 1200)


def test_content_hash_ignores_case_punctuation_and_spacing():
    a = content_hash("[Hiring] React Dev!", "Need help with   Next.js, ASAP.")
    b = content_hash("hiring react dev", "need help with next js asap")
    c = content_hash("hiring vue dev", "need help with next js asap")

    assert a == b
    assert a != c


def test_same_post_as_html_and_as_text_hashes_the_same():
    as_html = normalize(raw(title="React dev", body="<p>Build a <b>dashboard</b></p>"))
    as_text = normalize(raw(title="React dev", body="Build a dashboard"))

    assert as_html.content_hash == as_text.content_hash


def test_contact_hint_from_collector_wins_otherwise_email_is_found_in_body():
    assert normalize(raw(contact_hint="u/someone", body="mail a@b.co")).contact_hint == "u/someone"
    assert normalize(raw(body="Reach me: jobs@acme.io.")).contact_hint == "jobs@acme.io"
    assert normalize(raw(body="DM me")).contact_hint is None


def test_find_email_understands_spelled_out_addresses():
    assert find_email("contact: jane [at] acme [dot] com") == "jane@acme.com"
    assert find_email("we are great at solving problems dot com style") is None


def test_naive_posted_at_is_treated_as_utc():
    lead = normalize(raw(posted_at=datetime(2026, 11, 1, 12, 0)))
    assert lead.posted_at == datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
