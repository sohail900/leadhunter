import re
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from leadhunter.db.models import Conversation, Draft, Lead, Message, MessageKind
from leadhunter.pipeline.scorer import format_budget
from leadhunter.text import EMAIL_RE
from leadhunter.tracker.stats import Counts, Stats

TELEGRAM_LIMIT = 4096

SOURCE_NAMES = {
    "remoteok": "Remote OK",
    "weworkremotely": "We Work Remotely",
    "hackernews": "Hacker News",
    "reddit": "Reddit",
    "upwork": "Upwork (email alert)",
    "telegram": "Telegram",
    "discord": "Discord",
}
VARIANT_NAMES = {
    "short": "short",
    "portfolio_led": "portfolio-led",
    "problem_led": "problem-led",
    "custom": "my own text",
    "followup": "follow-up",
}

REFUSAL = (
    "I only work on lead hunting: finding projects, talking to clients, prices and reports. "
    "I can't help with that. Send /help to see what I can do."
)
STATE_NAMES = {
    "active": "in conversation",
    "waiting_owner": "waiting for your answer",
    "agreed": "deal agreed",
    "closed": "closed",
}
SPEAKERS = {
    MessageKind.CLIENT: "Client",
    MessageKind.SENT: "Sent",
    MessageKind.DRAFT: "Draft, not sent",
    MessageKind.OWNER: "Your instruction",
}

HELP_TEXT = (
    "<b>LeadHunter</b>\n\n"
    "You can write to me in plain words, for example “what is open?”, "
    "“tell #12 the price is 800” or “show lead 12”. I only do lead hunting.\n\n"
    "/report - today's report (also sent every evening)\n"
    "/auto on|off - send first messages and replies to clients without asking me\n"
    "/client &lt;id&gt; &lt;text&gt; - what a client wrote to me outside email\n"
    "/stats - results per source and per draft variant\n"
    "/leads - open leads\n"
    "/replied &lt;id&gt; - the client replied\n"
    "/won &lt;id&gt; - I got the job\n"
    "/lost &lt;id&gt; - it went nowhere\n"
    "/threshold &lt;n&gt; - minimum score (0-10) for a lead to be drafted\n"
    "/pause - stop collecting, scoring and sending\n"
    "/resume - start again\n\n"
    "/connect - choose the AI provider, paste its key, pick a model\n"
    "/models - switch model\n"
    "/llm - which model is in use\n"
    "/disconnect &lt;provider&gt; - forget a saved key"
)


def source_name(source: str) -> str:
    return SOURCE_NAMES.get(source, source)


def email_contact(lead: Lead) -> str | None:
    hint = (lead.contact_hint or "").strip()
    return hint if EMAIL_RE.fullmatch(hint) else None


def _rate(counts: Counts) -> str:
    return "–" if counts.reply_rate is None else f"{counts.reply_rate:.0%}"


def _table(first_column: str, rows: dict[str, Counts], count_label: str) -> str:
    width = max([len(first_column), *(len(name) for name in rows)])
    header = f"{first_column:<{width}}  {count_label:>5}  sent  repl  won  rate"
    lines = [
        f"{escape(name):<{width}}  {c.found:>5}  {c.contacted:>4}  {c.replied:>4}"
        f"  {c.won:>3}  {_rate(c):>4}"
        for name, c in rows.items()
    ]
    return "<pre>" + "\n".join([header, *lines]) + "</pre>"


def format_stats(stats: Stats, title: str = "LeadHunter stats") -> str:
    t = stats.total
    lines = [
        f"<b>{escape(title)}</b>",
        "",
        f"Found: <b>{t.found}</b>",
        f"Contacted: <b>{t.contacted}</b>",
        f"Replied: <b>{t.replied}</b>",
        f"Won: <b>{t.won}</b>",
        f"Reply rate: <b>{_rate(t)}</b>",
    ]
    if not stats.by_source:
        lines += ["", "No leads collected yet."]
        return "\n".join(lines)

    lines += ["", _table("source", stats.by_source, "found")]
    if stats.by_variant:
        variants = {VARIANT_NAMES.get(k, k): v for k, v in stats.by_variant.items()}
        lines += ["", _table("draft", variants, "used")]
    return "\n".join(lines)


def lead_keyboard(lead_id: int, draft_count: int) -> InlineKeyboardMarkup:
    use = [
        InlineKeyboardButton(f"Use draft {n}", callback_data=f"use:{lead_id}:{n}")
        for n in range(1, draft_count + 1)
    ]
    rest = [
        InlineKeyboardButton("Edit", callback_data=f"edit:{lead_id}"),
        InlineKeyboardButton("Skip", callback_data=f"skip:{lead_id}"),
    ]
    return InlineKeyboardMarkup([use, rest])


def followup_keyboard(lead_id: int, draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Send follow-up", callback_data=f"fu:{draft_id}")],
            [
                InlineKeyboardButton("Edit", callback_data=f"fuedit:{lead_id}"),
                InlineKeyboardButton("Skip", callback_data=f"fuskip:{draft_id}"),
            ],
        ]
    )


def mark_sent_keyboard(lead_id: int, kind: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Mark as sent", callback_data=f"sent:{lead_id}:{kind}")]]
    )


def _link(lead: Lead) -> str:
    return f'<a href="{escape(lead.url, quote=True)}">Open the post</a>' if lead.url else "no link"


def _contact_line(lead: Lead, can_email: bool) -> str:
    if not lead.contact_hint:
        return "Contact: reply on the post"
    hint = escape(lead.contact_hint)
    if email_contact(lead) and can_email:
        return f"Contact: {hint} (approving a draft emails it)"
    return f"Contact: {hint}"


def lead_header(lead: Lead, can_email: bool) -> str:
    reasons = lead.score_reasons or {}
    score = "?" if lead.score is None else f"{lead.score:g}"
    lines = [
        f"<b>{score}/10 · {escape(source_name(lead.source))} · lead #{lead.id}</b>",
        f"<b>{escape(lead.title[:300])}</b>",
    ]
    if reasons.get("summary"):
        lines.append(escape(reasons["summary"]))
    lines += ["", f"Budget: {format_budget(lead.budget_min, lead.budget_max)}"]
    if lead.tech_stack:
        lines.append(f"Stack: {escape(', '.join(lead.tech_stack))}")
    for reason in (reasons.get("reasons") or [])[:4]:
        lines.append(f"• {escape(str(reason))}")
    lines += [_contact_line(lead, can_email), _link(lead)]
    return "\n".join(lines)


def format_lead_card(lead: Lead, drafts: list[Draft], can_email: bool) -> list[str]:
    header = lead_header(lead, can_email)
    draft_blocks = [
        f"<b>Draft {n} · {VARIANT_NAMES.get(draft.variant, draft.variant)}</b>\n"
        + escape(draft.text)
        for n, draft in enumerate(drafts, start=1)
    ]
    body = "\n\n".join(draft_blocks)
    whole = f"{header}\n\n{body}"
    if len(whole) <= TELEGRAM_LIMIT:
        return [whole]
    return [header[:TELEGRAM_LIMIT], body[:TELEGRAM_LIMIT]]


def format_followup_card(lead: Lead, draft: Draft, can_email: bool) -> str:
    return "\n".join(
        [
            f"<b>Follow-up due · lead #{lead.id}</b>",
            f"<b>{escape(lead.title[:300])}</b>",
            "No reply yet to the proposal you sent.",
            _contact_line(lead, can_email),
            _link(lead),
            "",
            "<b>Suggested follow-up</b>",
            escape(draft.text),
        ]
    )


def format_to_send_yourself(lead: Lead, text: str, note: str = "") -> str:
    lines = [f"<b>Send this yourself · lead #{lead.id}</b>"]
    if note:
        lines.append(escape(note))
    lines += [_contact_line(lead, can_email=False), _link(lead), "", f"<pre>{escape(text)}</pre>"]
    return "\n".join(lines)


def email_subject(lead: Lead) -> str:
    title = re.sub(r"^\s*(\[[^\]]{1,20}\]\s*)+", "", lead.title).strip() or lead.title
    return f"Re: {title[:120]}"


def format_leads_list(leads: list[Lead], waiting_for_digest: int) -> str:
    if not leads and not waiting_for_digest:
        return "No open leads."
    lines = ["<b>Open leads</b>", ""]
    for lead in leads:
        score = "?" if lead.score is None else f"{lead.score:g}"
        title = escape(lead.title[:70])
        if lead.url:
            title = f'<a href="{escape(lead.url, quote=True)}">{title}</a>'
        status = lead.status.replace("_", " ")
        lines.append(f"#{lead.id} · {status} · {score}/10 · {title}")
    if waiting_for_digest:
        lines += ["", f"{waiting_for_digest} more drafted and waiting for the next digest."]
    return "\n".join(lines)


def shorten(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def client_line(conversation: Conversation) -> str:
    parts = [p for p in (conversation.client_name, conversation.address) if p]
    return "Client: " + escape(" · ".join(dict.fromkeys(parts)) or "unknown")


def _conversation_head(title: str, lead: Lead, conversation: Conversation) -> list[str]:
    return [
        f"<b>{title} · lead #{lead.id}</b>",
        f"<b>{escape(shorten(lead.title, 200))}</b>",
        client_line(conversation),
        _link(lead),
    ]


def _client_wrote(client_text: str) -> list[str]:
    return ["", "<b>Client wrote</b>", escape(shorten(client_text, 1200))] if client_text else []


def question_keyboard(lead_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Answer", callback_data=f"cans:{lead_id}"),
                InlineKeyboardButton("Write the reply myself", callback_data=f"cown:{lead_id}"),
            ]
        ]
    )


def draft_keyboard(message_id: int, lead_id: int, sends_email: bool) -> InlineKeyboardMarkup:
    send = "Send" if sends_email else "Mark as sent"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(send, callback_data=f"csend:{message_id}")],
            [
                InlineKeyboardButton("Write it myself", callback_data=f"cown:{lead_id}"),
                InlineKeyboardButton("Skip", callback_data=f"cskip:{message_id}"),
            ],
        ]
    )


def format_owner_question(
    lead: Lead, conversation: Conversation, client_text: str, question: str
) -> str:
    lines = _conversation_head("Client question", lead, conversation)
    if conversation.brief:
        lines += ["", "<b>Project</b>", escape(shorten(conversation.brief, 700))]
    lines += _client_wrote(client_text)
    lines += [
        "",
        "<b>Needs your answer</b>",
        escape(shorten(question, 600)),
        "",
        "Reply to this message with your answer, for example the price. I will write it to "
        "the client.",
    ]
    return "\n".join(lines)


def format_reply_draft(
    lead: Lead,
    conversation: Conversation,
    client_text: str,
    text: str,
    sends_email: bool,
    note: str = "",
) -> str:
    lines = _conversation_head("Reply to client", lead, conversation)
    if note:
        lines.append(escape(note))
    lines += _client_wrote(client_text)
    lines += ["", "<b>My reply</b>", f"<pre>{escape(shorten(text, 1800))}</pre>", ""]
    lines.append(
        "Send it, or reply to this message to tell me what to change."
        if sends_email
        else "I cannot send on this channel: send it yourself, then mark it as sent. "
        "Reply to this message to tell me what to change."
    )
    return "\n".join(lines)


def format_sent_notice(lead: Lead, conversation: Conversation, text: str, first: bool) -> str:
    title = "First message sent" if first else "Replied to client"
    lines = _conversation_head(title, lead, conversation)
    lines += ["", f"<pre>{escape(shorten(text, 2500))}</pre>"]
    return "\n".join(lines)


def format_deal(
    lead: Lead, conversation: Conversation, instructions: list[str], last_client_text: str
) -> str:
    lines = _conversation_head("Deal agreed", lead, conversation)
    lines += ["", "<b>Project brief</b>", escape(shorten(conversation.brief or lead.title, 1200))]
    if instructions:
        lines += ["", "<b>What you told me (price and terms)</b>"]
        lines += [f"• {escape(shorten(text, 300))}" for text in instructions[-5:]]
    lines += _client_wrote(last_client_text)
    lines += ["", f"The lead is marked won. If that is wrong, send /lost {lead.id}."]
    return "\n".join(lines)


def format_client_lost(lead: Lead, conversation: Conversation, client_text: str) -> str:
    lines = _conversation_head("Client declined", lead, conversation)
    lines += _client_wrote(client_text)
    lines += ["", "The lead is marked lost and I have stopped replying."]
    return "\n".join(lines)


def format_conversation(
    lead: Lead, conversation: Conversation | None, messages: list[Message], can_email: bool
) -> str:
    lines = [lead_header(lead, can_email)]
    if conversation is None:
        lines += ["", f"Status: {lead.status.replace('_', ' ')}. No conversation yet."]
        return "\n".join(lines)
    lines += ["", client_line(conversation), f"Status: {STATE_NAMES[conversation.state]}"]
    if conversation.brief:
        lines += ["", "<b>Project</b>", escape(shorten(conversation.brief, 600))]
    for message in messages[-6:]:
        lines += ["", f"<b>{SPEAKERS[message.kind]}</b>", escape(shorten(message.text, 400))]
    return "\n".join(lines)[:TELEGRAM_LIMIT]
