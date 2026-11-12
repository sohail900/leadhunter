from collections.abc import Callable

import structlog

from leadhunter.collectors.base import BaseCollector
from leadhunter.collectors.discord import DiscordCollector
from leadhunter.collectors.hackernews import HackerNewsCollector
from leadhunter.collectors.imap_upwork import UpworkEmailCollector
from leadhunter.collectors.job_feeds import RemoteOKCollector, WWRCollector
from leadhunter.collectors.reddit import RedditCollector
from leadhunter.collectors.telegram import TelegramChannelCollector
from leadhunter.config import Settings

log = structlog.get_logger()


class NotConfigured(Exception):
    pass


def _require(settings: Settings, *names: str) -> None:
    missing = [name.upper() for name in names if not getattr(settings, name)]
    if missing:
        raise NotConfigured(f"{', '.join(missing)} not set")


def _reddit(s: Settings) -> BaseCollector:
    _require(s, "reddit_client_id", "reddit_client_secret", "reddit_subreddits")
    return RedditCollector(
        s.reddit_client_id, s.reddit_client_secret, s.reddit_user_agent, s.reddit_subreddits
    )


def _upwork(s: Settings) -> BaseCollector:
    _require(s, "imap_host", "imap_username", "imap_password")
    return UpworkEmailCollector(
        s.imap_host,
        s.imap_port,
        s.imap_username,
        s.imap_password,
        s.imap_folder,
        s.upwork_sender,
        s.max_lead_age_hours,
    )


def _telegram(s: Settings) -> BaseCollector:
    _require(s, "telegram_api_id", "telegram_api_hash", "telegram_session", "telegram_channels")
    return TelegramChannelCollector(
        s.telegram_api_id,
        s.telegram_api_hash,
        s.telegram_session,
        s.telegram_channels,
        s.max_lead_age_hours,
    )


def _discord(s: Settings) -> BaseCollector:
    _require(s, "discord_bot_token", "discord_channel_ids")
    return DiscordCollector(s.discord_bot_token, [int(c) for c in s.discord_channel_ids])


BUILDERS: dict[str, Callable[[Settings], BaseCollector]] = {
    "remoteok": lambda s: RemoteOKCollector(s.remoteok_tags),
    "weworkremotely": lambda s: WWRCollector(s.wwr_feeds),
    "hackernews": lambda s: HackerNewsCollector(s.hn_keywords, s.max_lead_age_hours),
    "reddit": _reddit,
    "upwork": _upwork,
    "telegram": _telegram,
    "discord": _discord,
}


def build_collectors(settings: Settings) -> list[BaseCollector]:
    collectors = []
    for name in dict.fromkeys(n.lower() for n in settings.collectors):
        builder = BUILDERS.get(name)
        if builder is None:
            log.warning("unknown_collector", source=name)
            continue
        try:
            collectors.append(builder(settings))
        except NotConfigured as reason:
            log.info("collector_disabled", source=name, reason=str(reason))
    return collectors
