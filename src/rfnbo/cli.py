"""Command-line entry point.

rfnbo validate <config.json>
rfnbo edit <config.json> [--port 8080] [--no-browser]
rfnbo run --config C --source S --data D --decisions X --quarter 2026-Q4 --out DIR
          [--opening-from PRIOR_RUN_DIR]
rfnbo verify <run-dir>
rfnbo batch --config C --source S --data D --quarter 2026-Q4 --out decisions.json
rfnbo optimise --config C --source S --data D --quarter 2026-Q4
               --consignments requirements.json --out DIR
rfnbo report <run-dir> --out DIR [--baseline <run-dir>] [--csv]
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


def _optimise(args: argparse.Namespace) -> int:
    import json

    from pydantic import TypeAdapter

    from rfnbo.model.entities import ConsignmentSpec
    from rfnbo.optimise.pipeline import optimise

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
    consignments = TypeAdapter(list[ConsignmentSpec]).validate_json(
        Path(args.consignments).read_text(encoding="utf-8")
    )
    result = optimise(cfg, frame, consignments, batch_penalty_MJ=args.batch_penalty_MJ)

    out = Path(args.out)
    ids = {}
    for name, outcome in (("optimised", result.optimised), ("baseline", result.baseline)):
        d = sealed.seal(outcome.run, out / name, flags)
        ids[name] = sealed.load_record(d)["run_id"]
    bound = result.bound()
    summary = {
        "run_ids": ids,
        "feasible": result.feasible,
        "shortfalls": [
            {"consignment": s.consignment, "reason": s.reason, "achievable_kg": s.achievable_kg}
            for s in result.shortfalls
        ],
        "objectives": {
            name: {
                "consignments_met": o.objectives.consignments_met,
                "final_rfnbo_kg": o.objectives.final_rfnbo_kg,
                "carried_emissions_g": o.objectives.carried_emissions_g,
                "carried_ghg": o.objectives.carried_ghg,
                "batches": o.objectives.batches,
                "failed_checks": len(o.run.failures),
            }
            for name, o in (("optimised", result.optimised), ("baseline", result.baseline))
        },
        "bound": None
        if bound is None
        else {"upper_kg": bound.upper_kg, "achieved_kg": bound.achieved_kg, "gap": bound.gap},
        "log": result.log,
    }
    (out / "comparison.json").write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")

    print(f"{'':>24} {'optimised':>18} {'baseline':>18}")
    o, b = result.optimised.objectives, result.baseline.objectives
    print(f"{'consignments met':>24} {o.consignments_met!s:>18} {b.consignments_met!s:>18}")
    print(f"{'final RFNBO (kg)':>24} {o.final_rfnbo_kg:>18,.1f} {b.final_rfnbo_kg:>18,.1f}")
    print(
        f"{'carried GHG (g)':>24} {o.carried_emissions_g:>18,.0f} {b.carried_emissions_g:>18,.0f}"
    )
    print(f"{'batches':>24} {o.batches:>18,} {b.batches:>18,}")
    if bound is not None:
        print(f"upper bound for this batching {bound.upper_kg:,.1f} kg (gap {bound.gap:.4%})")
    for s in result.shortfalls:
        print(
            f"  {s.consignment} not met: {s.reason}; achievable {s.achievable_kg:,.1f} kg",
            file=sys.stderr,
        )
    from rfnbo.report.build import report_sealed

    written = report_sealed(out / "optimised", out, baseline_dir=out / "baseline")
    print(f"sealed to {out}/optimised and {out}/baseline; comparison in {out}/comparison.json")
    print(f"report: {written.xlsx}")
    if not result.feasible:
        return 3
    return 0 if result.optimised.run.passed else 1


def _report(args: argparse.Namespace) -> int:
    from rfnbo.report.build import report_sealed

    problems = sealed.verify(args.run_dir)
    if problems:
        print("sealed run does not reproduce; refusing to report it:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1
    written = report_sealed(args.run_dir, args.out, args.baseline, with_csv=args.csv)
    print(f"report: {written.xlsx}")
    if written.csv:
        print(f"csv: {len(written.csv)} files in {written.csv[0].parent}")
    return 0


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

    o = sub.add_parser("optimise", help="optimise a quarter and seal it with its baseline")
    o.add_argument("--config", required=True)
    o.add_argument("--source", required=True)
    o.add_argument("--data", required=True)
    o.add_argument("--quarter", required=True)
    o.add_argument(
        "--consignments",
        required=True,
        help="JSON list of consignments (target null = minimise mode)",
    )
    o.add_argument("--out", required=True)
    o.add_argument("--batch-penalty-MJ", type=float, default=0.01)
    o.set_defaults(fn=_optimise)

    rp = sub.add_parser("report", help="Excel/CSV reports for a sealed run")
    rp.add_argument("run_dir")
    rp.add_argument("--out", required=True)
    rp.add_argument("--baseline", help="sealed baseline run to compare against")
    rp.add_argument("--csv", action="store_true", help="also write one CSV per table")
    rp.set_defaults(fn=_report)

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
