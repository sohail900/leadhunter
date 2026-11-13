from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.request import BaseRequest

from leadhunter.bot import chat, connect, deals, handlers
from leadhunter.config import Profile, Settings
from leadhunter.mailer import Mailer


def build_application(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    request: BaseRequest | None = None,
    mailer: Mailer | None = None,
    profile: Profile | None = None,
) -> Application:
    builder = Application.builder().token(settings.telegram_bot_token).job_queue(None)
    if request is not None:
        builder = builder.request(request).get_updates_request(request)
    elif settings.telegram_proxy_url:
        proxy = settings.telegram_proxy_url
        builder = builder.proxy(proxy).get_updates_proxy(proxy)
    app = builder.build()

    if mailer is None and Mailer.is_configured(settings):
        mailer = Mailer(settings)
    app.bot_data.update(
        owner_chat_id=settings.telegram_chat_id,
        sessionmaker=sessionmaker,
        settings=settings,
        mailer=mailer,
        profile=profile,
    )

    app.add_handler(TypeHandler(Update, handlers.owner_guard), group=-1)

    commands = {
        "start": handlers.help_command,
        "help": handlers.help_command,
        "stats": handlers.stats_command,
        "leads": handlers.leads_command,
        "replied": handlers.replied_command,
        "won": handlers.won_command,
        "lost": handlers.lost_command,
        "pause": handlers.pause_command,
        "resume": handlers.resume_command,
        "threshold": handlers.threshold_command,
        "connect": connect.connect_command,
        "models": connect.models_command,
        "llm": connect.llm_command,
        "disconnect": connect.disconnect_command,
        "auto": deals.auto_command,
        "report": deals.report_command,
        "client": deals.client_command,
    }
    for name, callback in commands.items():
        app.add_handler(CommandHandler(name, callback))
    app.add_handler(CallbackQueryHandler(connect.on_button, pattern=connect.BUTTON_PATTERN))
    app.add_handler(CallbackQueryHandler(handlers.on_button))
    app.add_handler(
        MessageHandler(filters.TEXT & filters.REPLY & ~filters.COMMAND, chat.on_text_reply)
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat.on_message))
    app.add_error_handler(handlers.on_error)
    return app
