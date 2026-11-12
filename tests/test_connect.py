import httpx
import pytest

from leadhunter.bot.connect import PAGE_SIZE, models_keyboard, providers_keyboard
from leadhunter.llm.providers import PROVIDERS, LLMError
from leadhunter.llm.providers.gemini import GeminiProvider
from leadhunter.llm.providers.openai_compat import OpenAICompatProvider


def transport_returning(response: httpx.Response):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        return response

    return httpx.MockTransport(handler), seen


def openai_provider(transport, api_key="sk-test"):
    return OpenAICompatProvider(
        model="",
        api_key=api_key,
        base_url="https://llm.test/v1/",
        json_mode="json_object",
        transport=transport,
    )


async def test_openai_compat_lists_chat_models_in_order():
    listing = {
        "data": [
            {"id": "zeta-large"},
            {"id": "alpha-mini", "type": "chat"},
            {"id": "text-embedding-3-small"},
            {"id": "whisper-1"},
            {"id": "pixel-xl", "type": "image"},
        ]
    }
    transport, seen = transport_returning(httpx.Response(200, json=listing))

    models = await openai_provider(transport).list_models()

    assert models == ["alpha-mini", "zeta-large"]
    assert seen["url"] == "https://llm.test/v1/models"
    assert seen["headers"]["authorization"] == "Bearer sk-test"


async def test_a_rejected_key_is_reported_with_its_status():
    transport, _ = transport_returning(httpx.Response(401, json={"error": "bad key"}))

    with pytest.raises(LLMError) as error:
        await openai_provider(transport).list_models()

    assert error.value.status == 401


async def test_an_unexpected_model_list_is_an_error_not_a_crash():
    transport, _ = transport_returning(httpx.Response(200, json={"models": "?"}))

    with pytest.raises(LLMError, match="unexpected model list"):
        await openai_provider(transport).list_models()


async def test_gemini_lists_only_models_that_can_generate_text():
    listing = {
        "models": [
            {"name": "models/gem-pro", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gem-embed", "supportedGenerationMethods": ["embedContent"]},
            {"name": "models/gem-flash", "supportedGenerationMethods": ["generateContent"]},
        ]
    }
    transport, seen = transport_returning(httpx.Response(200, json=listing))
    provider = GeminiProvider(
        model="",
        api_key="g-key",
        base_url="https://gemini.test/v1beta",
        json_mode="json_object",
        transport=transport,
    )

    assert await provider.list_models() == ["gem-flash", "gem-pro"]
    assert seen["url"].startswith("https://gemini.test/v1beta/models?")
    assert seen["headers"]["x-goog-api-key"] == "g-key"


async def test_gemini_bad_key_counts_as_a_rejected_key():
    body = {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key."}}
    transport, _ = transport_returning(httpx.Response(400, json=body))
    provider = GeminiProvider(
        model="",
        api_key="bad",
        base_url="https://gemini.test/v1beta",
        json_mode="none",
        transport=transport,
    )

    with pytest.raises(LLMError) as error:
        await provider.list_models()

    assert error.value.status == 401


def test_providers_keyboard_offers_every_provider_and_marks_those_with_a_key():
    keyboard = providers_keyboard({"deepseek": "sk-saved"})

    buttons = {b.callback_data: b.text for row in keyboard.inline_keyboard for b in row}
    assert set(buttons) == {f"prov:{name}" for name in PROVIDERS}
    assert buttons["prov:deepseek"] == "✓ DeepSeek"
    assert buttons["prov:opencode"] == "OpenCode Zen"
    assert buttons["prov:ollama"] == "Ollama (local)"


def test_models_keyboard_pages_through_a_long_list():
    models = [f"model-{i:02}" for i in range(PAGE_SIZE * 2 + 3)]
    menu = {"token": "abc12345", "provider": "openrouter", "models": models}

    first = models_keyboard(menu, 0, current="model-01").inline_keyboard
    last = models_keyboard(menu, 2).inline_keyboard

    assert [row[0].text for row in first[:-1]] == [
        "model-00",
        "● model-01",
        *models[2:PAGE_SIZE],
    ]
    assert first[0][0].callback_data == "model:abc12345:0"
    assert [(b.text, b.callback_data) for b in first[-1]] == [
        ("More › (2 of 3)", "page:abc12345:1")
    ]
    assert [row[0].callback_data for row in last[:-1]] == [
        f"model:abc12345:{i}" for i in range(PAGE_SIZE * 2, PAGE_SIZE * 2 + 3)
    ]
    assert [(b.text, b.callback_data) for b in last[-1]] == [("‹ Back", "page:abc12345:1")]


def test_a_short_list_has_no_paging_row():
    menu = {"token": "t", "provider": "deepseek", "models": ["a", "b"]}
    assert len(models_keyboard(menu, 0).inline_keyboard) == 2


def test_button_data_fits_telegrams_64_byte_limit():
    assert all(len(f"prov:{name}".encode()) <= 64 for name in PROVIDERS)
    assert len(b"model:abc12345:99999") <= 64
