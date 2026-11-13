from datetime import UTC, datetime, timedelta

from leadhunter import jobs
from leadhunter.agent import conversation as talk
from leadhunter.agent.report import build_report, pack, start_of_day
from leadhunter.db.models import ConversationState, LeadStatus, MessageKind
from leadhunter.tracker.events import set_status
from tests.conftest import OWNER_CHAT_ID, message_update
from tests.test_approval_db import add_lead, deliver
from tests.test_conversation_db import CLIENT, contacted, make_deps

HOUR_AGO = timedelta(hours=1)


async def report(session) -> str:
    since = datetime.now(UTC) - HOUR_AGO
    return "\n\n".join(await build_report(session, since=since, day="Fri 13 Nov"))


async def test_an_empty_day_reports_zeros(db_session):
    text = await report(db_session)

    assert "<b>Daily report · Fri 13 Nov</b>" in text
    assert "Projects found today: <b>0</b>" in text
    assert "Waiting for you: <b>0</b>" in text
    assert "Clients I am talking to" not in text


async def test_report_lists_each_client_with_brief_and_last_message(db_session):
    lead, conversation = await contacted(db_session)
    conversation.client_name = "Maria"
    conversation.brief = "Booking dashboard in React with an admin view."
    conversation.state = ConversationState.WAITING_OWNER
    conversation.question = "What price for the dashboard?"
    talk._add(db_session, conversation, MessageKind.CLIENT, "What would it cost?")
    talk._add(db_session, conversation, MessageKind.DRAFT, "A draft that was never sent")
    await add_lead(
        db_session, LeadStatus.SENT_TO_ME, source="hackernews", contact="jo@acme.example"
    )
    await add_lead(db_session, LeadStatus.REJECTED, source="hackernews", score=2)
    won = await add_lead(db_session, LeadStatus.CONTACTED)
    set_status(db_session, won, LeadStatus.WON)
    await db_session.flush()

    text = await report(db_session)

    assert "Projects found today: <b>4</b> (Hacker News 2, Reddit 2)" in text
    assert "Passed scoring: <b>3</b>" in text
    assert "Messages sent to clients: <b>1</b>" in text
    assert "Client replies: <b>1</b>" in text
    assert "Deals agreed: <b>1</b>" in text
    assert "Waiting for you: <b>2</b> (1 client questions, 1 lead cards)" in text

    assert f"<b>#{lead.id} · [Hiring] React dev" in text
    assert f"Client: Maria · {CLIENT}" in text
    assert "Status: waiting for your answer" in text
    assert "Project: Booking dashboard in React with an admin view." in text
    assert "Last message sent: “Hi Maria, I can build this. Test”" in text
    assert "Client's last message: “What would it cost?”" in text
    assert "Needs your answer: What price for the dashboard?" in text
    assert "never sent" not in text

    assert "<b>Found today, not contacted yet</b>" in text
    assert "Contact: jo@acme.example" in text


async def test_a_quiet_open_conversation_is_still_listed_but_a_closed_one_is_not(db_session):
    open_lead, open_one = await contacted(db_session)
    closed_lead, closed_one = await contacted(db_session, contact="old@client.example")
    closed_one.state = ConversationState.CLOSED
    await db_session.flush()
    tomorrow = datetime.now(UTC) + timedelta(days=1)

    text = "\n".join(await build_report(db_session, since=tomorrow, day="Sat 14 Nov"))

    assert f"#{open_lead.id} ·" in text
    assert f"#{closed_lead.id} ·" not in text


def test_long_reports_are_split_into_telegram_sized_messages():
    blocks = ["a" * 1500, "b" * 1500, "c" * 1500, "d" * 100]

    messages = pack(blocks)

    assert [len(m) for m in messages] == [3002, 1602]
    assert pack([]) == []


def test_the_day_starts_at_local_midnight():
    start = start_of_day("Asia/Karachi")

    assert (start.hour, start.minute, start.utcoffset()) == (0, 0, timedelta(hours=5))


async def test_daily_report_job_and_command_send_the_report(
    db_session, bot_app_db, telegram, mailer
):
    await contacted(db_session)

    await jobs.daily_report(make_deps(db_session, bot_app_db, mailer))
    assert "<b>Daily report ·" in telegram.sent_messages()[-1]["text"]
    assert "Clients I am talking to" in telegram.sent_messages()[-1]["text"]

    count = len(telegram.sent_messages())
    await deliver(bot_app_db, message_update("/report", OWNER_CHAT_ID))
    assert len(telegram.sent_messages()) == count + 1
    assert "<b>Daily report ·" in telegram.sent_messages()[-1]["text"]
