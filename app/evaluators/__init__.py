"""Per-case evaluators. Pure functions, no I/O.

Pass rule for a case (see README):
  - if the case has a JSON Schema, the output must be valid JSON matching it;
  - if the case has required facts, all of them must be present;
  - if the case has neither, the output must exactly match the reference (normalized).
Exact match is always reported, but it does not gate cases that define facts or a
schema, because free-form answers rarely match a reference word for word.
"""

import json
import re
import unicodedata
from typing import Any

from jsonschema import Draft202012Validator

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = _PUNCT.sub(" ", text)
    return _SPACE.sub(" ", text).strip()


def _parse_json(text: str) -> tuple[bool, Any]:
    try:
        return True, json.loads(text)
    except ValueError:
        return False, None


def exact_match(output: str, expected: str) -> bool:
    exp_ok, exp_json = _parse_json(expected)
    if exp_ok and isinstance(exp_json, (dict, list)):
        # Structured reference: compare parsed values. Text normalization would strip
        # brackets and let malformed JSON "match".
        out_ok, out_json = _parse_json(output)
        return out_ok and out_json == exp_json
    return normalize(output) == normalize(expected)


def facts_present(output: str, facts: list[str]) -> list[bool]:
    padded = f" {normalize(output)} "
    return [f" {normalize(fact)} " in padded for fact in facts]


def json_checks(output: str, schema: dict[str, Any]) -> tuple[bool, bool, str | None]:
    """Return (valid_json, matches_schema, first_schema_error)."""
    ok, value = _parse_json(output)
    if not ok:
        return False, False, "output is not valid JSON"
    error = next(iter(Draft202012Validator(schema).iter_errors(value)), None)
    if error is not None:
        return True, False, error.message[:300]
    return True, True, None


def evaluate_case(
    output: str,
    expected: str,
    required_facts: list[str],
    json_schema: dict[str, Any] | None,
) -> tuple[bool, dict[str, Any]]:
    scores: dict[str, Any] = {"exact_match": exact_match(output, expected)}
    passed = True

    if required_facts:
        found = facts_present(output, required_facts)
        scores["facts_found"] = sum(found)
        scores["facts_total"] = len(found)
        scores["facts_missing"] = [f for f, hit in zip(required_facts, found, strict=True) if not hit]
        passed = passed and all(found)

    if json_schema is not None:
        valid_json, schema_ok, schema_error = json_checks(output, json_schema)
        scores["valid_json"] = valid_json
        scores["schema_valid"] = schema_ok
        if schema_error:
            scores["schema_error"] = schema_error
        passed = passed and schema_ok

    if not required_facts and json_schema is None:
        passed = scores["exact_match"]

    return passed, scores
