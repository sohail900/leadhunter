import pytest

from leadhunter.agent.guard import amounts, unapproved_amounts


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("I can do it for $800.", [800]),
        ("That would be 1,500 USD in total", [1500]),
        ("€2k upfront and £300 on delivery", [2000, 300]),
        ("My rate is 40 per hour", [40]),
        ("45/hr works", [45]),
        ("about 1.5k dollars", [1500]),
        ("I have 5 years with React 18 and can start in 2 weeks", []),
        ("around 20 hours a week for 3 days", []),
    ],
)
def test_money_amounts_are_found_and_plain_numbers_are_not(text, expected):
    assert amounts(text) == expected


def test_a_figure_the_owner_never_gave_is_flagged():
    assert unapproved_amounts("I can do it for $800.", []) == ["800"]
    assert unapproved_amounts("$45/hour is my rate", ["tell him 40 an hour"]) == ["45"]


def test_a_figure_the_owner_gave_passes_however_it_was_written():
    assert unapproved_amounts("I can do it for $800.", ["800 is fine"]) == []
    assert unapproved_amounts("That would be 1,500 USD", ["say 1.5k"]) == []
    assert unapproved_amounts("$2,000 as discussed", ["2k"]) == []


def test_a_message_without_money_is_never_flagged():
    assert unapproved_amounts("Happy to help. Do you have designs already?", []) == []
