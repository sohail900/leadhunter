from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class RawLead(BaseModel):
    source: str
    external_id: str
    url: str | None = None
    title: str
    body: str = ""
    author: str | None = None
    posted_at: datetime | None = None
    contact_hint: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class BaseCollector(ABC):
    name: str
    interval_minutes: int = 30

    @abstractmethod
    async def fetch(self) -> list[RawLead]:
        pass
