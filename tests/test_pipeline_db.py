from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from leadhunter.collectors.base import BaseCollector, RawLead
from leadhunter.config import Preferences, Profile
from leadhunter.db.models import Event, Lead, LeadStatus
from leadhunter.pipeline.dedupe import is_duplicate
from leadhunter.pipeline.normalize import normalize
from leadhunter.pipeline.runner import run_collector
from tests.conftest import sessionmaker_for

PROFILE = Profile(name="Test", preferences=Preferences(deal_breaker_keywords=["unpaid"]))


def raw(source="reddit", external_id="1", title="React dev wanted", body="Build a dashboard"):
    return RawLead(source=source, external_id=external_id, title=title, body=body)


async def store(session, raw_lead, fetched_at=None) -> Lead:
    lead = Lead(**normalize(raw_lead).model_dump())
    if fetched_at:
        lead.fetched_at = fetched_at
    session.add(lead)
    await session.flush()
    return lead


class FakeCollector(BaseCollector):
    name = "fake"

    def __init__(self, leads=None, error=None):
        self.leads = leads or []
        self.error = error

    async def fetch(self):
        if self.error:
            raise self.error
        return self.leads


async def count_leads(session) -> int:
    return await session.scalar(select(func.count(Lead.id)))


async def test_new_lead_is_not_a_duplicate(db_session):
    assert await is_duplicate(db_session, normalize(raw())) is False


async def test_same_source_and_external_id_is_a_duplicate_even_if_text_changed(db_session):
    await store(db_session, raw(external_id="42"))

    edited = normalize(raw(external_id="42", body="Completely rewritten post"))

    assert await is_duplicate(db_session, edited) is True


async def test_same_external_id_from_another_source_is_not_a_duplicate(db_session):
    await store(db_session, raw(source="reddit", external_id="42"))

    other = normalize(raw(source="hackernews", external_id="42", title="Different job"))

    assert await is_duplicate(db_session, other) is False


async def test_cross_posted_content_within_14_days_is_a_duplicate(db_session):
    await store(
        db_session,
        raw(source="reddit", external_id="1"),
        fetched_at=datetime.now(UTC) - timedelta(days=13),
    )

    cross_post = normalize(raw(source="hackernews", external_id="999", title="REACT DEV WANTED!"))

    assert await is_duplicate(db_session, cross_post) is True


async def test_same_content_older_than_14_days_is_not_a_duplicate(db_session):
    await store(
        db_session,
        raw(source="reddit", external_id="1"),
        fetched_at=datetime.now(UTC) - timedelta(days=15),
    )

    repost = normalize(raw(source="hackernews", external_id="999"))

    assert await is_duplicate(db_session, repost) is False


async def test_runner_stores_new_leads_and_counts_duplicates(db_session):
    collector = FakeCollector(
        [
            raw(source="fake", external_id="1", title="Job one", body="<p>Pay $500</p>"),
            raw(source="fake", external_id="2", title="Job two"),
            raw(source="fake", external_id="3", title="Job one", body="Pay $500"),
        ]
    )

    result = await run_collector(collector, sessionmaker_for(db_session), PROFILE)

    assert (result.fetched, result.new, result.duplicates, result.failed) == (3, 2, 1, False)
    stored = (await db_session.scalars(select(Lead).order_by(Lead.external_id))).all()
    assert [lead.external_id for lead in stored] == ["1", "2"]
    assert stored[0].status is LeadStatus.NEW
    assert stored[0].body == "Pay $500"
    assert (stored[0].budget_min, stored[0].budget_max) == (500, 500)
    assert stored[0].fetched_at is not None


async def test_running_the_same_collector_twice_adds_nothing(db_session):
    collector = FakeCollector([raw(source="fake", external_id="1")])
    await run_collector(collector, sessionmaker_for(db_session), PROFILE)

    second = await run_collector(collector, sessionmaker_for(db_session), PROFILE)

    assert (second.fetched, second.new, second.duplicates) == (1, 0, 1)
    assert await count_leads(db_session) == 1


async def test_a_failing_collector_is_reported_not_raised(db_session):
    collector = FakeCollector(error=RuntimeError("source is down"))

    result = await run_collector(collector, sessionmaker_for(db_session), PROFILE)

    assert result.failed is True
    assert await count_leads(db_session) == 0


async def test_prefiltered_leads_are_stored_as_rejected_with_the_reason(db_session):
    old = RawLead(
        source="fake",
        external_id="old",
        title="Old job",
        body="Stale",
        posted_at=datetime.now(UTC) - timedelta(days=5),
    )
    collector = FakeCollector(
        [
            old,
            raw(source="fake", external_id="offer", title="[For Hire] React dev", body="Hire me"),
            raw(source="fake", external_id="unpaid", title="Intern", body="This is unpaid"),
            raw(source="fake", external_id="good", title="[Hiring] React dev", body="Pay $900"),
        ]
    )

    result = await run_collector(collector, sessionmaker_for(db_session), PROFILE)

    assert (result.fetched, result.new, result.rejected) == (4, 4, 3)
    statuses = dict((await db_session.execute(select(Lead.external_id, Lead.status))).all())
    assert statuses == {
        "old": LeadStatus.REJECTED,
        "offer": LeadStatus.REJECTED,
        "unpaid": LeadStatus.REJECTED,
        "good": LeadStatus.NEW,
    }
    reasons = (await db_session.scalars(select(Event.payload["reason"].as_string()))).all()
    assert sorted(reasons) == [
        "deal-breaker keyword: unpaid",
        "offering services, not hiring",
        "older than 48 hours",
    ]

    again = await run_collector(collector, sessionmaker_for(db_session), PROFILE)
    assert (again.new, again.duplicates) == (0, 4)
