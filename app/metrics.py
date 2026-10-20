"""Run-level metric aggregation. Pure functions over per-case rows."""

import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class CaseRow:
    case_id: str
    passed: bool
    error: str | None
    scores: dict[str, Any]
    latency_ms: float | None
    input_tokens: int | None
    output_tokens: int | None
    attempts: int = 1


@dataclass(frozen=True)
class Pricing:
    input_usd_per_1m: Decimal
    output_usd_per_1m: Decimal
    effective_from: date


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile; None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _cost(rows: list[CaseRow], ok: list[CaseRow], total_cases: int,
          pricing: Pricing) -> dict[str, Any]:
    """Cost from recorded token usage.

    Usage is only recorded for the attempt that produced a stored answer. Failed cases,
    retried attempts and calls repeated after a lost lease may also have been billed, but
    their usage is unknown. In that case `usd` is None and `known_usd` is a lower bound.
    """
    known = [r for r in ok if r.input_tokens is not None and r.output_tokens is not None]
    known_usd = (
        sum(Decimal(r.input_tokens) for r in known) * pricing.input_usd_per_1m
        + sum(Decimal(r.output_tokens) for r in known) * pricing.output_usd_per_1m
    ) / Decimal(1_000_000)
    # Every provider call beyond the one whose usage was stored is unaccounted for.
    unaccounted_calls = sum(r.attempts - 1 for r in ok) + sum(r.attempts for r in rows if r.error)
    incomplete_reasons = []
    if unaccounted_calls:
        incomplete_reasons.append(f"{unaccounted_calls} provider call(s) without recorded usage")
    if len(known) < len(ok):
        incomplete_reasons.append(f"{len(ok) - len(known)} answer(s) without token usage")
    if len(rows) < total_cases:
        incomplete_reasons.append(f"{total_cases - len(rows)} case(s) still pending")
    complete = not incomplete_reasons
    return {
        "usd": float(round(known_usd, 6)) if complete else None,
        "known_usd": float(round(known_usd, 6)),
        "complete": complete,
        "incomplete_reasons": incomplete_reasons,
        "pricing_effective_from": pricing.effective_from.isoformat(),
        "input_usd_per_1m": float(pricing.input_usd_per_1m),
        "output_usd_per_1m": float(pricing.output_usd_per_1m),
    }


def aggregate(rows: list[CaseRow], total_cases: int, pricing: Pricing | None) -> dict[str, Any]:
    """`total_cases` is the run size; cases without a row yet count as not passed."""
    ok = [r for r in rows if r.error is None]
    errors = [r for r in rows if r.error is not None]
    json_rows = [r for r in ok if "valid_json" in r.scores]
    fact_rows = [r for r in ok if "facts_total" in r.scores]
    latencies = [r.latency_ms for r in ok if r.latency_ms is not None]
    usage_known = all(r.input_tokens is not None and r.output_tokens is not None for r in ok)
    input_tokens = sum(r.input_tokens or 0 for r in ok)
    output_tokens = sum(r.output_tokens or 0 for r in ok)

    cost = _cost(rows, ok, total_cases, pricing) if pricing is not None else None

    return {
        "total_cases": total_cases,
        "evaluated_cases": len(ok),
        "error_count": len(errors),
        "pending_cases": total_cases - len(rows),
        "passed_cases": sum(r.passed for r in rows),
        "pass_rate": _rate(sum(r.passed for r in rows), total_cases),
        "exact_match_rate": _rate(sum(bool(r.scores.get("exact_match")) for r in ok), len(ok)),
        "facts_recall": _rate(
            sum(r.scores["facts_found"] for r in fact_rows),
            sum(r.scores["facts_total"] for r in fact_rows),
        ),
        "json_valid_rate": _rate(sum(r.scores["valid_json"] for r in json_rows), len(json_rows)),
        "schema_valid_rate": _rate(sum(r.scores["schema_valid"] for r in json_rows), len(json_rows)),
        "latency_ms_p50": percentile(latencies, 50),
        "latency_ms_p95": percentile(latencies, 95),
        "input_tokens": input_tokens if usage_known else None,
        "output_tokens": output_tokens if usage_known else None,
        "cost": cost,
    }
