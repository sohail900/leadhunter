from pathlib import Path

from leadhunter.config import Settings, load_profile

ROOT = Path(__file__).resolve().parent.parent


def test_missing_for_bot_names_the_unset_variables():
    settings = Settings(_env_file=None, telegram_bot_token="", telegram_chat_id=0)
    assert settings.missing_for_bot() == ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]

    settings = Settings(_env_file=None, telegram_bot_token="123:abc", telegram_chat_id=42)
    assert settings.missing_for_bot() == []


def test_shipped_profile_template_loads():
    profile = load_profile(ROOT / "profile.yaml")
    assert profile.name
    assert profile.skills
    assert "unpaid" in profile.preferences.deal_breaker_keywords


def test_env_example_documents_every_setting_and_nothing_about_the_llm():
    lines = (ROOT / ".env.example").read_text().splitlines()
    documented = {line.split("=", 1)[0] for line in lines if "=" in line and line[0] != "#"}

    settings = {name.upper() for name in Settings.model_fields}
    assert settings - documented == set()
    assert [name for name in documented if "LLM" in name or name.endswith("_API_KEY")] == []


def test_comma_separated_lists_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("REDDIT_SUBREDDITS", "forhire, jobbit ,")
    monkeypatch.setenv("DIGEST_TIMES", "08:30")

    settings = Settings(_env_file=None)

    assert settings.reddit_subreddits == ["forhire", "jobbit"]
    assert settings.digest_times == ["08:30"]


def test_the_env_example_loads_as_it_is_shipped():
    settings = Settings(_env_file=ROOT / ".env.example")

    assert settings.telegram_api_id == 0
    assert settings.missing_for_bot() == ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]


def test_blank_numbers_fall_back_to_their_defaults(monkeypatch):
    for name in (
        "TELEGRAM_CHAT_ID",
        "TELEGRAM_API_ID",
        "IMAP_PORT",
        "SMTP_PORT",
        "SCORE_THRESHOLD",
    ):
        monkeypatch.setenv(name, "")

    settings = Settings(_env_file=None)

    assert (settings.telegram_chat_id, settings.telegram_api_id) == (0, 0)
    assert (settings.imap_port, settings.smtp_port, settings.score_threshold) == (993, 587, 6)
