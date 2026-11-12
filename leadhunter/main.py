import asyncio
import signal
import sys
from datetime import datetime, timedelta

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.error import InvalidToken, NetworkError
from telegram.ext import Application

from leadhunter import jobs
from leadhunter.bot.app import build_application
from leadhunter.collectors import build_collectors
from leadhunter.config import Settings, get_settings, load_profile
from leadhunter.db.session import get_engine, get_sessionmaker
from leadhunter.llm.providers import LLMError
from leadhunter.llm.selection import active_llm
from leadhunter.log import setup_logging

log = structlog.get_logger()


def schedule_jobs(scheduler: AsyncIOScheduler, deps: jobs.Deps, settings: Settings) -> None:
    now = datetime.now(scheduler.timezone)

    for i, collector in enumerate(build_collectors(settings)):
        scheduler.add_job(
            jobs.collect,
            "interval",
            minutes=collector.interval_minutes,
            args=[deps, collector],
            id=f"collect:{collector.name}",
            next_run_time=now + timedelta(seconds=10 + 5 * i),
        )

    scheduler.add_job(
        jobs.pipeline,
        "interval",
        minutes=settings.score_interval_minutes,
        args=[deps],
        id="pipeline",
        next_run_time=now + timedelta(seconds=60),
    )
    scheduler.add_job(jobs.followups, "interval", hours=1, args=[deps], id="followups")

    for time_of_day in settings.digest_times:
        hour, minute = time_of_day.split(":")
        scheduler.add_job(
            jobs.digest,
            "cron",
            hour=int(hour),
            minute=int(minute),
            args=[deps],
            id=f"digest:{time_of_day}",
            misfire_grace_time=6 * 3600,
        )
    scheduler.add_job(
        jobs.weekly_summary,
        "cron",
        day_of_week="mon",
        hour=9,
        minute=5,
        args=[deps],
        id="weekly_summary",
        misfire_grace_time=24 * 3600,
    )


async def wait_for_telegram(app: Application, max_delay: float = 60.0) -> None:
    delay = 5.0
    while True:
        try:
            await app.bot.initialize()
            return
        except InvalidToken:
            log.error("telegram_token_rejected", hint="check TELEGRAM_BOT_TOKEN in .env")
            sys.exit(1)
        except NetworkError as exc:
            log.error(
                "telegram_unreachable",
                error=str(exc)[:200],
                retry_in_seconds=delay,
                hint="api.telegram.org cannot be reached; if Telegram is blocked on your "
                "network, set TELEGRAM_PROXY_URL in .env (and check the proxy is running) "
                "or connect a VPN",
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)


async def run() -> None:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_format)

    missing = settings.missing_for_bot()
    if missing:
        log.error("missing_config", variables=missing, hint="set them in .env, see .env.example")
        sys.exit(1)

    profile = load_profile()
    sessionmaker = get_sessionmaker()
    app = build_application(settings, sessionmaker)
    deps = jobs.Deps(
        settings=settings,
        profile=profile,
        sessionmaker=sessionmaker,
        bot=app.bot,
        can_email=app.bot_data["mailer"] is not None,
    )
    try:
        async with sessionmaker() as session:
            llm = await active_llm(session)
        log.info("llm_enabled", model=llm.name)
        llm_connected = True
    except LLMError as exc:
        log.warning("llm_not_connected", reason=str(exc))
        llm_connected = False

    scheduler = AsyncIOScheduler(
        timezone=settings.timezone,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
    )
    schedule_jobs(scheduler, deps, settings)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    await wait_for_telegram(app)
    async with app:
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        scheduler.start()
        log.info(
            "started",
            bot=app.bot.username,
            timezone=settings.timezone,
            jobs=[job.id for job in scheduler.get_jobs()],
            email=deps.can_email,
            telegram_proxy=bool(settings.telegram_proxy_url),
        )
        if not llm_connected:
            await app.bot.send_message(
                settings.telegram_chat_id,
                "No model is connected yet, so leads are collected but not scored. "
                "Send /connect to choose a provider and a model.",
            )

        await stop.wait()

        log.info("stopping")
        scheduler.shutdown(wait=False)
        await app.updater.stop()
        await app.stop()

    await get_engine().dispose()


if __name__ == "__main__":
    asyncio.run(run())
