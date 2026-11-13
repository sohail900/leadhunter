import re

_NUMBER = r"\d[\d,]*(?:\.\d+)?\s?k?\b"
_MONEY = re.compile(
    rf"[$€£]\s?({_NUMBER})"
    rf"|\b({_NUMBER})\s?(?:usd|eur|gbp|dollars?|euros?|pounds?|bucks)\b"
    rf"|\b({_NUMBER})\s?(?:/|per\s|an\s|a\s)\s?(?:hr|hour|day|week|month)\b",
    re.IGNORECASE,
)
_ANY_NUMBER = re.compile(_NUMBER, re.IGNORECASE)


def _value(number: str) -> float:
    number = number.lower().replace(",", "").strip()
    thousands = number.endswith("k")
    value = float(number.rstrip("k").strip())
    return value * 1000 if thousands else value


def amounts(text: str) -> list[float]:
    return [_value(next(g for g in match.groups() if g)) for match in _MONEY.finditer(text)]


def unapproved_amounts(message: str, approved_texts: list[str]) -> list[str]:
    approved = {_value(n) for text in approved_texts for n in _ANY_NUMBER.findall(text)}
    return [f"{amount:g}" for amount in dict.fromkeys(amounts(message)) if amount not in approved]
