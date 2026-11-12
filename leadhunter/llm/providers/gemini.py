import httpx
from pydantic import BaseModel

from leadhunter.llm.providers.base import LLMError, LLMTransientError, Provider
from leadhunter.llm.providers.openai_compat import LIST_TIMEOUT, TIMEOUT, raise_for_llm_status


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code == 400 and "API key not valid" in response.text:
        raise LLMError("HTTP 400: API key not valid", status=401)
    raise_for_llm_status(response)


class GeminiProvider(Provider):
    def __init__(self, *args, transport: httpx.AsyncBaseTransport | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._transport = transport

    async def list_models(self) -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=LIST_TIMEOUT, transport=self._transport) as client:
                response = await client.get(
                    f"{self.base_url}/models",
                    params={"pageSize": 1000},
                    headers={"x-goog-api-key": self.api_key},
                )
        except httpx.TransportError as exc:
            raise LLMTransientError(f"network error: {exc!r}") from exc
        _raise_for_status(response)

        return sorted(
            model["name"].removeprefix("models/")
            for model in response.json().get("models", [])
            if "generateContent" in model.get("supportedGenerationMethods", [])
        )

    async def complete(self, system: str, user: str, schema: type[BaseModel]) -> str:
        body: dict = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
        }
        if self.json_mode != "none":
            body["generationConfig"] = {"responseMimeType": "application/json"}

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, transport=self._transport) as client:
                response = await client.post(
                    f"{self.base_url}/models/{self.model}:generateContent",
                    json=body,
                    headers={"x-goog-api-key": self.api_key},
                )
        except httpx.TransportError as exc:
            raise LLMTransientError(f"network error: {exc!r}") from exc
        _raise_for_status(response)

        data = response.json()
        candidates = data.get("candidates") or []
        if not candidates:
            raise LLMError(f"no candidates returned: {str(data.get('promptFeedback'))[:300]}")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
        if not text:
            raise LLMError(f"empty response, finishReason={candidates[0].get('finishReason')}")
        return text
