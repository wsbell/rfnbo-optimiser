"""Phase 6 acceptance on the workbook data.

Stage A isolates dark intervals with no surplus, averages when surplus exists, and keeps
electrolyser batches within one clock hour from 2030. Every proposal is re-run through the
calculator.
"""

from __future__ import annotations

import math
from functools import cache

import pytest

from rfnbo.calc import RunResult, calculate
from rfnbo.config.schema import ProcessingUnit
from rfnbo.data import calendar as cal
from rfnbo.data.quality import Frame
from rfnbo.model.entities import Decisions
from rfnbo.optimise import batching
from tests.golden.test_golden import _run

BATCH_CHECKS = {"batch_cover", "batch_within_month", "batch_max_length"}


@cache
def _optimised(case: str) -> tuple[batching.BatchingResult, RunResult]:
    run, _, _ = _run(case)
    res = batching.optimise_batching(run.config, run.frame)
    return res, calculate(run.config, run.frame, Decisions(batches=res.batches))


@cache
def _december_2030(case: str) -> tuple[batching.BatchingResult, RunResult]:
    """The workbook's variant V3: December data re-dated to December 2030."""
    run, _, _ = _run(case)
    f = run.frame
    dec = [i for i, t in enumerate(f.index) if cal.month_key(t, f.timezone) == "2026-12"]
    start, end = cal.month_bounds(2030, 12, f.timezone)
    index = tuple(cal.intervals(start, end))
    assert len(index) == len(dec)
    frame = Frame(
        index, {k: v[dec[0] : dec[-1] + 1] for k, v in f.series.items()}, f.timezone, "2030-Q4"
    )
    res = batching.optimise_batching(run.config, frame)
    return res, calculate(run.config, frame, Decisions(batches=res.batches))


def electrolyser(run: RunResult) -> list:  # type: ignore[type-arg]
    return sorted(
        (b for b in run.batches.values() if b.unit == "electrolyser"), key=lambda b: b.start
    )


@pytest.mark.parametrize("case", ["tc1", "tc2"])
def test_proposals_pass_the_calculator_batch_checks(case: str) -> None:
    _, run = _optimised(case)
    assert not [f for f in run.failures if f.name in BATCH_CHECKS]


def test_tc1_reproduces_the_workbook_boundaries() -> None:
    """Oct and Nov: one batch each (surplus covers the dark spell). Dec: dark spells and
    the 90% day isolated, exactly as the workbook's expected batching."""
    workbook, _, _ = _run("tc1")
    _, run = _optimised("tc1")
    got = [(b.start, b.end) for b in electrolyser(run)]
    want = [(b.start, b.end) for b in electrolyser(workbook)]
    assert got == want
    shares = [b.rfnbo_share for b in electrolyser(run)]
    assert shares == [1.0, 1.0, 1.0, 0.0, 1.0, 0.0, 1.0, pytest.approx(0.9), 1.0]


def test_tc1_nh3_is_one_batch_per_month() -> None:
    _, run = _optimised("tc1")
    nh3 = sorted(b.month for b in run.batches.values() if b.unit == "nh3_synthesis")
    assert nh3 == ["2026-10", "2026-11", "2026-12"]


def test_downstream_limit_matches_the_workbook() -> None:
    """Workbook Parameters: max blended H₂ feed GHG for NH₃ ≤ 28.2 = 13.4513108614232."""
    run, _, _ = _run("tc2")
    unit = run.config.block("electrolyser")
    assert isinstance(unit, ProcessingUnit)
    down = batching.downstream(run.config, run.frame, unit)
    assert down is not None
    assert math.isclose(down.limit_gCO2e_per_MJ, 13.4513108614232, rel_tol=1e-9)


def test_tc2_beats_the_workbook_batching() -> None:
    """Under the same measure (RFNBO H₂ that NH₃ can take while compliant), stage A's
    batching does better than the workbook's reference batching."""
    workbook, _, _ = _run("tc2")
    res, _ = _optimised("tc2")
    cfg, frame = workbook.config, workbook.frame
    unit = cfg.block("electrolyser")
    assert isinstance(unit, ProcessingUnit)
    us = batching.unit_series(cfg, frame, unit)
    down = batching.downstream(cfg, frame, unit)
    limit = batching.product_limit(cfg, frame, unit)
    slices, _ = batching.windows(cfg, frame, unit)
    threshold = cfg.settings.rfnbo_threshold_gCO2e_per_MJ
    reference = batching.score_of(
        us,
        [(b.start_index, b.end_index) for b in electrolyser(workbook)],
        down,
        limit,
        slices,
        threshold,
    )
    ours = res.units[0].score
    # Months are averaged apart (§12.1): 27.67 million MJ against the workbook's 27.27 million.
    assert ours.feedable_MJ > reference.feedable_MJ * 1.01


def test_stage_a_metrics_agree_with_the_calculator() -> None:
    res, run = _optimised("tc2")
    unit = run.config.block("electrolyser")
    assert isinstance(unit, ProcessingUnit)
    us = batching.unit_series(run.config, run.frame, unit)
    for b in electrolyser(run):
        import numpy as np

        rf, attributed, mj = us.metrics(np.array([b.start_index]), b.end_index)
        assert math.isclose(float(rf[0]), b.rfnbo_MJ, rel_tol=1e-9, abs_tol=1e-6)
        assert math.isclose(float(mj[0]), b.energy_MJ, rel_tol=1e-12)
        if b.rfnbo_MJ > 0 and b.ghg_gCO2e_per_MJ is not None:
            assert math.isclose(
                float(attributed[0]) / float(rf[0]), b.ghg_gCO2e_per_MJ, rel_tol=1e-9
            )
    assert res.units[0].score.batches == len(electrolyser(run))


def test_hourly_correlation_from_2030_matches_the_workbook_variant() -> None:
    """Workbook V3 (test case 1): 745 batches, 3 at 0%, 24 part-RFNBO, 147,870 kg RFNBO H₂,
    every batch within one clock hour; NH₃ still one batch for the month."""
    _, run = _december_2030("tc1")
    el = electrolyser(run)
    assert not [f for f in run.failures if f.name in BATCH_CHECKS]
    assert len(el) == 745
    assert max(b.intervals for b in el) <= 4
    assert sum(b.rfnbo_share == 0 for b in el) == 3
    assert sum(0 < b.rfnbo_share < 1 for b in el) == 24
    assert math.isclose(sum(b.rfnbo_kg for b in el), 147870.0, rel_tol=1e-12)
    assert sum(b.unit == "nh3_synthesis" for b in run.batches.values()) == 1


def test_hourly_batches_never_exceed_four_intervals_on_variable_wind() -> None:
    _, run = _december_2030("tc2")
    assert not [f for f in run.failures if f.name in BATCH_CHECKS]
    assert max(b.intervals for b in electrolyser(run)) <= 4


def test_cli_batch_writes_a_decision_set(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    from pathlib import Path

    from rfnbo.cli import main

    root = Path(__file__).resolve().parents[2]
    out = tmp_path / "batches.json"
    code = main(
        [
            "batch",
            "--config",
            str(root / "examples" / "test_plant.json"),
            "--source",
            str(root / "examples" / "test_workbook_source.json"),
            "--data",
            str(root / "tests" / "golden" / "workbooks" / "RFNBO_optimiser_test_Q4_2026.xlsx"),
            "--quarter",
            "2026-Q4",
            "--out",
            str(out),
        ]
    )
    assert code == 0, capsys.readouterr().err
    decisions = Decisions.model_validate_json(out.read_text(encoding="utf-8"))
    assert len(decisions.batches) == 9 + 3
    assert "electrolyser: 9 batches (6 at 100%, 1 part, 2 at 0%)" in capsys.readouterr().out
