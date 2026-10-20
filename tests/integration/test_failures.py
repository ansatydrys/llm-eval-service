"""Retries, timeouts, partial failures, lease recovery, duplicates and concurrency."""

import os
import signal
import subprocess
import sys
import threading
import time
from collections import Counter

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, text

from app import queue
from app.models import CaseResult, Run, RunJob
from app.providers import Generation, TransientProviderError
from app.worker import default_provider_factory
from tests.conftest import ROOT, TEST_DATABASE_URL
from tests.integration.helpers import jsonl, prompt, simple_cases, start_run, upload

pytestmark = pytest.mark.integration


def _setup(client, n=5, **run_kwargs):
    ds = upload(client, "ds", jsonl(simple_cases(n)))
    pv = prompt(client)
    return start_run(client, ds["dataset_version_id"], pv["prompt_version_id"], **run_kwargs)


def _results(sessions, run_id):
    with sessions() as s:
        return {r.case_id: r for r in s.scalars(select(CaseResult).where(CaseResult.run_id == run_id))}


def _status(sessions, run_id):
    with sessions() as s:
        return s.get(Run, run_id).status


def test_transient_errors_are_retried_until_success(client, sessions, make_worker):
    run = _setup(client, config={"max_attempts": 3, "fake": {"accuracy": 1,
                                                            "transient_failures": {"c1": 2}}})
    make_worker().drain()
    results = _results(sessions, run["id"])
    assert results["c1"].attempts == 3 and results["c1"].error is None and results["c1"].passed
    assert results["c0"].attempts == 1
    assert _status(sessions, run["id"]) == "completed"


def test_transient_errors_exhaust_retries_and_run_is_partial(client, sessions, make_worker):
    run = _setup(client, config={"max_attempts": 2, "fake": {"transient_failures": {"c1": 5}}})
    make_worker().drain()
    r = _results(sessions, run["id"])["c1"]
    assert r.attempts == 2 and r.error_kind == "transient" and "after 2 attempts" in r.error
    assert not r.passed
    assert _status(sessions, run["id"]) == "partial"
    metrics = client.get(f"/runs/{run['id']}/report").json()["metrics"]
    assert metrics["error_count"] == 1 and metrics["evaluated_cases"] == 4


def test_permanent_errors_are_not_retried(client, sessions, make_worker):
    run = _setup(client, config={"max_attempts": 3, "fake": {"permanent_failures": ["c2"]}})
    make_worker().drain()
    r = _results(sessions, run["id"])["c2"]
    assert r.attempts == 1 and r.error_kind == "permanent"
    assert _status(sessions, run["id"]) == "partial"


def test_run_fails_when_every_case_fails(client, sessions, make_worker):
    cases = [f"c{i}" for i in range(3)]
    run = _setup(client, n=3, config={"fake": {"permanent_failures": cases}})
    make_worker().drain()
    assert _status(sessions, run["id"]) == "failed"


def test_timeout_is_a_transient_error(client, sessions, make_worker):
    run = _setup(client, n=1, config={"timeout_s": 0.05, "max_attempts": 2,
                                      "fake": {"latency_ms": 500}})
    make_worker().drain()
    r = _results(sessions, run["id"])["c0"]
    assert r.error_kind == "transient" and "timed out" in r.error and r.attempts == 2


def test_retry_respects_backoff(client, sessions, make_worker, settings):
    settings.retry_backoff_base_s = 30
    settings.retry_backoff_max_s = 30
    run = _setup(client, n=1, config={"fake": {"transient_failures": {"c0": 1}}})
    worker = make_worker()
    assert worker.drain() == 1  # first attempt fails; the retry is not available yet
    with sessions() as s:
        job = s.scalars(select(RunJob).where(RunJob.run_id == run["id"])).one()
        assert job.status == "queued" and job.attempts == 1
        s.execute(text("UPDATE run_jobs SET available_at = now()"))
        s.commit()
    assert worker.drain() == 1
    assert _results(sessions, run["id"])["c0"].attempts == 2


def test_expired_lease_is_recovered_by_another_worker(client, sessions, make_worker, settings):
    run = _setup(client, n=3)
    # Worker A claims a job and dies without finishing it.
    with sessions() as s:
        dead = queue.claim(s, "worker-a", settings.lease_seconds)
    assert dead is not None
    # While the lease is live, nobody else can take that job.
    assert make_worker(worker_id="worker-b").drain() == 2
    assert _status(sessions, run["id"]) == "running"

    with sessions() as s:
        s.execute(text("UPDATE run_jobs SET lease_expires_at = now() - interval '1 second' "
                       "WHERE id = :id"), {"id": dead.job_id})
        s.commit()
    assert make_worker(worker_id="worker-b").drain() == 1

    results = _results(sessions, run["id"])
    assert len(results) == 3 and results[dead.case_id].attempts == 2
    assert _status(sessions, run["id"]) == "completed"

    # The dead worker coming back cannot overwrite or duplicate the result.
    with sessions() as s:
        assert queue.complete(s, dead, "worker-a", output="late", error=None, error_kind=None,
                              passed=True, scores={}, latency_ms=1.0, input_tokens=1,
                              output_tokens=1) is False
    assert _results(sessions, run["id"])[dead.case_id].output != "late"


def test_lease_lost_mid_call_gives_at_least_once_but_single_result(
    client, sessions, make_worker, settings
):
    """A slow worker loses its lease; another worker redoes the call. The provider is
    called twice (at-least-once), but only one result is stored."""
    run = _setup(client, n=1)
    calls = Counter()

    class SlowThenStolen:
        name, is_synthetic = "fake", True

        def generate(self, request):
            calls[request.case_id] += 1
            if calls[request.case_id] == 1:
                # Simulate a stall long enough for the lease to expire and be taken over.
                with sessions() as s:
                    s.execute(text("UPDATE run_jobs SET lease_expires_at = now() - "
                                   "interval '1 second'"))
                    s.commit()
                make_worker(provider_factory=lambda r, st: Fast(), worker_id="b").drain()
            return Generation(text="answer 0", input_tokens=1, output_tokens=2)

    class Fast:
        name, is_synthetic = "fake", True

        def generate(self, request):
            calls[request.case_id] += 1
            return Generation(text="answer 0", input_tokens=1, output_tokens=2)

    make_worker(provider_factory=lambda r, st: SlowThenStolen(), worker_id="a").drain()

    assert calls["c0"] == 2
    with sessions() as s:
        assert s.scalar(select(func.count()).select_from(CaseResult)) == 1
        job = s.scalars(select(RunJob)).one()
        assert job.status == "succeeded" and job.attempts == 2
    assert _status(sessions, run["id"]) == "completed"


def test_lease_expired_on_last_attempt_is_reaped(client, sessions, make_worker, settings):
    run = _setup(client, n=1, config={"max_attempts": 1})
    with sessions() as s:
        assert queue.claim(s, "worker-a", settings.lease_seconds) is not None
        s.execute(text("UPDATE run_jobs SET lease_expires_at = now() - interval '1 second'"))
        s.commit()
    assert make_worker().drain() == 0  # nothing claimable, but the reaper runs
    r = _results(sessions, run["id"])["c0"]
    assert r.error_kind == "lease_expired" and r.attempts == 1
    assert _status(sessions, run["id"]) == "failed"


def test_concurrent_workers_process_each_case_exactly_once(client, sessions, make_worker):
    run = _setup(client, n=60, config={"fake": {"latency_ms": 5}})
    calls = Counter()
    lock = threading.Lock()

    def counting(run_, settings_):
        inner = default_provider_factory(run_, settings_)

        class Counting:
            name, is_synthetic = inner.name, inner.is_synthetic

            def generate(self, request):
                with lock:
                    calls[request.case_id] += 1
                return inner.generate(request)

        return Counting()

    workers = [make_worker(provider_factory=counting, worker_id=f"w{i}") for i in range(6)]
    threads = [threading.Thread(target=w.drain) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert len(calls) == 60 and set(calls.values()) == {1}
    with sessions() as s:
        assert s.scalar(select(func.count()).select_from(CaseResult)) == 60
        assert set(s.scalars(select(RunJob.attempts))) == {1}
        statuses = set(s.scalars(select(RunJob.status)))
        assert statuses == {"succeeded"}
    assert _status(sessions, run["id"]) == "completed"


def test_results_unique_per_run_and_case(client, sessions, make_worker):
    run = _setup(client, n=1)
    make_worker().drain()
    with sessions() as s:
        with pytest.raises(Exception, match="unique|duplicate"):
            s.add(CaseResult(run_id=run["id"], case_id="c0", attempts=1, passed=False, scores={}))
            s.commit()


def test_disabled_provider_after_queueing_fails_cases_without_crash(client, sessions,
                                                                   make_worker, settings):
    settings.openai_enabled = True
    settings.openai_api_key = SecretStr("test-key-not-real")
    run = _setup(client, n=2, provider="openai", config={"model": "gpt-test"})
    settings.openai_enabled = False
    make_worker().drain()
    results = _results(sessions, run["id"])
    assert {r.error_kind for r in results.values()} == {"config"}
    assert all("test-key" not in (r.error or "") for r in results.values())
    assert _status(sessions, run["id"]) == "failed"


def test_cost_reported_only_with_dated_pricing(client, sessions, make_worker, settings):
    settings.openai_enabled = True
    settings.openai_api_key = SecretStr("test-key-not-real")

    class StubReal:
        name, is_synthetic = "openai", False

        def generate(self, request):
            return Generation(text="answer", input_tokens=1000, output_tokens=500)

    run = _setup(client, n=2, provider="openai", config={"model": "gpt-test"})
    make_worker(provider_factory=lambda r, s: StubReal()).drain()
    report = client.get(f"/runs/{run['id']}/report").json()
    assert report["run"]["synthetic"] is False and "note" not in report
    assert report["metrics"]["cost"] is None  # no pricing configured

    # Pricing that starts after the run does not apply.
    client.post("/pricing", json={"provider": "openai", "model": "gpt-test",
                                  "input_usd_per_1m": 2, "output_usd_per_1m": 8,
                                  "effective_from": "2999-01-01"})
    assert client.get(f"/runs/{run['id']}/report").json()["metrics"]["cost"] is None

    client.post("/pricing", json={"provider": "openai", "model": "gpt-test",
                                  "input_usd_per_1m": 2, "output_usd_per_1m": 8,
                                  "effective_from": "2020-01-01"})
    cost = client.get(f"/runs/{run['id']}/report").json()["metrics"]["cost"]
    # 2000 input tokens * $2/1M + 1000 output tokens * $8/1M = $0.012
    assert cost["usd"] == pytest.approx(0.012) and cost["pricing_effective_from"] == "2020-01-01"
    assert cost["complete"] is True


def test_cost_of_partial_run_is_not_reported_as_total(client, sessions, make_worker, settings):
    settings.openai_enabled = True
    settings.openai_api_key = SecretStr("test-key-not-real")

    class FlakyReal:
        name, is_synthetic = "openai", False

        def generate(self, request):
            if request.case_id == "c1":
                raise TransientProviderError("HTTP 503")
            if request.case_id == "c2" and request.attempt == 1:
                raise TransientProviderError("HTTP 429")
            return Generation(text="answer", input_tokens=1000, output_tokens=500)

    client.post("/pricing", json={"provider": "openai", "model": "gpt-test",
                                  "input_usd_per_1m": 2, "output_usd_per_1m": 8,
                                  "effective_from": "2020-01-01"})
    run = _setup(client, n=3, provider="openai", config={"model": "gpt-test", "max_attempts": 2})
    make_worker(provider_factory=lambda r, s: FlakyReal()).drain()

    report = client.get(f"/runs/{run['id']}/report").json()
    assert report["run"]["status"] == "partial"
    cost = report["metrics"]["cost"]
    assert cost["usd"] is None and cost["complete"] is False
    # Only c0 and c2's successful attempt have recorded usage: 2 * $0.006.
    assert cost["known_usd"] == pytest.approx(0.012)
    # c1 failed twice, c2 had one failed attempt before succeeding.
    assert cost["incomplete_reasons"] == ["3 provider call(s) without recorded usage"]


def test_compare_rejects_fake_vs_real(client, sessions, make_worker, settings):
    settings.openai_enabled = True
    settings.openai_api_key = SecretStr("test-key-not-real")

    class StubReal:
        name, is_synthetic = "openai", False

        def generate(self, request):
            return Generation(text="answer", input_tokens=1, output_tokens=1)

    fake = _setup(client, n=2)
    ds_id, pv_id = fake["dataset_version_id"], fake["prompt_version_id"]
    real = start_run(client, ds_id, pv_id, provider="openai", config={"model": "gpt-test"})
    make_worker(provider_factory=lambda r, s: (
        StubReal() if r.provider == "openai" else default_provider_factory(r, s))).drain()
    r = client.get("/runs/compare", params={"base": fake["id"], "head": real["id"]})
    assert r.status_code == 422 and "synthetic" in r.text


def test_killed_worker_process_is_recovered(client, sessions, make_worker, settings):
    """SIGKILL a real worker process mid-job; after its lease expires another worker
    finishes the run with no duplicate results."""
    run = _setup(client, n=2, config={"timeout_s": 2.5, "fake": {"latency_ms": 2000}})
    env = {**os.environ, "DATABASE_URL": TEST_DATABASE_URL, "LEASE_SECONDS": "3",
           "MAX_TIMEOUT_S": "2.5", "POLL_INTERVAL_S": "0.05"}
    proc = subprocess.Popen([sys.executable, "-m", "app.worker"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with sessions() as s:
                if s.scalar(select(func.count()).where(RunJob.status == "running")):
                    break
            time.sleep(0.05)
        else:
            pytest.fail("worker process never claimed a job")
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()

    time.sleep(3.2)  # let the dead worker's lease expire
    make_worker().drain()

    results = _results(sessions, run["id"])
    assert len(results) == 2 and all(r.error is None for r in results.values())
    assert max(r.attempts for r in results.values()) == 2
    assert _status(sessions, run["id"]) == "completed"
