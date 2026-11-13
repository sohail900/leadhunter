from datetime import datetime
from types import SimpleNamespace

from leadhunter.config import Settings
from leadhunter.inbox import InboxReader, is_automatic, parse_message, strip_quoted
from leadhunter.mailer import Mailer


def email(**overrides):
    fields = {
        "subject": "Re: React dev",
        "from_": "Maria@StudioFlow.example",
        "text": "Sounds good. What would it cost?\n\nOn Fri, 13 Nov 2026, Dev wrote:\n> Hi Maria",
        "html": "",
        "uid": "77",
        "date": datetime(2026, 11, 13, 10, 0),
        "headers": {
            "message-id": ("<reply-1@studioflow.example>",),
            "in-reply-to": ("<sent-1@me.test>",),
            "references": ("<sent-0@me.test> <sent-1@me.test>",),
        },
    }
    return SimpleNamespace(**(fields | overrides))


def test_quoted_history_and_signatures_are_removed():
    text = (
        "Sounds good.\nWhat would it cost?\n\n"
        "On Fri, 13 Nov 2026 at 10:00, Dev <me@x.test> wrote:\n> Hi Maria\n> I can build it"
    )
    assert strip_quoted(text) == "Sounds good.\nWhat would it cost?"
    assert strip_quoted("Yes please\n> earlier line\nand call me\n--\nMaria") == (
        "Yes please\nand call me"
    )
    assert strip_quoted("Ok\n\n-----Original Message-----\nFrom: me") == "Ok"


def test_a_reply_is_parsed_with_its_thread_references():
    parsed = parse_message(email())

    assert parsed.sender == "maria@studioflow.example"
    assert parsed.message_id == "<reply-1@studioflow.example>"
    assert parsed.references == ("<sent-1@me.test>", "<sent-0@me.test>")
    assert parsed.text == "Sounds good. What would it cost?"
    assert parsed.received_at.tzinfo is not None


def test_html_only_mail_is_read_and_a_missing_id_falls_back_to_the_uid():
    parsed = parse_message(email(text="", html="<p>Yes, <b>go ahead</b></p>", headers={}))

    assert parsed.text == "Yes, go ahead"
    assert parsed.message_id == "uid:77"


def test_automatic_replies_and_empty_mail_are_ignored():
    assert parse_message(email(subject="Automatic reply: Re: React dev")) is None
    assert parse_message(email(headers={"auto-submitted": ("auto-replied",)})) is None
    assert parse_message(email(text="", html="")) is None
    assert not is_automatic("Re: React dev", {"auto-submitted": ("no",)})


def test_inbox_reader_needs_host_user_and_password():
    assert not InboxReader.is_configured(Settings(_env_file=None))
    assert InboxReader.is_configured(
        Settings(_env_file=None, imap_host="imap.x.test", imap_username="me", imap_password="p")
    )


def test_a_reply_email_is_threaded_onto_the_message_it_answers():
    mailer = Mailer(
        Settings(_env_file=None, smtp_host="smtp.x.test", smtp_username="me@example.test")
    )

    message = mailer.build_message("a@b.test", "Re: Job", "Hello", reply_to="<abc@client.test>")

    assert message["In-Reply-To"] == "<abc@client.test>"
    assert message["References"] == "<abc@client.test>"
    assert mailer.build_message("a@b.test", "Re: Job", "Hello")["In-Reply-To"] is None
