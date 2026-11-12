import json
from datetime import UTC, datetime
from pathlib import Path

from leadhunter.collectors import build_collectors
from leadhunter.collectors.hackernews import find_hiring_story, parse_comments
from leadhunter.collectors.job_feeds import parse_remoteok, parse_wwr
from leadhunter.collectors.reddit import is_hiring, parse_submission
from leadhunter.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"
HN_KEYWORDS = ["contract", "freelance", "part-time", "consulting"]


def load_json(name: str):
    return json.loads((FIXTURES / name).read_text())


def test_remoteok_skips_the_legal_notice_and_keeps_every_job_without_a_tag_filter():
    leads = parse_remoteok(load_json("remoteok.json"), tags=[])

    assert [lead.external_id for lead in leads] == ["1137716", "1137714", "1137708", "1137719"]
    assert all(lead.source == "remoteok" for lead in leads)


def test_remoteok_keeps_only_jobs_with_a_wanted_tag():
    leads = parse_remoteok(load_json("remoteok.json"), tags=["Python", "dev"])

    assert [lead.external_id for lead in leads] == ["1137716", "1137714"]


def test_remoteok_lead_fields():
    lead = next(
        lead
        for lead in parse_remoteok(load_json("remoteok.json"), tags=[])
        if lead.external_id == "1137714"
    )

    assert lead.title.endswith(": Data Engineer")
    assert lead.url.startswith("https://remoteOK.com/remote-jobs/")
    assert lead.posted_at.tzinfo is not None
    assert lead.posted_at.year == 2026
    assert "Tags: " in lead.body
    assert lead.raw["id"] == "1137714"


def test_remoteok_ignores_implausible_salary_numbers():
    leads = {lead.external_id: lead for lead in parse_remoteok(load_json("remoteok.json"), [])}

    assert "Salary:" not in leads["1137716"].body
    assert "Salary:" not in leads["1137708"].body


def test_remoteok_formats_a_plausible_salary_so_the_budget_parser_can_read_it():
    job = {"id": "1", "position": "Dev", "company": "Acme", "epoch": "1794384008"}
    [lead] = parse_remoteok(
        [{"legal": "..."}, {**job, "salary_min": 60000, "salary_max": 90000}], []
    )

    assert "Salary: $60,000 - $90,000 per year" in lead.body


def test_wwr_parses_items():
    leads = parse_wwr((FIXTURES / "wwr.xml").read_text())

    assert len(leads) == 3
    first = leads[0]
    assert first.source == "weworkremotely"
    assert first.title == "Sezzle: Data Engineer II, Credit Reporting"
    assert first.author == "Sezzle"
    assert first.url.startswith("https://weworkremotely.com/")
    assert first.external_id
    assert first.posted_at == datetime(2026, 11, 11, 17, 1, 13, tzinfo=UTC)
    assert "Region: EMEA Only" in first.body
    assert "Skills: " in first.body
    assert "Sezzle" in first.body


def test_wwr_ids_are_unique():
    leads = parse_wwr((FIXTURES / "wwr.xml").read_text())
    assert len({lead.external_id for lead in leads}) == 3


def test_hn_finds_the_newest_who_is_hiring_story_not_who_wants_to_be_hired():
    story = find_hiring_story(load_json("hn_stories.json")["hits"])

    assert story["objectID"] == "50338728"
    assert story["title"] == "Ask HN: Who is hiring? (November 2026)"


def test_hn_returns_none_when_no_hiring_story_is_listed():
    assert find_hiring_story([{"objectID": "1", "title": "Ask HN: Who wants to be hired?"}]) is None


def test_hn_keeps_top_level_comments_that_mention_contract_work():
    hits = load_json("hn_comments.json")["hits"]

    leads = parse_comments(hits, story_id="50338728", keywords=HN_KEYWORDS)

    assert [lead.external_id for lead in leads] == ["50466168", "50428062"]
    first = leads[0]
    assert first.source == "hackernews"
    assert first.url == "https://news.ycombinator.com/item?id=50466168"
    assert first.title.startswith("Native | Founding Builder-in-Residence")
    assert "<" not in first.title
    assert first.author == "georgewithnativ"
    assert first.posted_at == datetime(2026, 11, 12, 12, 23, 34, tzinfo=UTC)


def test_hn_without_keywords_keeps_every_top_level_comment_but_never_replies():
    hits = load_json("hn_comments.json")["hits"]

    leads = parse_comments(hits, story_id="50338728", keywords=[])

    assert len(leads) == 4
    assert "50464784" not in [lead.external_id for lead in leads]


def test_hn_keyword_matches_whole_words_and_hyphen_variants():
    def hit(text):
        return {"objectID": "9", "parent_id": 1, "comment_text": text, "created_at_i": 1794486214}

    def kept(text):
        return bool(parse_comments([hit(text)], 1, ["part-time", "contract"]))

    assert kept("Acme | Engineer | Part time or full time")
    assert kept("Acme | Engineer | parttime")
    assert kept("Acme | 6 month CONTRACT")
    assert not kept("Acme | We build smart contracts tooling | Full-time")


def test_reddit_keeps_only_posts_tagged_as_hiring():
    posts = load_json("reddit_new.json")["posts"]

    leads = [lead for post in posts if (lead := parse_submission(post))]

    assert [lead.external_id for lead in leads] == ["1abc001", "1abc003", "1abc005"]


def test_reddit_lead_fields():
    post = load_json("reddit_new.json")["posts"][0]

    lead = parse_submission(post)

    assert lead.source == "reddit"
    assert lead.url == (
        "https://www.reddit.com/r/forhire/comments/1abc001/"
        "hiring_fullstack_dev_to_finish_a_nextjs_supabase/"
    )
    assert lead.author == "studioflow_maria"
    assert lead.contact_hint == "maria@studioflow.example"
    assert lead.posted_at == datetime.fromtimestamp(1794480000, UTC)
    assert "Next.js 14" in lead.body


def test_reddit_contact_falls_back_to_the_author_handle_or_nothing():
    posts = load_json("reddit_new.json")["posts"]

    assert parse_submission(posts[2]).contact_hint == "u/quick_task_guy"
    assert parse_submission(posts[4]).contact_hint is None


def test_is_hiring():
    assert is_hiring("[HIRING] React dev", None)
    assert is_hiring("[ Hiring ] React dev", None)
    assert is_hiring("[Task] rename 50 files", None)
    assert is_hiring("Need a dev", "Hiring - Open")
    assert not is_hiring("[For Hire] React dev", "For Hire")
    assert not is_hiring("[OFFER] logos", "Offer")
    assert not is_hiring("Need a dev", None)


def test_reddit_collector_is_left_out_until_credentials_are_set():
    without = build_collectors(Settings(_env_file=None))
    assert [c.name for c in without] == ["remoteok", "weworkremotely", "hackernews"]

    with_creds = build_collectors(
        Settings(_env_file=None, reddit_client_id="id", reddit_client_secret="secret")
    )
    assert "reddit" in [c.name for c in with_creds]


def test_collectors_setting_switches_sources_off():
    collectors = build_collectors(Settings(_env_file=None, collectors="hackernews"))
    assert [c.name for c in collectors] == ["hackernews"]
