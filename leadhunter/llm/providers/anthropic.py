import anthropic
from pydantic import BaseModel

from leadhunter.llm.providers.base import LLMError, LLMTransientError, Provider

FALLBACK_MODELS = {
    "claude-fable-5-2",
    "claude-fable-5-1",
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-sonnet-5-5",
}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 16000
EFFORT = "medium"


class AnthropicProvider(Provider):
    def __init__(self, *args, effort: str = EFFORT, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.effort = effort
        self._client = anthropic.AsyncAnthropic(
            api_key=self.api_key or None, base_url=self.base_url or None
        )

    async def list_models(self) -> list[str]:
        try:
            return sorted([model.id async for model in self._client.models.list()])
        except anthropic.APIConnectionError as exc:
            raise LLMTransientError(f"network error: {exc!r}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"HTTP {exc.status_code}: {exc.message}", exc.status_code) from exc

    async def complete(self, system: str, user: str, schema: type[BaseModel]) -> str:
        request: dict = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_format": schema,
        }
        if self.effort:
            request["output_config"] = {"effort": self.effort}

        try:
            if self.model in FALLBACK_MODELS:
                response = await self._client.beta.messages.parse(
                    betas=[FALLBACK_BETA], fallbacks="default", **request
                )
            else:
                response = await self._client.messages.parse(**request)
        except anthropic.RateLimitError as exc:
            retry_after = exc.response.headers.get("retry-after", "")
            raise LLMTransientError(
                f"rate limited: {exc.message}",
                retry_after=float(retry_after) if retry_after.isdigit() else None,
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise LLMTransientError(f"network error: {exc!r}") from exc
        except anthropic.APIStatusError as exc:
            message = f"HTTP {exc.status_code}: {exc.message}"
            if exc.status_code >= 500:
                raise LLMTransientError(message, status=exc.status_code) from exc
            raise LLMError(message, exc.status_code) from exc

        if response.stop_reason == "refusal":
            category = response.stop_details.category if response.stop_details else None
            raise LLMError(f"the model declined the request (category: {category})")
        if response.stop_reason == "max_tokens":
            raise LLMError("the reply was cut off at max_tokens")

        text = "".join(block.text for block in response.content if block.type == "text")
        if not text:
            raise LLMError("the model returned no text")
        return text
