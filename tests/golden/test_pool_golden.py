"""Phase 13 acceptance: stage A splits a shared renewable pool between the electrolyser and the
conditioning units as test case 3's hand calculation does, never over-allocates it, and
leaves stage A unchanged on test cases 1 and 2 (their pools have one user)."""

from __future__ import annotations

import datetime as dt
import json
from functools import cache
from pathlib import Path
from typing import Any

import numpy as np

from rfnbo.calc import RunResult, calculate
from rfnbo.config.schema import PlantConfig
from rfnbo.data.quality import Frame
from rfnbo.model.entities import Decisions
from rfnbo.optimise.baseline import monthly_split
from rfnbo.optimise.batching import BatchingResult, optimise_batching
from tests.golden import tc3_reference as R
from tests.golden.test_tc3_calc import _inputs, approx
from tests.golden.test_tc3_workbook import SERIES

GOLDEN = Path(__file__).resolve().parent


def _frame(data: R.Data) -> Frame:
    series = {n: np.asarray(data.s[r], dtype=np.float64) for n, r in SERIES.items()}
    return Frame(tuple(data.utc), series, "Europe/Amsterdam", "2026-Q4")


@cache
def _case(name: str) -> tuple[PlantConfig, Frame, BatchingResult, RunResult]:
    cfg, frame, _ = _inputs()
    if name == "v1_hourly":  # the same quarter under the hourly rule (as if dated 2030)
        settings = cfg.settings.model_copy(update={"hourly_correlation_from": dt.date(2026, 1, 1)})
        cfg = cfg.model_copy(update={"settings": settings})
    elif name == "v2_shortfall":
        frame = _frame(R.build_data(dec_renewable_kWh=R.V2_DEC_RENEWABLE_KWH, spot=False))
    res = optimise_batching(cfg, frame)
    run = calculate(
        cfg, frame, Decisions(batches=res.batches, renewable_allocations=res.renewable_allocations)
    )
    return cfg, frame, res, run


@cache
def _variants() -> dict[str, Any]:
    out: dict[str, Any] = json.loads((GOLDEN / "tc3.expected.json").read_text("utf-8"))["variants"]
    return out


def _by_unit(run: RunResult, unit: str) -> list[Any]:
    return sorted((b for b in run.batches.values() if b.unit == unit), key=lambda b: b.start)


def test_base_case_covers_every_unit_with_one_batch_a_month() -> None:
    _, _, res, run = _case("base")
    assert not [f for f in run.failures if f.name == "renewable_pool"]
    assert {a.unit for a in res.renewable_allocations} == {
        "electrolyser",
        "liquefier",
        "compressor",
    }
    for unit in ("electrolyser", "liquefier", "compressor"):
        batches = _by_unit(run, unit)
        assert len(batches) == 3, unit
        for b in batches:
            approx(b.renewable_matched_kWh, b.electricity_kWh, f"{b.id} fully matched")
    assert all(b.rfnbo_share == 1.0 for b in _by_unit(run, "electrolyser"))


def test_hourly_variant() -> None:
    _, _, _, run = _case("v1_hourly")
    v1 = _variants()["v1_hourly"]
    e = _by_unit(run, "electrolyser")
    assert len(e) == v1["electrolyser_batches"] == 2209
    assert sum(b.rfnbo_share == 0 for b in e) == v1["zero_share_batches"]
    approx(sum(b.rfnbo_kg for b in e), v1["rfnbo_h2_kg"], "RFNBO H2")
    for unit in ("liquefier", "compressor"):
        for b in _by_unit(run, unit):
            approx(b.renewable_matched_kWh, b.electricity_kWh, f"{b.id} fully matched")
    assert not [f for f in run.failures if f.name == "renewable_pool"]


def test_shortfall_gives_the_liquefier_just_enough_to_comply() -> None:
    """December renewable 2,500 kWh per interval: the liquefier takes exactly the least it
    needs to comply, from the end of December; the electrolyser isolates what it gave up and
    makes at least the hand calculation's RFNBO."""
    _, _, res, run = _case("v2_shortfall")
    v2 = _variants()["v2_shortfall"]
    (liq,) = [b for b in _by_unit(run, "liquefier") if b.month == "2026-12"]
    approx(liq.renewable_matched_kWh, v2["least_liquefier_renewable_kWh"], "liquefier renewable")
    (comp,) = [b for b in _by_unit(run, "compressor") if b.month == "2026-12"]
    assert comp.renewable_matched_kWh == 0  # complies on grid power
    dec = [b for b in _by_unit(run, "electrolyser") if b.month == "2026-12"]
    assert sum(b.rfnbo_kg for b in dec) >= v2["december_rfnbo_h2_kg"] - 1e-6
    assert sum(b.intervals for b in dec if b.rfnbo_share == 0) == v2["zero_share_intervals"]
    assert any("liquefier 2026-12 takes" in line for line in res.log)
    assert not [f for f in run.failures if f.name == "renewable_pool"]


def test_baseline_split_puts_the_electrolyser_first() -> None:
    cfg, frame, _, _ = _case("v2_shortfall")
    split = {a.unit: np.asarray(a.kWh) for a in monthly_split(cfg, frame)}
    pool = frame.series["renewable_available_kWh"]
    assert (split["electrolyser"] + split["liquefier"] + split["compressor"] <= pool + 1e-9).all()
    dec = np.array([t.astimezone(R.TZ).month == 12 for t in frame.index])
    assert split["liquefier"][dec].sum() == 0  # no surplus in December, no repair
    approx(float(split["electrolyser"][dec].sum()), float(pool[dec].sum()), "electrolyser first")


def test_single_user_pools_are_unchanged() -> None:
    """Test plant 1 has one unit on its pool: no allocation is proposed and the batches are
    those of the single-pool stage A (the phase 6 golden tests check them in detail)."""
    from tests.golden.test_batching_golden import _optimised

    res, _ = _optimised("tc1")
    assert res.renewable_allocations == [] and res.log == []
