from datetime import date
from decimal import Decimal

import pytest

from app.metrics import CaseRow, Pricing, aggregate, percentile


def row(case_id, passed=True, error=None, scores=None, latency=10.0, tin=10, tout=5):
    return CaseRow(case_id, passed, error, scores or {"exact_match": passed}, latency, tin, tout)


@pytest.mark.parametrize(
    ("values", "p", "expected"),
    [
        ([], 50, None),
        ([7.0], 95, 7.0),
        ([1, 2, 3, 4], 50, 2),
        ([1, 2, 3, 4], 95, 4),
        (list(range(1, 101)), 95, 95),
        (list(range(1, 101)), 50, 50),
        ([5, 1, 3], 50, 3),  # unsorted input
    ],
)
def test_percentile_nearest_rank(values, p, expected):
    assert percentile(values, p) == expected


def test_aggregate_counts_errors_and_pending_as_not_passed():
    rows = [row("a"), row("b", passed=False), row("c", passed=False, error="boom", latency=None,
                                                    tin=None, tout=None)]
    m = aggregate(rows, total_cases=4, pricing=None)
    assert m["pass_rate"] == 0.25
    assert m["error_count"] == 1 and m["evaluated_cases"] == 2 and m["pending_cases"] == 1
    assert m["exact_match_rate"] == 0.5
    assert m["input_tokens"] == 20 and m["output_tokens"] == 10  # errors excluded
    assert m["cost"] is None


def test_aggregate_fact_and_json_rates():
    rows = [
        row("a", scores={"exact_match": False, "facts_found": 2, "facts_total": 3}),
        row("b", scores={"exact_match": False, "facts_found": 1, "facts_total": 1}),
        row("c", scores={"exact_match": True, "valid_json": True, "schema_valid": False}),
        row("d", scores={"exact_match": True, "valid_json": True, "schema_valid": True}),
    ]
    m = aggregate(rows, 4, None)
    assert m["facts_recall"] == 0.75
    assert m["json_valid_rate"] == 1.0 and m["schema_valid_rate"] == 0.5


def test_aggregate_rates_are_none_without_data():
    m = aggregate([], 3, None)
    assert m["pass_rate"] == 0 and m["exact_match_rate"] is None
    assert m["facts_recall"] is None and m["json_valid_rate"] is None
    assert m["latency_ms_p50"] is None


PRICING = Pricing(Decimal("2"), Decimal("8"), date(2026, 1, 1))


def test_cost_when_every_call_is_accounted_for():
    m = aggregate([row("a", tin=1_000_000, tout=500_000)], 1, PRICING)
    assert m["cost"]["usd"] == pytest.approx(6.0) and m["cost"]["known_usd"] == pytest.approx(6.0)
    assert m["cost"]["complete"] is True and m["cost"]["incomplete_reasons"] == []
    assert m["cost"]["pricing_effective_from"] == "2026-01-01"


def test_cost_is_null_without_pricing():
    assert aggregate([row("a")], 1, None)["cost"] is None


@pytest.mark.parametrize(
    ("rows", "total", "reason"),
    [
        # A failed case may have been billed before failing.
        ([row("a", tin=10**6, tout=0), row("b", passed=False, error="boom", tin=None, tout=None)],
         2, "provider call"),
        # Earlier attempts of a retried case were not recorded.
        ([CaseRow("a", True, None, {"exact_match": True}, 1.0, 10**6, 0, attempts=3)], 1,
         "provider call"),
        ([row("a", tin=10**6, tout=0), row("b", tin=None, tout=None)], 2, "without token usage"),
        ([row("a", tin=10**6, tout=0)], 2, "pending"),
    ],
)
def test_cost_is_incomplete_when_some_usage_is_unknown(rows, total, reason):
    cost = aggregate(rows, total, PRICING)["cost"]
    assert cost["usd"] is None and cost["complete"] is False
    assert cost["known_usd"] == pytest.approx(2.0)  # lower bound from recorded usage only
    assert any(reason in r for r in cost["incomplete_reasons"])


def test_unaccounted_calls_are_counted():
    rows = [CaseRow("a", True, None, {}, 1.0, 1, 1, attempts=2),
            CaseRow("b", False, "boom", {}, None, None, None, attempts=3)]
    cost = aggregate(rows, 2, PRICING)["cost"]
    assert cost["incomplete_reasons"] == ["4 provider call(s) without recorded usage"]
