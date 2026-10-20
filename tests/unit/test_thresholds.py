from app.runs import check_thresholds


def comparison(base=0.8, head=0.7, regressed=2, errors=1, total=10):
    return {
        "head_total_cases": total,
        "metrics": {
            "pass_rate": {"base": base, "head": head,
                          "delta": None if base is None or head is None else head - base},
            "error_count": {"base": 0, "head": errors, "delta": errors},
        },
        "regressed_cases": [f"c{i}" for i in range(regressed)],
    }


def test_no_thresholds_means_no_violations():
    assert check_thresholds(comparison()) == []


def test_min_pass_rate():
    assert check_thresholds(comparison(head=0.7), min_pass_rate=0.7) == []
    assert len(check_thresholds(comparison(head=0.69), min_pass_rate=0.7)) == 1


def test_max_pass_rate_drop():
    assert check_thresholds(comparison(base=0.8, head=0.75), max_pass_rate_drop=0.05 + 1e-9) == []
    (msg,) = check_thresholds(comparison(base=0.8, head=0.7), max_pass_rate_drop=0.05)
    assert "dropped" in msg
    assert check_thresholds(comparison(base=0.5, head=0.9), max_pass_rate_drop=0) == []


def test_max_regressions():
    assert check_thresholds(comparison(regressed=0), max_regressions=0) == []
    assert len(check_thresholds(comparison(regressed=1), max_regressions=0)) == 1


def test_max_error_rate():
    assert check_thresholds(comparison(errors=1, total=10), max_error_rate=0.1) == []
    assert len(check_thresholds(comparison(errors=2, total=10), max_error_rate=0.1)) == 1


def test_multiple_violations_are_all_reported():
    v = check_thresholds(comparison(base=0.9, head=0.5, regressed=4), min_pass_rate=0.8,
                         max_pass_rate_drop=0.1, max_regressions=0)
    assert len(v) == 3


def test_missing_head_pass_rate_counts_as_zero():
    assert len(check_thresholds(comparison(head=None), min_pass_rate=0.1)) == 1
