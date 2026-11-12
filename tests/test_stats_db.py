from leadhunter.db.models import Event, Lead, LeadStatus
from leadhunter.tracker.events import set_status
from leadhunter.tracker.stats import collect_stats


def _lead(source: str, external_id: str) -> Lead:
    return Lead(source=source, external_id=external_id, title="A job", content_hash=external_id)


async def test_empty_database_gives_zeros(db_session):
    stats = await collect_stats(db_session)

    assert stats.by_source == {}
    assert (stats.total.found, stats.total.contacted, stats.total.replied, stats.total.won) == (
        0,
        0,
        0,
        0,
    )
    assert stats.total.reply_rate is None


async def test_counts_come_from_status_history(db_session):
    untouched = _lead("reddit", "1")
    contacted_then_lost = _lead("reddit", "2")
    won_without_replied = _lead("reddit", "3")
    replied = _lead("hackernews", "4")
    db_session.add_all([untouched, contacted_then_lost, won_without_replied, replied])
    await db_session.flush()

    set_status(db_session, contacted_then_lost, LeadStatus.CONTACTED)
    set_status(db_session, contacted_then_lost, LeadStatus.LOST)
    set_status(db_session, won_without_replied, LeadStatus.CONTACTED)
    set_status(db_session, won_without_replied, LeadStatus.WON)
    set_status(db_session, replied, LeadStatus.CONTACTED)
    set_status(db_session, replied, LeadStatus.REPLIED)
    await db_session.commit()

    stats = await collect_stats(db_session)

    reddit = stats.by_source["reddit"]
    assert (reddit.found, reddit.contacted, reddit.replied, reddit.won) == (3, 2, 1, 1)
    assert reddit.reply_rate == 0.5
    hn = stats.by_source["hackernews"]
    assert (hn.found, hn.contacted, hn.replied, hn.won) == (1, 1, 1, 0)
    assert (stats.total.found, stats.total.contacted) == (4, 3)


async def test_set_status_records_an_event(db_session):
    lead = _lead("reddit", "1")
    db_session.add(lead)
    await db_session.flush()

    set_status(db_session, lead, LeadStatus.SCORED)
    await db_session.commit()

    event = (await db_session.execute(Event.__table__.select())).one()
    assert event.lead_id == lead.id
    assert event.type == "status_changed"
    assert event.payload == {"from": "new", "to": "scored"}
    assert lead.status is LeadStatus.SCORED
