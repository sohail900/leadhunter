import smtplib
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from leadhunter import jobs, state
from leadhunter.agent import conversation as talk
from leadhunter.agent.conversation import Decision
from leadhunter.agent.outreach import auto_contact
from leadhunter.bot import deals
from leadhunter.config import Profile
from leadhunter.db.models import (
    Conversation,
    ConversationState,
    LeadStatus,
    Message,
    MessageKind,
)
from leadhunter.inbox import InboundEmail
from leadhunter.llm.providers import LLMError
from leadhunter.tracker.followups import mark_contacted
from tests.conftest import (
    OWNER_CHAT_ID,
    callback_update,
    make_settings,
    message_update,
    reply_update,
    sessionmaker_for,
)
from tests.test_approval_db import add_drafted_lead, add_lead, deliver

PROFILE = Profile(name="Test Dev", skills=["React"])
CLIENT = "maria@studioflow.example"


class ScriptedLLM:
    name = "scripted"

    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.calls = []

    async def complete_json(self, system, user, schema):
        self.calls.append((system, user, schema))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        assert isinstance(reply, schema)
        return reply


class FakeInbox:
    def __init__(self, *emails: InboundEmail) -> None:
        self.emails = list(emails)
        self.asked_for: list[list[str]] = []

    async def fetch(self, senders, since):
        self.asked_for.append(sorted(senders))
        return [e for e in self.emails if e.sender in senders]


def reply(text="Happy to. Do you have designs already?", **fields) -> Decision:
    return Decision(action="reply", message=text, brief="A booking dashboard in React.", **fields)


def ask(question="The client asks for a price for the dashboard.") -> Decision:
    return Decision(action="ask_owner", owner_question=question, brief="A booking dashboard.")


def inbound(text, n=1, sender=CLIENT, references=(), minutes_ago=0) -> InboundEmail:
    return InboundEmail(
        sender=sender,
        message_id=f"<client-{n}@studioflow.example>",
        references=tuple(references),
        text=text,
        received_at=datetime.now(UTC) + timedelta(minutes=5 - minutes_ago),
    )


def connect(monkeypatch, llm) -> None:

    async def connected(session):
        if llm is None:
            raise LLMError("no model is connected")
        return llm

    monkeypatch.setattr(jobs, "active_llm", connected)
    monkeypatch.setattr(deals, "active_llm", connected)


def make_deps(db_session, app, mailer, inbox=None) -> jobs.Deps:
    return jobs.Deps(
        settings=make_settings(),
        profile=PROFILE,
        sessionmaker=sessionmaker_for(db_session),
        bot=app.bot,
        can_email=True,
        mailer=mailer,
        inbox=inbox,
    )


async def contacted(session, contact=CLIENT, first="Hi Maria, I can build this. Test"):
    lead = await add_lead(session, LeadStatus.APPROVED, contact=contact, author="maria_sf")
    mark_contacted(session, lead, via="smtp")
    channel = talk.EMAIL if "@" in (contact or "") else talk.MANUAL
    conversation = await talk.record_sent(
        session, lead, first, channel=channel, external_id="<first@me.test>"
    )
    await session.flush()
    return lead, conversation


async def kinds(session, conversation) -> list[tuple[str, str]]:
    return [(m.kind.value, m.text) for m in await talk.messages_of(session, conversation.id)]


def buttons(card: dict) -> dict[str, str]:
    rows = card["reply_markup"]["inline_keyboard"]
    return {button["text"]: button["callback_data"] for row in rows for button in row}


async def test_approving_a_draft_by_email_opens_a_conversation(db_session, bot_app_db, mailer):
    lead = await add_drafted_lead(db_session, contact=CLIENT)

    await deliver(bot_app_db, callback_update(f"use:{lead.id}:1"))

    conversation = await talk.conversation_for(db_session, lead.id)
    assert (conversation.channel, conversation.address) == ("email", CLIENT)
    assert conversation.state is ConversationState.ACTIVE
    assert conversation.subject == mailer.sent[0]["subject"]
    messages = await talk.messages_of(db_session, conversation.id)
    assert [(m.kind, m.text, m.external_id) for m in messages] == [
        (MessageKind.SENT, f"short text for {lead.id}", "<sent-1@me.test>")
    ]


async def test_marking_a_proposal_as_sent_by_hand_opens_a_manual_conversation(
    db_session, bot_app_db
):
    lead = await add_drafted_lead(db_session, contact="u/maria_sf")
    await deliver(bot_app_db, callback_update(f"use:{lead.id}:2"))

    await deliver(bot_app_db, callback_update(f"sent:{lead.id}:proposal"))

    conversation = await talk.conversation_for(db_session, lead.id)
    assert (conversation.channel, conversation.address) == ("manual", "u/maria_sf")
    assert await kinds(db_session, conversation) == [("sent", f"portfolio_led text for {lead.id}")]


async def test_a_client_reply_is_drafted_and_sent_only_after_i_press_send(
    db_session, bot_app_db, telegram, mailer, monkeypatch
):
    lead, conversation = await contacted(db_session)
    llm = ScriptedLLM(reply(client_name="Maria"))
    connect(monkeypatch, llm)
    inbox = FakeInbox(inbound("Thanks! Can you tell me more about how you work?"))

    await jobs.inbox(make_deps(db_session, bot_app_db, mailer, inbox))

    assert inbox.asked_for == [[CLIENT]]
    assert mailer.sent == []
    assert lead.status is LeadStatus.REPLIED
    assert (conversation.client_name, conversation.brief) == (
        "Maria",
        "A booking dashboard in React.",
    )
    card = telegram.sent_messages()[-1]
    assert f"Reply to client · lead #{lead.id}" in card["text"]
    assert "Can you tell me more about how you work?" in card["text"]
    assert "Do you have designs already?" in card["text"]
    system, user, _ = llm.calls[0]
    assert "name: Test Dev" in system
    assert "Freelancer:\nHi Maria, I can build this. Test" in user
    assert "Client:\nThanks! Can you tell me more about how you work?" in user

    await deliver(bot_app_db, callback_update(buttons(card)["Send"]))

    assert mailer.sent == [
        {
            "to": CLIENT,
            "subject": conversation.subject,
            "text": "Happy to. Do you have designs already?",
            "reply_to": "<client-1@studioflow.example>",
        }
    ]
    assert [kind for kind, _ in await kinds(db_session, conversation)] == [
        "sent",
        "client",
        "sent",
    ]

    await deliver(bot_app_db, callback_update(buttons(card)["Send"]))
    assert len(mailer.sent) == 1


async def test_with_auto_on_the_reply_is_emailed_at_once_and_i_am_told(
    db_session, bot_app_db, telegram, mailer, monkeypatch
):
    lead, conversation = await contacted(db_session)
    await state.set_state(db_session, state.AUTO, True)
    connect(monkeypatch, ScriptedLLM(reply()))

    await jobs.inbox(make_deps(db_session, bot_app_db, mailer, FakeInbox(inbound("Tell me more"))))

    assert [m["text"] for m in mailer.sent] == ["Happy to. Do you have designs already?"]
    notice = telegram.sent_messages()[-1]
    assert f"Replied to client · lead #{lead.id}" in notice["text"]
    assert "reply_markup" not in notice or not notice["reply_markup"]
    assert conversation.state is ConversationState.ACTIVE


async def test_the_same_email_is_handled_once(db_session, bot_app_db, mailer, monkeypatch):
    _, conversation = await contacted(db_session)
    llm = ScriptedLLM(reply(), reply())
    connect(monkeypatch, llm)
    deps = make_deps(db_session, bot_app_db, mailer, FakeInbox(inbound("Tell me more")))

    await jobs.inbox(deps)
    await jobs.inbox(deps)

    assert len(llm.calls) == 1
    assert [k for k, _ in await kinds(db_session, conversation)] == ["sent", "client", "draft"]


async def test_mail_from_before_the_first_contact_or_from_strangers_is_ignored(
    db_session, bot_app_db, mailer, monkeypatch
):
    _, conversation = await contacted(db_session)
    llm = ScriptedLLM()
    connect(monkeypatch, llm)
    inbox = FakeInbox(
        inbound("An old newsletter", minutes_ago=600),
        inbound("Hello", n=2, sender="stranger@else.example"),
    )

    await jobs.inbox(make_deps(db_session, bot_app_db, mailer, inbox))

    assert llm.calls == []
    assert [k for k, _ in await kinds(db_session, conversation)] == ["sent"]


async def test_a_price_question_comes_to_me_and_my_answer_goes_to_the_client(
    db_session, bot_app_db, telegram, mailer, monkeypatch
):
    lead, conversation = await contacted(db_session)
    await state.set_state(db_session, state.AUTO, True)
    llm = ScriptedLLM(ask(), reply("I can do the dashboard for $800, delivered in two weeks. Test"))
    connect(monkeypatch, llm)

    await jobs.inbox(
        make_deps(db_session, bot_app_db, mailer, FakeInbox(inbound("What would it cost?")))
    )

    assert mailer.sent == []
    assert conversation.state is ConversationState.WAITING_OWNER
    card = telegram.sent_messages()[-1]
    assert card["text"].startswith(f"<b>Client question · lead #{lead.id}</b>")
    assert "What would it cost?" in card["text"]
    assert "The client asks for a price for the dashboard." in card["text"]
    assert CLIENT in card["text"]
    assert set(buttons(card)) == {"Answer", "Write the reply myself"}

    question_text = f"Client question · lead #{lead.id}\nrest of the card"
    await deliver(bot_app_db, reply_update("800 dollars, two weeks", question_text))

    assert (
        "Owner instruction (private, the client never sees this):\n800 dollars, two weeks"
        in (llm.calls[1][1])
    )
    assert (
        mailer.sent[-1]["text"] == "I can do the dashboard for $800, delivered in two weeks. Test"
    )
    assert llm.calls[0][1].endswith("Decide the next step.")
    assert "Do not ask the owner to confirm" in llm.calls[1][1]
    assert conversation.state is ConversationState.ACTIVE
    assert conversation.question is None


async def test_the_answer_button_asks_for_my_answer(db_session, bot_app_db, telegram):
    lead, _ = await contacted(db_session)

    await deliver(bot_app_db, callback_update(f"cans:{lead.id}"))

    prompt = telegram.sent_messages()[-1]
    assert prompt["text"].startswith(f"Answer for lead #{lead.id}:")
    assert prompt["reply_markup"]["force_reply"] is True
    assert deals.CLIENT_PROMPT.match(prompt["text"]).group(2) == str(lead.id)


async def test_a_price_i_never_gave_is_stopped_before_it_reaches_the_client(
    db_session, bot_app_db, telegram, mailer, monkeypatch
):
    lead, conversation = await contacted(db_session)
    await state.set_state(db_session, state.AUTO, True)
    connect(monkeypatch, ScriptedLLM(reply("Sure, I can do this for $500.")))

    await jobs.inbox(
        make_deps(db_session, bot_app_db, mailer, FakeInbox(inbound("Is $500 ok for you?")))
    )

    assert mailer.sent == []
    assert conversation.state is ConversationState.WAITING_OWNER
    card = telegram.sent_messages()[-1]
    assert f"Client question · lead #{lead.id}" in card["text"]
    assert "would have mentioned 500" in card["text"]


async def test_a_deal_needs_my_terms_first(db_session, bot_app_db, telegram, mailer, monkeypatch):
    lead, conversation = await contacted(db_session)
    await state.set_state(db_session, state.AUTO, True)
    deal = Decision(action="deal", message="Great, let's start Monday.", brief="Dashboard.")
    connect(monkeypatch, ScriptedLLM(deal))

    await jobs.inbox(make_deps(db_session, bot_app_db, mailer, FakeInbox(inbound("Let's do it"))))

    assert mailer.sent == []
    assert lead.status is LeadStatus.REPLIED
    assert conversation.state is ConversationState.WAITING_OWNER
    assert "have not given me a price or terms" in telegram.sent_messages()[-1]["text"]


async def test_an_accepted_price_closes_the_deal_and_i_get_the_brief(
    db_session, bot_app_db, telegram, mailer, monkeypatch
):
    lead, conversation = await contacted(db_session)
    await state.set_state(db_session, state.AUTO, True)
    talk._add(db_session, conversation, MessageKind.OWNER, "800 dollars, two weeks")
    deal = Decision(
        action="deal",
        message="Great. $800 for the dashboard, I will send the details today. Test",
        brief="Booking dashboard in React with an admin view. $800, two weeks.",
        client_name="Maria",
    )
    connect(monkeypatch, ScriptedLLM(deal))

    await jobs.inbox(
        make_deps(db_session, bot_app_db, mailer, FakeInbox(inbound("$800 works, let's go")))
    )

    assert mailer.sent[-1]["text"].startswith("Great. $800 for the dashboard")
    assert lead.status is LeadStatus.WON
    assert conversation.state is ConversationState.AGREED
    card = telegram.sent_messages()[-1]
    assert f"Deal agreed · lead #{lead.id}" in card["text"]
    assert "Booking dashboard in React with an admin view." in card["text"]
    assert "800 dollars, two weeks" in card["text"]
    assert f"Maria · {CLIENT}" in card["text"]


async def test_a_client_who_declines_closes_the_conversation(
    db_session, bot_app_db, telegram, mailer, monkeypatch
):
    lead, conversation = await contacted(db_session)
    await state.set_state(db_session, state.AUTO, True)
    connect(monkeypatch, ScriptedLLM(Decision(action="lost", brief="Dashboard.")))

    await jobs.inbox(
        make_deps(db_session, bot_app_db, mailer, FakeInbox(inbound("We hired someone else")))
    )

    assert mailer.sent == []
    assert lead.status is LeadStatus.LOST
    assert conversation.state is ConversationState.CLOSED
    assert f"Client declined · lead #{lead.id}" in telegram.sent_messages()[-1]["text"]


async def test_when_the_model_fails_the_client_message_still_reaches_me(
    db_session, bot_app_db, telegram, mailer, monkeypatch
):
    lead, conversation = await contacted(db_session)
    connect(monkeypatch, ScriptedLLM(LLMError("HTTP 500")))

    await jobs.inbox(make_deps(db_session, bot_app_db, mailer, FakeInbox(inbound("Any update?"))))

    card = telegram.sent_messages()[-1]
    assert f"Client question · lead #{lead.id}" in card["text"]
    assert "Any update?" in card["text"]
    assert "model call failed" in card["text"]
    assert conversation.state is ConversationState.WAITING_OWNER


async def test_without_a_model_the_client_message_still_reaches_me(
    db_session, bot_app_db, telegram, mailer, monkeypatch
):
    lead, _ = await contacted(db_session)
    connect(monkeypatch, None)

    await jobs.inbox(make_deps(db_session, bot_app_db, mailer, FakeInbox(inbound("Any update?"))))

    card = telegram.sent_messages()[-1]
    assert "Any update?" in card["text"]
    assert "No model is connected" in card["text"]


async def test_my_own_words_are_emailed_exactly(db_session, bot_app_db, telegram, mailer):
    lead, conversation = await contacted(db_session)
    conversation.state = ConversationState.WAITING_OWNER
    await deliver(bot_app_db, callback_update(f"cown:{lead.id}"))
    prompt = telegram.sent_messages()[-1]["text"]
    assert prompt.startswith(f"Your reply for lead #{lead.id}:")

    await deliver(bot_app_db, reply_update("It is 900 USD, fixed.", prompt))

    assert mailer.sent[-1] == {
        "to": CLIENT,
        "subject": conversation.subject,
        "text": "It is 900 USD, fixed.",
        "reply_to": "<first@me.test>",
    }
    assert conversation.state is ConversationState.ACTIVE
    assert f"Sent to {CLIENT}" in telegram.sent_messages()[-1]["text"]


async def test_skipping_a_drafted_reply_sends_nothing(
    db_session, bot_app_db, telegram, mailer, monkeypatch
):
    _, conversation = await contacted(db_session)
    connect(monkeypatch, ScriptedLLM(reply()))
    await jobs.inbox(make_deps(db_session, bot_app_db, mailer, FakeInbox(inbound("Tell me more"))))
    card = telegram.sent_messages()[-1]

    await deliver(bot_app_db, callback_update(buttons(card)["Skip"]))

    assert mailer.sent == []
    assert [k for k, _ in await kinds(db_session, conversation)] == ["sent", "client"]


async def test_replying_to_a_draft_tells_the_bot_what_to_change(
    db_session, bot_app_db, telegram, mailer, monkeypatch
):
    lead, conversation = await contacted(db_session)
    llm = ScriptedLLM(reply("A long first attempt."), reply("Shorter."))
    connect(monkeypatch, llm)
    await jobs.inbox(make_deps(db_session, bot_app_db, mailer, FakeInbox(inbound("Tell me more"))))

    await deliver(
        bot_app_db, reply_update("make it shorter", f"Reply to client · lead #{lead.id}\n…")
    )

    assert (
        "Owner instruction (private, the client never sees this):\nmake it shorter"
        in (llm.calls[1][1])
    )
    assert await kinds(db_session, conversation) == [
        ("sent", "Hi Maria, I can build this. Test"),
        ("client", "Tell me more"),
        ("owner", "make it shorter"),
        ("draft", "Shorter."),
    ]
    assert "Shorter." in telegram.sent_messages()[-1]["text"]


async def test_a_reply_i_relay_by_hand_gets_a_draft_to_send_myself(
    db_session, bot_app_db, telegram, mailer, monkeypatch
):
    lead, conversation = await contacted(db_session, contact="u/maria_sf")
    await state.set_state(db_session, state.AUTO, True)
    connect(monkeypatch, ScriptedLLM(reply()))

    await deliver(
        bot_app_db, message_update(f"/client {lead.id} Sounds good, tell me more", OWNER_CHAT_ID)
    )

    assert mailer.sent == []
    assert lead.status is LeadStatus.REPLIED
    card = telegram.sent_messages()[-1]
    assert "Sounds good, tell me more" in card["text"]
    assert "send it yourself" in card["text"]

    await deliver(bot_app_db, callback_update(buttons(card)["Mark as sent"]))

    assert [k for k, _ in await kinds(db_session, conversation)] == ["sent", "client", "sent"]


async def test_client_command_explains_itself(db_session, bot_app_db, telegram):
    await deliver(bot_app_db, message_update("/client 5", OWNER_CHAT_ID))
    assert "Usage: /client" in telegram.sent_messages()[-1]["text"]

    await deliver(bot_app_db, message_update("/client 999 hello there", OWNER_CHAT_ID))
    assert "There is no lead #999" in telegram.sent_messages()[-1]["text"]


async def test_a_message_after_the_deal_comes_to_me_instead_of_being_answered(
    db_session, bot_app_db, telegram, mailer, monkeypatch
):
    lead, conversation = await contacted(db_session)
    conversation.state = ConversationState.AGREED
    await state.set_state(db_session, state.AUTO, True)
    llm = ScriptedLLM()
    connect(monkeypatch, llm)

    await jobs.inbox(
        make_deps(db_session, bot_app_db, mailer, FakeInbox(inbound("Here is the repo access")))
    )

    assert llm.calls == [] and mailer.sent == []
    assert conversation.state is ConversationState.AGREED
    card = telegram.sent_messages()[-1]
    assert "Here is the repo access" in card["text"]
    assert "already marked deal agreed" in card["text"]


async def test_a_reply_is_matched_to_the_thread_it_answers(
    db_session, bot_app_db, mailer, monkeypatch
):
    _, older = await contacted(db_session)
    newer_lead = await add_lead(db_session, LeadStatus.CONTACTED, contact=CLIENT)
    newer = await talk.record_sent(
        db_session, newer_lead, "Second job", channel=talk.EMAIL, external_id="<second@me.test>"
    )
    connect(monkeypatch, ScriptedLLM(reply()))
    email = inbound("About the first job", references=["<first@me.test>"])

    await jobs.inbox(make_deps(db_session, bot_app_db, mailer, FakeInbox(email)))

    assert [k for k, _ in await kinds(db_session, older)] == ["sent", "client", "draft"]
    assert [k for k, _ in await kinds(db_session, newer)] == ["sent"]


async def test_marking_a_lead_lost_stops_the_conversation(db_session, bot_app_db):
    lead, conversation = await contacted(db_session)

    await deliver(bot_app_db, message_update(f"/lost {lead.id}", OWNER_CHAT_ID))

    assert conversation.state is ConversationState.CLOSED


async def test_auto_command_switches_automatic_sending(db_session, bot_app_db, telegram):
    await deliver(bot_app_db, message_update("/auto", OWNER_CHAT_ID))
    assert "Automatic sending is off" in telegram.sent_messages()[-1]["text"]

    await deliver(bot_app_db, message_update("/auto on", OWNER_CHAT_ID))
    assert await state.is_auto(db_session)
    assert "at most 10 a day" in telegram.sent_messages()[-1]["text"]

    await deliver(bot_app_db, message_update("/auto off", OWNER_CHAT_ID))
    assert not await state.is_auto(db_session)


async def auto_run(db_session, app, mailer, limit=10) -> int:
    deps = make_deps(db_session, app, mailer)
    return await auto_contact(
        deps.sessionmaker, deps.desk, daily_limit=limit, timezone="Asia/Karachi"
    )


async def test_auto_contact_emails_the_first_message_and_opens_the_conversation(
    db_session, bot_app_db, telegram, mailer
):
    await state.set_state(db_session, state.AUTO, True)
    with_email = await add_drafted_lead(db_session, LeadStatus.DRAFTED, contact=CLIENT, score=9)
    on_reddit = await add_drafted_lead(db_session, LeadStatus.DRAFTED, contact="u/someone")

    assert await auto_run(db_session, bot_app_db, mailer) == 1

    assert [(m["to"], m["text"]) for m in mailer.sent] == [
        (CLIENT, f"short text for {with_email.id}")
    ]
    assert with_email.status is LeadStatus.CONTACTED
    assert on_reddit.status is LeadStatus.DRAFTED
    conversation = await talk.conversation_for(db_session, with_email.id)
    assert await kinds(db_session, conversation) == [("sent", f"short text for {with_email.id}")]
    notice = telegram.sent_messages()[-1]["text"]
    assert f"First message sent · lead #{with_email.id}" in notice
    assert f"short text for {with_email.id}" in notice


async def test_auto_contact_does_nothing_while_auto_is_off(db_session, bot_app_db, mailer):
    lead = await add_drafted_lead(db_session, LeadStatus.DRAFTED, contact=CLIENT)

    assert await auto_run(db_session, bot_app_db, mailer) == 0

    assert mailer.sent == []
    assert lead.status is LeadStatus.DRAFTED


async def test_auto_contact_stops_at_the_daily_limit(db_session, bot_app_db, mailer):
    await state.set_state(db_session, state.AUTO, True)
    for n in range(3):
        await add_drafted_lead(db_session, LeadStatus.DRAFTED, contact=f"c{n}@acme.example")

    assert await auto_run(db_session, bot_app_db, mailer, limit=2) == 2
    assert await auto_run(db_session, bot_app_db, mailer, limit=2) == 0

    assert len(mailer.sent) == 2
    waiting = await db_session.scalars(
        select(Conversation.id).join(Message, Message.conversation_id == Conversation.id)
    )
    assert len(waiting.all()) == 2


async def test_a_failed_email_leaves_the_lead_for_me(db_session, bot_app_db, mailer):
    await state.set_state(db_session, state.AUTO, True)
    lead = await add_drafted_lead(db_session, LeadStatus.DRAFTED, contact=CLIENT)
    mailer.error = smtplib.SMTPServerDisconnected("gone")

    assert await auto_run(db_session, bot_app_db, mailer) == 0

    assert lead.status is LeadStatus.DRAFTED
    assert await talk.conversation_for(db_session, lead.id) is None
