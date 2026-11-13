# LeadHunter: handoff notes

Written 2026-11-12 at the end of the first build session. Read this before
changing anything. The original brief is `proj-idea.md`; the user-facing
documentation is `README.md`.

## What this is

A single-user tool that finds freelance full-stack leads, scores them with an
LLM, drafts three proposal variants, sends them to the owner on Telegram, and
then holds the conversation with the client. Telegram is the only interface;
there is no web UI.

Pipeline: collectors -> normalize -> dedupe -> pre-filter -> score -> draft ->
Telegram card -> approve, edit or skip -> follow-up after 3 days -> client
conversation -> price and commitments from the owner -> deal brief.

## Second session, 2026-11-13: the "project manager" loop

The owner asked for the bot to act as a project manager: contact clients, talk
to them, bring pricing to him, close, hand over a brief; to refuse anything
that is not lead hunting; to use retrieval for better prompts; and to send an
end-of-day report. Built in this session, uncommitted at the time of writing:

- `agent/conversation.py`: one conversation per lead (`conversations`,
  `messages` tables, migration `2fb3e31ab7ba`). The model returns reply,
  ask_owner, deal or lost. `enforce()` and `agent/guard.py` are code-level
  rules: no money figure the owner has not given, no deal without owner terms.
- `inbox.py` + `jobs.inbox`: client replies read over IMAP, matched to a
  conversation by thread references, then by sender address.
- `agent/outreach.py`: with `/auto on`, first emails go out without approval,
  capped by `AUTO_DAILY_LIMIT`.
- `knowledge.py`: keyword (BM25) retrieval over `knowledge/*.md` plus won
  proposals, fed to the drafter, the reply prompt and the chat prompt.
- `bot/chat.py` + `agent/assistant.py`: plain messages are mapped to a fixed
  list of actions; anything else gets `formatting.REFUSAL`.
- `agent/report.py` + `jobs.daily_report`: the daily report and `/report`.

Decisions the owner made, replacing the earlier "a human approves every
outgoing message" rule: automatic sending exists but is off until `/auto on`;
price, dates and commitments always go to the owner.

Decided against, with the reason given to the owner: a Discord account that
searches for and joins servers by itself, and automatic direct messages on
Discord. A bot cannot join servers, and automating a user account breaks
Discord's rules. Non-email channels stay manual (`/client`).

Verified live with Gemini (`gemini-3.1-flash-lite`): the chat scope refusals
and a full conversation (question, price hand-off, exact price relayed, "are you
an AI" handed to the owner, deal). Not verified against real services: IMAP
reading of client replies, and automatic SMTP sending.

Known limit: the owner's free Gemini key allows 20 requests a day on
`gemini-3.6-flash`, which scoring alone exhausts. Lite models have separate
quotas.

## Current state

- All six phases of the brief are built, plus the second-session work above.
- 272 tests pass on both SQLite and Postgres. Lint (`ruff`) is clean.
- The bot is deployed and was started successfully on the owner's shared web
  hosting, running with plain Python and SQLite (no Docker there).
- Repository: https://github.com/sohail900/leadhunter (public, branch `main`).

Verified against real services:

- Remote OK, We Work Remotely and Hacker News collectors (live fetches).
- Every LLM provider's model-list endpoint answers and rejects a bad key.
- The bot connecting to Telegram and starting its jobs on the hosting.

Not verified yet:

- `/connect` with a real API key, and so any real scoring or drafting.
- Prompt quality (scorer, drafter, follow-up).
- Reddit, Upwork email (IMAP), Telegram channels (Telethon), Discord: parsing
  is tested with fixtures only; none has connected to the real service. The
  Reddit and Upwork fixtures are hand-written, not captured.
- SMTP sending.
- Whether the hosting keeps a background process alive long term.

## How it runs

There are two supported ways, and both must keep working.

| | Plain Python | Docker Compose |
|---|---|---|
| Start | `.venv/bin/python main.py` | `docker compose up -d --build` |
| Database | SQLite file `data/leadhunter.db` | Postgres 16 container |
| `DATABASE_URL` | from `.env` (SQLite by default) | overridden in `docker-compose.yml` |
| Migrations | run by `main.py` | run by the container command |
| Used on | the owner's shared hosting | any machine with Docker |

Root `main.py` changes to the project folder, takes a lock in `data/bot.lock`
so a second copy exits, runs `alembic upgrade head`, then starts
`leadhunter.main.run()`.

On the hosting the libraries are installed with `pip` from `requirements.txt`
into a `uv`-created Python 3.12 virtualenv. `uv sync` crashes there on the
host's thread limit, so do not switch the hosting back to it. After changing
dependencies, regenerate the file:

```sh
uv export --no-dev --no-hashes --no-emit-project -o requirements.txt
```

Background start on the hosting:

```sh
nohup .venv/bin/python main.py >> data/bot.log 2>&1 &
```

The same command can be a cron job as a restart after a crash or reboot.

## Decisions that are settled

Do not reopen these without the owner asking.

- **LLM setup is Telegram-only.** Provider, API key and model are chosen with
  `/connect` and stored in the `app_state` table. There are no LLM variables in
  `.env`, and none should be added back. One model does both scoring and
  drafting. Model lists come from each provider's API; no model names are
  hard-coded.
- **Providers:** Anthropic, Gemini, OpenAI, DeepSeek, Qwen, Kimi, GLM, MiniMax,
  Doubao, SiliconFlow, OpenRouter, OpenCode Zen, Groq, Ollama. Adding an
  OpenAI-compatible one is one line in `leadhunter/llm/providers/__init__.py`.
- **Hacker News source** is the monthly "Who is hiring?" thread filtered by
  keywords, because the freelancer thread no longer exists.
- **Telegram channels** are read with the owner's own account, read-only.
- **Both databases are supported.** Models use JSON and datetime types that
  work on SQLite and Postgres. Any new query must run on both; the test suite
  runs against either through `TEST_DATABASE_URL`.

## Ground rules from the brief

- A human approves every outgoing message unless the owner has switched
  `/auto on`; even then price, dates and commitments need the owner.
- The bot refuses anything that is not lead hunting.
- Only official APIs, public feeds and the owner's own inbox. No scraping of
  logged-in pages, no automating a Discord or Upwork user account, nothing
  meant to avoid bot detection.
- The bot answers only the chat ID in `TELEGRAM_CHAT_ID`.
- Secrets come from `.env`, except LLM keys, which the owner chose to enter
  through Telegram.
- Simple, readable code over abstraction.

## How the owner wants work done

- **No comments and no docstrings in the code.** They were removed on request.
  Only `.env.example` has comments, and those are one short line each.
- **Never commit or push without being asked.** Finishing a task is not
  permission, and permission covers only the piece of work it was given for.
- **Commits are authored by the owner only**, with no AI attribution in commit
  messages or pull requests. This repository commits with the owner's GitHub
  no-reply address because it is public.
- **Give one direct instruction at a time.** Lists of alternatives were not
  welcome during setup.

## Known constraints

- Telegram is blocked on the network of the owner's own computer, so the bot
  cannot run there without a proxy or VPN. `TELEGRAM_PROXY_URL` (SOCKS5 or
  HTTP) exists for that case. MTProto proxies do not work for the Bot API.
- The hosting is shared web hosting: no root, no Docker service, no Postgres.
  It can reach Telegram directly.
- The Telegram channel collector (Telethon) does not use `TELEGRAM_PROXY_URL`.

## Open items

1. An uncommitted fix in `leadhunter/main.py` and `tests/test_bot.py`: a failed
   first message to the owner ("Chat not found") used to crash startup; it now
   logs `cannot_message_owner` and keeps running. It needs the owner's go-ahead
   to commit and push, then `git pull` on the hosting.
2. Confirm `/connect` works with a real key and look at the first real scores.
3. Replace the placeholder content in `profile.yaml` with the owner's real
   skills, rate and portfolio. The scorer and drafter only know what is there.
4. Add credentials for the remaining collectors (Reddit, Upwork IMAP, Telegram
   channels, Discord) and check each against the real service. Check the
   Upwork parser against a real alert email first.
5. Set up the cron restart on the hosting if it is not done.
6. Tune the scorer with `python -m leadhunter.score_test` once a model is
   connected.

## Where things are

```
main.py                 plain-Python entry point
leadhunter/main.py      scheduler and bot startup
leadhunter/jobs.py      scheduled jobs
leadhunter/collectors/  one file per source, registry in __init__.py
leadhunter/pipeline/    normalize, dedupe, prefilter, scorer, drafter, runner
leadhunter/bot/         Telegram app, handlers, /connect flow, cards
leadhunter/llm/         client, selection (the /connect choice), providers, prompts
leadhunter/tracker/     audit events, stats, follow-ups
leadhunter/db/          models, session, Alembic migrations
tests/                  199 tests, fixtures in tests/fixtures
```

## Checks to run before handing work back

```sh
uv run ruff check . && uv run ruff format --check .
TEST_DATABASE_URL=sqlite+aiosqlite:///tmp-test.db uv run pytest -q
TEST_DATABASE_URL=postgresql+asyncpg://leadhunter:leadhunter@localhost:5432/leadhunter_test uv run pytest -q
```

The Postgres run needs `docker compose up -d db` and a `leadhunter_test`
database.
