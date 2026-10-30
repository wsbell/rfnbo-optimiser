"""Command-line entry point.

rfnbo validate <config.json>
rfnbo edit <config.json> [--port 8080] [--no-browser]
rfnbo run --config C --source S --data D --decisions X --quarter 2026-Q4 --out DIR
          [--opening-from PRIOR_RUN_DIR]
rfnbo verify <run-dir>
rfnbo batch --config C --source S --data D --quarter 2026-Q4 --out decisions.json
rfnbo optimise --config C --source S --data D --quarter 2026-Q4
               --consignments requirements.json --out DIR [--pos pos.csv]
rfnbo optimise --project DIR [--label NAME] [--compare]
rfnbo data DIR [--port 8082] [--no-browser]
rfnbo report <run-dir> --out DIR [--baseline <run-dir>] [--csv]
rfnbo results <dir> [--port 8081] [--no-browser]
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable
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
    if "provisional" in rec:
        print(f"  PROVISIONAL: quarter to date until {rec['provisional']['until']}")
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
    result = optimise_batching(cfg, frame, materiality_MJ=args.batch_materiality_MJ)
    decisions = Decisions(
        batches=result.batches, renewable_allocations=result.renewable_allocations
    )
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
    from pydantic import TypeAdapter

    from rfnbo.model.entities import ConsignmentSpec, ProofOfSustainability
    from rfnbo.optimise.pipeline import Objectives, OptimisationResult, optimise
    from rfnbo.project.run import Sealed, seal_optimisation

    result: OptimisationResult
    out: Sealed
    records: list[ProofOfSustainability] = []
    if args.project:
        from rfnbo.project.folder import Project, ProjectError, prepare
        from rfnbo.project.run import run_project

        try:
            project = Project.load(args.project)
            prep = prepare(project)
            for f in prep.flags:
                print(f"  {f}", file=sys.stderr)
            result, out = run_project(project, args.label, prep, compare=args.compare)
        except (ProjectError, ConfigError, ValueError) as exc:
            print(f"{args.project}: {exc}", file=sys.stderr)
            return 1
        records = project.pos
    else:
        missing = [
            name
            for name in ("config", "source", "data", "quarter", "consignments", "out")
            if not getattr(args, name)
        ]
        if missing:
            print(f"give --project, or {', '.join('--' + m for m in missing)}", file=sys.stderr)
            return 2
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
        if args.pos:
            from rfnbo.model.pos import PoSError, load_pos, pos_rules

            try:
                records = load_pos(args.pos)
            except PoSError as exc:
                print(f"{args.pos}: invalid PoS records\n{exc}", file=sys.stderr)
                return 1
            issues = list(pos_rules(cfg, records, args.quarter))
            if issues:
                print(f"{args.pos}: {len(issues)} issue(s)", file=sys.stderr)
                for issue in issues:
                    print(f"  {issue}", file=sys.stderr)
                return 1
        result = optimise(
            cfg,
            frame,
            consignments,
            batch_materiality_MJ=args.batch_materiality_MJ,
            pos=records,
            compare=args.compare,
        )
        out = seal_optimisation(result, args.out, flags)

    frame = result.optimised.run.frame
    if frame.provisional:
        print(
            f"PROVISIONAL: the data ends {frame.end.isoformat()} (UTC), before {frame.quarter} "
            "does; quarter to date, closed as if the quarter ended there"
        )
    vb = result.value_bound()
    bound = result.bound()
    columns = [("result", result.optimised.objectives)]
    if result.baseline is not None:
        columns.append(("baseline", result.baseline.objectives))
    rows: list[tuple[str, Callable[[Objectives], str]]] = [
        ("consignments met", lambda o: str(o.consignments_met)),
        ("carried value (MJ)", lambda o: f"{o.carried_value_MJ:,.1f}"),
        ("final RFNBO (kg)", lambda o: f"{o.final_rfnbo_kg:,.1f}"),
        ("carried GHG (g)", lambda o: f"{o.carried_emissions_g:,.0f}"),
        ("batches", lambda o: f"{o.batches:,}"),
    ]
    if records:
        rows.append(("PoS written off (kg)", lambda o: f"{o.pos_written_off_kg:,.1f}"))
    print(f"{'':>24}" + "".join(f" {name:>18}" for name, _ in columns))
    for label, show in rows:
        print(f"{label:>24}" + "".join(f" {show(o):>18}" for _, o in columns))
    if bound is not None:
        print(f"upper bound for this batching {bound.upper_kg:,.1f} kg (gap {bound.gap:.4%})")
    print(f"carried value bound {vb.upper_MJ:,.1f} MJ (gap {vb.gap:.4%})")
    for s in result.shortfalls:
        print(
            f"  {s.consignment} not met: {s.reason}; achievable {s.achievable_kg:,.1f} kg",
            file=sys.stderr,
        )
    if result.baseline is not None:
        print(f"sealed to {out.out}/optimised and {out.out}/baseline")
    else:
        print(f"sealed to {out.out}/optimised")
    print(f"summary: {out.out}/summary.json")
    print(f"report: {out.report}")
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


def _results(args: argparse.Namespace) -> int:
    try:
        from rfnbo.ui.results.app import run
    except ImportError:
        print("the results browser needs the ui extra: uv sync --extra ui", file=sys.stderr)
        return 1
    run(Path(args.root), port=args.port, show=not args.no_browser)
    return 0


def _data(args: argparse.Namespace) -> int:
    import importlib

    try:
        page = importlib.import_module("rfnbo.ui.data.app")
    except ImportError:
        print(
            "the Data screen needs the ui extra (uv sync --extra ui); its page is still being "
            "built (phase 17): use rfnbo optimise --project meanwhile",
            file=sys.stderr,
        )
        return 1
    page.run(Path(args.project), port=args.port, show=not args.no_browser)
    return 0


def _edit(args: argparse.Namespace) -> int:
    try:
        from rfnbo.ui.bfd_editor.page import run
    except ImportError:
        print("the editor needs the ui extra: uv sync --extra ui", file=sys.stderr)
        return 1
    run(Path(args.path), port=args.port, show=not args.no_browser)
    return 0


def _app(args: argparse.Namespace) -> int:
    try:
        from rfnbo.ui.home.app import run
    except ImportError:
        print("the app needs the ui extra: uv sync --extra ui", file=sys.stderr)
        return 1
    run(Path(args.dir) if args.dir else None, port=args.port, show=not args.no_browser)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rfnbo", description="with no command, opens the app on the projects folder"
    )
    parser.set_defaults(fn=_app, dir=None, port=8090, no_browser=False)
    sub = parser.add_subparsers(dest="command")

    a = sub.add_parser("app", help="the app: projects, then the Data screen and results")
    a.add_argument("--dir", help="projects folder (default $RFNBO_PROJECTS or ~/rfnbo-projects)")
    a.add_argument("--port", type=int, default=8090)
    a.add_argument("--no-browser", action="store_true")
    a.set_defaults(fn=_app)

    v = sub.add_parser("validate", help="validate a plant config JSON file")
    v.add_argument("path")
    v.set_defaults(fn=_validate)

    r = sub.add_parser("run", help="calculate a decision set and seal the run")
    r.add_argument("--config", required=True)
    r.add_argument("--source", required=True, help="data source spec (column mapping) JSON")
    r.add_argument("--data", required=True, help="data file (CSV or Excel), at its own time step")
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
        "--batch-materiality-MJ",
        type=float,
        default=120.0,
        help="final-product MJ a split must gain, else fewer batches win (default 120, 1 kg H2)",
    )
    b.set_defaults(fn=_batch)

    o = sub.add_parser("optimise", help="optimise a quarter and seal it with its baseline")
    o.add_argument(
        "--project",
        help="a project folder (rfnbo data): every input from its files, sealed under runs/",
    )
    o.add_argument("--label", help="with --project: folder name under runs/")
    o.add_argument(
        "--compare",
        action="store_true",
        help="also calculate and seal the baseline (one batch per unit and month, FIFO)",
    )
    o.add_argument("--config")
    o.add_argument("--source")
    o.add_argument("--data")
    o.add_argument("--quarter")
    o.add_argument("--consignments", help="JSON list of consignments (target null = minimise mode)")
    o.add_argument("--out")
    o.add_argument(
        "--pos",
        help="proofs of sustainability for purchased RFNBO (CSV or JSON list); see rfnbo.model.pos",
    )
    o.add_argument(
        "--batch-materiality-MJ",
        type=float,
        default=120.0,
        help="final-product MJ a split must gain, else fewer batches win (default 120, 1 kg H2)",
    )
    o.set_defaults(fn=_optimise)

    rp = sub.add_parser("report", help="Excel/CSV reports for a sealed run")
    rp.add_argument("run_dir")
    rp.add_argument("--out", required=True)
    rp.add_argument("--baseline", help="sealed baseline run to compare against")
    rp.add_argument("--csv", action="store_true", help="also write one CSV per table")
    rp.set_defaults(fn=_report)

    rs = sub.add_parser("results", help="browse sealed runs down to the interval data")
    rs.add_argument("root", help="a sealed run, or a folder containing sealed runs")
    rs.add_argument("--port", type=int, default=8081)
    rs.add_argument("--no-browser", action="store_true")
    rs.set_defaults(fn=_results)

    d = sub.add_parser("data", help="the Data screen: set up, check and run a project folder")
    d.add_argument("project", help="project folder (created on first save if missing)")
    d.add_argument("--port", type=int, default=8082)
    d.add_argument("--no-browser", action="store_true")
    d.set_defaults(fn=_data)

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
