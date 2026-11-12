import itertools
import smtplib
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from telegram import Update

from leadhunter import jobs
from leadhunter.bot.delivery import send_drafted_leads
from leadhunter.config import Profile
from leadhunter.db.models import Draft, DraftVariant, Event, Followup, Lead, LeadStatus
from leadhunter.llm.providers import LLMError, LLMInvalidReply
from leadhunter.pipeline.drafter import DraftSet, FollowupDraft, draft_scored_leads
from leadhunter.pipeline.scorer import ScoreResult
from leadhunter.state import PAUSED, THRESHOLD, get_state
from leadhunter.tracker.events import set_status
from leadhunter.tracker.followups import draft_due_followups, mark_contacted
from leadhunter.tracker.stats import build_guidance, collect_stats
from tests.conftest import (
    OWNER_CHAT_ID,
    callback_update,
    make_settings,
    message_update,
    reply_update,
    sessionmaker_for,
)

PROFILE = Profile(name="Test Dev", skills=["React"])
DRAFTS = DraftSet(short="Short text.", portfolio_led="Portfolio text.", problem_led="Problem text.")


def connect_llm(monkeypatch, llm) -> None:

    async def connected(session):
        return llm

    monkeypatch.setattr(jobs, "active_llm", connected)


class FakeLLM:
    name = "fake"

    def __init__(self, reply=None, error=None, score=7):
        self.reply, self.error, self.score = reply, error, score
        self.calls = []

    async def complete_json(self, system, user, schema):
        self.calls.append((system, user, schema))
        if self.error:
            raise self.error
        if schema is ScoreResult:
            return ScoreResult(
                score=self.score,
                stack_match=8,
                budget_fit=5,
                client_quality=6,
                scam_risk=1,
                tech_stack=["React"],
                summary="A summary",
                reasons=["Because"],
            )
        if schema is FollowupDraft:
            return FollowupDraft(text="Following up with a question.")
        return self.reply or DRAFTS


_counter = itertools.count(1)


async def add_lead(session, status=LeadStatus.NEW, *, score=7, contact=None, **fields):
    n = next(_counter)
    lead = Lead(
        source=fields.pop("source", "reddit"),
        external_id=f"x{n}",
        title=fields.pop("title", f"[Hiring] React dev {n}"),
        body="Build a dashboard",
        url=f"https://example.test/post/{n}",
        content_hash=f"h{n}",
        posted_at=datetime.now(UTC),
        status=status,
        score=score,
        contact_hint=contact,
        score_reasons={"summary": "Needs a dashboard", "reasons": ["Matches React"]},
        tech_stack=["React"],
        **fields,
    )
    session.add(lead)
    await session.flush()
    return lead


async def add_drafted_lead(session, status=LeadStatus.SENT_TO_ME, **kwargs) -> Lead:
    lead = await add_lead(session, status, **kwargs)
    for variant in ("short", "portfolio_led", "problem_led"):
        session.add(Draft(lead_id=lead.id, variant=variant, text=f"{variant} text for {lead.id}"))
    await session.flush()
    return lead


async def deliver(app, payload):
    await app.process_update(Update.de_json(payload, app.bot))


async def chosen_draft(session, lead) -> Draft | None:
    return await session.scalar(select(Draft).where(Draft.lead_id == lead.id, Draft.chosen))


async def event_types(session, lead) -> list[str]:
    rows = await session.scalars(select(Event).where(Event.lead_id == lead.id).order_by(Event.id))
    return [
        e.payload["to"] if e.type == "status_changed" else f"{e.type}:{e.payload.get('channel')}"
        for e in rows
    ]


async def test_drafter_writes_three_variants_for_scored_leads_only(db_session):
    scored = await add_lead(db_session, LeadStatus.SCORED)
    untouched = await add_lead(db_session, LeadStatus.NEW)
    llm = FakeLLM()

    counts = await draft_scored_leads(sessionmaker_for(db_session), llm, PROFILE)

    assert counts == {"drafted": 1, "failed": 0}
    assert scored.status is LeadStatus.DRAFTED
    assert untouched.status is LeadStatus.NEW
    drafts = (await db_session.scalars(select(Draft).order_by(Draft.id))).all()
    assert [(d.variant, d.text, d.chosen) for d in drafts] == [
        (DraftVariant.SHORT, "Short text.", False),
        (DraftVariant.PORTFOLIO_LED, "Portfolio text.", False),
        (DraftVariant.PROBLEM_LED, "Problem text.", False),
    ]
    system, user, _ = llm.calls[0]
    assert "name: Test Dev" in system
    assert "Under 150 words" in system
    assert scored.title in user


async def test_a_provider_error_leaves_the_lead_scored_for_the_next_run(db_session):
    lead = await add_lead(db_session, LeadStatus.SCORED)

    counts = await draft_scored_leads(
        sessionmaker_for(db_session), FakeLLM(error=LLMError("HTTP 500")), PROFILE
    )

    assert counts["failed"] == 1
    assert lead.status is LeadStatus.SCORED


async def test_an_unusable_reply_rejects_only_that_lead(db_session):
    lead = await add_lead(db_session, LeadStatus.SCORED)

    await draft_scored_leads(
        sessionmaker_for(db_session), FakeLLM(error=LLMInvalidReply("too long")), PROFILE
    )

    assert lead.status is LeadStatus.REJECTED


def test_drafts_over_150_words_are_rejected_by_the_schema():
    import pytest
    from pydantic import ValidationError

    long_text = "word " * 151
    with pytest.raises(ValidationError, match="151 words; the limit is 150"):
        DraftSet(short=long_text, portfolio_led="ok", problem_led="ok")
    assert DraftSet(short="word " * 150, portfolio_led="ok", problem_led="ok")


async def test_urgent_leads_are_sent_immediately_and_the_rest_wait_for_the_digest(
    db_session, bot_app_db, telegram
):
    urgent = await add_drafted_lead(db_session, LeadStatus.DRAFTED, score=9)
    normal = await add_drafted_lead(db_session, LeadStatus.DRAFTED, score=7)
    send = dict(can_email=True)
    maker = sessionmaker_for(db_session)

    sent = await send_drafted_leads(bot_app_db.bot, OWNER_CHAT_ID, maker, min_score=9, **send)

    assert sent == 1
    assert urgent.status is LeadStatus.SENT_TO_ME
    assert normal.status is LeadStatus.DRAFTED
    [card] = telegram.sent_messages()
    assert f"lead #{urgent.id}" in card["text"]

    sent = await send_drafted_leads(bot_app_db.bot, OWNER_CHAT_ID, maker, digest=True, **send)

    assert sent == 1
    assert normal.status is LeadStatus.SENT_TO_ME
    assert "Digest: 1 lead to review" in telegram.sent_messages()[1]["text"]
    assert await send_drafted_leads(bot_app_db.bot, OWNER_CHAT_ID, maker, digest=True, **send) == 0


async def test_lead_card_shows_everything_needed_to_decide(db_session, bot_app_db, telegram):
    lead = await add_drafted_lead(
        db_session,
        LeadStatus.DRAFTED,
        score=8,
        contact="maria@studioflow.example",
        budget_min=1500,
        budget_max=2000,
        source="remoteok",
        title="Acme <Labs>: React dev",
    )

    await send_drafted_leads(
        bot_app_db.bot, OWNER_CHAT_ID, sessionmaker_for(db_session), can_email=True, digest=True
    )

    card = telegram.sent_messages()[-1]
    text = card["text"]
    assert f"8/10 · Remote OK · lead #{lead.id}" in text
    assert "Acme &lt;Labs&gt;: React dev" in text
    assert "Needs a dashboard" in text
    assert "Budget: $1,500 - $2,000" in text
    assert "• Matches React" in text
    assert "maria@studioflow.example (approving a draft emails it)" in text
    assert f'href="{lead.url}"' in text
    assert "Draft 1 · short" in text and "Draft 3 · problem-led" in text
    buttons = [b["callback_data"] for row in card["reply_markup"]["inline_keyboard"] for b in row]
    assert buttons == [
        f"use:{lead.id}:1",
        f"use:{lead.id}:2",
        f"use:{lead.id}:3",
        f"edit:{lead.id}",
        f"skip:{lead.id}",
    ]


async def test_approving_a_lead_with_an_email_sends_it_and_marks_it_contacted(
    db_session, bot_app_db, telegram, mailer
):
    lead = await add_drafted_lead(db_session, contact="maria@studioflow.example")

    await deliver(bot_app_db, callback_update(f"use:{lead.id}:2"))

    assert mailer.sent == [
        {
            "to": "maria@studioflow.example",
            "subject": f"Re: React dev {lead.external_id[1:]}",
            "text": f"portfolio_led text for {lead.id}",
        }
    ]
    assert lead.status is LeadStatus.CONTACTED
    assert (await chosen_draft(db_session, lead)).variant == DraftVariant.PORTFOLIO_LED
    followup = await db_session.scalar(select(Followup).where(Followup.lead_id == lead.id))
    assert timedelta(days=2, hours=23) < followup.due_at - datetime.now(UTC) <= timedelta(days=3)
    assert await event_types(db_session, lead) == ["approved", "message_sent:smtp", "contacted"]
    assert "Emailed to maria@studioflow.example" in telegram.sent_messages()[-1]["text"]
    assert ("editMessageReplyMarkup") in [method for method, _ in telegram.calls]


async def test_without_an_email_i_get_the_text_to_copy_and_a_mark_as_sent_button(
    db_session, bot_app_db, telegram, mailer
):
    lead = await add_drafted_lead(db_session, contact="u/some_client")

    await deliver(bot_app_db, callback_update(f"use:{lead.id}:1"))

    assert mailer.sent == []
    assert lead.status is LeadStatus.APPROVED
    reply = telegram.sent_messages()[-1]
    assert f"<pre>short text for {lead.id}</pre>" in reply["text"]
    assert lead.url in reply["text"]
    [[button]] = reply["reply_markup"]["inline_keyboard"]
    assert (button["text"], button["callback_data"]) == ("Mark as sent", f"sent:{lead.id}:proposal")

    await deliver(bot_app_db, callback_update(button["callback_data"]))

    assert lead.status is LeadStatus.CONTACTED
    assert await db_session.scalar(select(Followup).where(Followup.lead_id == lead.id))
    assert await event_types(db_session, lead) == ["approved", "contacted", "message_sent:manual"]


async def test_nothing_is_marked_contacted_when_the_email_fails(db_session, telegram, mailer):
    from leadhunter.bot.app import build_application

    mailer.error = smtplib.SMTPAuthenticationError(535, b"bad credentials")
    app = build_application(
        make_settings(), sessionmaker_for(db_session), request=telegram, mailer=mailer
    )
    await app.initialize()
    lead = await add_drafted_lead(db_session, contact="maria@studioflow.example")

    await deliver(app, callback_update(f"use:{lead.id}:1"))
    await app.shutdown()

    assert lead.status is LeadStatus.APPROVED
    reply = telegram.sent_messages()[-1]
    assert "Email to maria@studioflow.example failed" in reply["text"]
    assert "nothing was sent" in reply["text"]
    assert reply["reply_markup"]["inline_keyboard"][0][0]["text"] == "Mark as sent"


async def test_a_second_press_on_an_old_card_does_not_send_twice(
    db_session, bot_app_db, telegram, mailer
):
    lead = await add_drafted_lead(db_session, contact="maria@studioflow.example")
    await deliver(bot_app_db, callback_update(f"use:{lead.id}:1"))

    await deliver(bot_app_db, callback_update(f"use:{lead.id}:3"))

    assert len(mailer.sent) == 1
    alerts = [p for method, p in telegram.calls if method == "answerCallbackQuery"]
    assert "already contacted" in alerts[-1]["text"]


async def test_edit_sends_my_own_text(db_session, bot_app_db, telegram, mailer):
    lead = await add_drafted_lead(db_session, contact="maria@studioflow.example")

    await deliver(bot_app_db, callback_update(f"edit:{lead.id}"))

    prompt = telegram.sent_messages()[-1]
    assert (
        prompt["text"] == f"Proposal for lead #{lead.id}: reply to this message with your own text."
    )
    assert prompt["reply_markup"]["force_reply"] is True
    assert mailer.sent == []

    await deliver(bot_app_db, reply_update("Hi Maria, my own words.", prompt["text"]))

    assert mailer.sent[0]["text"] == "Hi Maria, my own words."
    chosen = await chosen_draft(db_session, lead)
    assert (chosen.variant, chosen.edited_text) == (DraftVariant.CUSTOM, "Hi Maria, my own words.")
    assert lead.status is LeadStatus.CONTACTED


async def test_a_reply_to_any_other_message_is_ignored(db_session, bot_app_db, telegram, mailer):
    lead = await add_drafted_lead(db_session, contact="maria@studioflow.example")

    await deliver(bot_app_db, reply_update("random text", "Digest: 3 leads to review"))

    assert mailer.sent == []
    assert lead.status is LeadStatus.SENT_TO_ME


async def test_skip(db_session, bot_app_db, telegram, mailer):
    lead = await add_drafted_lead(db_session, contact="maria@studioflow.example")

    await deliver(bot_app_db, callback_update(f"skip:{lead.id}"))

    assert lead.status is LeadStatus.SKIPPED
    assert mailer.sent == []


async def test_buttons_from_another_chat_do_nothing(db_session, bot_app_db, mailer):
    lead = await add_drafted_lead(db_session, contact="maria@studioflow.example")

    await deliver(bot_app_db, callback_update(f"use:{lead.id}:1", chat_id=OWNER_CHAT_ID + 1))

    assert mailer.sent == []
    assert lead.status is LeadStatus.SENT_TO_ME


async def test_outcome_commands(db_session, bot_app_db, telegram):
    lead = await add_lead(db_session, LeadStatus.CONTACTED)

    await deliver(bot_app_db, message_update(f"/replied {lead.id}", OWNER_CHAT_ID))
    assert lead.status is LeadStatus.REPLIED
    await deliver(bot_app_db, message_update(f"/won #{lead.id}", OWNER_CHAT_ID))
    assert lead.status is LeadStatus.WON
    await deliver(bot_app_db, message_update(f"/lost {lead.id}", OWNER_CHAT_ID))
    assert lead.status is LeadStatus.LOST

    await deliver(bot_app_db, message_update("/won", OWNER_CHAT_ID))
    assert "Usage: /won" in telegram.sent_messages()[-1]["text"]
    await deliver(bot_app_db, message_update("/won 99999", OWNER_CHAT_ID))
    assert "There is no lead #99999" in telegram.sent_messages()[-1]["text"]


async def test_threshold_pause_and_resume_are_persisted(db_session, bot_app_db, telegram):
    await deliver(bot_app_db, message_update("/threshold", OWNER_CHAT_ID))
    assert "Threshold is 6" in telegram.sent_messages()[-1]["text"]

    await deliver(bot_app_db, message_update("/threshold 8", OWNER_CHAT_ID))
    assert await get_state(db_session, THRESHOLD) == 8

    await deliver(bot_app_db, message_update("/threshold 11", OWNER_CHAT_ID))
    assert await get_state(db_session, THRESHOLD) == 8
    assert "0 to 10" in telegram.sent_messages()[-1]["text"]

    await deliver(bot_app_db, message_update("/pause", OWNER_CHAT_ID))
    assert await get_state(db_session, PAUSED) is True
    await deliver(bot_app_db, message_update("/resume", OWNER_CHAT_ID))
    assert await get_state(db_session, PAUSED) is False


async def test_leads_lists_open_leads_and_counts_those_waiting(db_session, bot_app_db, telegram):
    contacted = await add_lead(db_session, LeadStatus.CONTACTED, title="Contacted job")
    await add_lead(db_session, LeadStatus.REJECTED, title="Rejected job")
    await add_lead(db_session, LeadStatus.DRAFTED, title="Waiting job")

    await deliver(bot_app_db, message_update("/leads", OWNER_CHAT_ID))

    text = telegram.sent_messages()[-1]["text"]
    assert f"#{contacted.id} · contacted · 7/10" in text
    assert "Rejected job" not in text
    assert "1 more drafted and waiting for the next digest." in text


async def contacted_lead(session, days_ago=4, **kwargs) -> Lead:
    lead = await add_drafted_lead(session, LeadStatus.APPROVED, **kwargs)
    first = await session.scalar(select(Draft).where(Draft.lead_id == lead.id).order_by(Draft.id))
    first.chosen = True
    mark_contacted(session, lead)
    await session.flush()
    followup = await session.scalar(select(Followup).where(Followup.lead_id == lead.id))
    followup.due_at = datetime.now(UTC) - timedelta(days=days_ago - 3, minutes=1)
    return lead


async def test_due_followups_are_drafted_once_for_leads_still_waiting(db_session):
    waiting = await contacted_lead(db_session)
    replied = await contacted_lead(db_session)
    set_status(db_session, replied, LeadStatus.REPLIED)
    not_due = await contacted_lead(db_session, days_ago=1)
    llm = FakeLLM()

    drafts = await draft_due_followups(db_session, llm, PROFILE)

    assert [(d.lead_id, d.variant, d.text) for d in drafts] == [
        (waiting.id, DraftVariant.FOLLOWUP, "Following up with a question.")
    ]
    assert f"short text for {waiting.id}" in llm.calls[0][1]
    done = dict((await db_session.execute(select(Followup.lead_id, Followup.done))).all())
    assert done == {waiting.id: True, replied.id: True, not_due.id: False}
    assert await draft_due_followups(db_session, llm, PROFILE) == []


async def test_followup_job_sends_a_card_and_approving_it_emails_the_client(
    db_session, bot_app_db, telegram, mailer, monkeypatch
):
    lead = await contacted_lead(db_session, contact="maria@studioflow.example")
    connect_llm(monkeypatch, FakeLLM())
    deps = jobs.Deps(
        settings=make_settings(),
        profile=PROFILE,
        sessionmaker=sessionmaker_for(db_session),
        bot=bot_app_db.bot,
        can_email=True,
    )

    await jobs.followups(deps)

    card = telegram.sent_messages()[-1]
    assert f"Follow-up due · lead #{lead.id}" in card["text"]
    assert "Following up with a question." in card["text"]
    send_button = card["reply_markup"]["inline_keyboard"][0][0]
    assert send_button["text"] == "Send follow-up"

    await deliver(bot_app_db, callback_update(send_button["callback_data"]))

    assert mailer.sent[-1]["text"] == "Following up with a question."
    assert lead.status is LeadStatus.CONTACTED
    followups = (await db_session.scalars(select(Followup))).all()
    assert len(followups) == 1

    await deliver(bot_app_db, callback_update(send_button["callback_data"]))
    assert len(mailer.sent) == 1


async def test_pipeline_job_waits_while_no_model_is_connected(db_session, bot_app_db, telegram):
    lead = await add_lead(db_session, LeadStatus.NEW, score=None)
    deps = jobs.Deps(
        settings=make_settings(),
        profile=PROFILE,
        sessionmaker=sessionmaker_for(db_session),
        bot=bot_app_db.bot,
        can_email=False,
    )

    await jobs.pipeline(deps)
    await jobs.followups(deps)

    assert lead.status is LeadStatus.NEW
    assert telegram.sent_messages() == []


async def test_pipeline_job_scores_drafts_and_sends_only_urgent_leads(
    db_session, bot_app_db, telegram, monkeypatch
):
    lead = await add_lead(db_session, LeadStatus.NEW, score=None)
    connect_llm(monkeypatch, FakeLLM(score=9))
    deps = jobs.Deps(
        settings=make_settings(),
        profile=PROFILE,
        sessionmaker=sessionmaker_for(db_session),
        bot=bot_app_db.bot,
        can_email=False,
    )

    await jobs.pipeline(deps)

    assert lead.status is LeadStatus.SENT_TO_ME
    assert f"9/10 · Reddit · lead #{lead.id}" in telegram.sent_messages()[-1]["text"]
    assert await event_types(db_session, lead) == [
        "scored",
        "drafted",
        "sent_to_me",
        "message_sent:telegram",
    ]


async def test_pipeline_job_does_nothing_while_paused(
    db_session, bot_app_db, telegram, monkeypatch
):
    lead = await add_lead(db_session, LeadStatus.NEW, score=None)
    await deliver(bot_app_db, message_update("/pause", OWNER_CHAT_ID))
    llm = FakeLLM(score=9)
    connect_llm(monkeypatch, llm)
    deps = jobs.Deps(
        settings=make_settings(),
        profile=PROFILE,
        sessionmaker=sessionmaker_for(db_session),
        bot=bot_app_db.bot,
        can_email=False,
    )

    await jobs.pipeline(deps)

    assert lead.status is LeadStatus.NEW
    assert llm.calls == []


async def test_stats_per_draft_variant_and_weekly_window(db_session):
    async def lead_sent_with(variant, outcome=None, source="reddit", days_old=0):
        lead = await add_lead(db_session, LeadStatus.APPROVED, source=source)
        lead.fetched_at = datetime.now(UTC) - timedelta(days=days_old)
        db_session.add(Draft(lead_id=lead.id, variant=variant, text="t", chosen=True))
        db_session.add(Draft(lead_id=lead.id, variant="problem_led", text="t", chosen=False))
        set_status(db_session, lead, LeadStatus.CONTACTED)
        if outcome:
            set_status(db_session, lead, outcome)

    await lead_sent_with("short", LeadStatus.REPLIED)
    await lead_sent_with("short")
    await lead_sent_with("portfolio_led", LeadStatus.WON, source="hackernews", days_old=10)
    await lead_sent_with("custom")
    await db_session.flush()

    stats = await collect_stats(db_session)

    short = stats.by_variant["short"]
    assert (short.found, short.contacted, short.replied, short.won) == (2, 2, 1, 0)
    portfolio = stats.by_variant["portfolio_led"]
    assert (portfolio.contacted, portfolio.replied, portfolio.won) == (1, 1, 1)
    assert stats.by_variant["custom"].contacted == 1
    assert "problem_led" not in stats.by_variant
    assert stats.total.contacted == 4

    week = await collect_stats(db_session, since=datetime.now(UTC) - timedelta(days=7))
    assert week.total.contacted == 3
    assert "hackernews" not in week.by_source
    assert "portfolio_led" not in week.by_variant


def test_guidance_appears_only_after_20_contacted_leads():
    from leadhunter.tracker.stats import Counts, Stats

    early = Stats(
        total=Counts(found=50, contacted=19, replied=5),
        by_source={"reddit": Counts(found=50, contacted=19, replied=5)},
    )
    assert build_guidance(early) == ("", "")

    later = Stats(
        total=Counts(found=90, contacted=24, replied=7),
        by_source={
            "reddit": Counts(found=50, contacted=12, replied=6),
            "hackernews": Counts(found=30, contacted=10, replied=1),
            "remoteok": Counts(found=10, contacted=2, replied=0),
        },
        by_variant={
            "short": Counts(found=14, contacted=14, replied=6),
            "problem_led": Counts(found=8, contacted=8, replied=1),
            "custom": Counts(found=2, contacted=2, replied=0),
        },
    )
    scorer, drafter = build_guidance(later)
    assert "reddit: 6 of 12 contacted replied; hackernews: 1 of 10 contacted replied" in scorer
    assert "remoteok" not in scorer
    assert "short: 6 of 14 contacted replied; problem_led: 1 of 8 contacted replied" in drafter
    assert "custom" not in drafter
