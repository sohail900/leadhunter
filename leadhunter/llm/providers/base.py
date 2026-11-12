from abc import ABC, abstractmethod

from pydantic import BaseModel


class LLMError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class LLMInvalidReply(LLMError):
    pass


class LLMTransientError(LLMError):
    def __init__(
        self, message: str, retry_after: float | None = None, status: int | None = None
    ) -> None:
        super().__init__(message, status)
        self.retry_after = retry_after


class Provider(ABC):
    def __init__(self, model: str, api_key: str, base_url: str | None, json_mode: str) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = (base_url or "").rstrip("/")
        self.json_mode = json_mode

    @abstractmethod
    async def complete(self, system: str, user: str, schema: type[BaseModel]) -> str:
        pass

    async def list_models(self) -> list[str]:
        raise LLMError("this provider cannot list its models")
