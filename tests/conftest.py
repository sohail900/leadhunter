import json
import os
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from telegram.request import BaseRequest

from leadhunter.bot.app import build_application
from leadhunter.config import Settings
from leadhunter.db.models import Base
from leadhunter.db.session import make_engine

OWNER_CHAT_ID = 111
BOT_USER = {"id": 999, "is_bot": True, "first_name": "LeadHunter", "username": "leadhunter_bot"}


class FakeTelegram(BaseRequest):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    @property
    def read_timeout(self):
        return 1.0

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def do_request(self, url, method, request_data=None, *args, **kwargs):
        api_method = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        self.calls.append((api_method, params))

        if api_method == "getMe":
            result = BOT_USER
        elif api_method == "sendMessage":
            result = {
                "message_id": len(self.calls),
                "date": 0,
                "chat": {"id": params["chat_id"], "type": "private"},
                "text": params["text"],
            }
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()

    def sent_messages(self) -> list[dict]:
        return [params for method, params in self.calls if method == "sendMessage"]


def message_update(text: str, chat_id: int, update_id: int = 1) -> dict:
    entities = []
    if text.startswith("/"):
        entities = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "is_bot": False, "first_name": "Someone"},
            "text": text,
            "entities": entities,
        },
    }


def callback_update(data: str, chat_id: int = OWNER_CHAT_ID, update_id: int = 50) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "chat_instance": "x",
            "from": {"id": chat_id, "is_bot": False, "first_name": "Someone"},
            "data": data,
            "message": {
                "message_id": 900,
                "date": 0,
                "chat": {"id": chat_id, "type": "private"},
                "from": BOT_USER,
                "text": "a card",
            },
        },
    }


def reply_update(text: str, replied_to_text: str, chat_id: int = OWNER_CHAT_ID) -> dict:
    payload = message_update(text, chat_id, update_id=60)
    payload["message"]["reply_to_message"] = {
        "message_id": 901,
        "date": 0,
        "chat": {"id": chat_id, "type": "private"},
        "from": BOT_USER,
        "text": replied_to_text,
    }
    return payload


class FakeMailer:
    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[dict] = []
        self.error = error

    async def send(self, to: str, subject: str, text: str) -> None:
        if self.error:
            raise self.error
        self.sent.append({"to": to, "subject": subject, "text": text})


def make_settings(**overrides) -> Settings:
    values = {"telegram_bot_token": "123:TEST", "telegram_chat_id": OWNER_CHAT_ID, **overrides}
    return Settings(_env_file=None, **values)


def sessionmaker_for(session):

    @asynccontextmanager
    async def factory():
        yield session

    return factory


@pytest.fixture
def telegram() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
def mailer() -> FakeMailer:
    return FakeMailer()


@pytest.fixture
async def bot_app(telegram):

    @asynccontextmanager
    async def no_database():
        yield None

    app = build_application(make_settings(), no_database, request=telegram)
    await app.initialize()
    yield app
    await app.shutdown()


@pytest.fixture
async def bot_app_db(telegram, mailer, db_session):
    app = build_application(
        make_settings(), sessionmaker_for(db_session), request=telegram, mailer=mailer
    )
    await app.initialize()
    yield app
    await app.shutdown()


@pytest.fixture
async def db_session():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()
