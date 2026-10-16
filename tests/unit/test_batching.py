"""Stage A building blocks: the DP is exact, windows respect correlation periods."""

from __future__ import annotations

import datetime as dt
import itertools
import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rfnbo.config import load_config
from rfnbo.config.schema import ProcessingUnit
from rfnbo.data import calendar as cal
from rfnbo.optimise.batching import feedable_MJ, segment, windows
from tests.conftest import EXAMPLE
from tests.unit.test_calc import make_frame

CFG = load_config(EXAMPLE)
ELECTROLYSER = CFG.block("electrolyser")
NH3 = CFG.block("nh3_synthesis")
assert isinstance(ELECTROLYSER, ProcessingUnit) and isinstance(NH3, ProcessingUnit)


def brute_force(n: int, lo: list[int], value: np.ndarray, eps: float) -> float:
    """Best Σ(value − eps) over every segmentation of [0, n) allowed by ``lo``."""
    best = -math.inf
    for mask in itertools.product((0, 1), repeat=n - 1):
        cuts = [0, *[i + 1 for i, m in enumerate(mask) if m], n]
        pairs = list(itertools.pairwise(cuts))
        if all(s >= lo[e] for s, e in pairs):
            best = max(best, sum(value[s, e] - eps for s, e in pairs))
    return best


@settings(max_examples=150, deadline=None)
@given(
    n=st.integers(1, 9),
    data=st.data(),
    eps=st.sampled_from([0.0, 0.01, 1.0]),
)
def test_dp_matches_brute_force(n: int, data: st.DataObject, eps: float) -> None:
    value = np.array(
        data.draw(
            st.lists(
                st.lists(st.floats(0, 100, allow_nan=False), min_size=n + 1, max_size=n + 1),
                min_size=n + 1,
                max_size=n + 1,
            )
        )
    )
    # Earliest allowed start for a batch ending at e: non-decreasing, < e.
    lo = [0]
    for e in range(1, n + 1):
        lo.append(data.draw(st.integers(lo[-1], e - 1)))
    cuts = segment([(0, n)], np.array(lo, dtype=np.int64), lambda s, e: value[s, e], eps)
    assert cuts[0][0] == 0 and cuts[-1][1] == n
    assert all(a[1] == b[0] for a, b in itertools.pairwise(cuts))
    assert all(s >= lo[e] for s, e in cuts)
    got = sum(value[s, e] - eps for s, e in cuts)
    assert math.isclose(got, brute_force(n, lo, value, eps), rel_tol=1e-12, abs_tol=1e-9)


def test_equal_values_give_fewest_batches() -> None:
    n = 6
    value = np.zeros((n + 1, n + 1))
    for s in range(n + 1):
        for e in range(s + 1, n + 1):
            value[s, e] = 10.0 * (e - s)  # additive: any segmentation ties
    cuts = segment([(0, n)], np.zeros(n + 1, dtype=np.int64), lambda s, e: value[s, e], 0.01)
    assert cuts == [(0, n)]


def _frame(start: dt.datetime, n: int):  # type: ignore[no-untyped-def]
    return make_frame(CFG, {"electrolyser_electricity_kWh": [1.0] * n}, start=start)


def test_monthly_windows_stop_at_the_local_month() -> None:
    # 31 Oct 2026 22:00 CET → 1 Nov local.
    frame = _frame(dt.datetime(2026, 10, 31, 21, 0, tzinfo=dt.UTC), 12)
    slices, lo = windows(CFG, frame, ELECTROLYSER)
    assert slices == [(0, 8), (8, 12)]
    assert list(lo[1:9]) == [0] * 8 and list(lo[9:]) == [8] * 4


def test_hourly_windows_from_2030_for_electrolytic_units_only() -> None:
    # 31 Dec 2029 23:00 CET (22:00 UTC): one hour before the switch, then hourly.
    frame = _frame(dt.datetime(2029, 12, 31, 22, 0, tzinfo=dt.UTC), 16)
    _, lo = windows(CFG, frame, ELECTROLYSER)
    assert list(lo[1:5]) == [0, 0, 0, 0]  # 2029: whole month (here, the frame start)
    assert list(lo[5:]) == [4] * 4 + [8] * 4 + [12] * 4
    assert all(e - lo[e] <= 4 for e in range(5, 17))
    _, lo_nh3 = windows(CFG, frame, NH3)
    assert list(lo_nh3[5:]) == [4] * 12  # NH₃ stays monthly (here, from 1 Jan)


def test_hourly_windows_on_the_repeated_autumn_hour() -> None:
    # 25 Oct 2030 is not the change; use 27 Oct 2030 (last Sunday): 00:00 UTC = 02:00 CEST.
    frame = _frame(dt.datetime(2030, 10, 27, 0, 0, tzinfo=dt.UTC), 8)
    _, lo = windows(CFG, frame, ELECTROLYSER)
    hours = {cal.hour_key(t, CFG.timezone) for t in frame.index}
    assert len(hours) == 2  # 02:00 CEST and 02:00 CET are different clock hours
    assert list(lo[1:]) == [0, 0, 0, 0, 4, 4, 4, 4]


def test_feedable_takes_lowest_ghg_first_and_stops_at_the_blend() -> None:
    rf = np.array([100.0, 100.0, 100.0])
    ghg = np.array([5.0, 25.0, 15.0])
    # Limit 10: 100 MJ at 5 leaves 500 g of room; 15-GHG batch costs 5 g/MJ → all of it;
    # the 25-GHG batch would cost 15 g/MJ with no room left → none.
    assert feedable_MJ(rf, rf * ghg, limit=10.0, cap=1e9) == pytest.approx(200.0)
    # Limit 12: room 700 g; 15-GHG uses 300 g; 400 g left → 400/13 MJ of the 25-GHG batch.
    assert feedable_MJ(rf, rf * ghg, limit=12.0, cap=1e9) == pytest.approx(200 + 400 / 13)
    # Cap binds before the blend does.
    assert feedable_MJ(rf, rf * ghg, limit=100.0, cap=150.0) == pytest.approx(150.0)
    assert feedable_MJ(rf, rf * ghg, limit=1.0, cap=1e9) == 0.0
