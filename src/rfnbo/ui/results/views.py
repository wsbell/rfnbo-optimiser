"""View models for the results browser, independent of any UI toolkit.

Every page is built from a sealed run re-calculated by the calculator, so what a user sees is
what an auditor would recompute. The drill-down goes run → consignment → aggregated batch →
batch (and its upstream feed batches) → the 15-minute intervals behind the batch, whose totals
are checked against the batch record.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from rfnbo.calc.batch import BatchResult
from rfnbo.calc.calculator import RunResult
from rfnbo.calc.tolerance import close
from rfnbo.config.schema import ELECTRICITY, InputBlock, ProcessingUnit, RfnboSupply, StorageUnit
from rfnbo.report import tables as T
from rfnbo.trace import json_log, lineage
from rfnbo.trace import run as sealed


class NotFound(LookupError):
    pass


# ---- runs -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunInfo:
    run_id: str
    path: Path
    plant: str
    quarter: str
    config_version: str
    checks: int
    failed: int
    label: str  # folder name, e.g. "optimised" or "baseline"


def find_runs(root: str | Path, depth: int = 3) -> list[RunInfo]:
    """Sealed runs under ``root`` (or ``root`` itself), newest folder name last."""
    base = Path(root)
    candidates = [base] if (base / sealed.RUN_FILE).exists() else []
    for d in range(1, depth + 1):
        candidates += sorted(
            p.parent for p in base.glob("/".join(["*"] * d) + "/" + sealed.RUN_FILE)
        )
    out: list[RunInfo] = []
    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        try:
            rec = json.loads((path / sealed.RUN_FILE).read_text(encoding="utf-8"))
            summary = rec["results"]["summary"]
            out.append(
                RunInfo(
                    rec["run_id"],
                    path,
                    rec["plant"],
                    rec["quarter"],
                    rec["config_version"],
                    summary["checks"],
                    summary["failed_checks"],
                    path.name,
                )
            )
        except (OSError, KeyError, ValueError):
            continue  # not a sealed run this version can read
    return out


@dataclass
class LoadedRun:
    info: RunInfo
    run: RunResult
    problems: list[str] = field(default_factory=list)  # from verify(); empty = reproduces


def load(info: RunInfo, check: bool = True) -> LoadedRun:
    """Re-calculate a sealed run from its files (and check it reproduces byte for byte)."""
    problems = sealed.verify(info.path) if check else []
    return LoadedRun(info, sealed.recalculate(info.path), problems)


# ---- run overview ---------------------------------------------------------------------------


def overview(run: RunResult) -> dict[str, Any]:
    from rfnbo.optimise.pipeline import objectives

    obj = objectives(run)
    units = []
    for unit in run.config.blocks_of(ProcessingUnit):
        bs = [b for b in run.batches.values() if b.unit == unit.id]
        units.append(
            {
                "unit": unit.id,
                "name": unit.name,
                "batches": len(bs),
                "product_kg": sum(b.product_kg for b in bs),
                "rfnbo_kg": sum(b.rfnbo_kg for b in bs),
                "compliant_rfnbo_kg": sum(b.rfnbo_kg for b in bs if b.compliant),
                "non_compliant_batches": sum(b.is_rfnbo and not b.compliant for b in bs),
            }
        )
    return {
        "objectives": obj,
        "units": units,
        "consignments": T.consignment_summary(run).rows,
        "products": T.product_summary(run).rows,
        "pos": T.pos_register(run).rows,
        "ledgers": T.ledger_summary(run).rows,
        "failures": [
            {"check": c.name, "subject": c.subject, "detail": c.detail, "reference": c.reference}
            for c in run.failures
        ],
    }


# ---- consignment ----------------------------------------------------------------------------


def consignment_view(run: RunResult, consignment_id: str) -> dict[str, Any]:
    try:
        c = run.consignment(consignment_id)
    except StopIteration:
        raise NotFound(f"no consignment {consignment_id!r}") from None
    summary = next(r for r in T.consignment_summary(run).rows if r["consignment"] == c.id)
    aggregated = [
        {
            "id": a.id,
            "months": ", ".join(a.months),
            "kg": a.kg,
            "MJ": a.energy_MJ,
            "ghg": a.ghg_gCO2e_per_MJ,  # energy-weighted average (§12.1)
            "parts": [
                {
                    "batch": p.batch,
                    "month": p.month,
                    "kg": p.kg,
                    "MJ": p.energy_MJ,
                    "ghg": p.ghg_gCO2e_per_MJ,
                    "compliant": p.compliant,
                    "drillable": p.batch in run.batches,
                }
                for p in a.parts
            ],
        }
        for a in c.aggregated
    ]
    checks = [
        {"check": x.name, "passed": x.passed, "detail": x.detail, "reference": x.reference}
        for x in run.checks
        if x.subject == c.id or x.subject in {a.id for a in c.aggregated}
    ]
    return {"summary": summary, "aggregated": aggregated, "checks": checks}


# ---- batch ----------------------------------------------------------------------------------


def batch_view(run: RunResult, batch_id: str) -> dict[str, Any]:
    """A produced batch (traceability record), an opening-ledger entry, or a PoS."""
    if batch_id not in run.batches and any(p.id == batch_id for p in run.purchases):
        return pos_view(run, batch_id)
    all_edges = lineage.edges(run)
    owner = {a.id: c.id for c in run.consignments for a in c.aggregated}
    downstream = [
        {"to": e.target, "kind": e.kind, "kg": e.kg, "consignment": owner.get(e.target)}
        for e in all_edges
        if e.source == batch_id
    ]
    if batch_id not in run.batches:
        line = next((x for led in run.ledgers for x in led.lines if x.batch == batch_id), None)
        if line is None:
            raise NotFound(f"no batch {batch_id!r}")
        return {
            "kind": "opening",
            "id": batch_id,
            "month": line.month,
            "ghg": line.ghg_gCO2e_per_MJ,
            "rfnbo_share": line.rfnbo_share,
            "rfnbo_kg": line.in_kg,
            "compliant": line.compliant,
            "storage": run.storage_of[batch_id],
            "downstream": downstream,
        }
    b = run.batches[batch_id]
    rec = json_log.batch_record(run, b, "", [e for e in all_edges if e.source == batch_id])
    again = json_log.recompute_ghg(rec)
    parts_again = json_log.recompute_parts(rec)
    return {
        "kind": b.kind,
        "id": b.id,
        "record": rec,
        "outputs": rec["output"]["to"],  # each storage the batch's RFNBO went to
        "upstream": [
            {
                "batch": f.source_batch,
                "origin": f.origin,  # "pos": credentials from a proof of sustainability
                "via": run.feed_via(f, b.id),
                "kg": f.kg,
                "MJ": f.energy_MJ,
                "ghg": f.ghg_gCO2e_per_MJ,
                "compliant": f.compliant,
            }
            for f in b.feed
        ],
        "downstream": downstream,
        "parts": rec["parts"],
        "recomputed_ghg": again,
        "recompute_ok": all(
            p.id in parts_again and close(parts_again[p.id], p.ghg_gCO2e_per_MJ, rel=1e-9)
            for p in b.parts
        )
        and (
            (again is None and b.ghg_gCO2e_per_MJ is None)
            or (
                again is not None
                and b.ghg_gCO2e_per_MJ is not None
                and close(again, b.ghg_gCO2e_per_MJ, rel=1e-9)
            )
        ),
    }


# ---- proof of sustainability -----------------------------------------------------------------


def pos_view(run: RunResult, pos_id: str) -> dict[str, Any]:
    """A PoS: where its credentials went, and what was written off unallocated (§11.1)."""
    p = next((x for x in run.purchases if x.id == pos_id), None)
    if p is None:
        raise NotFound(f"no PoS {pos_id!r}")
    return {
        "kind": "pos",
        "id": p.id,
        "supplier": p.supplier,
        "product": p.product,
        "dispatch_date": p.dispatch_date.isoformat(),
        "quarter": p.quarter,
        "quantity_kg": p.quantity_kg,
        "ghg": p.ghg_gCO2e_per_MJ,
        "compliant": p.compliant,
        "allocated_kg": p.allocated_kg,
        "written_off_kg": p.unallocated_kg,
        "allocations": [
            {
                "flow": a.flow,
                "to": a.target,
                "kind": a.kind,
                "start": json_log.local_iso(run.frame.index[a.start_index], run.frame.timezone),
                "intervals": a.end_index - a.start_index,
                "kg": a.kg,
                "metered_kg": a.metered_kg,
                "drillable": a.target in run.batches,
            }
            for a in p.allocations
        ],
        "downstream": [
            {"to": e.target, "kind": e.kind, "kg": e.kg}
            for e in lineage.edges(run)
            if e.source == p.id
        ],
        "checks": [
            {"check": c.name, "passed": c.passed, "detail": c.detail, "reference": c.reference}
            for c in run.checks
            if c.subject == p.id
        ],
    }


# ---- intervals ------------------------------------------------------------------------------


@dataclass(frozen=True)
class TotalCheck:
    label: str
    unit: str
    intervals_total: float
    batch_value: float

    @property
    def ok(self) -> bool:
        return close(self.intervals_total, self.batch_value)


@dataclass(frozen=True)
class IntervalView:
    batch: BatchResult
    table: T.Table
    totals: dict[str, float]
    checks: list[TotalCheck]
    matched_note: str

    @property
    def consistent(self) -> bool:
        return all(c.ok for c in self.checks)


def interval_view(run: RunResult, batch_id: str) -> IntervalView:
    """The 15-minute data behind a batch, with totals checked against the batch record."""
    b = run.batches.get(batch_id)
    if b is None:
        raise NotFound(f"no produced batch {batch_id!r} (opening entries have no intervals)")
    cfg, frame = run.config, run.frame
    unit = cfg.block(b.unit)
    assert isinstance(unit, ProcessingUnit)
    s, e = b.start_index, b.end_index
    cols: list[T.Column] = [
        T.Column("utc", "Start (UTC)", kind="text"),
        T.Column("local", "Start (local)", kind="text"),
    ]
    series: dict[str, np.ndarray] = {}
    checks: list[TotalCheck] = []

    into = cfg.flows_into(unit.id)
    renewable = [
        blk for f in into if isinstance(blk := cfg.block(f.source), InputBlock) and blk.renewable
    ]
    if renewable:
        given = {a.unit: a.kWh for a in run.decisions.renewable_allocations}
        src = renewable[0]
        assert isinstance(src, InputBlock)
        values = np.asarray(given[unit.id]) if unit.id in given else frame.series[src.series or ""]
        series["renewable"] = values[s:e]
        cols.append(T.Column("renewable", "Renewable available to unit", "kWh"))
        checks.append(
            TotalCheck(
                "Renewable available", "kWh", float(values[s:e].sum()), b.renewable_available_kWh
            )
        )
    if unit.electricity_series and any(f.material == ELECTRICITY for f in into):
        series["electricity"] = frame.series[unit.electricity_series][s:e]
        cols.append(T.Column("electricity", "Electricity", "kWh"))
        checks.append(
            TotalCheck("Electricity", "kWh", float(series["electricity"].sum()), b.electricity_kWh)
        )
    for f in into:
        inp = cfg.block(f.source)
        if isinstance(inp, InputBlock) and f.material != ELECTRICITY and f.series:
            key = f"in:{f.id}"
            series[key] = frame.series[f.series][s:e]
            cols.append(T.Column(key, inp.name, inp.quantity_unit))
            line = next((x for x in b.inputs if x.source == inp.id), None)
            checks.append(
                TotalCheck(
                    inp.name, inp.quantity_unit, float(series[key].sum()), line.qty if line else 0.0
                )
            )
    feed_total = 0.0
    for f in into:
        if isinstance(cfg.block(f.source), StorageUnit) and f.series:
            key = f"feed:{f.id}"
            series[key] = frame.series[f.series][s:e]
            cols.append(T.Column(key, f"{f.material} feed from {f.source}", "kg"))
            feed_total += float(series[key].sum())
        elif isinstance(sup := cfg.block(f.source), RfnboSupply) and f.series:
            key = f"supply:{f.id}"
            series[key] = frame.series[f.series][s:e]
            cols.append(T.Column(key, f"{f.material} delivered by {sup.supplier}", "kg"))
            feed_total += float(series[key].sum())
    if b.kind == "downstream":
        checks.append(TotalCheck("Feed consumed", "kg", feed_total, b.feed_consumed_kg))
    series["product"] = frame.series[unit.production_series or ""][s:e]
    cols.append(T.Column("product", f"{unit.product} produced", "kg"))
    checks.append(
        TotalCheck(f"{unit.product} produced", "kg", float(series["product"].sum()), b.product_kg)
    )

    rows = []
    for i in range(s, e):
        row: dict[str, Any] = {
            "utc": frame.index[i].isoformat(),
            "local": json_log.local_iso(frame.index[i], frame.timezone),
        }
        for key, values in series.items():
            row[key] = float(values[i - s])
        rows.append(row)
    totals = {key: float(values.sum()) for key, values in series.items()}
    note = (
        f"Renewable is matched within the batch: matched = min(Σ renewable, Σ electricity) = "
        f"min({b.renewable_available_kWh:,.1f}, {b.electricity_kWh:,.1f}) = "
        f"{b.renewable_matched_kWh:,.1f} kWh, so RFNBO share = {b.rfnbo_share:.6f}."
        if b.kind == "electrolytic"
        else f"RFNBO share = RFNBO feed energy ÷ feed energy consumed = {b.rfnbo_share:.6f}."
    )
    table = T.Table(f"Intervals {b.id}", f"15-minute data behind {b.id}", tuple(cols), rows)
    return IntervalView(b, table, totals, checks, note)


def trace_paths(run: RunResult, consignment_id: str) -> list[list[str]]:
    """Every chain consignment → … → produced batch, for testing the drill-down."""
    by_target: dict[str, list[str]] = {}
    for e in lineage.edges(run):
        if e.kind in ("feed", "output", "aggregation", "consignment"):
            by_target.setdefault(e.target, []).append(e.source)
    paths: list[list[str]] = []

    def walk(node: str, path: Sequence[str]) -> None:
        sources = by_target.get(node, [])
        if not sources:
            paths.append([*path, node])
        for src in sources:
            walk(src, [*path, node])

    walk(consignment_id, [])
    return paths
