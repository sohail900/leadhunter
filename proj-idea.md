You are building "LeadHunter", a personal tool that finds freelance full-stack
development leads across several platforms, scores them, drafts proposals, and
sends them to me on Telegram for approval. I am a solo full-stack freelancer and
the only user. The goal is to turn an hour of daily manual job searching into
about ten minutes of reviewing good leads.

## Ground rules

- A human (me) approves every outgoing message. The tool never contacts a client
  without my explicit approval in Telegram.
- Use only official APIs, public feeds, and my own email inbox. Do not scrape
  logged-in pages, do not automate a Discord or Upwork user account, and do not
  add anything designed to avoid bot detection. My accounts on these platforms
  are how I earn, so anything that risks a ban is out of scope.
- There is no web UI. Telegram is the only interface.
- Favor simple, readable code over abstraction. This is a single-user tool.

## Stack

- Python 3.12, asyncio throughout
- PostgreSQL 16, SQLAlchemy 2.x (async), Alembic migrations
- APScheduler for scheduling collectors and follow-up checks
- python-telegram-bot for the approval interface
- httpx for HTTP, feedparser for RSS, imap-tools for email, PRAW (or asyncpraw)
  for Reddit, Telethon for Telegram channels, discord.py for the Discord bot
- pydantic and pydantic-settings for models and config; all secrets from .env
- The LLM is called through one small interface (llm/client.py) with a
  `complete_json(system, user, schema)` method, so the provider can be swapped
  by changing an env var. Implement one provider and keep the interface clean.
- Docker Compose with two services: app and db
- pytest for tests, ruff for linting

## Project layout

leadhunter/
collectors/ base.py plus one file per source
pipeline/ normalize.py, dedupe.py, scorer.py, drafter.py
bot/ telegram handlers, message formatting
tracker/ follow-ups, stats
llm/ client.py, prompts/
db/ models.py, session.py, migrations/
config.py
main.py starts scheduler and Telegram bot
profile.yaml my skills, rate, portfolio, preferences
docker-compose.yml, Dockerfile, .env.example, README.md

## Collector interface

Each collector subclasses BaseCollector and implements
`async def fetch(self) -> list[RawLead]`. RawLead has: source, external_id,
url, title, body, author, posted_at, contact_hint (email or handle if present),
and raw (original payload as JSON). Collectors only fetch; they never write to
the database or call the LLM. A failing collector logs the error and must not
stop the others.

Collectors to build:

1. job_feeds: RemoteOK JSON API and We Work Remotely RSS
2. hackernews: the current "Who is hiring" and "Freelancer? Seeking freelancer?"
   threads via the HN Algolia API
3. reddit: new posts in a configurable list of subreddits (default: forhire,
   slavelabour, jobbit, remotejs), keeping only posts tagged as hiring
4. imap_upwork: reads Upwork job alert emails from my inbox over IMAP and
   extracts title, description, budget, and job link
5. telegram: reads new messages from a configurable list of public job channels
6. discord: a normal Discord bot (bot token, not a user token) that listens in
   configured channels of servers it has been invited to and captures messages
   that look like job posts

## Data model

- leads: id, source, external_id (unique with source), url, title, body, author,
  contact_hint, posted_at, fetched_at, content_hash, status, score,
  score_reasons (JSON), budget_min, budget_max, tech_stack (array)
  status values: new, scored, rejected, drafted, sent_to_me, approved, skipped,
  contacted, replied, won, lost
- drafts: id, lead_id, variant (short, portfolio_led, problem_led), text,
  chosen (bool), edited_text
- events: id, lead_id, type, payload (JSON), created_at (audit trail of every
  status change and every message sent)
- followups: id, lead_id, due_at, done

## Pipeline

1. Normalize: clean HTML, trim whitespace, parse budgets where present.
2. Dedupe: skip if (source, external_id) exists, or if content_hash matches a
   lead from the last 14 days (the same job is often cross-posted).
3. Cheap pre-filter before any LLM call: drop posts older than 48 hours, posts
   where someone is offering services rather than hiring, and posts matching
   deal-breaker keywords from profile.yaml.
4. Score: one LLM call per lead returning JSON with: score (0-10), stack_match,
   budget_fit, client_quality, scam_risk (each 0-10), tech_stack, a one-line
   summary, and reasons. Give the model my profile.yaml as context. Leads below
   the configurable threshold (default 6) are marked rejected.
5. Draft: for leads at or above the threshold, generate three proposal variants
   (short and direct; led by my most relevant past project; led by a specific
   observation about their problem). Proposals must be under 150 words, refer
   to concrete details from the post, sound like a person rather than a
   template, and never invent experience that is not in profile.yaml.

## Telegram bot

- Only responds to my chat ID (from .env).
- For each drafted lead, send a card: source, title, score, one-line summary,
  budget, reasons, link, then the three drafts.
- Inline buttons: Use draft 1 / 2 / 3, Edit (I reply with my own text), Skip.
- On approval: if the lead has an email contact, send it via SMTP from my
  address and mark it contacted. Otherwise reply with the link and the final
  text in a copyable block so I can send it myself, with a "Mark as sent"
  button.
- Leads scoring 9 or above are sent immediately; the rest are batched into a
  digest at configurable times (default 09:00 and 18:00 Asia/Karachi).
- Commands: /stats, /leads (open leads), /won <id>, /lost <id>, /replied <id>,
  /pause, /resume, /threshold <n>.

## Tracker

- When a lead is marked contacted, create a follow-up due in 3 days. When due,
  draft a short follow-up and send it to me for approval the same way.
- /stats and a weekly summary show, per source and per draft variant: leads
  found, contacted, replied, won, and reply rate.
- Include the best-performing sources and variants in the scorer and drafter
  prompts as light guidance once there are at least 20 contacted leads.

## Build order

Build and verify each phase before starting the next. After each phase, tell me
what works, how to run it, and what you could not verify.

Phase 1: repo skeleton, config, Docker Compose, db models and migrations,
Telegram bot that answers /stats with zeros.
Phase 2: collector interface, job_feeds, hackernews, reddit, normalizer,
deduper, scheduler. Leads land in the database.
Phase 3: LLM client, pre-filter, scorer. Add a `python -m leadhunter.score_test`
command that scores a pasted job post so I can tune the prompt.
Phase 4: drafter, Telegram lead cards with buttons, approval flow, SMTP sending.
Phase 5: imap_upwork, telegram, and discord collectors.
Phase 6: follow-ups, stats, weekly summary, feedback into prompts.

## Quality bar

- Unit tests for normalize, dedupe, pre-filter, and each collector's parsing
  (use saved sample payloads as fixtures; no live network calls in tests).
- Structured logging; every collector run logs how many leads were fetched,
  new, and rejected.
- Retries with backoff on network calls, and respect each API's rate limits.
- README with setup steps, how to obtain each API key or token, and how to add
  a new collector.
- .env.example listing every variable with a comment.

Before writing code, read this whole brief, then show me your plan for Phase 1
and list any questions that would change the design. If something here turns
out to be impossible (for example an API has changed), stop and tell me rather
than working around it silently.
