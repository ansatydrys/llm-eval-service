"""Run creation, status, reports and run-to-run comparison."""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.catalog import find_pricing
from app.config import Settings
from app.metrics import CaseRow, Pricing, aggregate
from app.models import (
    CaseResult,
    Dataset,
    DatasetCase,
    DatasetVersion,
    Prompt,
    PromptVersion,
    Run,
    RunJob,
)
from app.providers import SYNTHETIC_PROVIDERS, provider_available
from app.schemas import RunCreate, RunOut


class RunError(ValueError):
    """Invalid run request (bad references, limits, disabled provider)."""


class NotFound(LookupError):
    pass


def create_run(session: Session, req: RunCreate, settings: Settings) -> Run:
    cfg = req.config
    try:
        provider_available(req.provider, settings)
    except Exception as exc:
        raise RunError(str(exc)) from None
    if req.provider != "fake" and cfg.fake is not None:
        raise RunError("config.fake is only allowed with the fake provider")
    if cfg.max_attempts > settings.max_attempts_ceiling:
        raise RunError(f"max_attempts exceeds limit {settings.max_attempts_ceiling}")
    if cfg.max_output_tokens > settings.max_output_tokens_ceiling:
        raise RunError(f"max_output_tokens exceeds limit {settings.max_output_tokens_ceiling}")
    if cfg.timeout_s > settings.max_timeout_s:
        raise RunError(f"timeout_s exceeds limit {settings.max_timeout_s}")

    if session.get(DatasetVersion, req.dataset_version_id) is None:
        raise NotFound(f"dataset version {req.dataset_version_id} not found")
    if session.get(PromptVersion, req.prompt_version_id) is None:
        raise NotFound(f"prompt version {req.prompt_version_id} not found")

    limit = min(req.max_cases or settings.max_cases_per_run, settings.max_cases_per_run)
    cases = session.execute(
        select(DatasetCase.id, DatasetCase.case_id)
        .where(DatasetCase.dataset_version_id == req.dataset_version_id)
        .order_by(DatasetCase.position)
        .limit(limit)
    ).all()
    if not cases:
        raise RunError("dataset version has no cases")

    config = cfg.model_dump(mode="json")
    if req.provider == "fake":
        config["fake"] = cfg.fake.model_dump(mode="json") if cfg.fake else {}
    run = Run(
        dataset_version_id=req.dataset_version_id,
        prompt_version_id=req.prompt_version_id,
        provider=req.provider,
        model=cfg.model,
        config=config,
        status="queued",
        case_count=len(cases),
    )
    session.add(run)
    session.flush()
    session.add_all(
        RunJob(
            run_id=run.id,
            case_id=case_id,
            dataset_case_pk=pk,
            status="queued",
            attempts=0,
            max_attempts=cfg.max_attempts,
        )
        for pk, case_id in cases
    )
    session.commit()
    return run


def _get_run(session: Session, run_id: int) -> Run:
    run = session.get(Run, run_id)
    if run is None:
        raise NotFound(f"run {run_id} not found")
    return run


def run_out(session: Session, run_id: int) -> RunOut:
    run = _get_run(session, run_id)
    counts = dict(
        session.execute(
            select(RunJob.status, func.count()).where(RunJob.run_id == run_id).group_by(RunJob.status)
        ).all()
    )
    return RunOut(
        id=run.id,
        status=run.status,
        provider=run.provider,
        model=run.model,
        synthetic=run.provider in SYNTHETIC_PROVIDERS,
        config=run.config,
        dataset_version_id=run.dataset_version_id,
        prompt_version_id=run.prompt_version_id,
        case_count=run.case_count,
        progress={s: counts.get(s, 0) for s in ("queued", "running", "succeeded", "failed")},
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _rows(session: Session, run_id: int) -> list[CaseResult]:
    return list(
        session.scalars(select(CaseResult).where(CaseResult.run_id == run_id).order_by(CaseResult.id))
    )


def _metrics(session: Session, run: Run, results: list[CaseResult]) -> dict[str, Any]:
    pricing = None
    if run.provider not in SYNTHETIC_PROVIDERS:
        row = find_pricing(session, run.provider, run.model, run.created_at.date())
        if row is not None:
            pricing = Pricing(row.input_usd_per_1m, row.output_usd_per_1m, row.effective_from)
    rows = [
        CaseRow(r.case_id, r.passed, r.error, r.scores, r.latency_ms, r.input_tokens,
                r.output_tokens, r.attempts)
        for r in results
    ]
    return aggregate(rows, run.case_count, pricing)


def _describe(session: Session, run: Run) -> dict[str, Any]:
    dv, ds = session.execute(
        select(DatasetVersion, Dataset)
        .join(Dataset, Dataset.id == DatasetVersion.dataset_id)
        .where(DatasetVersion.id == run.dataset_version_id)
    ).one()
    pv, pr = session.execute(
        select(PromptVersion, Prompt)
        .join(Prompt, Prompt.id == PromptVersion.prompt_id)
        .where(PromptVersion.id == run.prompt_version_id)
    ).one()
    return {
        "run_id": run.id,
        "status": run.status,
        "provider": run.provider,
        "model": run.model,
        "synthetic": run.provider in SYNTHETIC_PROVIDERS,
        "config": run.config,
        "dataset": {"name": ds.name, "version": dv.version, "dataset_version_id": dv.id},
        "prompt": {"name": pr.name, "version": pv.version, "prompt_version_id": pv.id},
        "created_at": run.created_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


def report(session: Session, run_id: int, include_cases: bool = True) -> dict[str, Any]:
    run = _get_run(session, run_id)
    results = _rows(session, run_id)
    out = {"run": _describe(session, run), "metrics": _metrics(session, run, results)}
    if run.provider in SYNTHETIC_PROVIDERS:
        out["note"] = "Synthetic run (fake provider): metrics do not describe any real model."
    if include_cases:
        out["cases"] = [
            {
                "case_id": r.case_id,
                "passed": r.passed,
                "output": r.output,
                "error": r.error,
                "error_kind": r.error_kind,
                "attempts": r.attempts,
                "scores": r.scores,
                "latency_ms": r.latency_ms,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
            }
            for r in results
        ]
    return out


METRIC_KEYS = (
    "pass_rate",
    "exact_match_rate",
    "facts_recall",
    "json_valid_rate",
    "schema_valid_rate",
    "error_count",
    "latency_ms_p50",
    "latency_ms_p95",
    "input_tokens",
    "output_tokens",
)

FINISHED = ("completed", "partial", "failed")


def compare(session: Session, base_id: int, head_id: int) -> dict[str, Any]:
    base = _get_run(session, base_id)
    head = _get_run(session, head_id)
    if base.dataset_version_id != head.dataset_version_id:
        raise RunError("runs use different dataset versions and cannot be compared")
    if (base.provider in SYNTHETIC_PROVIDERS) != (head.provider in SYNTHETIC_PROVIDERS):
        raise RunError("cannot compare a synthetic (fake) run with a real-provider run")
    for run in (base, head):
        if run.status not in FINISHED:
            raise RunError(f"run {run.id} is not finished (status {run.status})")

    base_results = {r.case_id: r for r in _rows(session, base_id)}
    head_results = {r.case_id: r for r in _rows(session, head_id)}
    base_m = _metrics(session, base, list(base_results.values()))
    head_m = _metrics(session, head, list(head_results.values()))

    deltas: dict[str, Any] = {}
    for key in METRIC_KEYS:
        b, h = base_m.get(key), head_m.get(key)
        deltas[key] = {"base": b, "head": h, "delta": (h - b) if b is not None and h is not None else None}

    improved, regressed, new_errors = [], [], []
    for case_id in sorted(base_results.keys() & head_results.keys()):
        b, h = base_results[case_id], head_results[case_id]
        if not b.passed and h.passed:
            improved.append(case_id)
        elif b.passed and not h.passed:
            regressed.append(case_id)
        if b.error is None and h.error is not None:
            new_errors.append(case_id)

    return {
        "base": _describe(session, base),
        "head": _describe(session, head),
        "synthetic": base.provider in SYNTHETIC_PROVIDERS,
        "head_total_cases": head.case_count,
        "compared_cases": len(base_results.keys() & head_results.keys()),
        "only_in_base": sorted(base_results.keys() - head_results.keys()),
        "only_in_head": sorted(head_results.keys() - base_results.keys()),
        "metrics": deltas,
        "improved_cases": improved,
        "regressed_cases": regressed,
        "new_error_cases": new_errors,
    }


def check_thresholds(
    comparison: dict[str, Any],
    min_pass_rate: float | None = None,
    max_pass_rate_drop: float | None = None,
    max_regressions: int | None = None,
    max_error_rate: float | None = None,
) -> list[str]:
    """Return human-readable violations; empty list means the head run passes the gate."""
    violations: list[str] = []
    pr = comparison["metrics"]["pass_rate"]
    head_pass = pr["head"] or 0.0
    if min_pass_rate is not None and head_pass < min_pass_rate:
        violations.append(f"pass_rate {head_pass:.4f} < min {min_pass_rate:.4f}")
    if max_pass_rate_drop is not None and pr["delta"] is not None and -pr["delta"] > max_pass_rate_drop:
        violations.append(f"pass_rate dropped by {-pr['delta']:.4f} > allowed {max_pass_rate_drop:.4f}")
    if max_regressions is not None and len(comparison["regressed_cases"]) > max_regressions:
        violations.append(
            f"{len(comparison['regressed_cases'])} regressed cases > allowed {max_regressions}"
        )
    if max_error_rate is not None:
        total = comparison["head_total_cases"]
        errors = comparison["metrics"]["error_count"]["head"] or 0
        rate = errors / total if total else 0.0
        if rate > max_error_rate:
            violations.append(f"error rate {rate:.4f} > allowed {max_error_rate:.4f}")
    return violations
