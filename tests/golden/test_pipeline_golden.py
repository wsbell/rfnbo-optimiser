"""Phase 7 acceptance: the full pipeline matches or beats the (regenerated) workbook results
on the objectives (§2.8 as revised 2026-10-17), and every constraint check passes."""

from __future__ import annotations

import datetime as dt
import math
from functools import cache
from pathlib import Path

import pytest

from rfnbo.calc import calculate
from rfnbo.config.schema import ProcessingUnit
from rfnbo.model.entities import ConsignmentSpec, Decisions
from rfnbo.optimise import allocate
from rfnbo.optimise.pipeline import OptimisationResult, objectives, optimise
from rfnbo.trace import run as sealed
from tests.golden.test_batching_golden import _optimised as _batched
from tests.golden.test_golden import _run


@cache
def _pipeline(case: str) -> OptimisationResult:
    ref, _, _ = _run(case)
    batching, _ = _batched(case)
    return optimise(ref.config, ref.frame, ref.decisions.consignments, batching=batching)


@pytest.mark.parametrize("case", ["tc1", "tc2"])
def test_every_check_passes(case: str) -> None:
    res = _pipeline(case)
    assert res.feasible
    assert res.optimised.run.failures == []


def test_tc1_keeps_h2_rather_than_nh3_that_is_written_off() -> None:
    """Objective 2 values carried RFNBO by its final-product yield (decided 2026-10-17). In
    test case 1 the December NH₃ batch is written off in full (spot sale), so the workbook's
    feed into it gains nothing; the optimiser fills H₂ storage with RFNBO up to its measured
    inventory instead. Everything else matches the workbook."""
    ref, _, _ = _run("tc1")
    want, got = objectives(ref), _pipeline("tc1").optimised.objectives
    run = _pipeline("tc1").optimised.run
    assert got.consignments_met and want.consignments_met
    h2 = run.ledger("h2_storage")
    assert math.isclose(h2.carried_kg, h2.physical_kg, rel_tol=1e-9)
    w = run.storage_weights["h2_storage"].weight
    assert w is not None and math.isclose(w, 2429900 * 18.6 / (432522.2 * 120), rel_tol=1e-9)
    gain = (h2.physical_kg - ref.ledger("h2_storage").carried_kg) * 120 * w
    assert math.isclose(got.carried_value_MJ, want.carried_value_MJ + gain, rel_tol=1e-9)
    assert got.key() > want.key()
    nh3 = run.ledger("nh3_storage")
    assert math.isclose(nh3.carried_kg, ref.ledger("nh3_storage").carried_kg, rel_tol=1e-9)
    assert math.isclose(
        got.carried_ghg["nh3_storage"] or 0, want.carried_ghg["nh3_storage"] or 0, rel_tol=1e-6
    )


def test_tc2_beats_the_regenerated_workbook() -> None:
    """More compliant RFNBO carried (final-product MJ) than the workbook decisions under §2.5
    and the carry rule, all consignments met."""
    ref, expected, _ = _run("tc2")
    assert expected["rule_2_5_applied"]
    want, got = objectives(ref), _pipeline("tc2").optimised.objectives
    assert got.consignments_met
    assert got.key() > want.key()
    assert got.carried_value_MJ > want.carried_value_MJ


def test_noncompliant_rfnbo_is_blended_into_compliant_nh3() -> None:
    """§2.5: H₂ above 28.2 is still RFNBO, fed downstream and saved by blending."""
    run = _pipeline("tc2").optimised.run
    fed = {a.source for a in run.decisions.feed}
    assert any(not run.batches[b].compliant for b in fed if b in run.batches)
    nh3 = [b for b in run.batches.values() if b.unit == "nh3_synthesis" and b.rfnbo_kg > 0]
    assert nh3 and all(b.compliant for b in nh3)
    assert any(b.rfnbo_share < 1 for b in nh3)  # short feed, pro-rated
    # December NH₃ would be written off in full (spot sale), so it gets no RFNBO feed: the H₂
    # stays in storage, worth its downstream yield, up to measured inventory.
    dec = run.batches["nh3_synthesis-2026-12"]
    assert dec.rfnbo_kg == 0
    h2 = run.ledger("h2_storage")
    assert math.isclose(h2.carried_kg, h2.physical_kg, rel_tol=1e-9)


@pytest.mark.parametrize("case", ["tc1", "tc2"])
def test_optimised_is_at_least_as_good_as_the_baseline(case: str) -> None:
    res = _pipeline(case)
    assert res.optimised.objectives.key() >= res.baseline.objectives.key()


def test_stage_b_reaches_the_stage_a_bound() -> None:
    """Stage B's most-output LP reaches stage A's relaxation exactly. (The optimised run then
    gives some of that output up where it would only be written off.)"""
    res = _pipeline("tc2")
    bound = res.bound()
    assert bound is not None
    cfg = res.optimised.run.config
    plain = calculate(cfg, res.optimised.run.frame, Decisions(batches=res.batching.batches))
    unit = next(u for u in cfg.blocks_of(ProcessingUnit) if not u.electricity_adds_to_lhv)
    plan = allocate.stage_b(cfg, plain, unit)
    assert math.isclose(plan.rfnbo_kg, bound.upper_kg, rel_tol=1e-6)
    assert bound.achieved_kg <= bound.upper_kg * (1 + 1e-9)


@pytest.mark.parametrize(
    ("case", "volume", "shipped"), [("tc1", 400000.0, 11.0231182795699), ("tc2", 25000.0, 9.0)]
)
def test_minimise_mode_finds_the_lowest_ghg(case: str, volume: float, shipped: float) -> None:
    """Workbook variant V1: one volume for Customer A, no target."""
    ref, _, _ = _run(case)
    batching, _ = _batched(case)
    res = optimise(
        ref.config,
        ref.frame,
        [
            ConsignmentSpec(
                id="V1", output="consignment_a", volume_kg=volume, ship_date=dt.date(2026, 11, 15)
            )
        ],
        batching=batching,
    )
    assert res.optimised.run.failures == []
    got = res.optimised.run.consignment("V1").shipped_ghg_gCO2e_per_MJ
    assert got is not None and math.isclose(got, shipped, rel_tol=1e-6)


def test_infeasible_target_reports_the_binding_constraint() -> None:
    """Workbook variant V2: C1 target cut to 8.0. Nothing is below 8 − 3 = 5 gCO₂e/MJ, so
    C1 gets 0 kg; C2 is still met."""
    ref, _, _ = _run("tc1")
    batching, _ = _batched("tc1")
    cons = [
        c.model_copy(update={"target_gCO2e_per_MJ": 8.0}) if c.id == "C1" else c
        for c in ref.decisions.consignments
    ]
    res = optimise(ref.config, ref.frame, cons, batching=batching)
    assert not res.feasible
    (short,) = res.shortfalls
    assert short.consignment == "C1" and short.achievable_kg == 0.0
    assert "no compliant stock at or below 5" in short.reason
    assert [c.id for c in res.optimised.run.consignments] == ["C2"]
    assert res.optimised.run.failures == []


def test_same_inputs_give_the_same_decisions() -> None:
    ref, _, _ = _run("tc1")
    batching, _ = _batched("tc1")
    again = optimise(ref.config, ref.frame, ref.decisions.consignments, batching=batching)
    assert again.optimised.decisions == _pipeline("tc1").optimised.decisions


def test_sealed_optimised_run_records_the_solver(tmp_path: Path) -> None:
    res = _pipeline("tc1")
    d = sealed.seal(res.optimised.run, tmp_path / "opt")
    solver = sealed.load_record(d)["environment"]["solver"]
    assert solver["name"] == "HiGHS" and solver["version"] and solver["options"]
    assert sealed.verify(d) == []
