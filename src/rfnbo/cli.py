"""Command-line entry point.

rfnbo validate <config.json>
rfnbo run --config C --source S --data D --decisions X --quarter 2026-Q4 --out DIR
          [--opening-from PRIOR_RUN_DIR]
rfnbo verify <run-dir>
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from rfnbo.calc import CalculationError, calculate
from rfnbo.config import ConfigError, load_config
from rfnbo.data import ingest, quality
from rfnbo.model.entities import Decisions
from rfnbo.trace import run as sealed


def _validate(args: argparse.Namespace) -> int:
    try:
        cfg = load_config(args.path)
    except ConfigError as exc:
        print(f"{args.path}: {len(exc.issues)} issue(s)", file=sys.stderr)
        for issue in exc.issues:
            print(f"  {issue}", file=sys.stderr)
        return 1
    print(
        f"{args.path}: valid ({cfg.plant} v{cfg.version}, effective {cfg.effective_from}, "
        f"{len(cfg.blocks)} blocks, {len(cfg.flows)} flows)"
    )
    return 0


def _run(args: argparse.Namespace) -> int:
    m = re.fullmatch(r"(\d{4})-Q([1-4])", args.quarter)
    if not m:
        print(f"--quarter must look like 2026-Q4, got {args.quarter!r}", file=sys.stderr)
        return 2
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"{args.config}: invalid config\n{exc}", file=sys.stderr)
        return 1
    if args.opening_from:
        cfg = sealed.with_opening_from(cfg, args.opening_from, args.quarter)

    raw = ingest.load(args.data, ingest.load_source_spec(args.source))
    frame, flags = quality.prepare(cfg, raw, int(m.group(1)), int(m.group(2)))
    for f in flags:
        print(f"  {f}", file=sys.stderr)
    if quality.has_errors(flags):
        print("data quality errors: resolve them before calculating", file=sys.stderr)
        return 1

    decisions = Decisions.model_validate_json(Path(args.decisions).read_text(encoding="utf-8"))
    try:
        result = calculate(cfg, frame, decisions)
    except CalculationError as exc:
        print(f"cannot calculate: {exc}", file=sys.stderr)
        return 1
    out = sealed.seal(result, args.out, flags)
    rec = sealed.load_record(out)
    print(f"{rec['run_id']}: sealed to {out}")
    print(f"  {len(result.checks)} checks, {len(result.failures)} failed")
    for failure in result.failures[:20]:
        print(f"  {failure}")
    return 0 if result.passed else 3


def _verify(args: argparse.Namespace) -> int:
    problems = sealed.verify(args.run_dir)
    if problems:
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1
    print(f"{args.run_dir}: reproduces byte for byte")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rfnbo")
    sub = parser.add_subparsers(dest="command", required=True)

    v = sub.add_parser("validate", help="validate a plant config JSON file")
    v.add_argument("path")
    v.set_defaults(fn=_validate)

    r = sub.add_parser("run", help="calculate a decision set and seal the run")
    r.add_argument("--config", required=True)
    r.add_argument("--source", required=True, help="data source spec (column mapping) JSON")
    r.add_argument("--data", required=True, help="15-minute data file (CSV or Excel)")
    r.add_argument("--decisions", required=True)
    r.add_argument("--quarter", required=True, help="e.g. 2026-Q4")
    r.add_argument("--out", required=True, help="directory for the sealed run")
    r.add_argument("--opening-from", help="prior sealed run whose closing ledger opens this one")
    r.set_defaults(fn=_run)

    c = sub.add_parser("verify", help="re-run a sealed run and compare outputs byte for byte")
    c.add_argument("run_dir")
    c.set_defaults(fn=_verify)

    args = parser.parse_args(argv)
    code: int = args.fn(args)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
