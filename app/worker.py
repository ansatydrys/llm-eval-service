"""Background worker: claims jobs from PostgreSQL, calls the provider, stores results."""

import argparse
import logging
import os
import signal
import socket
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app import queue
from app.config import Settings, get_settings
from app.db import session_factory
from app.evaluators import evaluate_case
from app.models import DatasetCase, PromptVersion, Run
from app.providers import (
    GenerationRequest,
    PermanentProviderError,
    Provider,
    TransientProviderError,
    build_provider,
)
from app.schemas import PROMPT_PLACEHOLDER

log = logging.getLogger("llm_eval.worker")

ProviderFactory = Callable[[Run, Settings], Provider]


def default_provider_factory(run: Run, settings: Settings) -> Provider:
    return build_provider(run.provider, run.model, run.config, settings)


def render_prompt(template: str, question: str) -> str:
    return template.replace(PROMPT_PLACEHOLDER, question)


def backoff_seconds(attempt: int, settings: Settings) -> float:
    return min(settings.retry_backoff_base_s * 2 ** (attempt - 1), settings.retry_backoff_max_s)


class Worker:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        settings: Settings,
        provider_factory: ProviderFactory = default_provider_factory,
        worker_id: str | None = None,
    ) -> None:
        self.sessions = sessions
        self.settings = settings
        self.provider_factory = provider_factory
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        log.info("worker %s started", self.worker_id)
        while not self._stop.is_set():
            try:
                worked = self.run_once()
            except Exception:
                log.exception("worker iteration failed")
                worked = False
            if not worked:
                self._stop.wait(self.settings.poll_interval_s)
        log.info("worker %s stopped", self.worker_id)

    def drain(self, max_iterations: int = 100_000) -> int:
        """Process jobs until none is claimable right now. Used by tests and the CLI."""
        processed = 0
        for _ in range(max_iterations):
            if not self.run_once():
                break
            processed += 1
        return processed

    def run_once(self) -> bool:
        """Reap dead leases, then claim and process one job. True if a job was claimed."""
        with self.sessions() as session:
            queue.reap_exhausted(session)
            job = queue.claim(session, self.worker_id, self.settings.lease_seconds)
            if job is None:
                return False
            self._process(session, job)
            return True

    def _process(self, session: Session, job: queue.ClaimedJob) -> None:
        run = session.get(Run, job.run_id)
        case = session.get(DatasetCase, job.dataset_case_pk)
        prompt = session.get(PromptVersion, run.prompt_version_id)
        session.commit()  # end the read transaction; the provider call can be slow
        cfg: dict[str, Any] = run.config

        try:
            provider = self.provider_factory(run, self.settings)
        except Exception as exc:  # misconfiguration, e.g. provider disabled after queuing
            self._finish_error(session, job, f"provider unavailable: {exc}", "config")
            return

        request = GenerationRequest(
            prompt=render_prompt(prompt.template, case.question),
            max_output_tokens=cfg["max_output_tokens"],
            timeout_s=cfg["timeout_s"],
            temperature=cfg["temperature"],
            case_id=case.case_id,
            attempt=job.attempt,
            reference_answer=case.expected_answer,
        )
        started = time.perf_counter()
        try:
            generation = provider.generate(request)
        except TransientProviderError as exc:
            if job.attempt < job.max_attempts:
                delay = backoff_seconds(job.attempt, self.settings)
                log.info("run %s case %s attempt %s transient error, retry in %.1fs: %s",
                         job.run_id, job.case_id, job.attempt, delay, exc)
                if not queue.schedule_retry(session, job, self.worker_id, str(exc), delay):
                    log.warning("lease lost for job %s before retry scheduling", job.job_id)
                return
            self._finish_error(session, job, f"{exc} (after {job.attempt} attempts)", "transient")
            return
        except PermanentProviderError as exc:
            self._finish_error(session, job, str(exc), "permanent")
            return
        except Exception as exc:
            # Unknown failures are not retried: retrying blindly can burn paid requests.
            log.exception("unexpected provider failure on job %s", job.job_id)
            self._finish_error(session, job, f"unexpected error: {type(exc).__name__}", "unexpected")
            return
        latency_ms = (time.perf_counter() - started) * 1000

        passed, scores = evaluate_case(
            generation.text, case.expected_answer, case.required_facts, case.json_schema
        )
        stored = queue.complete(
            session,
            job,
            self.worker_id,
            output=generation.text,
            error=None,
            error_kind=None,
            passed=passed,
            scores=scores,
            latency_ms=latency_ms,
            input_tokens=generation.input_tokens,
            output_tokens=generation.output_tokens,
        )
        if not stored:
            log.warning("lease lost for job %s; result discarded (another worker owns it)",
                        job.job_id)

    def _finish_error(self, session: Session, job: queue.ClaimedJob, error: str, kind: str) -> None:
        stored = queue.complete(
            session, job, self.worker_id,
            output=None, error=error, error_kind=kind, passed=False, scores={},
            latency_ms=None, input_tokens=None, output_tokens=None,
        )
        if not stored:
            log.warning("lease lost for job %s; error result discarded", job.job_id)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="LLM eval worker")
    parser.add_argument("--drain", action="store_true", help="exit when the queue is empty")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    settings = get_settings()
    worker = Worker(session_factory(), settings)
    if args.drain:
        log.info("drained %d jobs", worker.drain())
        return
    signal.signal(signal.SIGTERM, lambda *_: worker.stop())
    signal.signal(signal.SIGINT, lambda *_: worker.stop())
    worker.run_forever()


if __name__ == "__main__":
    main()
