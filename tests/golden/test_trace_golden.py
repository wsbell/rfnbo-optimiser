"""Phase 5 acceptance: traceability records, lineage and sealed runs on the workbook runs."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path

import pytest

from rfnbo.calc import calculate
from rfnbo.trace import json_log, lineage
from rfnbo.trace import run as sealed
from tests.golden.test_golden import CASES, _run

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(params=sorted(CASES))
def case(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def records(case: str) -> dict[str, dict[str, object]]:
    run, _, _ = _run(case)
    recs = json_log.batch_records(run, "run-test", lineage.edges(run))
    return {str(r["batch"]): r for r in recs}


def test_record_matches_build_plan_example() -> None:
    """Build plan §5 uses E-2026-10-002 from test case 2 as its example."""
    rec = records("tc2")["E-2026-10-002"]
    assert rec["start"] == "2026-10-01T09:30+02:00" and rec["end"] == "2026-10-01T15:30+02:00"
    assert rec["intervals"] == 24 and rec["renewable_available_kWh"] == 50360.0
    inputs = {i["input"]: i for i in rec["inputs"]}  # type: ignore[attr-defined]
    assert inputs["Renewable electricity (matched in batch)"]["qty"] == 50360.0
    grid = inputs["Non-renewable electricity"]
    assert (grid["qty"], grid["ef"], grid["emissions_g"]) == (9640.0, 400.0, 3856000.0)
    assert inputs["Water"]["emissions_g"] == 24000.0
    assert rec["adders"][0]["emissions_g"] == 144000.0  # type: ignore[index]
    assert rec["output"]["energy_MJ"] == 144000.0  # type: ignore[index]
    assert math.isclose(rec["rfnbo_share"], 0.839333, rel_tol=1e-6)  # type: ignore[arg-type]
    assert math.isclose(rec["ghg"], 27.944444, rel_tol=1e-6)  # type: ignore[arg-type]
    assert rec["compliant"] is True
    assert math.isclose(rec["rfnbo_qty"], 1007.2, rel_tol=1e-9)  # type: ignore[arg-type]


def test_ghg_recomputes_from_every_record(case: str) -> None:
    for bid, rec in records(case).items():
        reported = rec["ghg"]
        again = json_log.recompute_ghg(rec)
        if reported is None:
            assert again is None, bid
        else:
            assert again is not None and math.isclose(again, reported, rel_tol=1e-9), bid  # type: ignore[arg-type]


def test_downstream_record_lists_upstream_batches(case: str) -> None:
    run, _, _ = _run(case)
    rec = records(case)["N-2026-12"]
    feed = rec["feed"]
    assert isinstance(feed, list)
    assert {f["batch"] for f in feed} == {
        a.source for a in run.decisions.feed if a.target == "N-2026-12"
    }
    assert math.isclose(sum(f["qty"] for f in feed), rec["rfnbo_feed_kg"], rel_tol=1e-12)  # type: ignore[arg-type]
    assert all(i["prorated_to_rfnbo"] == rec["rfnbo_share"] for i in rec["inputs"])  # type: ignore[attr-defined]


def test_noncompliant_rfnbo_is_traced_as_rfnbo() -> None:
    rec = records("tc2")["E-2026-10-001"]
    assert rec["rfnbo"] is True and rec["compliant"] is False
    checks = rec["checks"]
    assert isinstance(checks, list) and checks
    assert all(c["reference"] for c in checks)
    downstream = rec["lineage"]["downstream"]  # type: ignore[index]
    # The marginal write-off batch: part written off, the rest carried forward.
    assert {d["kind"] for d in downstream} == {"write_off", "carried"}
    assert math.isclose(sum(d["kg"] for d in downstream), rec["rfnbo_qty"], rel_tol=1e-9)  # type: ignore[arg-type]


def test_every_ledger_kg_is_accounted_for(case: str) -> None:
    """Each ledger batch's RFNBO kg leaves by feed, consignment, write-off or carry-forward."""
    run, _, _ = _run(case)
    out: dict[str, float] = defaultdict(float)
    for e in lineage.edges(run):
        if e.kind != "consignment":
            out[e.source] += e.kg
    for led in run.ledgers:
        for x in led.lines:
            assert math.isclose(out[x.batch], x.in_kg, rel_tol=1e-9, abs_tol=1e-6), x.batch


def test_consignment_drills_down_to_electrolyser_batches() -> None:
    run, _, _ = _run("tc1")
    up = lineage.upstream_of(lineage.edges(run), "C1")
    sources = {e.source for e in up}
    assert {"C1-2026-09", "C1-2026-10", "N-2026-09", "N-2026-10", "E-2026-10-01"} <= sources
    assert "E-2026-11-01" not in sources  # November H₂ only fed N-2026-11


def test_sealed_run_is_byte_identical(case: str, tmp_path: Path) -> None:
    run, _, flags = _run(case)
    a = sealed.seal(run, tmp_path / "a", flags)
    again = calculate(run.config, run.frame, run.decisions)
    b = sealed.seal(again, tmp_path / "b", flags)
    for name in (sealed.RUN_FILE, sealed.BATCHES_FILE, sealed.FRAME_FILE):
        assert (a / name).read_bytes() == (b / name).read_bytes(), name
    assert sealed.verify(a) == []


def test_sealed_frame_round_trips_exactly(case: str, tmp_path: Path) -> None:
    run, _, flags = _run(case)
    d = sealed.seal(run, tmp_path / "r", flags)
    frame = sealed.read_frame(d / sealed.FRAME_FILE, run.frame.timezone, run.frame.quarter)
    assert frame.index == run.frame.index
    for name, values in run.frame.series.items():
        assert (frame.series[name] == values).all(), name


def test_tampering_is_detected(tmp_path: Path) -> None:
    run, _, flags = _run("tc1")
    d = sealed.seal(run, tmp_path / "r", flags)
    text = (d / sealed.FRAME_FILE).read_text(encoding="utf-8")
    (d / sealed.FRAME_FILE).write_text(text.replace(",2500.0,", ",2499.0,", 1), encoding="utf-8")
    problems = sealed.verify(d)
    assert any("frame.csv does not match" in p for p in problems)

    d2 = sealed.seal(run, tmp_path / "r2", flags)
    rec = json.loads((d2 / sealed.RUN_FILE).read_text(encoding="utf-8"))
    rec["results"]["consignments"][0]["shipped_ghg"] = 14.0
    (d2 / sealed.RUN_FILE).write_text(
        json.dumps(rec, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    assert any("run.json does not reproduce" in p for p in sealed.verify(d2))


def test_seal_refuses_to_overwrite_another_run(tmp_path: Path) -> None:
    r1, _, f1 = _run("tc1")
    r2, _, f2 = _run("tc2")
    sealed.seal(r1, tmp_path / "r", f1)
    sealed.seal(r1, tmp_path / "r", f1)  # same run: fine
    with pytest.raises(sealed.SealError, match="different run"):
        sealed.seal(r2, tmp_path / "r", f2)


def test_run_id_depends_only_on_inputs(tmp_path: Path) -> None:
    r1, _, f1 = _run("tc1")
    r2, _, f2 = _run("tc2")
    ids = {
        sealed.load_record(sealed.seal(r, tmp_path / n, f))["run_id"]
        for n, r, f in (("a", r1, f1), ("b", r1, f1), ("c", r2, f2))
    }
    assert len(ids) == 2


def test_next_quarter_opens_from_sealed_closing_ledger(tmp_path: Path) -> None:
    run, _, flags = _run("tc1")
    d = sealed.seal(run, tmp_path / "q4", flags)
    nxt = sealed.with_opening_from(run.config, d, "2027-Q1")
    nh3 = next(b for b in nxt.blocks if b.id == "nh3_storage")
    opening = {e.batch_id: e for e in nh3.opening_ledger}  # type: ignore[union-attr]
    assert set(opening) == {"N-2026-09", "N-2026-10", "N-2026-11"}
    assert math.isclose(opening["N-2026-11"].rfnbo_kg, 455797.916667, rel_tol=1e-9)
    assert opening["N-2026-11"].production_month == "2026-11"
    assert math.isclose(opening["N-2026-10"].ghg_gCO2e_per_MJ, 14.0924731182796, rel_tol=1e-9)
    assert math.isclose(nh3.opening_inventory_kg, 575297.916667, rel_tol=1e-12)  # type: ignore[union-attr]
    with pytest.raises(sealed.SealError, match="cannot open"):
        sealed.with_opening_from(run.config, d, "2027-Q2")


def test_failed_run_cannot_open_a_quarter(tmp_path: Path) -> None:
    run, _, flags = _run("tc1")
    bad = calculate(run.config, run.frame, run.decisions.model_copy(update={"write_offs": []}))
    assert not bad.passed
    d = sealed.seal(bad, tmp_path / "bad", flags)
    with pytest.raises(sealed.SealError, match="failed checks"):
        sealed.carried_ledger(d)


def test_cli_run_and_verify(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from rfnbo.cli import main

    out = tmp_path / "sealed"
    code = main(
        [
            "run",
            "--config",
            str(ROOT / "examples" / "test_plant.json"),
            "--source",
            str(ROOT / "examples" / "test_workbook_source.json"),
            "--data",
            str(ROOT / "tests" / "golden" / "workbooks" / CASES["tc1"]),
            "--decisions",
            str(ROOT / "tests" / "golden" / "tc1.decisions.json"),
            "--quarter",
            "2026-Q4",
            "--out",
            str(out),
        ]
    )
    assert code == 0, capsys.readouterr().err
    assert main(["verify", str(out)]) == 0
    assert "byte for byte" in capsys.readouterr().out
