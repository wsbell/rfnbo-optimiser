"""Phase 16 acceptance: purchased RFNBO with proofs of sustainability (build plan §11.1).

Given test case 4's decisions, the calculator reproduces the workbook to 1e-6, every rule
check passes (and each new check fails when its rule is broken), a consignment drills down to
the PoS that fed it, and the sealed run reproduces byte for byte. The optimiser matches the
hand calculation, and in variant V1 puts no credentials into product that would be written off.
"""

from __future__ import annotations

import datetime as dt
import json
from functools import cache
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from rfnbo.calc import RunResult, calculate
from rfnbo.calc.batch import CalculationError
from rfnbo.config import load_config
from rfnbo.config.schema import PlantConfig
from rfnbo.data import ingest, quality
from rfnbo.data.quality import Frame
from rfnbo.model.entities import (
    AggregatedBatchSpec,
    ConsignmentSpec,
    Decisions,
    FeedAllocation,
    Part,
    PoSAllocation,
    ProofOfSustainability,
)
from rfnbo.optimise.pipeline import OptimisationResult, optimise
from rfnbo.trace import json_log, lineage
from rfnbo.trace import run as sealed
from rfnbo.ui.results import views as V
from tests.golden.export_tc4 import WORKBOOK
from tests.golden.test_tc3_calc import approx

GOLDEN = Path(__file__).resolve().parent


@cache
def _inputs() -> tuple[PlantConfig, Frame]:
    cfg = load_config(GOLDEN / "tc4.plant.json")
    raw = ingest.load(WORKBOOK, ingest.load_source_spec(GOLDEN / "tc4.source.json"))
    frame, flags = quality.prepare(cfg, raw, 2026, 4)
    assert not quality.has_errors(flags), flags
    return cfg, frame


@cache
def _decisions() -> Decisions:
    return Decisions.model_validate_json((GOLDEN / "tc4.decisions.json").read_text("utf-8"))


@cache
def _expected() -> dict[str, Any]:
    out: dict[str, Any] = json.loads((GOLDEN / "tc4.expected.json").read_text("utf-8"))
    return out


@cache
def _run() -> RunResult:
    cfg, frame = _inputs()
    return calculate(cfg, frame, _decisions())


def _with(**update: Any) -> RunResult:
    cfg, frame = _inputs()
    return calculate(cfg, frame, _decisions().model_copy(update=update))


def _failed(run: RunResult, name: str) -> list[str]:
    return [c.subject for c in run.failures if c.name == name]


def test_every_check_passes() -> None:
    run = _run()
    assert run.failures == [], [str(f) for f in run.failures][:5]
    names = {c.name for c in run.checks}
    assert {
        "pos_compliant",
        "pos_in_quarter",
        "pos_within_quantity",
        "pos_product",
        "pos_within_delivery",
        "aggregated_same_source",
    } <= names


@pytest.mark.parametrize("unit", ["liquefier", "compressor"])
def test_conditioning_batches_with_purchased_feed(unit: str) -> None:
    run, x = _run(), _expected()[f"{unit}_batches"]
    for bid, want in x.items():
        b = run.batches[bid]
        approx(b.feed_consumed_kg, want["feed_consumed_kg"], f"{bid} feed consumed")
        approx(b.rfnbo_feed_kg, want["rfnbo_feed_kg"], f"{bid} RFNBO feed")
        approx(b.rfnbo_share, want["rfnbo_share"], f"{bid} share")
        approx(b.rfnbo_kg, want["rfnbo_kg"], f"{bid} RFNBO")
        approx(b.max_ghg_gCO2e_per_MJ, want["ghg"], f"{bid} highest part GHG")
        parts = _expected()["parts"]
        assert {p.id for p in b.parts} == {pid for pid, x in parts.items() if x["batch"] == bid}
        for part in b.parts:
            approx(part.kg, parts[part.id]["rfnbo_kg"], part.id)
            approx(part.ghg_gCO2e_per_MJ, parts[part.id]["ghg"], f"{part.id} GHG")
            assert part.purchased is (parts[part.id]["origin"] == "purchased"), part.id
        credits = sum(f.kg for f in b.feed if f.origin == "pos")
        approx(credits, want["pos_feed_kg"], f"{bid} credentials") if credits else None
        assert b.compliant


def test_pos_register_ledgers_and_objectives() -> None:
    run, x = _run(), _expected()
    for p in run.purchases:
        approx(p.allocated_kg, x["pos"][p.id]["allocated_kg"], f"{p.id} allocated")
        assert abs(p.unallocated_kg - x["pos"][p.id]["written_off_kg"]) < 1e-6, p.id
    (sup,) = run.supplies
    approx(sup.metered_kg, x["supplier"]["metered_kg"], "metered")
    approx(sup.pos_kg, x["supplier"]["pos_kg"], "PoS")
    approx(sup.allocated_kg, x["supplier"]["allocated_kg"], "allocated")
    approx(sup.non_rfnbo_kg, x["supplier"]["non_rfnbo_kg"], "non-RFNBO delivery")
    for storage in ("h2_buffer", "pt_compressor"):
        approx(run.ledger(storage).carried_kg + 1, x["ledgers"][storage]["carried_kg"] + 1, storage)
    for storage in ("lh2_storage", "ch2_storage"):
        for bid, line in x["ledgers"][storage]["lines"].items():
            got = run.line(storage, bid)
            assert got is not None
            approx(got.carried_kg + 1, line["carried_kg"] + 1, f"{storage} {bid}")
    for cid, c in x["consignments"].items():
        approx(run.consignment(cid).shipped_ghg_gCO2e_per_MJ, c["shipped_ghg"], cid)
    approx(run.carried_value_MJ, x["objectives"]["carried_value_MJ"], "carried value")
    approx(run.storage_weights["h2_buffer"].weight, x["objectives"]["weights"]["h2_buffer"], "w")


def test_credentials_trace_to_their_parts(tmp_path: Path) -> None:
    """Credentials make their own parts (§12): December LH₂'s purchased part traces to POS-B;
    CA ships only the plant's own LH₂ (the purchased part is above its limit); POS-C is written
    off in full."""
    run = _run()
    paths = V.trace_paths(run, "CA")
    assert ["CA", "CA-2026-11", "L-2026-11", "E-2026-11"] in paths
    assert not {"POS-A", "POS-B", "POS-C"} & {p[-1] for p in paths}
    view = V.batch_view(run, "L-2026-12")
    credits = [u for u in view["upstream"] if u["origin"] == "pos"]
    assert {u["batch"] for u in credits} == {"POS-B"}
    assert {u["via"] for u in credits} == {"f_sup_liq"}
    assert view["recompute_ok"]
    part = next(p for p in run.batches["L-2026-12"].parts if p.purchased)
    assert part.id == "L-2026-12.2" and part.sources == ("POS-B",)
    iv = V.interval_view(run, "L-2026-12")
    assert iv.consistent
    pv = V.pos_view(run, "POS-C")
    assert pv["allocated_kg"] == pytest.approx(0) and pv["written_off_kg"] == pytest.approx(6000)
    pb = V.pos_view(run, "POS-B")
    assert {d["to"] for d in pb["downstream"]} >= {"C-2026-11", "L-2026-12"}
    graph = lineage.graph(run)
    assert {n["id"] for n in graph["nodes"] if n["kind"] == "pos"} == {"POS-A", "POS-B", "POS-C"}
    wo = [e for e in lineage.edges(run) if e.kind == "write_off" and e.source == "POS-C"]
    assert len(wo) == 1 and wo[0].kg == pytest.approx(6000)
    rec = json_log.batch_record(run, run.batches["C-2026-11"], "tc4", [])
    assert json_log.recompute_parts(rec) == pytest.approx(
        {p.id: p.ghg_gCO2e_per_MJ for p in run.batches["C-2026-11"].parts}
    )
    out = sealed.seal(run, tmp_path / "run")
    assert sealed.verify(out) == []
    reg = sealed.load_record(out)["results"]["purchases"]["register"]
    assert [p["written_off_kg"] for p in reg] == pytest.approx([0, 0, 6000])


# ---- each new rule fails when broken ---------------------------------------------------------


def _pos(pid: str, **update: Any) -> list[ProofOfSustainability]:
    return [p.model_copy(update=update) if p.id == pid else p for p in _decisions().pos]


def test_pos_above_threshold_fails() -> None:
    assert _failed(_with(pos=_pos("POS-C", ghg_gCO2e_per_MJ=28.3)), "pos_compliant") == ["POS-C"]


def test_pos_from_another_quarter_fails() -> None:
    run = _with(pos=_pos("POS-A", dispatch_date=dt.date(2026, 9, 30)))
    assert _failed(run, "pos_in_quarter") == ["POS-A"]


def test_allocation_beyond_quantity_fails() -> None:
    run = _with(pos=_pos("POS-A", quantity_kg=7000.0))
    assert _failed(run, "pos_within_quantity") == ["POS-A"]


def test_allocation_to_another_suppliers_delivery_fails() -> None:
    run = _with(pos=_pos("POS-B", supplier="Other Gas Ltd"))
    assert _failed(run, "pos_product") == ["POS-B"]


def test_allocation_beyond_metered_delivery_fails() -> None:
    d = _decisions()
    extra = PoSAllocation(pos="POS-C", flow="f_sup_liq", batch="L-2026-11", kg=10.0)
    # The extra credential splits L-2026-11 into parts (§12): leave the consignments out.
    run = _with(pos_allocations=[*d.pos_allocations, extra], consignments=[], aggregated_batches=[])
    assert [s for s in _failed(run, "pos_within_delivery")] == [
        f"f_sup_liq {run.batches['L-2026-11'].start.isoformat()}"
    ]


def test_allocation_must_name_a_batch_of_the_receiving_unit() -> None:
    bad = PoSAllocation(pos="POS-A", flow="f_sup_comp", batch="L-2026-11", kg=1.0)
    with pytest.raises(CalculationError, match="must name a batch of compressor"):
        _with(pos_allocations=[bad])


def test_unknown_pos_is_refused() -> None:
    bad = PoSAllocation(pos="POS-X", flow="f_sup_comp", batch="C-2026-11", kg=1.0)
    with pytest.raises(CalculationError, match="unknown PoS"):
        _with(pos_allocations=[bad])


# ---- credentials into a storage (§11.1 rule 3, decision 8) ----------------------------------


@cache
def _storage_plant() -> tuple[PlantConfig, Frame]:
    """Test plant 4 with the supplier also delivering H₂ into the H₂ buffer, and a CH₂
    supplier delivering into CH₂ storage (passed through to Customer B unprocessed)."""
    cfg, frame = _inputs()
    raw = cfg.model_dump(mode="json")
    raw["blocks"].append(
        {
            "id": "ch2_supplier",
            "name": "Purchased CH2",
            "kind": "rfnbo_supply",
            "supplier": "CH2 Co",
            "product": "CH2",
            "lhv_MJ_per_kg": 120.0,
        }
    )
    raw["flows"] += [
        {
            "id": "f_sup_buf",
            "source": "h2_supplier",
            "target": "h2_buffer",
            "material": "H2",
            "series": "supplier_to_buffer_kg",
        },
        {
            "id": "f_ch2sup",
            "source": "ch2_supplier",
            "target": "ch2_storage",
            "material": "CH2",
            "series": "ch2_supplier_kg",
        },
    ]
    cfg = PlantConfig.model_validate(raw)
    s = dict(frame.series)
    days = np.array(
        [(t.astimezone(cfg_tz()).month, t.astimezone(cfg_tz()).day) == (10, 2) for t in frame.index]
    )
    s["supplier_to_buffer_kg"] = np.where(days, 5.0, 0.0)
    s["ch2_supplier_kg"] = np.where(days, 10.0, 0.0)
    for name, add in (("h2_buffer_inventory_kg", 5.0), ("ch2_storage_inventory_kg", 10.0)):
        s[name] = frame.series[name] + np.cumsum(np.where(days, add, 0.0))
    return cfg, Frame(frame.index, s, frame.timezone, frame.quarter)


def cfg_tz() -> Any:
    from zoneinfo import ZoneInfo

    return ZoneInfo("Europe/Amsterdam")


def test_credentials_into_storage_are_ledger_batches() -> None:
    cfg, frame = _storage_plant()
    d = _decisions()
    q0, q1 = frame.index[0], frame.end
    pe = ProofOfSustainability(
        id="POS-E",
        supplier="H2 Trailers BV",
        product="H2",
        dispatch_date=dt.date(2026, 11, 5),
        quantity_kg=1000.0,
        ghg_gCO2e_per_MJ=2.0,
    )
    pd = ProofOfSustainability(
        id="POS-D",
        supplier="CH2 Co",
        product="CH2",
        dispatch_date=dt.date(2026, 10, 2),
        quantity_kg=1000.0,
        ghg_gCO2e_per_MJ=2.0,
    )
    allocs = [
        *d.pos_allocations,
        PoSAllocation(pos="POS-E", flow="f_sup_buf", start=q0, end=q1, kg=400.0),
        PoSAllocation(pos="POS-D", flow="f_ch2sup", start=q0, end=q1, kg=960.0),
    ]
    # POS-E in the buffer feeds December's liquefier batch in place of 400 kg of E-2026-12 (a
    # compliant credential: POS-C's part would not comply, §12).
    feed = [
        (
            a.model_copy(update={"kg": a.kg - 400.0})
            if (a.source, a.target) == ("E-2026-12", "L-2026-12")
            else a
        )
        for a in d.feed
    ]
    feed.append(FeedAllocation(source="POS-E", target="L-2026-12", kg=400.0))
    cb = ConsignmentSpec(
        id="CB2",
        output="consignment_b",
        volume_kg=900.0,
        ship_date=dt.date(2026, 12, 20),
        target_gCO2e_per_MJ=4.0,
    )
    aggs = [
        *d.aggregated_batches,
        AggregatedBatchSpec(id="CB2-POS", consignment="CB2", parts=[Part(batch="POS-D", kg=900.0)]),
    ]
    dec = d.model_copy(
        update={
            "pos": [*d.pos, pe, pd],
            "pos_allocations": allocs,
            "feed": feed,
            "consignments": [*d.consignments, cb],
            "aggregated_batches": aggs,
        }
    )
    run = calculate(cfg, frame, dec)
    assert run.failures == [], [str(f) for f in run.failures][:5]
    buf = run.line("h2_buffer", "POS-E")
    assert buf is not None and buf.origin == "purchased" and buf.month == "2026-11"
    assert buf.out_feed_kg == pytest.approx(400.0)
    line = run.line("ch2_storage", "POS-D")
    assert line is not None and line.month == "2026-10" and line.carried_kg == pytest.approx(60)
    assert run.pos("POS-E").unallocated_kg == pytest.approx(600)
    assert {r.storage: r.delivered_kg for r in run.reconciliation}["ch2_storage"] == 960
    f = next(f for f in run.batches["L-2026-12"].feed if f.source_batch == "POS-E")
    assert f.origin == "pos" and f.flow is None and run.feed_via(f, "L-2026-12") == "h2_buffer"
    # Passed-through RFNBO is never aggregated with RFNBO the plant processed.
    mixed = AggregatedBatchSpec(
        id="CB2-mixed",
        consignment="CB2",
        parts=[Part(batch="POS-D", kg=450.0), Part(batch="C-2026-10", kg=450.0)],
    )
    bad = calculate(
        cfg,
        frame,
        dec.model_copy(update={"aggregated_batches": [*d.aggregated_batches, mixed]}),
    )
    assert _failed(bad, "aggregated_same_source") == ["CB2-mixed"]


def test_deliveries_into_storage_balance_its_meter() -> None:
    cfg, frame = _storage_plant()
    assert [f for f in quality.check(cfg, frame) if f.code == "meter_balance"] == []


def test_storage_ranges_on_one_flow_may_not_overlap() -> None:
    cfg, frame = _storage_plant()
    d = _decisions()
    q0, mid, q1 = frame.index[0], frame.index[100], frame.end
    allocs = [
        *d.pos_allocations,
        PoSAllocation(pos="POS-C", flow="f_sup_buf", start=q0, end=q1, kg=100.0),
        PoSAllocation(pos="POS-C", flow="f_sup_buf", start=mid, end=q1, kg=100.0),
    ]
    run = calculate(cfg, frame, d.model_copy(update={"pos_allocations": allocs}))
    assert len(_failed(run, "pos_within_delivery")) == 2


# ---- the optimiser ---------------------------------------------------------------------------


@cache
def _optimised(case: str) -> OptimisationResult:
    cfg, frame = _inputs()
    d = _decisions()
    if case == "v1_spot":
        s = dict(frame.series)
        local = [t.astimezone(cfg_tz()) for t in frame.index]
        spot = np.array([60000.0 / 96 if (t.month, t.day) == (12, 29) else 0.0 for t in local])
        s["lh2_shipped_spot_kg"] = spot
        s["lh2_storage_inventory_kg"] = frame.series["lh2_storage_inventory_kg"] - np.cumsum(spot)
        frame = Frame(frame.index, s, frame.timezone, frame.quarter)
    return optimise(cfg, frame, d.consignments, pos=d.pos, compare=True)


def test_optimiser_matches_the_hand_calculation() -> None:
    res = _optimised("base")
    o, x = res.optimised, _expected()
    assert res.feasible and o.run.failures == []
    approx(o.objectives.carried_value_MJ, x["objectives"]["carried_value_MJ"], "carried value")
    assert o.objectives.carried_emissions_g <= x["objectives"]["carried_emissions_g"] * (1 + 1e-6)
    approx(o.objectives.purchased_emissions_g, x["objectives"]["purchased_emissions_g"], "PoS")
    for p in o.run.purchases:
        assert abs(p.allocated_kg - x["pos"][p.id]["allocated_kg"]) < 1e-3, p.id
    approx(o.objectives.pos_written_off_kg, 6000, "PoS written off")
    vb = res.value_bound()  # loose here: the buffer and the LH₂ it would become both count
    assert vb.upper_MJ >= o.objectives.carried_value_MJ - 1e-6
    assert res.baseline.run.failures == []
    assert o.objectives.key() >= res.baseline.objectives.key()


def test_optimiser_puts_no_credentials_into_product_written_off() -> None:
    res = _optimised("v1_spot")
    o, v1 = res.optimised, _expected()["variants"]["v1_spot"]
    assert res.feasible and o.run.failures == []
    approx(o.objectives.carried_value_MJ, v1["carried_value_MJ"], "V1 carried value")
    liq = sum(a.kg for a in o.decisions.pos_allocations if a.flow == "f_sup_liq")
    assert liq < 1e-3
    approx(o.objectives.pos_written_off_kg, v1["pos_written_off_kg"], "V1 PoS written off")
    # The baseline (first in, first out) puts credentials into the liquefier: worth less.
    assert res.baseline.objectives.carried_value_MJ < o.objectives.carried_value_MJ
