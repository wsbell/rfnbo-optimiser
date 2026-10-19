"""Phase 12 acceptance: given test case 3's decisions, the calculator reproduces every workbook
value to 1e-6 on a multi-step plant (split output, pass-through link, parallel units), every
rule check passes, records recompute their GHG and the sealed run reproduces byte for byte."""

from __future__ import annotations

import json
import math
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from rfnbo.calc import RunResult, calculate
from rfnbo.calc.batch import CalculationError
from rfnbo.config import load_config
from rfnbo.data import ingest, quality
from rfnbo.model.entities import Decisions, OutputAllocation, WriteOff
from rfnbo.trace import json_log, lineage
from rfnbo.trace import run as sealed
from rfnbo.ui.results import views as V
from tests.golden.export_tc3 import WORKBOOK

GOLDEN = Path(__file__).resolve().parent


@cache
def _inputs() -> tuple[Any, Any, list[quality.QualityFlag]]:
    cfg = load_config(GOLDEN / "tc3.plant.json")
    raw = ingest.load(WORKBOOK, ingest.load_source_spec(GOLDEN / "tc3.source.json"))
    frame, flags = quality.prepare(cfg, raw, 2026, 4)
    return cfg, frame, flags


@cache
def _decisions() -> Decisions:
    return Decisions.model_validate_json((GOLDEN / "tc3.decisions.json").read_text("utf-8"))


@cache
def _run() -> RunResult:
    cfg, frame, _ = _inputs()
    return calculate(cfg, frame, _decisions())


@cache
def _expected() -> dict[str, Any]:
    out: dict[str, Any] = json.loads((GOLDEN / "tc3.expected.json").read_text("utf-8"))
    return out


def approx(got: float | None, want: float | None, what: str) -> None:
    assert got is not None and want is not None, what
    assert math.isclose(got, want, rel_tol=1e-6, abs_tol=1e-6), f"{what}: {got!r} vs {want!r}"


def test_every_check_passes() -> None:
    run = _run()
    assert run.failures == []
    names = {c.name for c in run.checks}
    assert {"output_split", "pass_through_closes", "renewable_pool"} <= names


def test_electrolyser_batches_and_split_output() -> None:
    run, x = _run(), _expected()
    for bid, w in x["electrolyser_batches"].items():
        b = run.batches[bid]
        approx(b.intervals, w["intervals"], f"{bid} intervals")
        approx(b.renewable_available_kWh, w["renewable_allocated_kWh"], f"{bid} renewable")
        approx(b.electricity_kWh, w["electricity_kWh"], f"{bid} electricity")
        approx(b.renewable_matched_kWh, w["renewable_matched_kWh"], f"{bid} matched")
        approx(b.rfnbo_share, w["rfnbo_share"], f"{bid} share")
        approx(b.product_kg, w["product_kg"], f"{bid} H2")
        approx(b.energy_MJ, w["energy_MJ"], f"{bid} MJ")
        water = sum(i.emissions_g for i in b.inputs if i.source == "water")
        grid = sum(i.emissions_g for i in b.inputs if i.source == "grid_power")
        approx(water, w["water_emissions_g"], f"{bid} water")
        approx(grid, w["grid_emissions_g"], f"{bid} grid")
        approx(b.ghg_gCO2e_per_MJ, w["ghg"], f"{bid} GHG")
        assert b.compliant == w["compliant"]
        approx(b.rfnbo_kg, w["rfnbo_kg"], f"{bid} RFNBO")
        s = x["output_split"][bid]
        buffer, direct = run.line("h2_buffer", bid), run.line("pt_compressor", bid)
        assert buffer is not None and direct is not None
        approx(buffer.in_kg, s["assigned_to_buffer_kg"], f"{bid} to buffer")
        approx(direct.in_kg, s["assigned_to_compressor_kg"], f"{bid} to compressor")
    assert run.storages_of["E-2026-10"] == ("h2_buffer", "pt_compressor")


@pytest.mark.parametrize("unit", ["liquefier", "compressor"])
def test_conditioning_unit_batches(unit: str) -> None:
    """Losses raise GHG; matched renewable removes grid emissions; share from the feed."""
    run, x = _run(), _expected()
    for bid, w in x[f"{unit}_batches"].items():
        b = run.batches[bid]
        approx(b.feed_consumed_kg, w["feed_consumed_kg"], f"{bid} consumed")
        approx(b.rfnbo_feed_kg, w["rfnbo_feed_kg"], f"{bid} RFNBO feed")
        approx(b.feed_ghg_gCO2e_per_MJ, w["feed_ghg"], f"{bid} feed GHG")
        approx(b.rfnbo_share, w["rfnbo_share"], f"{bid} share")
        approx(b.product_kg, w["product_kg"], f"{bid} product")
        approx(b.feed_consumed_kg - b.product_kg, w["loss_kg"], f"{bid} loss")
        approx(b.renewable_available_kWh, w["renewable_allocated_kWh"], f"{bid} renewable")
        approx(b.electricity_kWh, w["electricity_kWh"], f"{bid} electricity")
        approx(b.renewable_matched_kWh, w["renewable_matched_kWh"], f"{bid} matched")
        approx(b.own_emissions_g, w["own_emissions_g"], f"{bid} own")
        approx(b.feed_emissions_g, w["feed_emissions_g"], f"{bid} feed emissions")
        approx(b.rfnbo_kg, w["rfnbo_kg"], f"{bid} RFNBO")
        approx(b.rfnbo_MJ, w["rfnbo_MJ"], f"{bid} RFNBO MJ")
        approx(b.ghg_gCO2e_per_MJ, w["ghg"], f"{bid} GHG")
        assert b.compliant == w["compliant"] and b.intervals == w["intervals"]


def test_ledgers_and_reconciliation() -> None:
    run, x = _run(), _expected()
    for storage in ("h2_buffer", "pt_compressor"):
        led, w = run.ledger(storage), x["ledgers"][storage]
        approx(led.opening_kg, w["opening_kg"], f"{storage} opening")
        approx(led.produced_kg, w["in_kg"], f"{storage} in")
        approx(led.out_feed_kg, w["out_feed_kg"], f"{storage} fed")
        approx(led.closing_before_kg, w["closing_before_kg"], f"{storage} closing")
        approx(led.physical_kg, w["physical_kg"], f"{storage} physical")
        approx(led.write_off_kg, w["write_off_kg"], f"{storage} write-off")
        approx(led.carried_kg, w["carried_kg"], f"{storage} carried")
        if w["carried_kg"]:
            approx(led.carried_ghg_gCO2e_per_MJ, w["carried_ghg"], f"{storage} carried GHG")
    for storage in ("lh2_storage", "ch2_storage"):
        led, w = run.ledger(storage), x["ledgers"][storage]
        approx(led.physical_kg, w["measured_kg"], f"{storage} measured")
        approx(led.required_write_off_kg, w["shortfall_kg"], f"{storage} shortfall")
        approx(led.carried_ghg_gCO2e_per_MJ, w["carried_ghg"], f"{storage} carried GHG")
        for bid, line in w["lines"].items():
            got = run.line(storage, bid)
            assert got is not None, (storage, bid)
            for attr, key in (
                ("ghg_gCO2e_per_MJ", "ghg"),
                ("in_kg", "in_kg"),
                ("out_consignment_kg", "out_consignment_kg"),
                ("closing_before_kg", "closing_before_kg"),
                ("write_off_kg", "write_off_kg"),
                ("carried_kg", "carried_kg"),
            ):
                approx(getattr(got, attr), line[key], f"{storage} {bid} {key}")
    recon = {r.storage: r for r in run.reconciliation}
    for storage, w in x["reconciliation"].items():
        approx(recon[storage].expected_kg, w["expected_kg"], f"{storage} expected")
        approx(recon[storage].measured_kg, w["measured_kg"], f"{storage} measured")
        approx(recon[storage].implied_loss_kg, w["implied_loss_kg"], f"{storage} loss")


def test_consignments_and_objectives() -> None:
    run, x = _run(), _expected()
    for cid, w in x["consignments"].items():
        c = run.consignment(cid)
        assert c.source_storage == w["storage"]
        by_month: dict[str, float] = {}
        for a in c.aggregated:
            (month,) = a.months
            by_month[month] = by_month.get(month, 0.0) + a.kg
        for month, kg in w["parts_by_month"].items():
            approx(by_month.get(month, 0.0), kg, f"{cid} {month}")
        approx(c.weighted_ghg_gCO2e_per_MJ, w["weighted_ghg"], f"{cid} weighted")
        approx(c.shipped_ghg_gCO2e_per_MJ, w["shipped_ghg"], f"{cid} shipped")
    o = x["objectives"]
    approx(run.carried_value_MJ, o["carried_value_MJ"], "carried value")
    for storage, weight in o["weights"].items():
        approx(run.storage_weights[storage].weight, weight, f"{storage} weight")
    approx(
        sum(
            x.carried_kg * x.lhv_MJ_per_kg * x.ghg_gCO2e_per_MJ
            for led in run.ledgers
            for x in led.lines
        ),
        o["carried_emissions_g"],
        "carried emissions",
    )
    assert len(run.batches) == o["batches"]


def test_records_recompute_and_lineage_crosses_every_step(tmp_path: Path) -> None:
    run = _run()
    _, _, flags = _inputs()
    edges = lineage.edges(run)
    for rec in json_log.batch_records(run, "tc3", edges):
        approx(
            json_log.recompute_ghg(rec), run.batches[rec["batch"]].ghg_gCO2e_per_MJ, rec["batch"]
        )
    rec = json_log.batch_record(run, run.batches["C-2026-12"], "tc3", [])
    assert rec["lineage"]["upstream"] == [
        {"batch": "E-2026-12", "origin": "batch", "via": "pt_compressor", "kg": 59520.0}
    ]
    e12 = json_log.batch_record(run, run.batches["E-2026-12"], "tc3", [])
    assert e12["output"]["to"] == [
        {"storage": "h2_buffer", "rfnbo_kg": 89280.0},
        {"storage": "pt_compressor", "rfnbo_kg": 59520.0},
    ]
    assert ["CA", "CA-2026-11", "L-2026-11", "E-2026-11"] in V.trace_paths(run, "CA")
    assert ["CB", "CB-2026-12", "C-2026-12", "E-2026-12"] in V.trace_paths(run, "CB")
    d = sealed.seal(run, tmp_path / "tc3", flags)
    assert sealed.verify(d) == []


def _with(**update: Any) -> RunResult:
    cfg, frame, _ = _inputs()
    return calculate(cfg, frame, _decisions().model_copy(update=update))


def test_output_split_check_fails_when_parts_do_not_add_up_or_exceed_the_meter() -> None:
    d = _decisions()
    dropped = [o for o in d.output_allocations if (o.batch, o.flow) != ("E-2026-12", "f_el_pt")]
    run = _with(output_allocations=dropped)
    assert "output_split" in {f.name for f in run.failures}
    moved = [
        o.model_copy(update={"kg": o.kg + (100.0 if o.flow == "f_el_buf" else -100.0)})
        if o.batch == "E-2026-12"
        else o
        for o in d.output_allocations
    ]
    failed = [f for f in _with(output_allocations=moved).failures if f.name == "output_split"]
    assert failed and "metered" in failed[0].detail


def test_pass_through_ledger_must_close_at_zero() -> None:
    """Rule 5: RFNBO left in the direct link is written off, never carried."""
    d = _decisions()
    less = [
        a.model_copy(update={"kg": a.kg - 100.0}) if a.target == "C-2026-12" else a for a in d.feed
    ]
    names = {f.name for f in _with(feed=less).failures}
    assert {"pass_through_closes", "write_off_amount", "ledger_within_physical"} <= names
    written = [*d.write_offs, WriteOff(storage="pt_compressor", batch="E-2026-12", kg=100.0)]
    fixed = _with(feed=less, write_offs=written)
    assert fixed.failures == []
    assert fixed.ledger("pt_compressor").carried_kg == 0


def test_output_allocation_only_for_split_outputs() -> None:
    d = _decisions()
    bad = [*d.output_allocations, OutputAllocation(batch="L-2026-10", flow="f_liq_lh2s", kg=1.0)]
    with pytest.raises(CalculationError, match="not a split output"):
        _with(output_allocations=bad)


# ---- the reference's other cases, run through the app ---------------------------------------

FLOW_OF = {"to_buffer": "f_el_buf", "to_compressor": "f_el_pt"}
OUTPUT_OF = {"CA": "consignment_a", "CB": "consignment_b"}


def _app_inputs(data: Any, d: Any, result: dict[str, Any]) -> tuple[Any, Decisions]:
    """A reference case as an app frame and decision set (write-offs from the reference)."""
    import datetime as dt

    import numpy as np

    from rfnbo.data.quality import Frame
    from tests.golden import tc3_reference as R
    from tests.golden.test_tc3_workbook import SERIES

    series = {name: np.asarray(data.s[ref], dtype=float) for name, ref in SERIES.items()}
    frame = Frame(tuple(data.utc), series, "Europe/Amsterdam", "2026-Q4")
    end = data.utc[-1] + R.STEP

    def at(i: int) -> dt.datetime:
        return end if i == len(data) else data.utc[i]

    months = {b: x["month"] for led in result["ledgers"].values() for b, x in led["lines"].items()}
    raw = {
        "batches": [
            {"id": bid, "unit": u, "start": at(i0).isoformat(), "end": at(i1).isoformat()}
            for u, bs in d.batches.items()
            for bid, i0, i1 in bs
        ],
        "renewable_allocations": [{"unit": u, "kWh": v} for u, v in d.renewable.items()],
        "output_allocations": [
            {"batch": b, "flow": FLOW_OF[f], "kg": kg} for (b, f), kg in d.outputs.items()
        ],
        "feed": [{"source": s, "target": t, "kg": kg} for (s, t), kg in d.feed.items()],
        "consignments": [
            {
                "id": cid,
                "output": OUTPUT_OF[cid],
                "volume_kg": c["volume_kg"],
                "ship_date": f"2026-12-{c['ship_day']:02d}",
                "target_gCO2e_per_MJ": d.targets.get(cid, c["target"]),
            }
            for cid, c in R.CONSIGNMENTS.items()
            if d.parts.get(cid)
        ],
        "aggregated_batches": [
            {"id": f"{cid}-{b}", "consignment": cid, "parts": [{"batch": b, "kg": kg}]}
            for cid, parts in d.parts.items()
            for b, kg in parts.items()
            if kg > 0 and months.get(b)
        ],
        "write_offs": [
            {"storage": s, "batch": b, "kg": x["write_off_kg"]}
            for s, led in result["ledgers"].items()
            for b, x in led["lines"].items()
            if x["write_off_kg"] > 1e-9
        ],
    }
    return frame, Decisions.model_validate(raw)


@pytest.mark.parametrize("case", ["v1_hourly", "v2_shortfall", "v2_all_to_electrolyser"])
def test_reference_cases_reproduce_in_the_app(case: str) -> None:
    """Hourly batches (2,209 electrolyser batches), a part-RFNBO batch whose non-compliant H₂
    goes through the pass-through, and non-compliant LH₂ written off at quarter end."""
    from tests.golden import tc3_reference as R

    if case == "v1_hourly":
        data = R.build_data()
        d = R.hourly_decisions(data)
    else:
        data = R.build_data(dec_renewable_kWh=R.V2_DEC_RENEWABLE_KWH, spot=False)
        d = R.v2_decisions(data, all_to_electrolyser=case == "v2_all_to_electrolyser")
    want = R.run(data, d)
    cfg, _, _ = _inputs()
    frame, decisions = _app_inputs(data, d, want)
    run = calculate(cfg, frame, decisions)
    assert run.failures == [], [str(f) for f in run.failures][:5]
    for batches in want["units"].values():
        for bid, w in batches.items():
            b = run.batches[bid]
            approx(b.rfnbo_kg, w["rfnbo_kg"], f"{bid} RFNBO")
            if w["ghg"] is not None:
                approx(b.ghg_gCO2e_per_MJ, w["ghg"], f"{bid} GHG")
    for storage, led in want["ledgers"].items():
        got = run.ledger(storage)
        approx(got.write_off_kg, led["write_off_kg"], f"{storage} write-off")
        approx(got.carried_kg, led["carried_kg"], f"{storage} carried")
    approx(run.carried_value_MJ, want["objectives"]["carried_value_MJ"], "carried value")


def test_every_consignment_traces_through_every_step_to_interval_data() -> None:
    """Phase 15: drill-down from each consignment through the conditioning unit and the split
    electrolyser output to 15-minute data, on a run the optimiser made."""
    from tests.golden.test_pipeline_network_golden import _optimised

    run = _optimised("base").optimised.run
    for c in run.consignments:
        paths = V.trace_paths(run, c.id)
        assert paths and all(len(p) == 4 for p in paths), (c.id, paths)
        for path in paths:
            _, _, conditioning, electrolysis = path
            assert run.batches[conditioning].kind == "downstream"
            assert run.batches[electrolysis].kind == "electrolytic"
            for bid in (conditioning, electrolysis):
                assert V.batch_view(run, bid)["recompute_ok"], bid
                iv = V.interval_view(run, bid)
                assert iv.consistent and len(iv.table.rows) == run.batches[bid].intervals, bid
        product = {"lh2_storage": "LH2", "ch2_storage": "CH2"}[c.source_storage]
        assert V.consignment_view(run, c.id)["summary"]["product"] == product
    e = next(b for b in run.batches.values() if b.unit == "electrolyser")
    view = V.batch_view(run, e.id)
    assert {o["storage"] for o in view["outputs"]} == {"h2_buffer", "pt_compressor"}
    liq = next(b for b in run.batches.values() if b.unit == "liquefier")
    assert {u["via"] for u in V.batch_view(run, liq.id)["upstream"]} == {"h2_buffer"}
    products = {r["storage"]: r for r in V.overview(run)["products"]}
    assert products["lh2_storage"]["kind"] == "final product"
    assert products["pt_compressor"]["kind"] == "pass-through"
    assert products["h2_buffer"]["kind"] == "intermediate"
