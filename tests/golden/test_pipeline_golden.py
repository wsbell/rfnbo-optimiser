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
    assert res.feasible is (case == "tc2")
    assert res.optimised.run.failures == []


def test_tc1_consignments_cannot_be_met_without_averaging() -> None:
    """§12: test case 1's workbook met C1 (limit 15 − 3) and C2 (limit 20 − 8) by averaging
    September NH₃ (6 gCO₂e/MJ) with Q4 NH₃ (14.09). Without averaging only the 300,000 kg of
    September NH₃ is at or below 12, short of either volume, so neither is met; the optimiser
    reports both and optimises the rest, carrying at least the workbook's value."""
    ref, _, _ = _run("tc1")
    res = _pipeline("tc1")
    assert [(s.consignment, s.achievable_kg) for s in res.shortfalls] == [
        ("C1", 300000.0),
        ("C2", 300000.0),
    ]
    assert all("only 300,000.0 kg is at or below 12" in s.reason for s in res.shortfalls)
    run = res.optimised.run
    assert run.consignments == []
    nh3 = run.ledger("nh3_storage")
    assert math.isclose(nh3.carried_kg, nh3.physical_kg, rel_tol=1e-9)
    want, got = objectives(ref), res.optimised.objectives
    assert not want.consignments_met
    assert got.carried_value_MJ >= want.carried_value_MJ * (1 - 1e-9)


def test_tc2_beats_the_regenerated_workbook() -> None:
    """More compliant RFNBO carried (final-product MJ) than the workbook decisions under §2.5
    and the carry rule, all consignments met."""
    ref, expected, _ = _run("tc2")
    assert expected["rule_2_5_applied"]
    want, got = objectives(ref), _pipeline("tc2").optimised.objectives
    assert got.consignments_met
    assert got.key() > want.key()
    assert got.carried_value_MJ > want.carried_value_MJ


def test_noncompliant_rfnbo_is_not_fed_downstream() -> None:
    """§12 decision 2: H₂ above 28.2 is still RFNBO but cannot be saved by blending, so the
    optimiser feeds none of it; every NH₃ part it makes complies."""
    run = _pipeline("tc2").optimised.run
    fed = {a.source for a in run.decisions.feed}
    assert fed and all(run.batches[b].compliant for b in fed if b in run.batches)
    nh3 = [b for b in run.batches.values() if b.unit == "nh3_synthesis" and b.rfnbo_kg > 0]
    assert nh3 and all(p.compliant for b in nh3 for p in b.parts)
    assert any(len(b.parts) > 1 for b in nh3)  # one part per source GHG
    assert any(b.rfnbo_share < 1 for b in nh3)  # short feed, pro-rated
    h2 = run.ledger("h2_storage")
    assert math.isclose(h2.carried_kg, h2.physical_kg, rel_tol=1e-9)


def test_compliant_same_month_h2_is_pooled_where_it_helps() -> None:
    """§12.1: stage B averages compliant H₂ of one month before it feeds NH₃ where that feeds
    more than separate parts could; every pool passes its checks, and the optimiser carries
    more than it did without averaging (26,508,856 MJ in phase 18)."""
    run = _pipeline("tc2").optimised.run
    pooled = [a for a in run.decisions.feed if a.pool]
    assert pooled
    for a in pooled:
        src = run.parts[a.source]
        assert src.compliant and a.pool.endswith(f"|{src.month}|own")
    names = {c.name for c in run.checks}
    assert {"feed_pool_same_source", "feed_pool_same_month", "feed_pool_compliant_only"} <= names
    assert _pipeline("tc2").optimised.objectives.carried_value_MJ > 26508856
    # Test case 1: December's 17.83 gCO₂e/MJ H₂ is above NH₃'s feed limit alone (13.45) but
    # compliant, so it is fed pooled with December's 1.17 H₂.
    tc1 = _pipeline("tc1").optimised.run
    (high,) = [
        b
        for b in tc1.batches.values()
        if b.unit == "electrolyser" and b.compliant and (b.ghg_gCO2e_per_MJ or 0) > 13.45
    ]
    fed = [a for a in tc1.decisions.feed if a.source == high.id]
    assert fed and all(a.pool for a in fed)
    assert math.isclose(sum(a.kg for a in fed), high.rfnbo_kg, rel_tol=1e-6)


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
    ("case", "volume", "shipped"), [("tc1", 400000.0, 17.0924731182796), ("tc2", 25000.0, 9.0)]
)
def test_minimise_mode_finds_the_lowest_ghg(case: str, volume: float, shipped: float) -> None:
    """Workbook variant V1: one volume for Customer A, no target. In test case 1 the 100,000 kg
    beyond September's stock is Q4 NH₃ at 14.09, which sets the shipped GHG (§12)."""
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
    C1 gets 0 kg; C2 cannot be met without averaging either (§12)."""
    ref, _, _ = _run("tc1")
    batching, _ = _batched("tc1")
    cons = [
        c.model_copy(update={"target_gCO2e_per_MJ": 8.0}) if c.id == "C1" else c
        for c in ref.decisions.consignments
    ]
    res = optimise(ref.config, ref.frame, cons, batching=batching)
    assert not res.feasible
    short, c2 = res.shortfalls
    assert short.consignment == "C1" and short.achievable_kg == 0.0
    assert "no compliant stock at or below 5" in short.reason
    assert c2.consignment == "C2" and c2.achievable_kg == 300000.0
    assert res.optimised.run.consignments == []
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
