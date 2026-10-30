"""A plant run on hourly data in MWh and tonnes, as given (decided 2026-10-30): no time step
conversion, units converted on reading and reported, one result sealed (no baseline)."""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

import pytest

from rfnbo.project.folder import prepare
from rfnbo.project.run import run_project
from rfnbo.report.tables import summary as summary_table
from rfnbo.trace import run as sealed
from rfnbo.ui.data.workspace import Workspace

PLANT = Path(__file__).resolve().parents[1] / "fixtures" / "simple_plant.json"
HEADERS = {
    "solar_available_kWh": "Solar Power (MWh)",
    "electrolyser_electricity_kWh": "Electrolyser Power (MWh)",
    "electrolyser_H2_kg": "H2 Production (tonne)",
    "compressor_electricity_kWh": "Compressor Power (MWh)",
    "compressor_CH2_kg": "Compressor Flow (tonne)",
}


def _hourly_csv(until: dt.datetime = dt.datetime(2027, 1, 1)) -> bytes:
    """Hourly data like the user's, from 1 January 2026 to ``until`` (UTC): the plant flat out,
    solar by day."""
    lines = ["Time," + ",".join(HEADERS.values())]
    start = dt.datetime(2026, 1, 1)
    for h in range(int((until - start).total_seconds() // 3600)):
        t = start + dt.timedelta(hours=h)
        sun = max(0.0, 400 * math.sin(math.pi * (t.hour - 6) / 12)) if 6 <= t.hour <= 18 else 0
        lines.append(f"{t.isoformat()},{sun:.3f},250,4.545454,9.545454,4.536363")
    return ("\n".join(lines) + "\n").encode()


def test_a_quarter_of_hourly_data_runs_as_given(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    (tmp_path / "plant.json").write_bytes(PLANT.read_bytes())
    ws.setup("plant.json", "2026-Q4")
    i = ws.add_data_file("plant_data.csv", _hourly_csv())
    for series, header in HEADERS.items():
        ws.map_series(series, i, header)
    assert ws.unit_of("solar_available_kWh") == "MWh"  # read from the header
    assert ws.unit_of("electrolyser_H2_kg") == "tonne"
    ws.add_consignment(
        id="C1", output="consignment_customer", volume_kg=100_000, ship_date="2026-12-15"
    )
    ws.save()
    assert ws.project is not None

    prep = prepare(ws.project)
    assert prep.ok
    assert prep.frame.interval == dt.timedelta(hours=1) and len(prep.frame) == 2209
    reported = [f.message for f in prep.flags if f.code == "data_read"]
    assert any("Solar Power (MWh)" in m and "x 1000" in m for m in reported)
    assert prep.frame.series["electrolyser_electricity_kWh"][0] == 250_000  # MWh -> kWh

    result, out = run_project(ws.project, "q4", prep)
    assert result.baseline is None  # no comparison unless asked for
    assert result.feasible and result.optimised.run.failures == []
    assert sorted(p.name for p in out.out.iterdir() if not p.name.endswith(".xlsx")) == [
        "optimised",
        "summary.json",
    ]
    summary = json.loads((out.out / "summary.json").read_text("utf-8"))
    assert list(summary["objectives"]) == ["optimised"]
    assert sealed.verify(out.out / "optimised") == []


def test_a_month_of_data_runs_provisionally_quarter_to_date(tmp_path: Path) -> None:
    """Data that ends before the quarter (here October only) is run quarter to date, closed as
    if the quarter ended at the data's end, and labelled provisional (decided 2026-10-30): it
    cannot open the next quarter."""
    ws = Workspace(tmp_path)
    (tmp_path / "plant.json").write_bytes(PLANT.read_bytes())
    ws.setup("plant.json", "2026-Q4")
    i = ws.add_data_file("october.csv", _hourly_csv(until=dt.datetime(2026, 11, 1)))
    for series, header in HEADERS.items():
        ws.map_series(series, i, header)
    ws.add_consignment(
        id="C1", output="consignment_customer", volume_kg=50_000, ship_date="2026-10-30"
    )
    ws.save()
    assert ws.project is not None

    prep = prepare(ws.project)
    assert prep.ok and prep.frame.provisional
    assert prep.frame.end == dt.datetime(2026, 11, 1, tzinfo=dt.UTC)  # Nov 1, 00:00 GMT
    assert len(prep.frame) == 31 * 24 + 1  # October, with the hour the clocks went back
    assert any(f.code == "provisional_period" for f in prep.flags)
    assert any(s.tab == "Period" and "PROVISIONAL" in s.detail for s in ws.status())

    result, out = run_project(ws.project, "october", prep)
    run = result.optimised.run
    assert result.feasible and run.failures == []
    fill = run.ledger("pt_compressor_customer")  # closed at the data's end: nothing carried
    assert abs(fill.carried_kg) <= 1e-6 and fill.write_off_kg > 0
    rows = {r["item"]: r["value"] for r in summary_table(run).rows}
    assert rows["Period"].startswith("PROVISIONAL")

    rec = sealed.load_record(out.out / "optimised")
    assert rec["provisional"]["until"] == "2026-11-01T00:00:00+00:00"
    assert sealed.verify(out.out / "optimised") == []
    with pytest.raises(sealed.SealError, match="provisional"):
        sealed.with_opening_from(prep.config, out.out / "optimised", "2027-Q1")
