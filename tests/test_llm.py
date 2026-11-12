import json

import httpx
import pytest
from pydantic import BaseModel

from leadhunter.config import Settings
from leadhunter.llm import client as llm_client
from leadhunter.llm.client import LLMClient, build_provider, extract_json
from leadhunter.llm.providers import PROVIDERS, LLMError, LLMTransientError, Provider
from leadhunter.llm.providers.anthropic import AnthropicProvider
from leadhunter.llm.providers.gemini import GeminiProvider
from leadhunter.llm.providers.openai_compat import OpenAICompatProvider


class Answer(BaseModel):
    score: int
    summary: str


class ScriptedProvider(Provider):
    def __init__(self, *replies):
        super().__init__(model="fake", api_key="", base_url=None, json_mode="none")
        self.replies = list(replies)
        self.calls = []

    async def complete(self, system, user, schema):
        self.calls.append((system, user))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    delays = []

    async def fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(llm_client.asyncio, "sleep", fake_sleep)
    return delays


def settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


@pytest.mark.parametrize(
    "reply",
    [
        '{"score": 7, "summary": "ok"}',
        '```json\n{"score": 7, "summary": "ok"}\n```',
        'Here is the result:\n{"score": 7, "summary": "ok"}\nHope that helps!',
        '<think>The post says {budget}...</think>\n{"score": 7, "summary": "ok"}',
    ],
)
def test_extract_json_finds_the_object_in_common_reply_shapes(reply):
    assert json.loads(extract_json(reply)) == {"score": 7, "summary": "ok"}


def test_extract_json_rejects_a_reply_with_no_object():
    with pytest.raises(ValueError):
        extract_json("I cannot help with that.")


async def test_complete_json_returns_a_validated_model_and_sends_the_schema():
    provider = ScriptedProvider('{"score": 8, "summary": "good lead"}')

    result = await LLMClient(provider).complete_json("You score leads.", "A post", Answer)

    assert result == Answer(score=8, summary="good lead")
    system, user = provider.calls[0]
    assert system.startswith("You score leads.")
    assert '"summary"' in system
    assert user == "A post"


async def test_invalid_reply_is_sent_back_once_with_the_error():
    provider = ScriptedProvider('{"score": "high"}', '{"score": 6, "summary": "fixed"}')

    result = await LLMClient(provider).complete_json("sys", "A post", Answer)

    assert result.score == 6
    assert len(provider.calls) == 2
    retry_user = provider.calls[1][1]
    assert "A post" in retry_user
    assert '{"score": "high"}' in retry_user
    assert "summary" in retry_user


async def test_gives_up_after_a_second_invalid_reply():
    provider = ScriptedProvider("not json", "still not json")

    with pytest.raises(LLMError, match="did not match the schema"):
        await LLMClient(provider).complete_json("sys", "A post", Answer)


async def test_transient_errors_are_retried_with_backoff(no_sleep):
    provider = ScriptedProvider(
        LLMTransientError("HTTP 503"),
        LLMTransientError("HTTP 429", retry_after=9),
        '{"score": 5, "summary": "ok"}',
    )

    result = await LLMClient(provider).complete_json("sys", "A post", Answer)

    assert result.score == 5
    assert no_sleep == [2.0, 9]


async def test_transient_errors_eventually_propagate(no_sleep):
    provider = ScriptedProvider(*[LLMTransientError("HTTP 503")] * 4)

    with pytest.raises(LLMTransientError):
        await LLMClient(provider).complete_json("sys", "A post", Answer)

    assert len(provider.calls) == 4


async def test_permanent_errors_are_not_retried(no_sleep):
    provider = ScriptedProvider(LLMError("HTTP 401: bad key"))

    with pytest.raises(LLMError, match="401"):
        await LLMClient(provider).complete_json("sys", "A post", Answer)

    assert len(provider.calls) == 1
    assert no_sleep == []


def test_the_registry_entry_decides_adapter_url_and_json_mode():
    name, provider = build_provider("deepseek", "deepseek-chat", api_key="sk-deep")

    assert name == "deepseek:deepseek-chat"
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.base_url == "https://api.deepseek.com"
    assert provider.api_key == "sk-deep"
    assert provider.json_mode == "json_object"


def test_anthropic_and_gemini_use_their_own_adapters():
    _, claude = build_provider("anthropic", "claude-opus-5-5", api_key="sk-ant-test")
    _, gemini = build_provider("gemini", "some-gemini-model", api_key="g-key")

    assert isinstance(claude, AnthropicProvider)
    assert claude.effort == "medium"
    assert isinstance(gemini, GeminiProvider)


def test_a_provider_without_a_saved_key_says_to_connect():
    with pytest.raises(LLMError, match="no API key saved for Kimi .*send /connect"):
        build_provider("kimi", "some-model")


def test_an_unknown_provider_is_named_in_the_error():
    with pytest.raises(LLMError, match="unknown LLM provider 'nope'"):
        build_provider("nope", "m", api_key="k")


def test_ollama_needs_no_key():
    _, provider = build_provider("ollama", "qwen3")
    assert provider.api_key == ""


def test_every_registered_provider_names_a_known_adapter():
    from leadhunter.llm.providers import ADAPTERS

    assert {"anthropic", "gemini", "openai", "deepseek", "qwen", "kimi", "glm"} <= set(PROVIDERS)
    assert all(spec.adapter in ADAPTERS for spec in PROVIDERS.values())


def capture(response: httpx.Response):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return response

    return httpx.MockTransport(handler), seen


def openai_provider(transport, json_mode="json_object", api_key="sk-test"):
    return OpenAICompatProvider(
        model="m1",
        api_key=api_key,
        base_url="https://llm.test/v1/",
        json_mode=json_mode,
        transport=transport,
    )


async def test_openai_compat_request_and_response():
    reply = {"choices": [{"message": {"role": "assistant", "content": '{"score": 1}'}}]}
    transport, seen = capture(httpx.Response(200, json=reply))

    text = await openai_provider(transport).complete("SYS", "USER", Answer)

    assert text == '{"score": 1}'
    assert seen["url"] == "https://llm.test/v1/chat/completions"
    assert seen["headers"]["authorization"] == "Bearer sk-test"
    assert seen["body"] == {
        "model": "m1",
        "messages": [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "USER"},
        ],
        "response_format": {"type": "json_object"},
    }


async def test_openai_compat_json_modes():
    reply = {"choices": [{"message": {"content": "{}"}}]}

    transport, seen = capture(httpx.Response(200, json=reply))
    await openai_provider(transport, json_mode="json_schema").complete("s", "u", Answer)
    assert seen["body"]["response_format"]["type"] == "json_schema"
    assert seen["body"]["response_format"]["json_schema"]["name"] == "Answer"
    assert "score" in seen["body"]["response_format"]["json_schema"]["schema"]["properties"]

    transport, seen = capture(httpx.Response(200, json=reply))
    await openai_provider(transport, json_mode="none", api_key="").complete("s", "u", Answer)
    assert "response_format" not in seen["body"]
    assert "authorization" not in seen["headers"]


@pytest.mark.parametrize(
    ("status", "error"), [(429, LLMTransientError), (503, LLMTransientError), (401, LLMError)]
)
async def test_openai_compat_maps_http_errors(status, error):
    transport, _ = capture(httpx.Response(status, json={"error": {"message": "nope"}}))

    with pytest.raises(error) as raised:
        await openai_provider(transport).complete("s", "u", Answer)

    assert type(raised.value) is error


async def test_openai_compat_reports_an_unexpected_body():
    transport, _ = capture(httpx.Response(200, json={"unexpected": True}))

    with pytest.raises(LLMError, match="unexpected response shape"):
        await openai_provider(transport).complete("s", "u", Answer)


async def test_gemini_request_and_response():
    reply = {
        "candidates": [
            {"content": {"parts": [{"text": '{"score"'}, {"text": ": 2}"}]}, "finishReason": "STOP"}
        ]
    }
    transport, seen = capture(httpx.Response(200, json=reply))
    provider = GeminiProvider(
        model="gem-1",
        api_key="g-key",
        base_url="https://gemini.test/v1beta",
        json_mode="json_object",
        transport=transport,
    )

    text = await provider.complete("SYS", "USER", Answer)

    assert text == '{"score": 2}'
    assert seen["url"] == "https://gemini.test/v1beta/models/gem-1:generateContent"
    assert seen["headers"]["x-goog-api-key"] == "g-key"
    assert seen["body"] == {
        "systemInstruction": {"parts": [{"text": "SYS"}]},
        "contents": [{"role": "user", "parts": [{"text": "USER"}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }


async def test_gemini_reports_a_blocked_prompt():
    transport, _ = capture(httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}))
    provider = GeminiProvider(
        model="gem-1",
        api_key="k",
        base_url="https://gemini.test",
        json_mode="",
        transport=transport,
    )

    with pytest.raises(LLMError, match="no candidates"):
        await provider.complete("s", "u", Answer)
