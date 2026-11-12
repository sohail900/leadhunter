from functools import cache
from pathlib import Path

import yaml

from leadhunter.config import Profile

_DIR = Path(__file__).parent


@cache
def load_prompt(name: str) -> str:
    return (_DIR / f"{name}.md").read_text(encoding="utf-8")


def render_profile(profile: Profile) -> str:
    data = profile.model_dump(exclude_none=True)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True).strip()
