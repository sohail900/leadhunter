import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from leadhunter.collectors.base import RawLead
from leadhunter.config import get_settings, load_profile
from leadhunter.db.session import get_engine, get_sessionmaker
from leadhunter.llm.client import LLMClient
from leadhunter.llm.providers import LLMError
from leadhunter.llm.selection import active_llm
from leadhunter.pipeline.normalize import normalize
from leadhunter.pipeline.prefilter import prefilter
from leadhunter.pipeline.scorer import build_scorer_prompts, score_text


def read_post(paths: list[str]) -> str:
    if paths:
        return Path(paths[0]).read_text(encoding="utf-8")
    if sys.stdin.isatty():
        print("Paste the job post, then press Ctrl-D:", file=sys.stderr)
    return sys.stdin.read()


async def connected_llm() -> LLMClient:
    try:
        async with get_sessionmaker()() as session:
            return await active_llm(session)
    except (OSError, SQLAlchemyError) as exc:
        raise LLMError(
            "the database is not reachable, so I cannot read which model is connected. "
            "Start it with: docker compose up -d db"
        ) from exc
    finally:
        await get_engine().dispose()


async def main(text: str, show_prompt: bool) -> int:
    if not text.strip():
        print("No job post given.", file=sys.stderr)
        return 1

    settings = get_settings()
    profile = load_profile()
    title, _, body = text.strip().partition("\n")
    lead = normalize(RawLead(source="score_test", external_id="0", title=title, body=body or title))

    reason = prefilter(lead.title, lead.body, None, profile, settings.max_lead_age_hours)
    if reason:
        print(f"Pre-filter would drop this before scoring: {reason}", file=sys.stderr)
        print("Scoring it anyway so you can see the result.\n", file=sys.stderr)

    fields = {
        "source": "pasted",
        "title": lead.title,
        "body": lead.body,
        "budget_min": lead.budget_min,
        "budget_max": lead.budget_max,
    }
    if show_prompt:
        system, user = build_scorer_prompts(profile, **fields)
        print(f"--- system ---\n{system}\n--- user ---\n{user}\n---", file=sys.stderr)

    try:
        llm = await connected_llm()
        print(f"Scoring with {llm.name} ...", file=sys.stderr)
        result = await score_text(llm, profile, **fields)
    except LLMError as exc:
        print(f"LLM error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result.model_dump(), indent=2, ensure_ascii=False))
    verdict = "sent for drafting" if result.score >= settings.score_threshold else "rejected"
    print(
        f"\nScore {result.score} vs threshold {settings.score_threshold}: {verdict}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    post = read_post([arg for arg in args if not arg.startswith("--")])
    sys.exit(asyncio.run(main(post, "--prompt" in args)))
