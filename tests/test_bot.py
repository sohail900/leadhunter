import pytest
from telegram import Update

from leadhunter.bot import handlers
from leadhunter.bot.formatting import format_stats
from leadhunter.tracker.stats import Counts, Stats
from tests.conftest import OWNER_CHAT_ID, message_update


async def _deliver(app, payload: dict) -> None:
    await app.process_update(Update.de_json(payload, app.bot))


async def test_stats_replies_with_zeros_on_empty_database(bot_app, telegram, monkeypatch):
    async def empty_stats(session):
        return Stats()

    monkeypatch.setattr(handlers, "collect_stats", empty_stats)

    await _deliver(bot_app, message_update("/stats", OWNER_CHAT_ID))

    [sent] = telegram.sent_messages()
    assert sent["chat_id"] == OWNER_CHAT_ID
    assert sent["parse_mode"] == "HTML"
    assert "Found: <b>0</b>" in sent["text"]
    assert "Contacted: <b>0</b>" in sent["text"]
    assert "Replied: <b>0</b>" in sent["text"]
    assert "Won: <b>0</b>" in sent["text"]


async def test_commands_from_other_chats_are_ignored(bot_app, telegram, monkeypatch):
    called = False

    async def spy(session):
        nonlocal called
        called = True
        return Stats()

    monkeypatch.setattr(handlers, "collect_stats", spy)

    await _deliver(bot_app, message_update("/stats", chat_id=OWNER_CHAT_ID + 1))

    assert telegram.sent_messages() == []
    assert called is False


def test_format_stats_shows_dash_for_reply_rate_when_nothing_contacted():
    text = format_stats(Stats())
    assert "Reply rate: <b>–</b>" in text
    assert "No leads collected yet." in text


def test_format_stats_lists_each_source_with_reply_rate():
    stats = Stats(
        total=Counts(found=12, contacted=4, replied=1, won=1),
        by_source={
            "reddit": Counts(found=10, contacted=4, replied=1, won=1),
            "hackernews": Counts(found=2),
        },
    )
    text = format_stats(stats)
    assert "Reply rate: <b>25%</b>" in text
    rows = text.replace("<pre>", "").replace("</pre>", "").splitlines()
    reddit_row = next(line for line in rows if line.startswith("reddit"))
    assert reddit_row.split() == ["reddit", "10", "4", "1", "1", "25%"]
    hn_row = next(line for line in rows if line.startswith("hackernews"))
    assert hn_row.split() == ["hackernews", "2", "0", "0", "0", "–"]


async def test_startup_waits_until_telegram_is_reachable(monkeypatch):
    from telegram.error import NetworkError

    from leadhunter import main
    from leadhunter.bot.app import build_application
    from tests.conftest import FakeTelegram, make_settings

    class DownAtFirst(FakeTelegram):
        failures = 2

        async def do_request(self, *args, **kwargs):
            if self.failures:
                self.failures -= 1
                raise NetworkError("httpx.ConnectError: ")
            return await super().do_request(*args, **kwargs)

    waits = []

    async def no_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(main.asyncio, "sleep", no_sleep)
    app = build_application(make_settings(), None, request=DownAtFirst())

    await main.wait_for_telegram(app)

    assert waits == [5.0, 10.0]
    assert app.bot.username == "leadhunter_bot"
    await app.bot.shutdown()


@pytest.mark.parametrize(
    ("scheme", "first_bytes"),
    [
        ("http", b"CONNECT api.telegram.org:443"),
        ("socks5", b"\x05"),
    ],
)
async def test_with_a_proxy_url_the_bot_talks_to_the_proxy(scheme, first_bytes):
    import asyncio

    from telegram.error import NetworkError

    from leadhunter.bot.app import build_application
    from tests.conftest import make_settings

    received = []

    async def proxy(reader, writer):
        received.append(await reader.read(200))
        writer.close()

    server = await asyncio.start_server(proxy, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    settings = make_settings(telegram_proxy_url=f"{scheme}://127.0.0.1:{port}")
    app = build_application(settings, None)

    with pytest.raises(NetworkError):
        await app.bot.initialize()

    server.close()
    await server.wait_closed()
    assert received[0].startswith(first_bytes)
