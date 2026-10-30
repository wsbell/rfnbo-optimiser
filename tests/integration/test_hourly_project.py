"""A plant run on hourly data in MWh and tonnes, as given (decided 2026-10-30): no time step
conversion, units converted on reading and reported, one result sealed (no baseline)."""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

from rfnbo.project.folder import prepare
from rfnbo.project.run import run_project
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


def _hourly_csv() -> bytes:
    """A year of hourly data like the user's: the plant flat out, solar by day."""
    lines = ["Time," + ",".join(HEADERS.values())]
    start = dt.datetime(2026, 1, 1)
    for h in range(365 * 24):
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
