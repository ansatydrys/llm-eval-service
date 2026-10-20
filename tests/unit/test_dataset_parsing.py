import json
from pathlib import Path

import pytest

from app.catalog import DatasetError, parse_jsonl
from app.config import Settings
from app.schemas import PromptVersionIn
from app.worker import backoff_seconds, render_prompt

DEMO = Path(__file__).resolve().parents[2] / "data" / "demo.jsonl"


def test_demo_dataset_is_valid_and_sized():
    cases = parse_jsonl(DEMO.read_bytes(), max_cases=1000)
    assert 30 <= len(cases) <= 50
    assert any(c.required_facts for c in cases)
    assert any(c.json_schema for c in cases)
    assert any(not c.required_facts and c.json_schema is None for c in cases)


def test_parse_skips_blank_lines_and_keeps_order():
    raw = (
        b'{"id":"a","question":"q","expected_answer":"x"}\n\n'
        b'{"id":"b","question":"q","expected_answer":"y"}\n'
    )
    assert [c.id for c in parse_jsonl(raw, 10)] == ["a", "b"]


def test_parse_reports_line_numbers():
    raw = b'{"id":"a","question":"q","expected_answer":"x"}\n{"id":"b"}\n'
    with pytest.raises(DatasetError) as exc:
        parse_jsonl(raw, 10)
    assert exc.value.errors[0]["line"] == 2


def test_parse_rejects_non_utf8_and_too_many_cases():
    with pytest.raises(DatasetError, match="invalid"):
        parse_jsonl(b"\xff\xfe", 10)
    line = json.dumps({"id": "x", "question": "q", "expected_answer": "a"})
    many = "\n".join(line.replace('"x"', f'"x{i}"') for i in range(4)).encode()
    with pytest.raises(DatasetError) as exc:
        parse_jsonl(many, 3)
    assert "exceeds 3" in exc.value.errors[-1]["error"]


def test_blank_required_fact_rejected():
    raw = b'{"id":"a","question":"q","expected_answer":"x","required_facts":["  "]}'
    with pytest.raises(DatasetError):
        parse_jsonl(raw, 10)


def test_prompt_rendering():
    assert render_prompt("Q: {{question}}\nA:", "2+2?") == "Q: 2+2?\nA:"
    # Braces in templates (e.g. JSON examples) are left untouched.
    assert render_prompt('Reply like {"a": 1}. {{question}}', "x") == 'Reply like {"a": 1}. x'
    with pytest.raises(ValueError):
        PromptVersionIn(template="no placeholder")


def test_backoff_is_exponential_and_capped():
    s = Settings(retry_backoff_base_s=1, retry_backoff_max_s=5)
    assert [backoff_seconds(a, s) for a in range(1, 6)] == [1, 2, 4, 5, 5]
