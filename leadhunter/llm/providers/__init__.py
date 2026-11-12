from dataclasses import dataclass

from leadhunter.llm.providers.anthropic import AnthropicProvider
from leadhunter.llm.providers.base import (
    LLMError,
    LLMInvalidReply,
    LLMTransientError,
    Provider,
)
from leadhunter.llm.providers.gemini import GeminiProvider
from leadhunter.llm.providers.openai_compat import OpenAICompatProvider

__all__ = [
    "ADAPTERS",
    "PROVIDERS",
    "LLMError",
    "LLMInvalidReply",
    "LLMTransientError",
    "Provider",
    "ProviderSpec",
]

ADAPTERS: dict[str, type[Provider]] = {
    "openai_compat": OpenAICompatProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
}


@dataclass(frozen=True)
class ProviderSpec:
    label: str
    adapter: str
    base_url: str | None
    needs_key: bool = True
    json_mode: str = "json_object"


def _openai(label: str, base_url: str, **kwargs) -> ProviderSpec:
    return ProviderSpec(label, "openai_compat", base_url, **kwargs)


PROVIDERS: dict[str, ProviderSpec] = {
    "anthropic": ProviderSpec("Anthropic (Claude)", "anthropic", None),
    "gemini": ProviderSpec(
        "Google Gemini", "gemini", "https://generativelanguage.googleapis.com/v1beta"
    ),
    "openai": _openai("OpenAI", "https://api.openai.com/v1", json_mode="json_schema"),
    "deepseek": _openai("DeepSeek", "https://api.deepseek.com"),
    "qwen": _openai("Qwen (Alibaba)", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
    "kimi": _openai("Kimi (Moonshot)", "https://api.moonshot.ai/v1"),
    "glm": _openai("GLM (Zhipu)", "https://open.bigmodel.cn/api/paas/v4"),
    "minimax": _openai("MiniMax", "https://api.minimax.io/v1"),
    "doubao": _openai("Doubao (Volcengine)", "https://ark.cn-beijing.volces.com/api/v3"),
    "siliconflow": _openai("SiliconFlow", "https://api.siliconflow.cn/v1"),
    "openrouter": _openai("OpenRouter", "https://openrouter.ai/api/v1"),
    "opencode": _openai("OpenCode Zen", "https://opencode.ai/zen/v1"),
    "groq": _openai("Groq", "https://api.groq.com/openai/v1"),
    "ollama": _openai("Ollama (local)", "http://host.docker.internal:11434/v1", needs_key=False),
}
