import asyncio
import json
import re
from typing import TypeVar

import structlog
from pydantic import BaseModel, ValidationError

from leadhunter.llm.providers import (
    ADAPTERS,
    PROVIDERS,
    LLMError,
    LLMInvalidReply,
    LLMTransientError,
    Provider,
    ProviderSpec,
)

log = structlog.get_logger()

T = TypeVar("T", bound=BaseModel)

MAX_TRANSIENT_RETRIES = 3
MAX_RETRY_DELAY = 60.0
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


def extract_json(text: str) -> str:
    text = _THINK.sub("", text).strip()
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in the reply")
    return text[start : end + 1]


def schema_instructions(schema: type[BaseModel]) -> str:
    return (
        "Reply with one JSON object and nothing else: no code fence, no commentary. "
        "It must match this JSON schema:\n"
        + json.dumps(schema.model_json_schema(), separators=(",", ":"))
    )


class LLMClient:
    def __init__(self, provider: Provider, name: str = "") -> None:
        self.provider = provider
        self.name = name or type(provider).__name__

    async def _complete_with_retry(self, system: str, user: str, schema: type[BaseModel]) -> str:
        for attempt in range(MAX_TRANSIENT_RETRIES + 1):
            try:
                return await self.provider.complete(system, user, schema)
            except LLMTransientError as exc:
                if attempt == MAX_TRANSIENT_RETRIES:
                    raise
                delay = min(exc.retry_after or 2.0 * 2**attempt, MAX_RETRY_DELAY)
                log.warning("llm_retry", provider=self.name, attempt=attempt + 1, delay=delay)
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def complete_json(self, system: str, user: str, schema: type[T]) -> T:
        system = f"{system}\n\n{schema_instructions(schema)}"
        text = await self._complete_with_retry(system, user, schema)
        try:
            return schema.model_validate_json(extract_json(text))
        except (ValueError, ValidationError) as first_error:
            log.warning("llm_invalid_json", provider=self.name, error=str(first_error)[:300])
            retry_user = (
                f"{user}\n\n---\nYour previous reply was rejected:\n{text[:2000]}\n\n"
                f"Problem: {str(first_error)[:1000]}\n"
                "Reply again with only the corrected JSON object."
            )
            text = await self._complete_with_retry(system, retry_user, schema)
            try:
                return schema.model_validate_json(extract_json(text))
            except (ValueError, ValidationError) as exc:
                raise LLMInvalidReply(
                    f"reply did not match the schema after a retry: {exc}"
                ) from exc


def get_spec(name: str) -> ProviderSpec:
    spec = PROVIDERS.get(name)
    if spec is None:
        raise LLMError(f"unknown LLM provider {name!r}; choose one of: {', '.join(PROVIDERS)}")
    return spec


def build_provider(name: str, model: str = "", api_key: str = "") -> tuple[str, Provider]:
    spec = get_spec(name)
    if spec.needs_key and not api_key:
        raise LLMError(f"no API key saved for {spec.label}: send /connect to the bot")
    provider = ADAPTERS[spec.adapter](
        model=model, api_key=api_key, base_url=spec.base_url, json_mode=spec.json_mode
    )
    return f"{name}:{model}", provider
