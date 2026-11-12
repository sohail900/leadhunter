import asyncio
import hashlib
import re
from html import escape

import structlog
from pydantic import BaseModel
from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from leadhunter.bot.delivery import reply_html
from leadhunter.db.models import EventType
from leadhunter.llm import selection
from leadhunter.llm.client import build_provider
from leadhunter.llm.providers import PROVIDERS, LLMError
from leadhunter.tracker.events import record_event

log = structlog.get_logger()

PAGE_SIZE = 8
TEST_TIMEOUT = 90
MENU = "model_menu"
KEY_PROMPT = re.compile(r"^API key for .+ \[(\w+)\]")
MODEL_PROMPT = re.compile(r"^Model for .+ \[(\w+)\]")
BUTTON_PATTERN = r"^(prov|key|page|model):"


class _Ping(BaseModel):
    ok: bool


def _redact(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def _label(name: str) -> str:
    return PROVIDERS[name].label


def providers_keyboard(keys: dict[str, str]) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            ("✓ " if spec.needs_key and keys.get(name) else "") + spec.label,
            callback_data=f"prov:{name}",
        )
        for name, spec in PROVIDERS.items()
    ]
    return InlineKeyboardMarkup([buttons[i : i + 2] for i in range(0, len(buttons), 2)])


def models_keyboard(menu: dict, page: int, current: str = "") -> InlineKeyboardMarkup:
    models, token = menu["models"], menu["token"]
    pages = max(1, -(-len(models) // PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    first = page * PAGE_SIZE
    rows = [
        [
            InlineKeyboardButton(
                ("● " if model == current else "") + model, callback_data=f"model:{token}:{i}"
            )
        ]
        for i, model in enumerate(models[first : first + PAGE_SIZE], start=first)
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("‹ Back", callback_data=f"page:{token}:{page - 1}"))
    if page < pages - 1:
        nav.append(
            InlineKeyboardButton(
                f"More › ({page + 2} of {pages})", callback_data=f"page:{token}:{page + 1}"
            )
        )
    return InlineKeyboardMarkup([*rows, nav] if nav else rows)


def _other_key_keyboard(name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Enter another key", callback_data=f"key:{name}")]]
    )


async def _ask_for_key(update: Update, name: str) -> None:
    await update.effective_message.reply_text(
        f"API key for {_label(name)} [{name}]: reply to this message with the key. "
        "I delete your message as soon as I have read it.",
        reply_markup=ForceReply(selective=True),
    )


async def _ask_for_model(update: Update, name: str) -> None:
    await update.effective_message.reply_text(
        f"Model for {_label(name)} [{name}]: reply to this message with the model ID, "
        "exactly as the provider's documentation writes it.",
        reply_markup=ForceReply(selective=True),
    )


async def _choose_model(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    name: str,
    api_key: str,
    *,
    save_key: bool = False,
    filter_text: str = "",
) -> None:
    label = _label(name)
    models, problem = None, ""
    try:
        _, provider = build_provider(name, api_key=api_key)
        models = await provider.list_models()
    except LLMError as exc:
        problem = _redact(str(exc), api_key)[:200]
        if exc.status in (401, 403):
            saved = " Nothing was saved." if save_key else ""
            await reply_html(
                update,
                f"{escape(label)} rejected the API key (HTTP {exc.status}).{saved}",
                _other_key_keyboard(name),
            )
            return

    async with context.bot_data["sessionmaker"]() as session:
        if save_key:
            await selection.save_key(session, name, api_key)
            record_event(session, EventType.SETTING_CHANGED, key="llm_key", provider=name)
            await session.commit()
        choice = await selection.get_choice(session)
    current = choice[1] if choice and choice[0] == name else ""

    if models is None:
        saved = "Key saved. " if save_key else ""
        await reply_html(
            update, f"{saved}I could not get {escape(label)}'s model list: {escape(problem)}"
        )
        await _ask_for_model(update, name)
        return

    if filter_text:
        models = [model for model in models if filter_text.lower() in model.lower()]
    if not models:
        about = f" matching “{escape(filter_text)}”" if filter_text else ""
        await reply_html(update, f"{escape(label)} lists no models{about}.")
        return

    token = hashlib.sha256("\n".join([name, *models]).encode()).hexdigest()[:8]
    menu = {"token": token, "provider": name, "models": models}
    context.bot_data[MENU] = menu
    noun = "model" if len(models) == 1 else "models"
    await reply_html(
        update,
        f"<b>{escape(label)}</b>: {len(models)} {noun}. Pick one.\n"
        "/models &lt;text&gt; narrows the list, for example /models flash.",
        models_keyboard(menu, 0, current),
    )


async def _use_model(update: Update, context, name: str, model: str) -> None:
    label = f"{escape(_label(name))} · <code>{escape(model)}</code>"
    async with context.bot_data["sessionmaker"]() as session:
        api_key = (await selection.saved_keys(session)).get(name, "")
        await reply_html(update, f"Testing {label} ...")
        try:
            llm = selection.client_for(name, model, api_key)
            await asyncio.wait_for(
                llm.complete_json(
                    "You are answering a connection test.", 'Reply with {"ok": true}.', _Ping
                ),
                TEST_TIMEOUT,
            )
        except (LLMError, TimeoutError) as exc:
            reason = _redact(str(exc), api_key)[:300] or "timed out"
            log.warning("llm_test_failed", provider=name, model=model, error=reason)
            bad_key = getattr(exc, "status", None) in (401, 403)
            advice = (
                "The provider did not accept the API key."
                if bad_key
                else "Pick another model with /models."
            )
            await reply_html(
                update,
                f"The test call to {label} failed, so nothing was changed.\n\n"
                f"<code>{escape(reason)}</code>\n\n{advice}",
                _other_key_keyboard(name) if bad_key else None,
            )
            return

        await selection.set_choice(session, name, model)
        record_event(
            session, EventType.SETTING_CHANGED, key=selection.CHOICE, value=f"{name}:{model}"
        )
        await session.commit()
    log.info("llm_connected", provider=name, model=model)
    await reply_html(update, f"Connected: {label}. Scoring and drafting use it from the next run.")


async def connect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with context.bot_data["sessionmaker"]() as session:
        keys = await selection.saved_keys(session)
    await reply_html(
        update,
        "<b>Connect a model</b>\nPick a provider. ✓ means I already have its API key.",
        providers_keyboard(keys),
    )


async def models_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with context.bot_data["sessionmaker"]() as session:
        choice = await selection.get_choice(session)
        keys = await selection.saved_keys(session)
    menu = context.bot_data.get(MENU)
    name = menu["provider"] if menu else (choice[0] if choice else "")
    if name not in PROVIDERS:
        await reply_html(update, "No provider is connected yet. Send /connect.")
        return
    await _choose_model(
        update, context, name, keys.get(name, ""), filter_text=" ".join(context.args or [])
    )


async def llm_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with context.bot_data["sessionmaker"]() as session:
        choice = await selection.get_choice(session)
        keys = await selection.saved_keys(session)
    if choice and choice[0] in PROVIDERS:
        in_use = f"{escape(_label(choice[0]))} · <code>{escape(choice[1])}</code>"
    else:
        in_use = "None yet. Send /connect."
    lines = [
        "<b>Model in use</b>",
        in_use,
        "",
        f"Keys saved: {', '.join(sorted(keys)) or 'none'}",
        "",
        "/connect - change provider or add a key",
        "/models - change model",
        "/disconnect &lt;provider&gt; - forget a saved key",
    ]
    await reply_html(update, "\n".join(lines))


async def disconnect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with context.bot_data["sessionmaker"]() as session:
        choice = await selection.get_choice(session)
        name = context.args[0].lower() if context.args else (choice[0] if choice else "")
        if not name:
            saved = ", ".join(sorted(await selection.saved_keys(session))) or "none"
            await reply_html(update, f"Usage: /disconnect &lt;provider&gt;. Saved keys: {saved}.")
            return

        forgot = await selection.forget_key(session, name)
        was_in_use = bool(choice and choice[0] == name)
        if was_in_use:
            await selection.clear_choice(session)
        if forgot or was_in_use:
            record_event(session, EventType.SETTING_CHANGED, key="llm_disconnect", provider=name)
        await session.commit()

    if not forgot and not was_in_use:
        await reply_html(update, f"There is no saved key for “{escape(name)}”.")
        return
    text = f"Disconnected {escape(PROVIDERS[name].label if name in PROVIDERS else name)}."
    if was_in_use:
        text += " It was the one in use, so nothing is scored until you /connect another."
    await reply_html(update, text)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action, _, rest = query.data.partition(":")

    if action in ("prov", "key"):
        if rest not in PROVIDERS:
            await query.answer("Unknown provider.")
            return
        await query.answer()
        async with context.bot_data["sessionmaker"]() as session:
            keys = await selection.saved_keys(session)
        if action == "key" or not selection.has_key(rest, keys):
            await _ask_for_key(update, rest)
        else:
            await _choose_model(update, context, rest, keys.get(rest, ""))
        return

    token, _, number = rest.partition(":")
    menu = context.bot_data.get(MENU)
    if not menu or menu["token"] != token or not number.isdigit():
        await query.answer("This list is out of date. Send /models.", show_alert=True)
        return
    if action == "page":
        await query.answer()
        await query.edit_message_reply_markup(models_keyboard(menu, int(number)))
    elif int(number) < len(menu["models"]):
        await query.answer()
        await query.edit_message_reply_markup(None)
        await _use_model(update, context, menu["provider"], menu["models"][int(number)])


async def on_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt_text: str) -> bool:
    message = update.effective_message
    key_prompt = KEY_PROMPT.match(prompt_text)
    model_prompt = MODEL_PROMPT.match(prompt_text)
    prompt = key_prompt or model_prompt
    if not prompt:
        return False
    name = prompt.group(1)
    if name not in PROVIDERS:
        return True
    text = message.text.strip()

    if model_prompt:
        await _use_model(update, context, name, text)
        return True

    try:
        await message.delete()
    except TelegramError:
        await reply_html(update, "I could not delete your message. Delete it yourself.")
    await _choose_model(update, context, name, text, save_key=True)
    return True
