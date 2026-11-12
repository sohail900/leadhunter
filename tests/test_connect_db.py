import json
from functools import partial

import httpx
import pytest
from sqlalchemy import select
from telegram import Update

from leadhunter.db.models import Event
from leadhunter.llm import providers, selection
from leadhunter.llm.providers import LLMError
from leadhunter.llm.providers.openai_compat import OpenAICompatProvider
from tests.conftest import OWNER_CHAT_ID, callback_update, message_update, reply_update

GOOD_KEY = "sk-good-0123456789"
MODELS = ["deepseek-chat", "deepseek-reasoner", "broken-model", "text-embedding-v1"]


class FakeProviderAPI:
    def __init__(self) -> None:
        self.models = list(MODELS)
        self.models_status = 200
        self.chat_calls: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {GOOD_KEY}":
            return httpx.Response(401, json={"error": "Authentication Fails"})
        if request.url.path.endswith("/models"):
            if self.models_status != 200:
                return httpx.Response(self.models_status, text="not here")
            return httpx.Response(200, json={"data": [{"id": m} for m in self.models]})
        body = json.loads(request.content)
        self.chat_calls.append(body)
        if body["model"] == "broken-model":
            return httpx.Response(400, json={"error": "this model cannot chat"})
        reply = {"choices": [{"message": {"content": '{"ok": true}'}}]}
        return httpx.Response(200, json=reply)


@pytest.fixture
def api(monkeypatch) -> FakeProviderAPI:
    api = FakeProviderAPI()
    adapter = partial(OpenAICompatProvider, transport=httpx.MockTransport(api))
    monkeypatch.setitem(providers.ADAPTERS, "openai_compat", adapter)
    monkeypatch.setattr(selection, "_clients", {})
    return api


async def deliver(app, payload: dict) -> None:
    await app.process_update(Update.de_json(payload, app.bot))


def last_text(telegram) -> str:
    return telegram.sent_messages()[-1]["text"]


def buttons(message: dict) -> dict[str, str]:
    rows = message["reply_markup"]["inline_keyboard"]
    return {button["text"]: button["callback_data"] for row in rows for button in row}


async def paste_key(app, telegram, key: str, provider: str = "deepseek") -> None:
    await deliver(app, callback_update(f"prov:{provider}"))
    prompt = last_text(telegram)
    assert prompt.startswith("API key for ")
    await deliver(app, reply_update(key, prompt))


async def test_connect_lists_the_providers(bot_app_db, telegram, api):
    await deliver(bot_app_db, message_update("/connect", OWNER_CHAT_ID))

    choices = buttons(telegram.sent_messages()[-1])
    assert choices["DeepSeek"] == "prov:deepseek"
    assert choices["OpenCode Zen"] == "prov:opencode"
    assert choices["Anthropic (Claude)"] == "prov:anthropic"


async def test_provider_then_key_then_model_connects_it(bot_app_db, telegram, api, db_session):
    app = bot_app_db

    await paste_key(app, telegram, GOOD_KEY)

    assert ("deleteMessage", {"chat_id": OWNER_CHAT_ID, "message_id": 60}) in telegram.calls
    assert await selection.saved_keys(db_session) == {"deepseek": GOOD_KEY}
    menu = telegram.sent_messages()[-1]
    assert "<b>DeepSeek</b>: 3 models" in menu["text"]
    assert list(buttons(menu)) == ["broken-model", "deepseek-chat", "deepseek-reasoner"]

    await deliver(app, callback_update(buttons(menu)["deepseek-chat"]))

    assert "Connected: DeepSeek · <code>deepseek-chat</code>" in last_text(telegram)
    assert await selection.get_choice(db_session) == ("deepseek", "deepseek-chat")
    assert api.chat_calls[-1]["model"] == "deepseek-chat"

    llm = await selection.active_llm(db_session)
    assert llm.name == "deepseek:deepseek-chat"
    assert llm.provider.api_key == GOOD_KEY

    events = (await db_session.scalars(select(Event))).all()
    assert [e.payload.get("key") for e in events] == ["llm_key", "llm_choice"]
    assert GOOD_KEY not in json.dumps([e.payload for e in events])
    assert all(GOOD_KEY not in m["text"] for m in telegram.sent_messages())


async def test_a_rejected_key_is_not_saved(bot_app_db, telegram, api, db_session):
    await paste_key(bot_app_db, telegram, "sk-wrong")

    message = telegram.sent_messages()[-1]
    assert "DeepSeek rejected the API key (HTTP 401). Nothing was saved." in message["text"]
    assert buttons(message) == {"Enter another key": "key:deepseek"}
    assert await selection.saved_keys(db_session) == {}

    await deliver(bot_app_db, callback_update("key:deepseek"))
    assert last_text(telegram).startswith("API key for DeepSeek [deepseek]")


async def test_a_model_that_fails_its_test_call_is_not_switched_to(
    bot_app_db, telegram, api, db_session
):
    await paste_key(bot_app_db, telegram, GOOD_KEY)
    menu = telegram.sent_messages()[-1]
    await deliver(bot_app_db, callback_update(buttons(menu)["deepseek-chat"]))

    await deliver(bot_app_db, message_update("/models", OWNER_CHAT_ID))
    menu = telegram.sent_messages()[-1]
    assert "● deepseek-chat" in buttons(menu)
    await deliver(bot_app_db, callback_update(buttons(menu)["broken-model"]))

    assert "failed, so nothing was changed" in last_text(telegram)
    assert "this model cannot chat" in last_text(telegram)
    assert await selection.get_choice(db_session) == ("deepseek", "deepseek-chat")


async def test_a_key_that_only_fails_at_the_test_call_can_be_replaced(
    bot_app_db, telegram, api, db_session
):
    await selection.save_key(db_session, "deepseek", GOOD_KEY)
    await deliver(bot_app_db, callback_update("prov:deepseek"))
    menu = telegram.sent_messages()[-1]
    await selection.save_key(db_session, "deepseek", "sk-wrong")

    await deliver(bot_app_db, callback_update(buttons(menu)["deepseek-chat"]))

    message = telegram.sent_messages()[-1]
    assert "The provider did not accept the API key." in message["text"]
    assert buttons(message) == {"Enter another key": "key:deepseek"}
    assert await selection.get_choice(db_session) is None


async def test_models_can_be_narrowed_and_old_lists_stop_working(
    bot_app_db, telegram, api, db_session
):
    await paste_key(bot_app_db, telegram, GOOD_KEY)
    old_menu = telegram.sent_messages()[-1]

    await deliver(bot_app_db, message_update("/models REASON", OWNER_CHAT_ID))

    menu = telegram.sent_messages()[-1]
    assert "1 model." in menu["text"]
    assert list(buttons(menu)) == ["deepseek-reasoner"]

    sent_before = len(telegram.sent_messages())
    await deliver(bot_app_db, callback_update(buttons(old_menu)["deepseek-chat"]))
    assert len(telegram.sent_messages()) == sent_before
    assert await selection.get_choice(db_session) is None

    await deliver(bot_app_db, message_update("/models nothing-like-this", OWNER_CHAT_ID))
    assert "lists no models matching" in last_text(telegram)


async def test_without_a_model_list_the_model_id_is_typed(bot_app_db, telegram, api, db_session):
    api.models_status = 404

    await paste_key(bot_app_db, telegram, GOOD_KEY)

    assert await selection.saved_keys(db_session) == {"deepseek": GOOD_KEY}
    assert "could not get DeepSeek's model list" in telegram.sent_messages()[-2]["text"]
    prompt = last_text(telegram)
    assert prompt.startswith("Model for DeepSeek [deepseek]")

    await deliver(bot_app_db, reply_update("deepseek-reasoner", prompt))

    assert await selection.get_choice(db_session) == ("deepseek", "deepseek-reasoner")


async def test_a_provider_with_a_key_goes_straight_to_its_models(
    bot_app_db, telegram, api, db_session
):
    await selection.save_key(db_session, "deepseek", GOOD_KEY)

    await deliver(bot_app_db, callback_update("prov:deepseek"))

    assert "<b>DeepSeek</b>: 3 models" in last_text(telegram)


async def test_models_before_connecting_explains_what_to_do(bot_app_db, telegram, api):
    await deliver(bot_app_db, message_update("/models", OWNER_CHAT_ID))
    assert last_text(telegram) == "No provider is connected yet. Send /connect."


async def test_llm_shows_what_is_in_use_and_disconnect_forgets_it(
    bot_app_db, telegram, api, db_session
):
    await deliver(bot_app_db, message_update("/llm", OWNER_CHAT_ID))
    assert "None yet. Send /connect." in last_text(telegram)

    await paste_key(bot_app_db, telegram, GOOD_KEY)
    await deliver(
        bot_app_db, callback_update(buttons(telegram.sent_messages()[-1])["deepseek-chat"])
    )
    await deliver(bot_app_db, message_update("/llm", OWNER_CHAT_ID))

    status = last_text(telegram)
    assert "DeepSeek · <code>deepseek-chat</code>" in status
    assert "Keys saved: deepseek" in status
    assert GOOD_KEY not in status

    await deliver(bot_app_db, message_update("/disconnect", OWNER_CHAT_ID))

    assert last_text(telegram).startswith("Disconnected DeepSeek.")
    assert await selection.saved_keys(db_session) == {}
    assert await selection.get_choice(db_session) is None
    with pytest.raises(LLMError, match="no model is connected"):
        await selection.active_llm(db_session)

    await deliver(bot_app_db, message_update("/disconnect kimi", OWNER_CHAT_ID))
    assert "no saved key" in last_text(telegram)
