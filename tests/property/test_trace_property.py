"""Property: the GHG recomputed from a batch's JSON record equals the reported GHG,
and batches never cross a local month, for random data and random batch boundaries."""

from __future__ import annotations

import datetime as dt
import itertools
import math

from hypothesis import given, settings
from hypothesis import strategies as st

from rfnbo.calc import calculate
from rfnbo.config import load_config
from rfnbo.data import calendar as cal
from rfnbo.model.entities import Decisions
from rfnbo.trace import json_log, lineage
from tests.conftest import EXAMPLE
from tests.unit.test_calc import make_frame

CFG = load_config(EXAMPLE)
# 31 Oct 2026 22:00 CET: the 16 intervals run across the month end into November.
START = dt.datetime(2026, 10, 31, 21, 0, tzinfo=dt.UTC)
N = 16
kwh = st.floats(0, 4000, allow_nan=False)


def cuts(n: int) -> st.SearchStrategy[list[int]]:
    return st.lists(st.integers(1, n - 1), unique=True, max_size=6).map(
        lambda xs: [0, *sorted(xs), n]
    )


@settings(max_examples=60, deadline=None)
@given(
    renewable=st.lists(kwh, min_size=N, max_size=N),
    electricity=st.lists(st.floats(1, 3000), min_size=N, max_size=N),
    h2_consumed=st.lists(st.floats(1, 60), min_size=N, max_size=N),
    e_cuts=cuts(N),
    feed_fraction=st.floats(0.05, 1.0),
)
def test_record_ghg_recomputes(
    renewable: list[float],
    electricity: list[float],
    h2_consumed: list[float],
    e_cuts: list[int],
    feed_fraction: float,
) -> None:
    cols = {
        "renewable_available_kWh": renewable,
        "electrolyser_electricity_kWh": electricity,
        "electrolyser_water_kg": [e / 5 for e in electricity],
        "electrolyser_H2_kg": [e / 50 for e in electricity],
        "nh3_synthesis_H2_kg": h2_consumed,
        "nh3_synthesis_NH3_kg": [h * 5.6 for h in h2_consumed],
        "nh3_synthesis_electricity_kWh": [h * 2.8 for h in h2_consumed],
    }
    frame = make_frame(CFG, cols, start=START)
    months = [cal.month_key(t, CFG.timezone) for t in frame.index]
    # Split electrolyser cuts at the month boundary so batches respect the rule by design.
    boundary = months.index(months[-1])
    e_points = sorted({*e_cuts, boundary})
    batches = [
        {
            "id": f"E{i}",
            "unit": "electrolyser",
            "start": frame.index[s].isoformat(),
            "end": (START + e * cal.INTERVAL).isoformat(),
        }
        for i, (s, e) in enumerate(itertools.pairwise(e_points))
    ]
    batches += [
        {
            "id": "N1",
            "unit": "nh3_synthesis",
            "start": frame.index[0].isoformat(),
            "end": frame.index[boundary].isoformat(),
        },
        {
            "id": "N2",
            "unit": "nh3_synthesis",
            "start": frame.index[boundary].isoformat(),
            "end": frame.end.isoformat(),
        },
    ]
    probe = calculate(CFG, frame, Decisions.model_validate({"batches": batches}))
    feed = []
    for b in probe.batches.values():
        if b.unit == "electrolyser" and b.is_rfnbo:
            target = "N1" if b.month == months[0] else "N2"
            feed.append({"source": b.id, "target": target, "kg": b.rfnbo_kg * feed_fraction})
    run = calculate(CFG, frame, Decisions.model_validate({"batches": batches, "feed": feed}))

    assert not [f for f in run.failures if f.name == "batch_within_month"]
    for rec in json_log.batch_records(run, "run-prop", lineage.edges(run)):
        again = json_log.recompute_ghg(rec)
        if rec["ghg"] is None:
            assert again is None
        else:
            assert again is not None and math.isclose(again, rec["ghg"], rel_tol=1e-9)
