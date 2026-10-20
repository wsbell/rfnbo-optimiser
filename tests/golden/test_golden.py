"""Phase 4 acceptance: given the workbook decisions, reproduce the workbook values to 1e-6.

Test case 2 predates the non-compliant RFNBO rule (§2.5); its fixtures were regenerated with
``export_workbook.py --rule-2-5``. Both workbooks predate the averaging rules (§12, §12.1): their
fixtures were regenerated with ``--rule-12``, which splits NH₃ batches into parts (compliant H₂
of one month averaged, non-compliant H₂ apart) and
re-derives the consignment allocations (see ``superseded`` in the expected files).
"""

from __future__ import annotations

import json
import math
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from rfnbo.calc import RunResult, calculate
from rfnbo.calc.consign import compute_consignments
from rfnbo.config import load_config
from rfnbo.data import ingest, quality
from rfnbo.model.entities import (
    AggregatedBatchSpec,
    ConsignmentSpec,
    Decisions,
    Part,
)

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = Path(__file__).resolve().parent
CASES = {
    "tc1": "RFNBO_optimiser_test_Q4_2026.xlsx",
    "tc2": "RFNBO_optimiser_test_Q4_2026_undersized.xlsx",
}


@cache
def _run(case: str) -> tuple[RunResult, dict[str, Any], list[quality.QualityFlag]]:
    cfg = load_config(ROOT / "examples" / "test_plant.json")
    source = ingest.load_source_spec(ROOT / "examples" / "test_workbook_source.json")
    raw = ingest.load(GOLDEN / "workbooks" / CASES[case], source)
    frame, flags = quality.prepare(cfg, raw, 2026, 4)
    decisions = Decisions.model_validate_json(
        (GOLDEN / f"{case}.decisions.json").read_text(encoding="utf-8")
    )
    expected = json.loads((GOLDEN / f"{case}.expected.json").read_text(encoding="utf-8"))
    return calculate(cfg, frame, decisions), expected, flags


def approx(actual: float | None, expected: float | None, what: str) -> None:
    assert actual is not None and expected is not None, what
    assert math.isclose(actual, expected, rel_tol=1e-6, abs_tol=1e-6), (
        f"{what}: got {actual!r}, workbook {expected!r}"
    )


def emissions(run: RunResult, batch: str, source: str) -> float:
    return sum(x.emissions_g for x in run.batches[batch].inputs if x.source == source)


@pytest.fixture(params=sorted(CASES))
def case(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def test_data_is_clean(case: str) -> None:
    run, expected, flags = _run(case)
    assert flags == []
    assert len(run.frame) == expected["intervals"] == 8836


def test_every_check_passes(case: str) -> None:
    """Every check passes except, in test case 1, the consignment targets: they were met only by
    averaging September and October NH₃, and no part of October NH₃ is low enough (§12)."""
    run, expected, _ = _run(case)
    missed = sorted(
        c for c, e in expected["consignments"].items() if e["shipped_ghg"] > e["target"] + 1e-9
    )
    assert missed == (["C1", "C2"] if case == "tc1" else [])
    failures = [(f.name, f.subject) for f in run.failures]
    assert failures == [("consignment_target", c) for c in missed], "\n".join(
        str(f) for f in run.failures[:20]
    )


def test_electrolyser_batches(case: str) -> None:
    run, expected, _ = _run(case)
    assert {b for b, r in run.batches.items() if r.unit == "electrolyser"} == set(
        expected["h2_batches"]
    )
    for bid, e in expected["h2_batches"].items():
        b = run.batches[bid]
        assert b.month == e["month"] and b.intervals == e["intervals"], bid
        assert b.compliant is e["compliant"], bid
        for field, value in (
            ("renewable_available_kWh", b.renewable_available_kWh),
            ("electricity_kWh", b.electricity_kWh),
            ("renewable_matched_kWh", b.renewable_matched_kWh),
            ("rfnbo_share", b.rfnbo_share),
            ("product_kg", b.product_kg),
            ("energy_MJ", b.energy_MJ),
            ("water_emissions_g", emissions(run, bid, "water")),
            ("grid_emissions_g", emissions(run, bid, "grid_power")),
            ("ghg", b.ghg_gCO2e_per_MJ),
            ("rfnbo_kg", b.rfnbo_kg),
            ("rfnbo_MJ", b.rfnbo_MJ),
        ):
            approx(value, e[field], f"{bid} {field}")


def test_nh3_batches(case: str) -> None:
    run, expected, _ = _run(case)
    for bid, e in expected["nh3_batches"].items():
        b = run.batches[bid]
        assert b.month == e["month"] and b.intervals == e["intervals"], bid
        assert b.compliant is e["compliant"], bid
        for field, value in (
            ("product_kg", b.product_kg),
            ("feed_consumed_kg", b.feed_consumed_kg),
            ("rfnbo_feed_kg", b.rfnbo_feed_kg),
            ("feed_ghg", b.feed_ghg_gCO2e_per_MJ),
            ("rfnbo_share", b.rfnbo_share),
            ("feed_emissions_g", b.feed_emissions_g),
            ("electricity_kWh", b.electricity_kWh),
            ("electricity_emissions_g", emissions(run, bid, "grid_power")),
            ("rfnbo_kg", b.rfnbo_kg),
            ("rfnbo_MJ", b.rfnbo_MJ),
        ):
            approx(value, e[field], f"{bid} {field}")
        if e["ghg"] is None:
            assert b.ghg_gCO2e_per_MJ is None, bid
        else:
            approx(b.ghg_gCO2e_per_MJ, e["ghg"], f"{bid} ghg")
        parts = {p.id: p for p in b.parts}
        assert set(parts) == set(e["parts"]), bid
        for pid, x in e["parts"].items():
            part = parts[pid]
            assert part.compliant is x["compliant"], pid
            assert sorted(part.sources) == sorted(x["sources"]), pid
            approx(part.ghg_gCO2e_per_MJ, x["ghg"], f"{pid} ghg")
            approx(part.kg, x["kg"], f"{pid} kg")
            approx(part.share, x["share"], f"{pid} share")


def test_h2_ledger_and_reconciliation(case: str) -> None:
    run, expected, _ = _run(case)
    led, e = run.ledger("h2_storage"), expected["h2_ledger"]
    for field in (
        "opening_kg",
        "produced_kg",
        "out_feed_kg",
        "closing_before_kg",
        "physical_kg",
        "write_off_kg",
        "carried_kg",
    ):
        approx(getattr(led, field), e[field], f"H2 ledger {field}")
    if e.get("carried_ghg") is not None:
        approx(led.carried_ghg_gCO2e_per_MJ, e["carried_ghg"], "H2 carried GHG")

    rec, r = (
        next(x for x in run.reconciliation if x.storage == "h2_storage"),
        expected["h2_reconciliation"],
    )
    approx(rec.opening_physical_kg, r["opening_physical_kg"], "H2 opening physical")
    approx(rec.produced_kg, r["produced_kg"], "H2 produced")
    approx(dict(rec.outflows_kg)["f_h2s_nh3"], r["consumed_kg"], "H2 consumed")
    approx(rec.expected_kg, r["expected_kg"], "H2 expected closing")
    approx(rec.measured_kg, r["measured_kg"], "H2 measured closing")
    approx(rec.implied_loss_kg, r["implied_loss_kg"], "H2 implied loss")


def test_nh3_ledger_and_reconciliation(case: str) -> None:
    run, expected, _ = _run(case)
    led, e = run.ledger("nh3_storage"), expected["nh3_ledger"]
    lines = {x.batch: x for x in led.lines}
    assert set(lines) == set(e["lines"])
    for bid, x in e["lines"].items():
        line = lines[bid]
        assert line.month == x["month"], bid
        for field, value in (
            ("ghg", line.ghg_gCO2e_per_MJ),
            ("in_kg", line.in_kg),
            ("out_consignment_kg", line.out_consignment_kg),
            ("closing_before_kg", line.closing_before_kg),
            ("write_off_kg", line.write_off_kg),
            ("carried_kg", line.carried_kg),
        ):
            approx(value, x[field], f"NH3 ledger {bid} {field}")
    approx(led.physical_kg, e["physical_kg"], "NH3 physical")
    approx(led.write_off_kg, e["write_off_kg"], "NH3 write-off")
    approx(led.carried_ghg_gCO2e_per_MJ, e["carried_ghg"], "NH3 carried GHG")

    rec, r = (
        next(x for x in run.reconciliation if x.storage == "nh3_storage"),
        expected["nh3_reconciliation"],
    )
    out = dict(rec.outflows_kg)
    approx(rec.opening_physical_kg, r["opening_physical_kg"], "NH3 opening physical")
    approx(rec.produced_kg, r["produced_kg"], "NH3 produced")
    approx(out["f_nh3s_a"], r["shipped_c1_kg"], "shipped C1")
    approx(out["f_nh3s_b"], r["shipped_c2_kg"], "shipped C2")
    approx(out["f_nh3s_spot"], r["shipped_spot_kg"], "spot sale")
    approx(rec.expected_kg, r["expected_kg"], "NH3 expected closing")
    approx(rec.measured_kg, r["measured_kg"], "NH3 measured closing")
    approx(rec.implied_loss_kg, r["implied_loss_kg"], "NH3 implied loss")


def test_consignments(case: str) -> None:
    run, expected, _ = _run(case)
    for cid, e in expected["consignments"].items():
        c = run.consignment(cid)
        approx(c.volume_kg, e["volume_kg"], f"{cid} volume")
        approx(c.distribution_adder_gCO2e_per_MJ, e["distribution_adder"], f"{cid} adder")
        parts = {p.batch: p.kg for a in c.aggregated for p in a.parts}
        assert set(parts) == set(e["parts"]), cid
        for b, kg in e["parts"].items():
            approx(parts[b], kg, f"{cid} {b}")
        approx(c.max_ghg_gCO2e_per_MJ, e["highest_ghg"], f"{cid} highest part GHG")
        approx(c.shipped_ghg_gCO2e_per_MJ, e["shipped_ghg"], f"{cid} shipped GHG")


def test_minimise_mode_variant(case: str) -> None:
    """Variant V1: the hand-calculated lowest shipped GHG for one volume, no target."""
    run, expected, _ = _run(case)
    v = expected["variant_minimise"]
    parts = sorted(v["parts"].items())
    decisions = Decisions(
        batches=[],
        consignments=[
            ConsignmentSpec(
                id="V1",
                output="consignment_a",
                volume_kg=v["volume_kg"],
                ship_date=run.decisions.consignments[0].ship_date,
            )
        ],
        aggregated_batches=[
            AggregatedBatchSpec(id=f"V1-{b}", consignment="V1", parts=[Part(batch=b, kg=kg)])
            for b, kg in parts
            if kg
        ],
    )
    lines = {(led.storage, x.batch): x for led in run.ledgers for x in led.lines}
    (c,) = compute_consignments(run.config, decisions, lines)
    approx(c.allocated_kg, v["volume_kg"], "V1 volume")
    approx(c.shipped_ghg_gCO2e_per_MJ, v["shipped_ghg"], "V1 shipped GHG")


def test_rule_2_5_regeneration() -> None:
    """Test case 2 under §2.5: non-compliant RFNBO H₂ is in the ledger and written off. It
    cannot be carried into the next quarter, and under the workbook's decisions all of the H₂
    left at quarter end (108,729.2 kg) is non-compliant, so all of it is written off."""
    run, expected, _ = _run("tc2")
    assert expected["rule_2_5_applied"]
    noncompliant = [
        b
        for b in run.batches.values()
        if b.unit == "electrolyser" and b.is_rfnbo and not b.compliant
    ]
    assert len(noncompliant) == len(expected["superseded"]["h2_rfnbo_kg"]) == 88
    led = run.ledger("h2_storage")
    approx(led.write_off_kg, 108729.2, "H2 write-off")
    approx(led.carried_kg, 0.0, "H2 carried")
    assert all(x.carried_kg <= 1e-6 for x in led.lines if not x.compliant)
    assert run.passed, run.failures
