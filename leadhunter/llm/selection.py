from sqlalchemy.ext.asyncio import AsyncSession

from leadhunter.llm.client import LLMClient, build_provider, get_spec
from leadhunter.llm.providers import LLMError
from leadhunter.state import get_state, set_state

KEYS = "llm_keys"
CHOICE = "llm_choice"

_clients: dict[tuple[str, str, str], LLMClient] = {}


async def saved_keys(session: AsyncSession) -> dict[str, str]:
    return dict(await get_state(session, KEYS, {}))


async def save_key(session: AsyncSession, name: str, api_key: str) -> None:
    await set_state(session, KEYS, {**await saved_keys(session), name: api_key})


async def forget_key(session: AsyncSession, name: str) -> bool:
    keys = await saved_keys(session)
    if keys.pop(name, None) is None:
        return False
    await set_state(session, KEYS, keys)
    return True


async def get_choice(session: AsyncSession) -> tuple[str, str] | None:
    choice = await get_state(session, CHOICE)
    return (choice["provider"], choice["model"]) if choice else None


async def set_choice(session: AsyncSession, name: str, model: str) -> None:
    await set_state(session, CHOICE, {"provider": name, "model": model})


async def clear_choice(session: AsyncSession) -> None:
    await set_state(session, CHOICE, None)


def has_key(name: str, keys: dict[str, str]) -> bool:
    return not get_spec(name).needs_key or bool(keys.get(name))


def client_for(name: str, model: str, api_key: str = "") -> LLMClient:
    cache_key = (name, model, api_key)
    if cache_key not in _clients:
        label, provider = build_provider(name, model, api_key)
        _clients[cache_key] = LLMClient(provider, label)
    return _clients[cache_key]


async def active_llm(session: AsyncSession) -> LLMClient:
    choice = await get_choice(session)
    if choice is None:
        raise LLMError("no model is connected: send /connect to the bot")
    name, model = choice
    keys = await saved_keys(session)
    return client_for(name, model, keys.get(name, ""))
