from functools import lru_cache
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, BeforeValidator, Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _split_csv(value):
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


CsvList = Annotated[list[str], NoDecode, BeforeValidator(_split_csv)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://leadhunter:leadhunter@localhost:5432/leadhunter"

    telegram_bot_token: str = ""
    telegram_chat_id: int = 0
    telegram_proxy_url: str = ""

    collectors: CsvList = [
        "remoteok",
        "weworkremotely",
        "hackernews",
        "reddit",
        "upwork",
        "telegram",
        "discord",
    ]
    max_lead_age_hours: int = 48
    remoteok_tags: CsvList = [
        "dev",
        "full stack",
        "javascript",
        "typescript",
        "react",
        "node",
        "python",
        "backend",
        "front end",
    ]
    wwr_feeds: CsvList = [
        "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss"
    ]
    hn_keywords: CsvList = [
        "contract",
        "contractor",
        "freelance",
        "freelancer",
        "part-time",
        "consulting",
        "consultant",
        "fractional",
    ]
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "linux:leadhunter:0.1 (personal job alert reader)"
    reddit_subreddits: CsvList = ["forhire", "slavelabour", "jobbit", "remotejs"]

    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    upwork_sender: str = "upwork.com"

    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_session: str = ""
    telegram_channels: CsvList = []

    discord_bot_token: str = ""
    discord_channel_ids: CsvList = []

    score_threshold: int = 6
    score_batch_size: int = 20
    score_interval_minutes: int = 5
    draft_batch_size: int = 10

    urgent_score: int = 9
    digest_times: CsvList = ["09:00", "18:00"]

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_from_name: str = ""

    timezone: str = "Asia/Karachi"
    profile_path: Path = Path("profile.yaml")

    log_level: str = "INFO"
    log_format: str = "json"

    @field_validator("*", mode="before")
    @classmethod
    def _blank_number_means_not_set(cls, value, info: ValidationInfo):
        field = cls.model_fields[info.field_name]
        if value == "" and field.annotation is int:
            return field.default
        return value

    def missing_for_bot(self) -> list[str]:
        missing = []
        if not self.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.telegram_chat_id:
            missing.append("TELEGRAM_CHAT_ID")
        return missing


@lru_cache
def get_settings() -> Settings:
    return Settings()


class PortfolioItem(BaseModel):
    name: str
    description: str
    tech: list[str] = Field(default_factory=list)
    url: str | None = None


class Rate(BaseModel):
    hourly_usd: int | None = None
    min_project_usd: int | None = None


class Preferences(BaseModel):
    deal_breaker_keywords: list[str] = Field(default_factory=list)
    preferred_work: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)


class Profile(BaseModel):
    name: str
    headline: str = ""
    skills: list[str] = Field(default_factory=list)
    rate: Rate = Field(default_factory=Rate)
    portfolio: list[PortfolioItem] = Field(default_factory=list)
    preferences: Preferences = Field(default_factory=Preferences)


def load_profile(path: Path | None = None) -> Profile:
    path = path or get_settings().profile_path
    with open(path, encoding="utf-8") as f:
        return Profile.model_validate(yaml.safe_load(f))
