import os

from leadhunter.config import Profile
from leadhunter.db.models import Lead
from leadhunter.knowledge import KnowledgeBase, load_knowledge, render, split_markdown, tokenize
from leadhunter.pipeline.drafter import build_drafter_prompts

NOTES = """Intro line before any heading.

# Shop dashboard

Built a Next.js and PostgreSQL admin dashboard for an online store.
Reporting time dropped by half.

# Clinic booking app

React Native app with Stripe payments for a dental clinic.

# How I work

A short call first, then a written scope, then a demo every week.
"""


def base() -> KnowledgeBase:
    return KnowledgeBase(split_markdown("portfolio", NOTES))


def test_markdown_is_split_into_one_chunk_per_heading():
    chunks = split_markdown("portfolio", NOTES)

    assert [c.title for c in chunks] == [
        "portfolio",
        "Shop dashboard",
        "Clinic booking app",
        "How I work",
    ]
    assert chunks[1].text.startswith("Built a Next.js")
    assert all(c.source == "portfolio" for c in chunks)


def test_a_long_section_is_split_between_paragraphs():
    text = "# Long\n\n" + "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(6))

    chunks = split_markdown("notes", text)

    assert len(chunks) > 1
    assert all(c.title == "Long" for c in chunks)
    assert all(len(c.text) <= 1000 for c in chunks)


def test_tech_names_match_however_they_are_written():
    assert {"next.js", "nextjs", "next", "js"} <= set(tokenize("Next.js"))
    assert "c#" in tokenize("C# developer")
    assert "the" not in tokenize("the dashboard")


def test_search_returns_the_relevant_chunk_first():
    kb = base()

    assert kb.search("We need a nextjs dashboard for our store")[0].title == "Shop dashboard"
    assert kb.search("mobile app taking Stripe payments")[0].title == "Clinic booking app"


def test_search_returns_nothing_when_nothing_matches():
    assert base().search("weather forecast tomorrow") == []
    assert base().search("") == []
    assert KnowledgeBase([]).search("dashboard") == []


def test_weak_matches_are_left_out():
    kb = base()

    titles = [c.title for c in kb.search("Next.js PostgreSQL dashboard for a store with a demo")]

    assert titles[0] == "Shop dashboard"
    assert "Clinic booking app" not in titles


def test_folder_is_loaded_and_reloaded_when_a_file_changes(tmp_path):
    (tmp_path / "portfolio.md").write_text(NOTES)
    (tmp_path / "_draft.md").write_text("# Hidden\n\nNot ready, dashboard dashboard dashboard.")
    (tmp_path / "image.png").write_bytes(b"x")

    first = load_knowledge(tmp_path)
    assert len(first) == 4
    assert load_knowledge(tmp_path) is first
    assert all(c.title != "Hidden" for c in first.search("dashboard"))

    notes = tmp_path / "faq.txt"
    notes.write_text("Invoices are sent through Wise.")
    os.utime(notes, ns=(1, 1))
    second = load_knowledge(tmp_path)
    assert second is not first
    assert second.search("how do invoices work")[0].source == "faq"


def test_a_missing_folder_is_an_empty_knowledge_base(tmp_path):
    assert len(load_knowledge(tmp_path / "nothing-here")) == 0


def test_retrieved_notes_reach_the_drafter_prompt_and_are_absent_otherwise():
    profile = Profile(name="Test Dev", skills=["React"])
    lead = Lead(source="reddit", title="Dashboard for our store", body="Next.js please")
    reference = render(base().search("Next.js dashboard for a store"))

    with_notes, _ = build_drafter_prompts(profile, lead, reference=reference)
    without_notes, _ = build_drafter_prompts(profile, lead)

    assert "## Reference material" in with_notes
    assert "[portfolio: Shop dashboard]" in with_notes
    assert "Reporting time dropped by half." in with_notes
    assert "## Reference material" not in without_notes
