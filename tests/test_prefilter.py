from datetime import UTC, datetime, timedelta

import pytest

from leadhunter.config import Preferences, Profile
from leadhunter.pipeline.prefilter import deal_breaker, is_offering_services, prefilter

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
PROFILE = Profile(
    name="Test",
    preferences=Preferences(deal_breaker_keywords=["unpaid", "equity only", "WordPress"]),
)


def check(title="Need a React developer", body="We have a dashboard to build.", age_hours=1.0):
    posted_at = None if age_hours is None else NOW - timedelta(hours=age_hours)
    return prefilter(title, body, posted_at, PROFILE, max_age_hours=48, now=NOW)


def test_a_fresh_hiring_post_passes():
    assert check() is None


def test_posts_older_than_48_hours_are_dropped():
    assert check(age_hours=49) == "older than 48 hours"
    assert check(age_hours=47) is None


def test_a_post_without_a_date_is_not_dropped_for_age():
    assert check(age_hours=None) is None


@pytest.mark.parametrize(
    ("title", "body"),
    [
        ("[For Hire] Senior React/Node developer, 8 years experience", "Portfolio in profile."),
        ("[OFFER] I will build your landing page for $50", "Fast turnaround."),
        ("SEEKING WORK | Remote | Full-stack", "Location: Lisbon. Stack: Django, React."),
        ("Full-stack developer available for hire", "10 years of experience."),
        ("Experienced dev looking for work", "Open to any stack."),
        ("Web developer here", "I can build your MVP in two weeks. Hire me!"),
        ("Need a website?", "I will create a modern responsive site for your business."),
        ("Freelance dev", "I'm a full-stack developer with 5 years of experience, available now."),
        ("Offering web development services", "Affordable rates."),
    ],
)
def test_people_offering_services_are_dropped(title, body):
    assert is_offering_services(title, body)
    assert check(title=title, body=body) == "offering services, not hiring"


@pytest.mark.parametrize(
    ("title", "body"),
    [
        ("[Hiring] Full-stack dev to finish a Next.js app", "Budget $1,500."),
        ("[TASK] Fix a bug in my Python scraper, $30", "I will pay on completion."),
        ("Looking for a contract TypeScript engineer", "2 month API project."),
        ("Acme | Senior Engineer | Remote | Contract", "We are hiring engineers to build our API."),
        ("Need help with my Shopify store", "Looking for someone who can build a custom theme."),
        ("[Hiring] React dev", "I will build the backend myself; you take the frontend."),
    ],
)
def test_hiring_posts_are_not_mistaken_for_offers(title, body):
    assert not is_offering_services(title, body)


def test_offer_wording_deep_in_a_long_hiring_post_is_ignored():
    body = "We need a backend engineer for a 3 month contract. " * 10
    body += "Past freelancers said: I can build this quickly."
    assert not is_offering_services("Backend contract role", body)


def test_deal_breaker_keywords_match_whole_words_case_insensitively():
    assert check(body="This is an UNPAID trial at first.") == "deal-breaker keyword: unpaid"
    assert check(body="Compensation is equity only for now.") == (
        "deal-breaker keyword: equity only"
    )
    assert check(title="Fix my wordpress theme") == "deal-breaker keyword: WordPress"


def test_deal_breaker_does_not_match_inside_other_words():
    assert deal_breaker("All invoices get paid, none go unpaidish", ["unpaid"]) is None
    assert deal_breaker("C++ and C# work", ["C"]) is None
    assert deal_breaker("We use C++ daily", ["C++"]) == "C++"


def test_no_deal_breakers_configured():
    profile = Profile(name="Test")
    assert prefilter("Unpaid internship", "", None, profile, now=NOW) is None
