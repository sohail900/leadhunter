from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from leadhunter.config import Preferences, Profile, Rate
from leadhunter.db.models import Event, Lead, LeadStatus
from leadhunter.llm.providers import LLMError
from leadhunter.pipeline.scorer import ScoreResult, build_scorer_prompts, score_new_leads
from leadhunter.state import THRESHOLD, set_state
from tests.conftest import sessionmaker_for

PROFILE = Profile(
    name="Test Dev",
    skills=["React", "PostgreSQL"],
    rate=Rate(hourly_usd=40),
    preferences=Preferences(deal_breaker_keywords=["unpaid"]),
)


class FakeLLM:
    name = "fake"

    def __init__(self, scores: dict[str, int] | None = None, error: Exception | None = None):
        self.scores = scores or {}
        self.error = error
        self.prompts: list[tuple[str, str]] = []

    async def complete_json(self, system, user, schema):
        self.prompts.append((system, user))
        if self.error:
            raise self.error
        title = next(t for t in self.scores if t in user)
        return ScoreResult(
            score=self.scores[title],
            stack_match=8,
            budget_fit=6,
            client_quality=7,
            scam_risk=1,
            tech_stack=["React"],
            summary=f"Summary of {title}",
            reasons=["Matches React"],
        )


async def add_lead(session, title, *, hours_old=1.0, body="Build a dashboard", external_id=None):
    lead = Lead(
        source="reddit",
        external_id=external_id or title,
        title=title,
        body=body,
        content_hash=title,
        posted_at=datetime.now(UTC) - timedelta(hours=hours_old),
    )
    session.add(lead)
    await session.flush()
    return lead


async def run(session, llm, **kwargs):
    options = {"default_threshold": 6, "max_age_hours": 48, **kwargs}
    return await score_new_leads(sessionmaker_for(session), llm, PROFILE, **options)


async def events_for(session, lead) -> list[dict]:
    rows = await session.scalars(select(Event).where(Event.lead_id == lead.id).order_by(Event.id))
    return [event.payload for event in rows]


async def test_leads_at_or_above_the_threshold_stay_scored_and_the_rest_are_rejected(db_session):
    good = await add_lead(db_session, "Good job")
    borderline = await add_lead(db_session, "Borderline job")
    bad = await add_lead(db_session, "Bad job")
    llm = FakeLLM({"Good job": 9, "Borderline job": 6, "Bad job": 5})

    counts = await run(db_session, llm)

    assert counts == {"scored": 3, "accepted": 2, "rejected": 1, "prefiltered": 0, "failed": 0}
    assert (good.status, good.score) == (LeadStatus.SCORED, 9)
    assert (borderline.status, borderline.score) == (LeadStatus.SCORED, 6)
    assert (bad.status, bad.score) == (LeadStatus.REJECTED, 5)
    assert good.tech_stack == ["React"]
    assert good.score_reasons["summary"] == "Summary of Good job"
    assert good.score_reasons["reasons"] == ["Matches React"]
    assert good.score_reasons["scam_risk"] == 1


async def test_every_status_change_is_recorded(db_session):
    bad = await add_lead(db_session, "Bad job")

    await run(db_session, FakeLLM({"Bad job": 3}))

    assert await events_for(db_session, bad) == [
        {"from": "new", "to": "scored", "score": 3},
        {"from": "scored", "to": "rejected", "stage": "score", "threshold": 6},
    ]


async def test_threshold_set_from_telegram_overrides_the_default(db_session):
    lead = await add_lead(db_session, "Decent job")
    await set_state(db_session, THRESHOLD, 8)

    await run(db_session, FakeLLM({"Decent job": 7}))

    assert lead.status is LeadStatus.REJECTED


async def test_stale_and_deal_breaker_leads_are_rejected_without_calling_the_llm(db_session):
    stale = await add_lead(db_session, "Old job", hours_old=72)
    unpaid = await add_lead(db_session, "Internship", body="This role is unpaid.")
    llm = FakeLLM()

    counts = await run(db_session, llm)

    assert counts["prefiltered"] == 2
    assert llm.prompts == []
    assert stale.status is LeadStatus.REJECTED
    assert stale.score is None
    assert (await events_for(db_session, unpaid))[0]["reason"] == "deal-breaker keyword: unpaid"


async def test_an_llm_failure_leaves_leads_new_and_stops_the_batch(db_session):
    first = await add_lead(db_session, "First job")
    second = await add_lead(db_session, "Second job")
    llm = FakeLLM(error=LLMError("HTTP 401: bad key"))

    counts = await run(db_session, llm)

    assert counts["failed"] == 1
    assert len(llm.prompts) == 1
    assert first.status is LeadStatus.NEW
    assert second.status is LeadStatus.NEW


async def test_only_new_leads_are_scored_and_the_batch_size_is_respected(db_session):
    for i in range(3):
        await add_lead(db_session, f"Job {i}")
    done = await add_lead(db_session, "Already rejected")
    done.status = LeadStatus.REJECTED
    llm = FakeLLM({f"Job {i}": 7 for i in range(3)})

    counts = await run(db_session, llm, batch_size=2)

    assert counts["scored"] == 2
    assert done.score is None


def test_prompt_contains_the_profile_the_post_and_optional_guidance():
    system, user = build_scorer_prompts(
        PROFILE,
        source="reddit",
        title="React dashboard",
        body="Need charts.",
        budget_min=500,
        budget_max=800,
    )

    assert "name: Test Dev" in system
    assert "hourly_usd: 40" in system
    assert "unpaid" in system
    assert "What has worked so far" not in system
    assert "Title: React dashboard" in user
    assert "$500 - $800" in user
    assert "Need charts." in user

    system, _ = build_scorer_prompts(
        PROFILE, source="x", title="t", body="b", guidance="Reddit leads reply most often."
    )
    assert "Reddit leads reply most often." in system


def test_ratings_outside_the_scale_are_clamped():
    result = ScoreResult(
        score=11,
        stack_match="7.6",
        budget_fit=-2,
        client_quality=5,
        scam_risk=0,
        tech_stack=[],
        summary="s",
        reasons=[],
    )
    assert (result.score, result.stack_match, result.budget_fit) == (10, 8, 0)
