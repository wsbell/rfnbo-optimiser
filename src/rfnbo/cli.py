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


def _batch(args: argparse.Namespace) -> int:
    from rfnbo.optimise.batching import optimise_batching

    m = re.fullmatch(r"(\d{4})-Q([1-4])", args.quarter)
    if not m:
        print(f"--quarter must look like 2026-Q4, got {args.quarter!r}", file=sys.stderr)
        return 2
    cfg = load_config(args.config)
    raw = ingest.load(args.data, ingest.load_source_spec(args.source))
    frame, flags = quality.prepare(cfg, raw, int(m.group(1)), int(m.group(2)))
    if quality.has_errors(flags):
        for f in flags:
            print(f"  {f}", file=sys.stderr)
        return 1
    result = optimise_batching(cfg, frame, eps=args.batch_penalty_MJ)
    decisions = Decisions(batches=result.batches)
    check = calculate(cfg, frame, decisions)
    Path(args.out).write_text(decisions.model_dump_json(indent=1) + "\n", encoding="utf-8")
    for ub in result.units:
        bs = [b for b in check.batches.values() if b.unit == ub.unit]
        full = sum(b.rfnbo_share == 1 for b in bs)
        part = sum(0 < b.rfnbo_share < 1 for b in bs)
        lim = (
            ""
            if ub.limit_gCO2e_per_MJ is None
            else f", downstream limit {ub.limit_gCO2e_per_MJ:.4f}"
        )
        print(
            f"{ub.unit}: {len(bs)} batches ({full} at 100%, {part} part, "
            f"{len(bs) - full - part} at 0%), RFNBO {sum(b.rfnbo_kg for b in bs):,.1f} kg, "
            f"feedable {ub.score.feedable_MJ:,.0f} MJ, mu {ub.mu}{lim}"
        )
    bad = [c for c in check.failures if c.name.startswith("batch")]
    for c in bad:
        print(f"  {c}", file=sys.stderr)
    print(
        f"wrote {len(decisions.batches)} batches to {args.out} "
        "(stage A only: feed, consignments and write-offs come from stages B/C)"
    )
    return 1 if bad else 0


def _edit(args: argparse.Namespace) -> int:
    try:
        from rfnbo.ui.bfd_editor.page import run
    except ImportError:
        print("the editor needs the ui extra: uv sync --extra ui", file=sys.stderr)
        return 1
    run(Path(args.path), port=args.port, show=not args.no_browser)
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

    b = sub.add_parser("batch", help="stage A: choose batch boundaries")
    b.add_argument("--config", required=True)
    b.add_argument("--source", required=True)
    b.add_argument("--data", required=True)
    b.add_argument("--quarter", required=True)
    b.add_argument("--out", required=True, help="decision set JSON (batches only)")
    b.add_argument(
        "--batch-penalty-MJ",
        type=float,
        default=0.01,
        help="value a batch must add to be split off (fewest-batches tie-break)",
    )
    b.set_defaults(fn=_batch)

    e = sub.add_parser("edit", help="open the block flow diagram editor")
    e.add_argument("path", help="plant config JSON (created on first save if missing)")
    e.add_argument("--port", type=int, default=8080)
    e.add_argument("--no-browser", action="store_true")
    e.set_defaults(fn=_edit)

    args = parser.parse_args(argv)
    code: int = args.fn(args)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
