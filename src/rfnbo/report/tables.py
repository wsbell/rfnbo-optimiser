"""Report tables (build plan §4 phase 8; scope doc "Outputs and traceability").

Every table is built from a calculated run, never from solver output. Tables are plain data
(columns with units, rows of values) so the Excel and CSV writers are thin.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from zoneinfo import ZoneInfo

from rfnbo.calc.calculator import RunResult
from rfnbo.config.schema import InputBlock, ProcessingUnit
from rfnbo.data import calendar as cal
from rfnbo.trace import lineage

Kind = Literal["text", "number", "bool", "datetime", "integer"]


@dataclass(frozen=True)
class Column:
    key: str
    header: str
    unit: str = ""
    kind: Kind = "number"

    @property
    def title(self) -> str:
        return f"{self.header} ({self.unit})" if self.unit else self.header


@dataclass(frozen=True)
class Table:
    name: str  # sheet / file name
    title: str
    columns: tuple[Column, ...]
    rows: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""

    def column(self, key: str) -> list[Any]:
        return [r.get(key) for r in self.rows]


def _c(key: str, header: str, unit: str = "", kind: Kind = "number") -> Column:
    return Column(key, header, unit, kind)


def _local(ts: dt.datetime, tz: str) -> dt.datetime:
    """Local wall-clock time without offset (as the workbooks show it)."""
    return ts.astimezone(ZoneInfo(tz)).replace(tzinfo=None)


# ---- batch register ---------------------------------------------------------------------


def batch_register(run: RunResult, unit: ProcessingUnit) -> Table:
    """Per unit: one row per batch, with every GHG term broken out."""
    tz = run.frame.timezone
    batches = sorted((b for b in run.batches.values() if b.unit == unit.id), key=lambda b: b.start)
    inputs: dict[str, InputBlock] = {}
    for b in batches:
        for line in b.inputs:
            src = run.config.block(line.source)
            if isinstance(src, InputBlock):
                inputs[f"{line.source}|{line.name}"] = src
    downstream = not unit.electricity_adds_to_lhv
    cols = [
        _c("batch", "Batch ID", kind="text"),
        _c("month", "Production month", kind="text"),
        _c("start_local", "Start (local)", kind="datetime"),
        _c("end_local", "End (local)", kind="datetime"),
        _c("start_utc", "Start (UTC)", kind="text"),
        _c("intervals", "Intervals", kind="integer"),
    ]
    if downstream:
        cols += [
            _c("feed_consumed_kg", "Feed consumed", "kg"),
            _c("rfnbo_feed_kg", "RFNBO feed allocated", "kg"),
            _c("feed_ghg", "Feed GHG", "gCO2e/MJ"),
            _c("feed_emissions_g", "Feed emissions", "gCO2e"),
        ]
    cols += [
        _c("renewable_available_kWh", "Renewable available", "kWh"),
        _c("electricity_kWh", "Electricity used", "kWh"),
        _c("renewable_matched_kWh", "Renewable matched", "kWh"),
        _c("nonrenewable_kWh", "Non-renewable electricity", "kWh"),
        _c("rfnbo_share", "RFNBO share"),
        _c("product_kg", f"{unit.product} produced", "kg"),
        _c("energy_MJ", f"{unit.product} energy", "MJ"),
    ]
    for key, src in inputs.items():
        name = key.split("|", 1)[1]
        cols += [
            _c(f"qty:{key}", name, src.quantity_unit),
            _c(f"em:{key}", f"{name} emissions", "gCO2e"),
        ]
    cols += [_c("own_emissions_g", "Own emissions", "gCO2e")]
    if downstream:
        cols += [_c("prorated_own_g", "Own emissions pro-rated to RFNBO", "gCO2e")]
    cols += [
        _c("adder_g", "Fixed adder emissions", "gCO2e"),
        _c("adder", "Fixed adder", "gCO2e/MJ"),
        _c("ghg", "Batch GHG", "gCO2e/MJ"),
        _c("compliant", "≤ threshold", kind="bool"),
        _c("rfnbo_kg", f"RFNBO {unit.product}", "kg"),
        _c("rfnbo_MJ", f"RFNBO {unit.product} energy", "MJ"),
    ]
    rows = []
    for b in batches:
        row: dict[str, Any] = {
            "batch": b.id,
            "month": b.month,
            "start_local": _local(b.start, tz),
            "end_local": _local(b.end, tz),
            "start_utc": b.start.isoformat(),
            "intervals": b.intervals,
            "feed_consumed_kg": b.feed_consumed_kg,
            "rfnbo_feed_kg": b.rfnbo_feed_kg,
            "feed_ghg": b.feed_ghg_gCO2e_per_MJ,
            "feed_emissions_g": b.feed_emissions_g,
            "renewable_available_kWh": b.renewable_available_kWh,
            "electricity_kWh": b.electricity_kWh,
            "renewable_matched_kWh": b.renewable_matched_kWh,
            "nonrenewable_kWh": b.nonrenewable_kWh,
            "rfnbo_share": b.rfnbo_share,
            "product_kg": b.product_kg,
            "energy_MJ": b.energy_MJ,
            "own_emissions_g": b.own_emissions_g,
            "prorated_own_g": b.prorated_own_emissions_g,
            "adder_g": b.adder_emissions_g,
            "adder": b.adder_gCO2e_per_MJ,
            "ghg": b.ghg_gCO2e_per_MJ,
            "compliant": b.compliant,
            "rfnbo_kg": b.rfnbo_kg,
            "rfnbo_MJ": b.rfnbo_MJ,
        }
        for key in inputs:
            found = next((x for x in b.inputs if f"{x.source}|{x.name}" == key), None)
            row[f"qty:{key}"] = found.qty if found else 0.0
            row[f"em:{key}"] = found.emissions_g if found else 0.0
        rows.append(row)
    return Table(
        f"Batches {unit.name}"[:31],
        f"Batch register: {unit.name}",
        tuple(cols),
        rows,
        "GHG = (own emissions + feed emissions) / energy + adder; downstream units "
        "pro-rate own emissions to the RFNBO share.",
    )


# ---- feed and lineage --------------------------------------------------------------------


def feed_matrix(run: RunResult, unit: ProcessingUnit) -> Table:
    """Upstream ledger batch × downstream batch of ``unit`` (kg), as the workbook's feed sheet."""
    targets = sorted((b for b in run.batches.values() if b.unit == unit.id), key=lambda b: b.start)
    feed: dict[tuple[str, str], float] = defaultdict(float)
    for a in run.decisions.feed:
        feed[a.source, a.target] += a.kg
    target_ids = {t.id for t in targets}
    store_ids = {run.storage_of[a.source] for a in run.decisions.feed if a.target in target_ids}
    lines = [x for led in run.ledgers if led.storage in store_ids for x in led.lines]
    cols = [
        _c("batch", "Upstream batch", kind="text"),
        _c("month", "Month", kind="text"),
        _c("ghg", "GHG", "gCO2e/MJ"),
        _c("compliant", "≤ threshold", kind="bool"),
        _c("available_kg", "RFNBO available", "kg"),
    ]
    cols += [_c(f"to:{t.id}", f"→ {t.id}", "kg") for t in targets]
    cols += [_c("left_kg", "Left in ledger", "kg")]
    rows = []
    for x in sorted(lines, key=lambda x: (x.month, x.batch)):
        row: dict[str, Any] = {
            "batch": x.batch,
            "month": x.month,
            "ghg": x.ghg_gCO2e_per_MJ,
            "compliant": x.compliant,
            "available_kg": x.in_kg,
        }
        for t in targets:
            row[f"to:{t.id}"] = feed.get((x.batch, t.id), 0.0)
        row["left_kg"] = x.closing_before_kg
        rows.append(row)
    return Table(f"Feed {unit.name}"[:31], f"Feed allocation to {unit.name}", tuple(cols), rows)


def lineage_table(run: RunResult) -> Table:
    """Every lineage edge: where each batch's RFNBO kg went."""
    batches = run.batches
    rows = []
    for e in lineage.edges(run):
        src = batches.get(e.source)
        rows.append(
            {
                "from": e.source,
                "from_unit": src.unit if src else "",
                "to": e.target,
                "kind": e.kind,
                "kg": e.kg,
            }
        )
    cols = (
        _c("from", "From", kind="text"),
        _c("from_unit", "From unit", kind="text"),
        _c("to", "To", kind="text"),
        _c("kind", "Kind", kind="text"),
        _c("kg", "Quantity", "kg"),
    )
    return Table(
        "Lineage",
        "Batch lineage",
        cols,
        rows,
        "Kinds: feed (to a downstream batch), aggregation (into an aggregated batch), "
        "consignment, write_off, carried (to the next quarter).",
    )


# ---- consignments ------------------------------------------------------------------------


def consignment_summary(run: RunResult) -> Table:
    cols = (
        _c("consignment", "Consignment", kind="text"),
        _c("customer", "Customer", kind="text"),
        _c("ship_date", "Ship date", kind="text"),
        _c("volume_kg", "Volume", "kg"),
        _c("energy_MJ", "Energy", "MJ"),
        _c("target", "Target", "gCO2e/MJ"),
        _c("weighted_ghg", "Weighted GHG before shipping", "gCO2e/MJ"),
        _c("adder", "Distribution adder", "gCO2e/MJ"),
        _c("shipped_ghg", "Shipped GHG", "gCO2e/MJ"),
        _c("margin_target", "Margin to target", "gCO2e/MJ"),
        _c("margin_threshold", "Margin to threshold", "gCO2e/MJ"),
        _c("meets_target", "Meets target", kind="bool"),
        _c("meets_threshold", "≤ threshold", kind="bool"),
    )
    rows = []
    for c in run.consignments:
        shipped = c.shipped_ghg_gCO2e_per_MJ
        target = c.target_gCO2e_per_MJ
        rows.append(
            {
                "consignment": c.id,
                "customer": c.customer,
                "ship_date": c.ship_date.isoformat(),
                "volume_kg": c.volume_kg,
                "energy_MJ": c.energy_MJ,
                "target": target,
                "weighted_ghg": c.weighted_ghg_gCO2e_per_MJ,
                "adder": c.distribution_adder_gCO2e_per_MJ,
                "shipped_ghg": shipped,
                "margin_target": None if shipped is None or target is None else target - shipped,
                "margin_threshold": None if shipped is None else c.threshold_gCO2e_per_MJ - shipped,
                "meets_target": None
                if target is None or shipped is None
                else shipped <= target + 1e-9,
                "meets_threshold": None
                if shipped is None
                else shipped <= c.threshold_gCO2e_per_MJ + 1e-9,
            }
        )
    return Table(
        "Consignments",
        "Consignment report",
        cols,
        rows,
        "Shipped GHG = energy-weighted GHG of the aggregated batches + distribution adder.",
    )


def consignment_parts(run: RunResult) -> Table:
    cols = (
        _c("consignment", "Consignment", kind="text"),
        _c("aggregated", "Aggregated batch", kind="text"),
        _c("month", "Production month", kind="text"),
        _c("batch", "Batch", kind="text"),
        _c("kg", "Quantity", "kg"),
        _c("MJ", "Energy", "MJ"),
        _c("ghg", "GHG", "gCO2e/MJ"),
        _c("emissions_g", "Emissions", "gCO2e"),
        _c("compliant", "≤ threshold", kind="bool"),
    )
    rows = [
        {
            "consignment": c.id,
            "aggregated": a.id,
            "month": p.month,
            "batch": p.batch,
            "kg": p.kg,
            "MJ": p.energy_MJ,
            "ghg": p.ghg_gCO2e_per_MJ,
            "emissions_g": p.energy_MJ * p.ghg_gCO2e_per_MJ,
            "compliant": p.compliant,
        }
        for c in run.consignments
        for a in c.aggregated
        for p in a.parts
    ]
    return Table("Consignment batches", "Batches allocated to consignments", cols, rows)


# ---- renewable matching -------------------------------------------------------------------


def _period(ts: dt.datetime, tz: str, hourly: bool) -> str:
    """Correlation period of an interval: its local month, or its clock hour when hourly."""
    if not hourly:
        return cal.month_key(ts, tz)
    hour = cal.hour_key(ts, tz)
    local = hour.astimezone(ZoneInfo(tz))
    # The repeated autumn hour is a separate clock hour.
    return local.strftime("%Y-%m-%d %H:00") + (" (repeated)" if local.fold else "")


def renewable_matching(run: RunResult) -> Table:
    """Per correlation period: renewable available, matched by each unit, unused."""
    tz, switch = run.frame.timezone, run.config.settings.hourly_correlation_from
    frame, cfg = run.frame, run.config
    pools = [b for b in cfg.blocks_of(InputBlock) if b.renewable]
    pool_ids = {p.id for p in pools}
    units = [
        u
        for u in cfg.blocks_of(ProcessingUnit)
        if any(f.source in pool_ids for f in cfg.flows_into(u.id))
    ]
    lhv_units = any(u.electricity_adds_to_lhv for u in units)

    def hourly(ts: dt.datetime) -> bool:
        return lhv_units and cal.hourly_correlation_applies(ts, tz, switch)

    available: dict[str, float] = defaultdict(float)
    for p in pools:
        values = frame.series[p.series or ""]
        for i, ts in enumerate(frame.index):
            available[_period(ts, tz, hourly(ts))] += float(values[i])
    matched: dict[tuple[str, str], float] = defaultdict(float)
    for b in run.batches.values():
        if b.unit in {u.id for u in units}:
            ts = frame.index[b.start_index]
            matched[_period(ts, tz, hourly(ts)), b.unit] += b.renewable_matched_kWh

    cols = [
        _c("period", "Correlation period", kind="text"),
        _c("available_kWh", "Renewable available", "kWh"),
    ]
    cols += [_c(f"unit:{u.id}", f"Matched by {u.name}", "kWh") for u in units]
    cols += [_c("unused_kWh", "Unused", "kWh")]
    rows = []
    for key in sorted(available):
        row: dict[str, Any] = {"period": key, "available_kWh": available[key]}
        for u in units:
            row[f"unit:{u.id}"] = matched.get((key, u.id), 0.0)
        row["unused_kWh"] = available[key] - sum(matched.get((key, u.id), 0.0) for u in units)
        rows.append(row)
    return Table(
        "Renewable matching",
        "Renewable matching by correlation period",
        tuple(cols),
        rows,
        "Matching is within each batch; a period's unused kWh were available but not "
        "matched by any batch.",
    )


# ---- ledgers and reconciliation ------------------------------------------------------------


def ledger_lines(run: RunResult) -> Table:
    cols = (
        _c("storage", "Storage", kind="text"),
        _c("quarter", "Quarter", kind="text"),
        _c("batch", "Batch (write-off priority order)", kind="text"),
        _c("origin", "Origin", kind="text"),
        _c("month", "Production month", kind="text"),
        _c("ghg", "GHG", "gCO2e/MJ"),
        _c("rfnbo_share", "RFNBO share"),
        _c("compliant", "≤ threshold", kind="bool"),
        _c("in_kg", "In", "kg"),
        _c("out_feed_kg", "Out: feed", "kg"),
        _c("out_consignment_kg", "Out: consignments", "kg"),
        _c("closing_before_kg", "Closing before write-off", "kg"),
        _c("write_off_kg", "Write-off", "kg"),
        _c("carried_kg", "Carried forward", "kg"),
    )
    rows = [
        {
            "storage": led.storage,
            "quarter": led.quarter,
            "batch": x.batch,
            "origin": x.origin,
            "month": x.month,
            "ghg": x.ghg_gCO2e_per_MJ,
            "rfnbo_share": x.rfnbo_share,
            "compliant": x.compliant,
            "in_kg": x.in_kg,
            "out_feed_kg": x.out_feed_kg,
            "out_consignment_kg": x.out_consignment_kg,
            "closing_before_kg": x.closing_before_kg,
            "write_off_kg": x.write_off_kg,
            "carried_kg": x.carried_kg,
        }
        for led in run.ledgers
        for x in led.lines
    ]
    return Table(
        "Ledger batches",
        "Storage RFNBO ledgers by batch",
        cols,
        rows,
        "Lines in write-off priority order: highest GHG first, newest month on ties.",
    )


def ledger_summary(run: RunResult) -> Table:
    cols = (
        _c("storage", "Storage", kind="text"),
        _c("quarter", "Quarter", kind="text"),
        _c("opening_kg", "Opening RFNBO ledger", "kg"),
        _c("produced_kg", "RFNBO produced", "kg"),
        _c("out_feed_kg", "Out: feed", "kg"),
        _c("out_consignment_kg", "Out: consignments", "kg"),
        _c("closing_before_kg", "Closing before write-off", "kg"),
        _c("physical_kg", "Measured physical inventory", "kg"),
        _c("excess_kg", "Excess over physical", "kg"),
        _c("noncompliant_kg", "Non-compliant at quarter end (not carried)", "kg"),
        _c("write_off_kg", "Write-off", "kg"),
        _c("carried_kg", "Carried forward", "kg"),
        _c("carried_ghg", "GHG of carried RFNBO", "gCO2e/MJ"),
        _c("weight", "Storage weight (MJ final product per MJ)", ""),
    )
    rows = [
        {
            "storage": led.storage,
            "quarter": led.quarter,
            "opening_kg": led.opening_kg,
            "produced_kg": led.produced_kg,
            "out_feed_kg": led.out_feed_kg,
            "out_consignment_kg": led.out_consignment_kg,
            "closing_before_kg": led.closing_before_kg,
            "physical_kg": led.physical_kg,
            "excess_kg": led.excess_kg,
            "noncompliant_kg": led.noncompliant_closing_kg,
            "write_off_kg": led.write_off_kg,
            "carried_kg": led.carried_kg,
            "carried_ghg": led.carried_ghg_gCO2e_per_MJ,
            "weight": run.storage_weights[led.storage].weight
            if led.storage in run.storage_weights
            else None,
        }
        for led in run.ledgers
    ]
    return Table("Ledgers", "Storage RFNBO ledgers by quarter", cols, rows)


def reconciliation(run: RunResult) -> Table:
    names = {b.id: b.name for b in run.config.blocks}
    cols = (
        _c("storage", "Storage", kind="text"),
        _c("quarter", "Quarter", kind="text"),
        _c("item", "Item", kind="text"),
        _c("kg", "Quantity", "kg"),
    )
    rows: list[dict[str, Any]] = []
    for r in run.reconciliation:

        def add(item: str, kg: float, r: Any = r) -> None:
            rows.append({"storage": r.storage, "quarter": r.quarter, "item": item, "kg": kg})

        add("Opening physical inventory", r.opening_physical_kg)
        add("Produced into storage", r.produced_kg)
        for flow_id, kg in r.outflows_kg:
            flow = next(f for f in run.config.flows if f.id == flow_id)
            add(f"Out to {names.get(flow.target, flow.target)} ({flow_id})", kg)
        add("Expected closing", r.expected_kg)
        add("Measured closing", r.measured_kg)
        add("Implied loss", r.implied_loss_kg)
    return Table(
        "Reconciliation",
        "Physical reconciliation",
        cols,
        rows,
        "Losses are not modelled; they appear here as expected minus measured.",
    )


# ---- log, comparison, summary --------------------------------------------------------------


def calculation_log(run: RunResult, flags: Sequence[dict[str, Any]] = ()) -> Table:
    cols = (
        _c("kind", "Kind", kind="text"),
        _c("name", "Rule or flag", kind="text"),
        _c("subject", "Subject", kind="text"),
        _c("result", "Result", kind="text"),
        _c("detail", "Detail", kind="text"),
        _c("reference", "Reference", kind="text"),
    )
    rows: list[dict[str, Any]] = [
        {
            "kind": "data quality",
            "name": f["code"],
            "subject": f.get("series") or "",
            "result": f["severity"],
            "detail": f"{f['message']} ({f.get('count', 1)} interval(s))",
            "reference": "Scope document: data quality",
        }
        for f in flags
    ]
    rows += [
        {
            "kind": "rule check",
            "name": c.name,
            "subject": c.subject,
            "result": "pass" if c.passed else "FAIL",
            "detail": c.detail,
            "reference": c.reference,
        }
        for c in run.checks
    ]
    return Table("Calculation log", "Calculation log", cols, rows)


def baseline_comparison(optimised: RunResult, baseline: RunResult) -> Table:
    from rfnbo.optimise.pipeline import objectives

    o, b = objectives(optimised), objectives(baseline)
    cols = (
        _c("metric", "Objective", kind="text"),
        _c("unit", "Unit", kind="text"),
        _c("optimised", "Optimised", kind="text"),
        _c("baseline", "Baseline", kind="text"),
        _c("difference", "Optimised − baseline"),
    )
    rows: list[dict[str, Any]] = [
        {
            "metric": "1. Consignments met",
            "unit": "",
            "optimised": str(o.consignments_met),
            "baseline": str(b.consignments_met),
            "difference": None,
        },
        {
            "metric": "2. Compliant RFNBO carried forward (storage-weighted)",
            "unit": "MJ final product",
            "optimised": o.carried_value_MJ,
            "baseline": b.carried_value_MJ,
            "difference": o.carried_value_MJ - b.carried_value_MJ,
        },
        {
            "metric": "Compliant RFNBO of the final product",
            "unit": "kg",
            "optimised": o.final_rfnbo_kg,
            "baseline": b.final_rfnbo_kg,
            "difference": o.final_rfnbo_kg - b.final_rfnbo_kg,
        },
        {
            "metric": "3. Emissions of RFNBO carried forward",
            "unit": "gCO2e",
            "optimised": o.carried_emissions_g,
            "baseline": b.carried_emissions_g,
            "difference": o.carried_emissions_g - b.carried_emissions_g,
        },
    ]
    for store in sorted(set(o.carried_ghg) | set(b.carried_ghg)):
        ov, bv = o.carried_ghg.get(store), b.carried_ghg.get(store)
        rows.append(
            {
                "metric": f"   GHG carried in {store}",
                "unit": "gCO2e/MJ",
                "optimised": ov,
                "baseline": bv,
                "difference": None if ov is None or bv is None else ov - bv,
            }
        )
    rows.append(
        {
            "metric": "Batches",
            "unit": "",
            "optimised": o.batches,
            "baseline": b.batches,
            "difference": o.batches - b.batches,
        }
    )
    rows.append(
        {
            "metric": "Failed rule checks",
            "unit": "",
            "optimised": len(optimised.failures),
            "baseline": len(baseline.failures),
            "difference": len(optimised.failures) - len(baseline.failures),
        }
    )
    return Table(
        "Baseline comparison",
        "Optimised result against the baseline (one batch per unit per month, FIFO)",
        cols,
        rows,
        "Objectives are lexicographic: 1, then 2, then 3.",
    )


def summary(run: RunResult, run_id: str | None = None) -> Table:
    cols = (_c("item", "Item", kind="text"), _c("value", "Value", kind="text"))
    cfg = run.config
    items = [
        ("Run id", run_id or "(not sealed)"),
        ("Plant", cfg.plant),
        ("Config version", f"{cfg.version} (effective {cfg.effective_from.isoformat()})"),
        ("Quarter", run.frame.quarter),
        ("Plant time zone", run.frame.timezone),
        ("Intervals", str(len(run.frame))),
        ("RFNBO threshold (gCO2e/MJ)", f"{cfg.settings.rfnbo_threshold_gCO2e_per_MJ:.4g}"),
        ("Rule checks", f"{len(run.checks)} run, {len(run.failures)} failed"),
    ]
    return Table("Summary", "Run summary", cols, [{"item": k, "value": v} for k, v in items])


def all_tables(
    run: RunResult,
    run_id: str | None = None,
    flags: Sequence[dict[str, Any]] = (),
    baseline: RunResult | None = None,
) -> list[Table]:
    from rfnbo.calc.batch import unit_order

    tables = [summary(run, run_id)]
    if baseline is not None:
        tables.append(baseline_comparison(run, baseline))
    for unit in unit_order(run.config):
        tables.append(batch_register(run, unit))
    for unit in unit_order(run.config):
        if not unit.electricity_adds_to_lhv and any(
            a.target in run.batches and run.batches[a.target].unit == unit.id
            for a in run.decisions.feed
        ):
            tables.append(feed_matrix(run, unit))
    tables += [
        lineage_table(run),
        consignment_summary(run),
        consignment_parts(run),
        renewable_matching(run),
        ledger_summary(run),
        ledger_lines(run),
        reconciliation(run),
        calculation_log(run, flags),
    ]
    return tables
