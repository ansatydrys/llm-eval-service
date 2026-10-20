"""API -> PostgreSQL queue -> worker -> results -> report/compare."""

import json

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app import cli
from app.models import CaseResult, RunJob
from tests.conftest import TEST_DATABASE_URL
from tests.integration.helpers import (
    TEMPLATE_V2,
    jsonl,
    prompt,
    simple_cases,
    start_run,
    upload,
)

pytestmark = pytest.mark.integration


def test_full_flow_on_demo_dataset(client, make_worker, sessions, demo_jsonl):
    ds = upload(client, "demo", demo_jsonl)
    assert ds["case_count"] == 40 and ds["version"] == 1 and ds["created"] is True
    pv = prompt(client)
    run = start_run(client, ds["dataset_version_id"], pv["prompt_version_id"])
    assert run["status"] == "queued"
    assert run["progress"]["queued"] == 40
    assert run["synthetic"] is True

    assert make_worker().drain() == 40

    run = client.get(f"/runs/{run['id']}").json()
    assert run["status"] == "completed"
    assert run["progress"] == {"queued": 0, "running": 0, "succeeded": 40, "failed": 0}

    report = client.get(f"/runs/{run['id']}/report").json()
    m = report["metrics"]
    assert m["total_cases"] == 40 and m["evaluated_cases"] == 40 and m["error_count"] == 0
    assert 0 < m["pass_rate"] < 1  # fake accuracy 0.8 by default
    assert m["json_valid_rate"] is not None and m["schema_valid_rate"] is not None
    assert m["facts_recall"] is not None
    assert m["latency_ms_p50"] is not None and m["latency_ms_p95"] >= m["latency_ms_p50"]
    assert m["input_tokens"] > 0 and m["output_tokens"] > 0
    assert m["cost"] is None  # no pricing for the fake provider
    assert report["run"]["provider"] == "fake" and report["run"]["synthetic"] is True
    assert "Synthetic" in report["note"]
    assert len(report["cases"]) == 40

    with sessions() as s:
        assert s.scalar(select(func.count()).select_from(CaseResult)) == 40


def test_results_are_deterministic_for_same_config(client, make_worker, demo_jsonl):
    ds = upload(client, "demo", demo_jsonl)
    pv = prompt(client)
    a = start_run(client, ds["dataset_version_id"], pv["prompt_version_id"])
    b = start_run(client, ds["dataset_version_id"], pv["prompt_version_id"])
    make_worker().drain()
    cmp_ = client.get("/runs/compare", params={"base": a["id"], "head": b["id"]}).json()
    assert cmp_["improved_cases"] == [] and cmp_["regressed_cases"] == []
    assert cmp_["metrics"]["pass_rate"]["delta"] == 0


def test_dataset_versions_are_immutable(client, sessions):
    v1 = upload(client, "ds", jsonl(simple_cases(3)))
    again = upload(client, "ds", jsonl(simple_cases(3)))
    assert again["dataset_version_id"] == v1["dataset_version_id"] and again["created"] is False
    v2 = upload(client, "ds", jsonl(simple_cases(4)))
    assert v2["version"] == 2 and v2["dataset_version_id"] != v1["dataset_version_id"]

    with sessions() as s:
        for stmt in (
            "UPDATE dataset_cases SET question = 'x'",
            "DELETE FROM dataset_versions",
            "UPDATE dataset_versions SET case_count = 0",
        ):
            with pytest.raises(DBAPIError, match="immutable"):
                s.execute(text(stmt))
            s.rollback()


def test_prompt_versions_are_immutable(client, sessions):
    p1 = prompt(client, "p")
    assert prompt(client, "p")["created"] is False
    assert prompt(client, "p", TEMPLATE_V2)["version"] == 2
    with sessions() as s, pytest.raises(DBAPIError, match="immutable"):
        s.execute(text("UPDATE prompt_versions SET template = 'x' WHERE id = :id"),
                  {"id": p1["prompt_version_id"]})


@pytest.mark.parametrize(
    ("lines", "fragment"),
    [
        ([b'{"id": "a", "question": "q"}'], "expected_answer"),
        ([b"not json"], "Invalid JSON"),
        ([b'{"id": "a", "question": "q", "expected_answer": "x", "extra": 1}'], "extra"),
        ([b'{"id": "a", "question": "q", "expected_answer": "x"}'] * 2, "duplicate id"),
        ([b'{"id": "a", "question": "q", "expected_answer": "x", "json_schema": {"type": 5}}'],
         "invalid JSON Schema"),
        ([b'{"id": "a b", "question": "q", "expected_answer": "x"}'], "pattern"),
        ([b""], "empty"),
    ],
)
def test_dataset_validation(client, lines, fragment):
    body = upload(client, "bad", b"\n".join(lines), expect=422)
    assert fragment in json.dumps(body)


def test_dataset_limits(client, settings):
    too_many = jsonl(simple_cases(settings.max_dataset_cases + 1))
    assert "exceeds" in json.dumps(upload(client, "big", too_many, expect=422))
    too_big = b'{"id": "a", "question": "' + b"x" * settings.max_upload_bytes + b'"}'
    upload(client, "huge", too_big, expect=413)


def test_prompt_requires_placeholder(client):
    r = client.post("/prompts/p/versions", json={"template": "no placeholder"})
    assert r.status_code == 422


def test_run_validation_and_limits(client, settings):
    ds = upload(client, "ds", jsonl(simple_cases(5)))
    pv = prompt(client)
    dv, p = ds["dataset_version_id"], pv["prompt_version_id"]

    start_run(client, dv, p, expect=422, provider="openai", config={"model": "gpt-x"})
    start_run(client, dv, p, expect=422, config={"api_key": "sk-should-not-be-accepted"})
    start_run(client, dv, p, expect=422, config={"max_attempts": settings.max_attempts_ceiling + 1})
    start_run(client, dv, p, expect=422,
              config={"max_output_tokens": settings.max_output_tokens_ceiling + 1})
    start_run(client, dv, p, expect=422, config={"timeout_s": settings.max_timeout_s + 1})
    start_run(client, 999, p, expect=404)

    limited = start_run(client, dv, p, max_cases=2)
    assert limited["case_count"] == 2


def test_compare_and_cli_gate(client, make_worker, sessions, demo_jsonl, capsys):
    ds = upload(client, "demo", demo_jsonl)
    p1 = prompt(client)
    p2 = prompt(client, template=TEMPLATE_V2)
    dv = ds["dataset_version_id"]
    base = start_run(client, dv, p1["prompt_version_id"])
    head = start_run(client, dv, p2["prompt_version_id"],
                     config={"fake": {"accuracy": 0.5}})
    make_worker().drain()

    cmp_ = client.get("/runs/compare", params={"base": base["id"], "head": head["id"]}).json()
    assert cmp_["compared_cases"] == 40
    assert cmp_["regressed_cases"], "lower fake accuracy must produce regressions"
    pr = cmp_["metrics"]["pass_rate"]
    assert pr["delta"] == pytest.approx(pr["head"] - pr["base"])

    db = ["--database-url", TEST_DATABASE_URL]
    assert cli.main([*db, "compare", str(base["id"]), str(head["id"])]) == 0
    assert cli.main([*db, "compare", str(base["id"]), str(head["id"]),
                     "--max-regressions", "0"]) == 1
    assert cli.main([*db, "compare", str(base["id"]), str(head["id"]),
                     "--min-pass-rate", "0.99"]) == 1
    assert cli.main([*db, "compare", str(base["id"]), str(base["id"]),
                     "--max-regressions", "0", "--max-pass-rate-drop", "0"]) == 0
    capsys.readouterr()

    # A different dataset version cannot be compared: exit code 2.
    other = upload(client, "other", jsonl(simple_cases(3)))
    other_run = start_run(client, other["dataset_version_id"], p1["prompt_version_id"])
    make_worker().drain()
    assert cli.main([*db, "compare", str(base["id"]), str(other_run["id"])]) == 2
    r = client.get("/runs/compare", params={"base": base["id"], "head": other_run["id"]})
    assert r.status_code == 422

    assert cli.main([*db, "report", str(base["id"]), "--summary"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["run"]["run_id"] == base["id"] and "cases" not in out


def test_compare_rejects_unfinished_run(client):
    ds = upload(client, "ds", jsonl(simple_cases(2)))
    pv = prompt(client)
    a = start_run(client, ds["dataset_version_id"], pv["prompt_version_id"])
    r = client.get("/runs/compare", params={"base": a["id"], "head": a["id"]})
    assert r.status_code == 422 and "not finished" in r.text


def test_pricing_applies_only_to_real_providers(client, sessions, make_worker):
    r = client.post("/pricing", json={"provider": "openai", "model": "fake-model",
                                      "input_usd_per_1m": 1, "output_usd_per_1m": 2,
                                      "effective_from": "2020-01-01"})
    assert r.status_code == 201
    ds = upload(client, "ds", jsonl(simple_cases(2)))
    pv = prompt(client)
    run = start_run(client, ds["dataset_version_id"], pv["prompt_version_id"])
    make_worker().drain()
    assert client.get(f"/runs/{run['id']}/report").json()["metrics"]["cost"] is None
    with sessions() as s:
        assert s.scalar(select(func.count()).select_from(RunJob)) == 2
