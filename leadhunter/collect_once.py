import asyncio
import sys

from leadhunter.collectors import build_collectors
from leadhunter.config import get_settings, load_profile
from leadhunter.db.session import get_engine, get_sessionmaker
from leadhunter.log import setup_logging
from leadhunter.pipeline.runner import run_collector


async def main(names: list[str]) -> None:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_format)
    collectors = build_collectors(settings)
    if names:
        collectors = [c for c in collectors if c.name in names]
    if not collectors:
        print("No matching collectors are enabled.", file=sys.stderr)
        sys.exit(1)

    profile = load_profile()
    await asyncio.gather(
        *(
            run_collector(c, get_sessionmaker(), profile, settings.max_lead_age_hours)
            for c in collectors
        )
    )
    await get_engine().dispose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
