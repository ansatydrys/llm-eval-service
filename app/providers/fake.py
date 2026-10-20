"""Deterministic fake provider for tests and local demos.

The output depends only on (seed, prompt text, case_id, attempt), so the same run
configuration always yields the same results. Its numbers say nothing about any real
model and are flagged as synthetic everywhere they are reported.
"""

import hashlib
import json
import time

from pydantic import BaseModel, ConfigDict, Field

from app.providers.base import (
    Generation,
    GenerationRequest,
    PermanentProviderError,
    TransientProviderError,
)


class FakeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Probability (by hash) that a case gets the reference answer.
    accuracy: float = Field(0.8, ge=0, le=1)
    seed: int = 0
    # Simulated latency per call; calls longer than the request timeout raise a timeout.
    latency_ms: float = Field(0, ge=0, le=60_000)
    # case_id -> number of leading attempts that fail with a transient error.
    transient_failures: dict[str, int] = Field(default_factory=dict)
    # case_ids that always fail with a permanent error.
    permanent_failures: list[str] = Field(default_factory=list)


def _unit(*parts: object) -> float:
    digest = hashlib.sha256("\x1f".join(map(str, parts)).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def _degrade(reference: str, u: float) -> str:
    """Produce a plausible wrong answer from the reference."""
    try:
        parsed = json.loads(reference)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        if u < 0.5:
            return reference.rstrip("}")  # malformed JSON
        return json.dumps({k: None for k in parsed})  # schema-violating values
    words = reference.split()
    if len(words) <= 2:
        return "I am not sure."
    # Drop the second half of the answer, which usually removes some required facts.
    return " ".join(words[: max(1, len(words) // 2)]) + " ..."


class FakeProvider:
    name = "fake"
    is_synthetic = True

    def __init__(self, config: FakeConfig) -> None:
        self.config = config

    def generate(self, request: GenerationRequest) -> Generation:
        cfg = self.config
        if request.case_id in cfg.permanent_failures:
            raise PermanentProviderError(f"fake permanent failure for case {request.case_id}")
        if request.attempt <= cfg.transient_failures.get(request.case_id, 0):
            raise TransientProviderError(f"fake transient failure on attempt {request.attempt}")
        if cfg.latency_ms:
            if cfg.latency_ms / 1000 > request.timeout_s:
                time.sleep(request.timeout_s)
                raise TransientProviderError(f"timed out after {request.timeout_s}s")
            time.sleep(cfg.latency_ms / 1000)

        u = _unit(cfg.seed, request.prompt, request.case_id)
        if u < cfg.accuracy:
            text = request.reference_answer
        else:
            text = _degrade(request.reference_answer, _unit("degrade", u))

        words = text.split()
        if len(words) > request.max_output_tokens:  # one word ~ one token for the fake
            text = " ".join(words[: request.max_output_tokens])
        return Generation(
            text=text,
            input_tokens=len(request.prompt.split()),
            output_tokens=min(len(words), request.max_output_tokens),
        )
