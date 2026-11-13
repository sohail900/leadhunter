# Your notes for the AI

Put `.md` or `.txt` files in this folder with facts about you that the AI may
use when it writes proposals, answers clients and answers you. Files whose
name starts with `_` (like this one) are ignored.

Good things to put here:

- `portfolio.md`: one heading per past project, with what you built, the stack
  and the result.
- `services.md`: what you offer and what you do not take on.
- `process.md`: how you work (calls, scope, demos, tools, payment steps).
- `faq.md`: answers to questions clients often ask.

How it is used: each file is split at its headings. For every post, client
message or question, the few sections whose words match best are added to the
prompt. Proposals that won a job are added automatically as examples.

Rules:

- Write only what is true. The AI is told it may state anything written here.
- Do not put prices here unless you want them quoted. Prices in a client
  conversation always come from you in Telegram.
- Edits are picked up on the next run; no restart is needed.
