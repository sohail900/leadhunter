import re

import httpx
from pydantic import BaseModel

from leadhunter.llm.providers.base import LLMError, LLMTransientError, Provider

TIMEOUT = httpx.Timeout(120.0, connect=10.0)
LIST_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_NOT_CHAT = re.compile(
    r"embed|whisper|tts|dall-e|moderation|rerank|transcri|speech|realtime|image|audio|video|sora",
    re.IGNORECASE,
)


def raise_for_llm_status(response: httpx.Response) -> None:
    if response.is_success:
        return
    detail = response.text[:500]
    if response.status_code == 429 or response.status_code >= 500:
        retry_after = response.headers.get("Retry-After", "")
        raise LLMTransientError(
            f"HTTP {response.status_code}: {detail}",
            retry_after=float(retry_after) if retry_after.isdigit() else None,
            status=response.status_code,
        )
    raise LLMError(f"HTTP {response.status_code}: {detail}", status=response.status_code)


class OpenAICompatProvider(Provider):
    def __init__(self, *args, transport: httpx.AsyncBaseTransport | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._transport = transport

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    async def list_models(self) -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=LIST_TIMEOUT, transport=self._transport) as client:
                response = await client.get(f"{self.base_url}/models", headers=self._headers())
        except httpx.TransportError as exc:
            raise LLMTransientError(f"network error: {exc!r}") from exc
        raise_for_llm_status(response)

        try:
            entries = response.json()["data"]
            return sorted(
                entry["id"]
                for entry in entries
                if entry.get("type", "chat") == "chat" and not _NOT_CHAT.search(entry["id"])
            )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise LLMError(f"unexpected model list: {response.text[:300]}") from exc

    def _response_format(self, schema: type[BaseModel]) -> dict | None:
        if self.json_mode == "json_schema":
            return {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()},
            }
        if self.json_mode == "json_object":
            return {"type": "json_object"}
        return None

    async def complete(self, system: str, user: str, schema: type[BaseModel]) -> str:
        body: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        response_format = self._response_format(schema)
        if response_format:
            body["response_format"] = response_format

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, transport=self._transport) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions", json=body, headers=self._headers()
                )
        except httpx.TransportError as exc:
            raise LLMTransientError(f"network error: {exc!r}") from exc
        raise_for_llm_status(response)

        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMError(f"unexpected response shape: {response.text[:500]}") from exc
        if not content:
            raise LLMError("the model returned an empty message")
        return content
