# LeadHunter

A personal tool that finds freelance full-stack leads across several platforms,
scores them, drafts proposals, and sends them to me on Telegram for approval.

Ground rules it is built around:

- I approve every outgoing message in Telegram. Nothing reaches a client without it.
- Only official APIs, public feeds and my own inbox. No scraping of logged-in
  pages, no automated Upwork or Discord user accounts, nothing that evades bot detection.
- Telegram is the only interface.

## How it works

```
collectors -> normalize -> dedupe -> pre-filter -> score (LLM) -> draft (LLM) -> Telegram card
                                                                                    |
                              follow-up after 3 days <- contacted <- I approve, edit or skip
```

1. **Collect.** Each source runs on its own schedule and only fetches posts.
2. **Normalize and dedupe.** HTML is stripped, budgets are parsed, and a post
   is skipped if it was seen before or the same text arrived from another
   source in the last 14 days.
3. **Pre-filter.** Posts older than 48 hours, people offering their own
   services, and posts containing a deal-breaker keyword from `profile.yaml`
   are rejected without an LLM call.
4. **Score.** One LLM call per lead returns a 0-10 score with reasons. Leads
   below the threshold (default 6) are rejected.
5. **Draft.** Three proposal variants per remaining lead: short, portfolio-led,
   problem-led. Each is under 150 words and may only use what is in `profile.yaml`.
6. **Send to me.** Leads scoring 9 or more arrive immediately; the rest come in
   digests at 09:00 and 18:00.
7. **Approve.** `Use draft 1/2/3`, `Edit` (reply with my own text) or `Skip`.
   If the lead has an email address and SMTP is set up, the proposal is emailed
   from my address. Otherwise I get the text in a copyable block with a
   `Mark as sent` button.
8. **Follow up.** Three days after a lead is contacted with no reply, a short
   follow-up is drafted and sent to me for approval the same way.

## Setup

You need Docker with Compose. For tests and the helper commands on the host
you also need [uv](https://docs.astral.sh/uv/).

1. Copy the env file:

   ```sh
   cp .env.example .env
   ```

2. Create the Telegram bot (required):
   - Message [@BotFather](https://t.me/BotFather), send `/newbot`, and put the
     token in `TELEGRAM_BOT_TOKEN`.
   - Message [@userinfobot](https://t.me/userinfobot) for your numeric ID and
     put it in `TELEGRAM_CHAT_ID`. The bot ignores every other chat.
   - Open a chat with your new bot and press Start, or it cannot message you.

3. Edit `profile.yaml` with your real skills, rate and portfolio. The scorer
   and drafter only know what is written there.

4. Start it:

   ```sh
   docker compose up -d --build
   docker compose logs -f app
   ```

   The app applies database migrations, then starts the scheduler and the bot.
   The `started` log line lists the jobs that are scheduled, and
   `collector_disabled` lines name any source that is missing credentials.
   Send `/stats` to your bot.

5. Connect a model (required for scoring and drafting): send `/connect` to the
   bot, pick a provider, paste its API key, pick a model. Nothing about the LLM
   has to go in `.env`. See [LLM providers](#llm-providers).

Remote OK, We Work Remotely and Hacker News need no credentials. The other
sources and email sending are optional and described below.

### If Telegram is blocked on your network

The bot talks to Telegram's HTTPS Bot API (`api.telegram.org`). Where that is
blocked, the log shows `telegram_unreachable` and the bot keeps retrying. Give
it a proxy in `.env`:

```
TELEGRAM_PROXY_URL=socks5://host.docker.internal:1080
```

- SOCKS5 (`socks5://`) and HTTP (`http://`) proxies work, with or without
  `user:password@`. Telegram's in-app MTProto proxies do not: they only carry
  the Telegram apps' own protocol, not HTTPS.
- For a proxy running on this machine, write `host.docker.internal` instead of
  `127.0.0.1`, because the bot runs in a container. The proxy must also listen
  on all interfaces (`0.0.0.0`), not only on `127.0.0.1`.
- Only the bot's Telegram traffic uses the proxy. Collectors and LLM calls go
  out directly. The proxy sees that you connect to Telegram, not the bot token
  or the messages, which stay inside TLS.
- A system-wide VPN works too and needs no setting.

After changing `.env`: `docker compose up -d`.

## Telegram commands

| Command | What it does |
|---|---|
| `/stats` | Found, contacted, replied, won and reply rate, per source and per draft variant |
| `/leads` | Open leads (sent to me, approved, contacted, replied) with their IDs |
| `/replied <id>` | The client replied |
| `/won <id>` | I got the job |
| `/lost <id>` | It went nowhere |
| `/threshold <n>` | Minimum score (0-10) for a lead to be drafted; no number shows the current one |
| `/pause` | Stop collecting, scoring and sending |
| `/resume` | Start again |
| `/connect` | Choose the AI provider, paste its API key, pick a model |
| `/models [text]` | Switch model; the text narrows the list, for example `/models flash` |
| `/llm` | Which model is in use and which keys are saved |
| `/disconnect [provider]` | Forget a saved key |

A weekly summary arrives on Mondays at 09:05.

## LLM providers

The AI provider, its API key and the model are chosen in Telegram. Nothing
about the LLM goes in `.env`.

1. Send `/connect`. The bot lists the providers; a tick marks the ones it
   already has a key for.
2. Pick one. The bot asks for the API key; reply with it. The bot deletes your
   message as soon as it has read it.
3. The bot fetches that provider's model list with your key and shows it as
   buttons. Pick a model. `/models glm` narrows a long list.
4. The bot makes one small test call to the model. If it works, scoring and
   drafting use that model from the next run. If it fails, nothing changes and
   the provider's error is shown.

Later, `/models` switches model and `/connect` switches provider. A provider
whose key is saved does not ask for it again. `/llm` shows what is in use and
`/disconnect <provider>` deletes a saved key.

Providers: Anthropic (Claude), Google Gemini, OpenAI, DeepSeek, Qwen (Alibaba
DashScope, international endpoint), Kimi (Moonshot), GLM (Zhipu), MiniMax,
Doubao (Volcengine Ark), SiliconFlow, OpenRouter, OpenCode Zen, Groq, and
Ollama running on this machine.

What to know:

- Model lists are read from each provider's API, so new models appear without
  a code change. If a provider does not publish a list, the bot asks you to
  type the model ID.
- Keys are stored as plain text in the `app_state` table of the local
  database. They are not written to the log or the `events` table. The key
  passes through Telegram on its way to the bot.
- One model is used for both scoring and drafting.
- Every provider's model-list endpoint was reached on 2026-11-12 and rejected
  an invalid key as expected; no chat call was made with a real key.
- OpenCode Zen lists all its models but serves only some of them through the
  OpenAI-compatible endpoint this adapter uses. The test call in `/connect`
  tells you when you pick one that is not. For Claude or Gemini models, use
  the Anthropic or Gemini provider directly.
- Anthropic requests for Opus, Sonnet and Fable models opt in to server-side
  fallback, so a request declined by a safety classifier is retried on the
  model Anthropic recommends instead of failing.
- Ollama is reached at `host.docker.internal:11434` from the app container and
  needs no key.

In the code, every LLM call is one `complete_json` on an `LLMClient`
(`leadhunter/llm/client.py`):

```python
llm = await active_llm(session)
result = await llm.complete_json(system, user, ScoreResult)
```

`active_llm` (in `leadhunter/llm/selection.py`) returns the model chosen with
`/connect`. The adapter comes from the registry in
`leadhunter/llm/providers/__init__.py`.

**Adding a provider.** If it speaks the OpenAI protocol, add one line to
`PROVIDERS` with its name and base URL. If it has its own protocol, write a
`Provider` subclass with `complete(system, user, schema)` and `list_models()`
and register it in `ADAPTERS`. Either way it appears in `/connect` by itself.

**Tuning the scorer.** Paste a job post and see how it scores with the connected
model (the database must be running):

```sh
uv run python -m leadhunter.score_test             # paste, then Ctrl-D
uv run python -m leadhunter.score_test post.txt --prompt
```

The prompts are plain files in `leadhunter/llm/prompts/`.

## Sources and how to get each credential

| Source | Needs | Notes |
|---|---|---|
| Remote OK | nothing | JSON API. Their terms ask API users to credit Remote OK and link back; lead cards do both. |
| We Work Remotely | nothing | RSS. Add more category feeds in `WWR_FEEDS`. |
| Hacker News | nothing | The monthly "Who is hiring?" thread via the Algolia API, filtered to `HN_KEYWORDS`. |
| Reddit | API app | See below. |
| Upwork | IMAP login | Reads the job alert emails Upwork sends you. See below. |
| Telegram channels | API ID, hash, session | Reads public channels as your account. See below. |
| Discord | bot token | A normal bot in servers that invited it. See below. |

**Reddit.** At <https://www.reddit.com/prefs/apps> create an app of type
"script". The ID under the app name is `REDDIT_CLIENT_ID`; "secret" is
`REDDIT_CLIENT_SECRET`. Put your username in `REDDIT_USER_AGENT`. Since late
2025 Reddit reviews new API access requests and may refuse personal scripts; if
you cannot get credentials, remove `reddit` from `COLLECTORS`. Only posts
tagged as hiring are kept (`[Hiring]`, `[TASK]`, or a hiring flair).

**Upwork.** In Upwork, save a search and turn on email alerts for it. Then set
`IMAP_HOST`, `IMAP_USERNAME` and `IMAP_PASSWORD` for the inbox that receives
them. For Gmail, enable 2-step verification and create an
[app password](https://myaccount.google.com/apppasswords); the host is
`imap.gmail.com`. Emails are read without being marked as read.

**Telegram channels.** At <https://my.telegram.org> open "API development
tools" and create an application to get `TELEGRAM_API_ID` and
`TELEGRAM_API_HASH`. Then log in once:

```sh
uv run python -m leadhunter.telegram_login
```

It asks for your phone number and the code Telegram sends, and prints a
`TELEGRAM_SESSION=...` line for `.env`. That string is a login to your account,
so guard it like a password; you can revoke it in Telegram under Settings ->
Devices. List channel usernames in `TELEGRAM_CHANNELS`. The collector only
reads those channels; it never sends, joins or marks anything as read.

**Discord.** At <https://discord.com/developers/applications> create an
application, add a bot, copy its token into `DISCORD_BOT_TOKEN`, and switch on
the **Message Content Intent** on the Bot page. Invite it with the OAuth2 URL
generator (scope `bot`, permissions View Channels and Read Message History); you
need permission to add bots to that server. Enable Developer Mode in Discord,
right-click each job channel, Copy Channel ID, and list them in
`DISCORD_CHANNEL_IDS`. The bot only listens, and it only sees messages posted
while it is running.

**Email sending (SMTP).** Set `SMTP_HOST`, `SMTP_USERNAME`, `SMTP_PASSWORD` and
optionally `SMTP_FROM` / `SMTP_FROM_NAME`. For Gmail use `smtp.gmail.com`, port
587 and an app password. Without SMTP, every approved proposal comes back to
you as text to send yourself.

## Adding a collector

1. Create `leadhunter/collectors/<name>.py` with a pure parsing function and a
   collector class:

   ```python
   from leadhunter.collectors.base import BaseCollector, RawLead


   def parse_posts(payload: dict) -> list[RawLead]:
       return [
           RawLead(
               source="mysource",
               external_id=str(p["id"]),
               url=p["url"],
               title=p["title"],
               body=p["html"],
               posted_at=...,
               raw=p,
           )
           for p in payload["posts"]
       ]


   class MySourceCollector(BaseCollector):
       name = "mysource"  # stored in leads.source
       interval_minutes = 30

       async def fetch(self) -> list[RawLead]:
           async with make_client() as client:  # leadhunter.http
               response = await get_with_retry(client, "https://...")
           return parse_posts(response.json())
   ```

   A collector only fetches. It must not touch the database or the LLM, and it
   may raise: the runner logs the error and the other collectors carry on.
   `body` can be HTML; the normalizer cleans it. Put structured fields the
   scorer should see (salary, location) into `body` as text lines.

2. Register it in `BUILDERS` in `leadhunter/collectors/__init__.py`, add any
   settings it needs to `Settings` in `config.py` and to `.env.example`, and add
   its name to the `COLLECTORS` default.

3. Save a real sample payload in `tests/fixtures/` and test the parsing
   function against it. Tests never use the network.

4. Try it for real: `uv run python -m leadhunter.collect_once mysource`.

## Development

```sh
uv sync                      # create .venv with Python 3.12 and all dependencies
docker compose up -d db      # Postgres on 127.0.0.1:5432
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

Tests never touch the network. Tests that need Postgres are skipped unless
`TEST_DATABASE_URL` points at a throwaway database (its tables are dropped and
recreated on every run):

```sh
docker compose exec db createdb -U leadhunter leadhunter_test
TEST_DATABASE_URL=postgresql+asyncpg://leadhunter:leadhunter@localhost:5432/leadhunter_test uv run pytest
```

Useful commands on the host (they read `.env` and use `DATABASE_URL`):

```sh
uv run alembic upgrade head                          # apply migrations
uv run python -m leadhunter.collect_once             # run every enabled collector once
uv run python -m leadhunter.collect_once hackernews  # or just one
uv run python -m leadhunter.score_test               # score a pasted job post
LOG_FORMAT=console uv run python -m leadhunter.main  # run the whole app without Docker
```

### Logs

Logs are JSON, one event per line. The ones worth watching:

| Event | Meaning |
|---|---|
| `collector_run` | `source`, `fetched`, `new`, `duplicates`, `rejected` for one collector run |
| `collector_failed` | A source raised; includes the traceback. Other sources are unaffected. |
| `score_run` / `draft_run` | How many leads were scored, accepted, rejected, drafted |
| `cards_sent` | Lead cards sent to Telegram |
| `llm_retry`, `llm_invalid_json` | The model was rate limited or returned something unusable |
| `unauthorized_update` | Someone other than you messaged the bot |

### Database

Tables: `leads`, `drafts`, `events` (the audit trail of every status change and
every message sent), `followups`, and `app_state` (the threshold and paused
flag set from Telegram).

After changing `leadhunter/db/models.py`:

```sh
uv run alembic revision --autogenerate -m "describe the change"
uv run alembic upgrade head
```

Read the generated file in `leadhunter/db/migrations/versions/` before applying it.

## Layout

```
leadhunter/
  collectors/   base.py and one file per source, registry in __init__.py
  pipeline/     normalize, dedupe, prefilter, scorer, drafter, runner
  bot/          Telegram application, handlers, /connect flow, card formatting, delivery
  tracker/      audit events, stats and prompt feedback, follow-ups
  llm/          client.py (the one entry point), selection.py (/connect choice), providers/, prompts/
  db/           models, session, Alembic migrations
  config.py     settings from .env, profile from profile.yaml
  jobs.py       the scheduled jobs
  main.py       starts the scheduler and the bot
  collect_once.py, score_test.py, telegram_login.py   command-line helpers
profile.yaml    your skills, rate, portfolio, preferences
```
