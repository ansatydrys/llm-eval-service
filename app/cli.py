"""Command line interface.

Exit codes for `compare`: 0 = gate passed, 1 = quality threshold violated,
2 = comparison impossible (unknown run, different dataset versions, unfinished run...).
"""

import argparse
import json
import sys
from pathlib import Path

from app import runs
from app.db import session_factory

EXIT_OK, EXIT_THRESHOLD, EXIT_INVALID = 0, 1, 2


def _cmd_compare(args: argparse.Namespace) -> int:
    with session_factory(args.database_url)() as session:
        try:
            comparison = runs.compare(session, args.base, args.head)
        except (runs.NotFound, runs.RunError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_INVALID
    violations = runs.check_thresholds(
        comparison,
        min_pass_rate=args.min_pass_rate,
        max_pass_rate_drop=args.max_pass_rate_drop,
        max_regressions=args.max_regressions,
        max_error_rate=args.max_error_rate,
    )
    comparison["gate"] = {"passed": not violations, "violations": violations}
    _emit(comparison, args.output)

    pr = comparison["metrics"]["pass_rate"]
    synthetic = " [synthetic: fake provider]" if comparison["synthetic"] else ""
    print(
        f"run {args.base} -> {args.head}{synthetic}: pass_rate {pr['base']} -> {pr['head']}, "
        f"improved {len(comparison['improved_cases'])}, "
        f"regressed {len(comparison['regressed_cases'])}",
        file=sys.stderr,
    )
    for v in violations:
        print(f"FAIL: {v}", file=sys.stderr)
    return EXIT_THRESHOLD if violations else EXIT_OK


def _cmd_report(args: argparse.Namespace) -> int:
    with session_factory(args.database_url)() as session:
        try:
            data = runs.report(session, args.run_id, include_cases=not args.summary)
        except runs.NotFound as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_INVALID
    _emit(data, args.output)
    return EXIT_OK


def _emit(data: dict, output: Path | None) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    if output:
        output.write_text(text + "\n")
    else:
        print(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm-eval")
    parser.add_argument("--database-url", default=None, help="defaults to DATABASE_URL")
    sub = parser.add_subparsers(dest="command", required=True)

    cmp_ = sub.add_parser("compare", help="compare two runs and enforce quality thresholds")
    cmp_.add_argument("base", type=int)
    cmp_.add_argument("head", type=int)
    cmp_.add_argument("--min-pass-rate", type=float)
    cmp_.add_argument("--max-pass-rate-drop", type=float)
    cmp_.add_argument("--max-regressions", type=int)
    cmp_.add_argument("--max-error-rate", type=float)
    cmp_.add_argument("-o", "--output", type=Path, help="write the JSON report to a file")
    cmp_.set_defaults(func=_cmd_compare)

    rep = sub.add_parser("report", help="print a run report as JSON")
    rep.add_argument("run_id", type=int)
    rep.add_argument("--summary", action="store_true", help="omit per-case results")
    rep.add_argument("-o", "--output", type=Path)
    rep.set_defaults(func=_cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
