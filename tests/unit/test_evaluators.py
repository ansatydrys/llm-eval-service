import pytest

from app.evaluators import evaluate_case, exact_match, facts_present, json_checks, normalize

PERSON = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
    "required": ["name", "age"],
    "additionalProperties": False,
}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Hello,   World! ", "hello world"),
        ("ÉCOLE", "école"),
        ("ﬁle", "file"),  # NFKC ligature
        ("a\tb\nc", "a b c"),
        ("", ""),
    ],
)
def test_normalize(raw, expected):
    assert normalize(raw) == expected


def test_exact_match_ignores_case_punctuation_and_spacing():
    assert exact_match("Port Amsel.", "port   amsel")
    assert not exact_match("Port Amsel East", "Port Amsel")


def test_exact_match_compares_json_structurally():
    assert exact_match('{"age": 34, "name": "Mira"}', '{"name": "Mira", "age": 34}')
    assert not exact_match('{"age": 35, "name": "Mira"}', '{"name": "Mira", "age": 34}')
    # Truncated JSON must not match after punctuation is normalized away.
    assert not exact_match('{"name": "Mira", "age": 34', '{"name": "Mira", "age": 34}')


def test_facts_present_matches_whole_words():
    assert facts_present("It weighs 210 kg and stores 40 kWh.", ["210 kg", "40 kwh", "25 minutes"]) == [
        True,
        True,
        False,
    ]
    # "40 kWh" must not match inside "140 kWh".
    assert facts_present("140 kWh", ["40 kWh"]) == [False]


def test_json_checks():
    assert json_checks('{"name": "A", "age": 3}', PERSON) == (True, True, None)
    valid, ok, err = json_checks('{"name": "A"}', PERSON)
    assert valid and not ok and "age" in err
    assert json_checks('{"name": "A"', PERSON)[:2] == (False, False)


def test_case_without_facts_or_schema_requires_exact_match():
    assert evaluate_case("84", "84", [], None)[0]
    passed, scores = evaluate_case("eighty four", "84", [], None)
    assert not passed and scores == {"exact_match": False}


def test_case_with_facts_passes_on_facts_even_without_exact_match():
    passed, scores = evaluate_case("Founded in 1742 by Ada Quell.", "x", ["Ada Quell", "1742"], None)
    assert passed and not scores["exact_match"]
    assert scores["facts_found"] == 2 and scores["facts_missing"] == []

    passed, scores = evaluate_case("Founded by Ada Quell.", "x", ["Ada Quell", "1742"], None)
    assert not passed and scores["facts_missing"] == ["1742"]


def test_case_with_schema_requires_schema_validity():
    passed, scores = evaluate_case('{"name": "A", "age": 1}', '{"name": "B", "age": 2}', [], PERSON)
    assert passed and scores["valid_json"] and scores["schema_valid"] and not scores["exact_match"]
    passed, scores = evaluate_case('{"name": "A", "age": null}', "{}", [], PERSON)
    assert not passed and scores["valid_json"] and not scores["schema_valid"]
    passed, scores = evaluate_case("not json", "{}", [], PERSON)
    assert not passed and not scores["valid_json"]


def test_case_with_facts_and_schema_needs_both():
    out = '{"name": "Ada", "age": 1}'
    assert evaluate_case(out, "", ["Ada"], PERSON)[0]
    assert not evaluate_case(out, "", ["Bob"], PERSON)[0]
