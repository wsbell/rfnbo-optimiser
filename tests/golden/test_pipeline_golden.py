"""Phase 7 acceptance: the full pipeline matches or beats the (regenerated) workbook results
on the §2.8 objectives, and every constraint check passes."""

from __future__ import annotations

import datetime as dt
import math
from functools import cache
from pathlib import Path

import pytest

from rfnbo.model.entities import ConsignmentSpec
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


def test_tc1_matches_the_workbook_on_every_objective() -> None:
    ref, _, _ = _run("tc1")
    want, got = objectives(ref), _pipeline("tc1").optimised.objectives
    assert got.consignments_met and want.consignments_met
    assert math.isclose(got.final_rfnbo_kg, want.final_rfnbo_kg, rel_tol=1e-9)
    assert got.carried_emissions_g <= want.carried_emissions_g * (1 + 1e-6)
    for store in ("h2_storage", "nh3_storage"):
        assert math.isclose(got.carried_ghg[store] or 0, want.carried_ghg[store] or 0, rel_tol=1e-6)


def test_tc2_beats_the_regenerated_workbook() -> None:
    """More compliant RFNBO NH₃ than the workbook decisions under §2.5, all consignments met."""
    ref, expected, _ = _run("tc2")
    assert expected["rule_2_5_applied"]
    want, got = objectives(ref), _pipeline("tc2").optimised.objectives
    assert got.consignments_met
    assert got.final_rfnbo_kg > want.final_rfnbo_kg * 1.07


def test_noncompliant_rfnbo_is_blended_into_compliant_nh3() -> None:
    """§2.5: H₂ above 28.2 is still RFNBO, fed downstream and saved by blending."""
    run = _pipeline("tc2").optimised.run
    fed = {a.source for a in run.decisions.feed}
    assert any(not run.batches[b].compliant for b in fed if b in run.batches)
    nh3 = [b for b in run.batches.values() if b.unit == "nh3_synthesis"]
    assert all(b.compliant and b.rfnbo_kg > 0 for b in nh3)
    assert all(b.rfnbo_share < 1 for b in nh3)  # short feed, pro-rated


@pytest.mark.parametrize("case", ["tc1", "tc2"])
def test_optimised_is_at_least_as_good_as_the_baseline(case: str) -> None:
    res = _pipeline(case)
    assert res.optimised.objectives.key() >= res.baseline.objectives.key()


def test_stage_b_reaches_the_stage_a_bound() -> None:
    bound = _pipeline("tc2").bound()
    assert bound is not None and bound.gap < 1e-6


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
