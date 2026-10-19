"""Phase 15 acceptance: one quarter of test plant 3 through the whole optimiser in under
10 minutes, and a five-unit plant (purifier, liquefier, compressor, NH₃ synthesis on one
renewable pool) passes every check within the same budget. Measured 2026-10-19: about 12 s
and 13 s; stage A's batching search takes about 90% of the time
(``uv run python tests/golden/benchmark_networks.py --profile``)."""

from __future__ import annotations

import pytest

from tests.golden.benchmark_networks import run

BUDGET_S = 600.0


@pytest.mark.parametrize("plant", ["plant_3", "five_unit"])
def test_quarter_within_budget(plant: str) -> None:
    res, secs = run(plant)
    assert secs < BUDGET_S, secs
    o = res.optimised
    assert o.run.failures == [], [str(f) for f in o.run.failures][:5]
    assert o.objectives.consignments_met
    assert res.value_bound().upper_MJ >= o.objectives.carried_value_MJ * (1 - 1e-9)
