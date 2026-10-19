"""Property: the electrolyser's share of a shared renewable pool never exceeds the pool, and
each batch gets exactly what it can match (phase 13)."""

from __future__ import annotations

import itertools

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

from rfnbo.optimise.pool import electrolyser_allocation

kwh = st.floats(0, 4000, allow_nan=False)


@settings(max_examples=200, deadline=None)
@given(
    pool=st.lists(kwh, min_size=12, max_size=12),
    elec=st.lists(kwh, min_size=12, max_size=12),
    cuts=st.sets(st.integers(1, 11), max_size=6),
)
def test_allocation_within_pool_and_matches_each_batch(
    pool: list[float], elec: list[float], cuts: set[int]
) -> None:
    p, e = np.asarray(pool), np.asarray(elec)
    edges = [0, *sorted(cuts), 12]
    batches = list(itertools.pairwise(edges))
    alloc, surplus = electrolyser_allocation(p, e, batches)
    assert (alloc >= -1e-9).all() and (surplus >= -1e-6).all()
    np.testing.assert_allclose(alloc + surplus, p, atol=1e-6)
    for s, t in batches:
        want = min(p[s:t].sum(), e[s:t].sum())
        assert abs(alloc[s:t].sum() - want) <= 1e-6 * max(1.0, want)
