from leadhunter.config import Settings
from leadhunter.mailer import Mailer


def settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_mailer_is_off_until_a_host_and_sender_are_set():
    assert not Mailer.is_configured(settings())
    assert not Mailer.is_configured(settings(smtp_host="smtp.example.test"))
    assert Mailer.is_configured(settings(smtp_host="smtp.example.test", smtp_username="me@x.test"))


def test_message_is_plain_text_from_my_address():
    mailer = Mailer(
        settings(
            smtp_host="smtp.example.test",
            smtp_username="login@example.test",
            smtp_from="me@example.test",
            smtp_from_name="Test Dev",
        )
    )

    message = mailer.build_message("client@acme.test", "Re: React dev", "Hi,\n\nI can help.\n")

    assert message["From"] == "Test Dev <me@example.test>"
    assert message["To"] == "client@acme.test"
    assert message["Subject"] == "Re: React dev"
    assert message["Message-ID"].endswith("@example.test>")
    assert message.get_content_type() == "text/plain"
    assert message.get_content() == "Hi,\n\nI can help.\n"


def test_sender_defaults_to_the_login():
    mailer = Mailer(settings(smtp_host="smtp.example.test", smtp_username="me@example.test"))
    assert mailer.build_message("a@b.test", "s", "t")["From"] == "me@example.test"
