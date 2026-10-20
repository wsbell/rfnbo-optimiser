"""Phase 9: every consignment can be traced to 15-minute data (view layer, no browser)."""

from __future__ import annotations

from pathlib import Path

import pytest

from rfnbo.optimise.pipeline import objectives
from rfnbo.trace import run as sealed
from rfnbo.ui.results import views as V
from tests.golden.test_golden import _run


@pytest.mark.parametrize("case", ["tc1", "tc2"])
def test_every_consignment_traces_to_interval_data(case: str) -> None:
    run, _, _ = _run(case)
    for c in run.consignments:
        paths = V.trace_paths(run, c.id)
        assert paths, c.id
        ends = {p[-1] for p in paths}
        for end in ends:
            view = V.batch_view(run, end)
            if view["kind"] == "opening":
                continue  # carried in from the previous quarter: no intervals in this run
            assert view["recompute_ok"], end
        produced = {node for p in paths for node in p if node in run.batches}
        if case == "tc2" and c.id != "C1":
            continue  # same upstream batches as C1; keep the test quick
        for batch_id in produced:
            iv = V.interval_view(run, batch_id)
            assert iv.consistent, (
                batch_id,
                [(x.label, x.intervals_total, x.batch_value) for x in iv.checks if not x.ok],
            )
            assert len(iv.table.rows) == run.batches[batch_id].intervals


def test_tc1_c1_reaches_the_electrolyser_through_nh3() -> None:
    run, _, _ = _run("tc1")
    paths = V.trace_paths(run, "C1")
    assert ["C1", "C1-2026-10", "N-2026-10", "E-2026-10-01"] in paths
    assert ["C1", "C1-2026-09", "N-2026-09"] in paths


def test_consignment_and_batch_views() -> None:
    run, _, _ = _run("tc1")
    cv = V.consignment_view(run, "C2")
    assert [a["id"] for a in cv["aggregated"]] == ["C2-2026-10"]
    # §12: October NH₃ (14.09) plus the 8 adder misses the 20 target; nothing averages it down.
    assert [x["check"] for x in cv["checks"] if not x["passed"]] == ["consignment_target"]
    bv = V.batch_view(run, "N-2026-12")
    assert {u["batch"] for u in bv["upstream"]} == {
        "E-2026-12-01",
        "E-2026-12-03",
        "E-2026-12-05",
        "E-2026-12-06",
        "E-2026-12-07",
    }
    # December NH₃ makes two parts (§12): from the 1.17 H₂ and from the 17.83 H₂.
    assert [(d["kind"], d["to"]) for d in bv["downstream"]] == [
        ("output", "N-2026-12.1"),
        ("output", "N-2026-12.2"),
    ]
    part = V.batch_view(run, "N-2026-12.2")
    assert {d["kind"] for d in part["downstream"]} == {"write_off"}
    opening = V.batch_view(run, "N-2026-09")
    assert opening["kind"] == "opening" and opening["rfnbo_kg"] == 300000.0
    agg = next(d for d in opening["downstream"] if d["kind"] == "aggregation")
    assert agg["consignment"] in {"C1", "C2"}
    with pytest.raises(V.NotFound):
        V.consignment_view(run, "C9")
    with pytest.raises(V.NotFound):
        V.batch_view(run, "nope")
    with pytest.raises(V.NotFound):
        V.interval_view(run, "N-2026-09")


def test_interval_view_explains_matching() -> None:
    run, _, _ = _run("tc1")
    iv = V.interval_view(run, "E-2026-12-06")
    assert iv.batch.rfnbo_share == pytest.approx(0.9)
    assert "min(" in iv.matched_note and "0.900000" in iv.matched_note
    assert iv.totals["renewable"] == pytest.approx(216000.0)


def test_runs_are_found_and_reload_through_the_calculator(tmp_path: Path) -> None:
    r1, _, f1 = _run("tc1")
    r2, _, f2 = _run("tc2")
    sealed.seal(r1, tmp_path / "q4" / "tc1", f1)
    sealed.seal(r2, tmp_path / "q4" / "tc2", f2)
    runs = V.find_runs(tmp_path)
    assert sorted(r.label for r in runs) == ["tc1", "tc2"]
    info = next(r for r in runs if r.label == "tc1")
    loaded = V.load(info)
    assert loaded.problems == []
    assert V.overview(loaded.run)["objectives"] == objectives(r1)
    assert V.find_runs(tmp_path / "q4" / "tc1")[0].run_id == info.run_id
