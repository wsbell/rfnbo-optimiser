"""Data at its own time step, and column units converted on reading (decided 2026-10-30)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from rfnbo.data import calendar as cal
from rfnbo.data import ingest, quality, units

HOUR = dt.timedelta(hours=1)
QUARTER_HOUR = dt.timedelta(minutes=15)


def test_units_are_read_from_headers() -> None:
    assert units.from_header("Solar Power (MWh)") == "MWh"
    assert units.from_header("H2 Production (tonne)") == "tonne"
    assert units.from_header("H2 flow [t/h]") == "t/h"
    assert units.from_header("Compressor power kW") == "kW"
    assert units.from_header("Start (UTC)") is None
    assert units.from_header("Electrolyser running hours") is None


def test_factors_to_the_series_unit() -> None:
    assert units.factor("MWh", "solar_available_kWh", HOUR) == 1000
    assert units.factor("tonne", "electrolyser_H2_kg", HOUR) == 1000
    assert units.factor("kWh", "x_kWh", QUARTER_HOUR) == 1
    # A rate becomes an amount per interval: MW over 15 minutes, t/h over an hour.
    assert units.factor("MW", "x_kWh", QUARTER_HOUR) == 250
    assert units.factor("t/h", "x_kg", HOUR) == 1000
    with pytest.raises(ValueError, match="is mass"):
        units.factor("t", "x_kWh", HOUR)
    with pytest.raises(ValueError, match="unknown unit"):
        units.factor("barrels", "x_kg", HOUR)
    with pytest.raises(ValueError, match="does not end in a unit"):
        units.factor("MWh", "solar", HOUR)


def test_the_time_step_is_the_data_s_own() -> None:
    start = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
    hourly = [start + i * HOUR for i in range(10)]
    assert cal.detect_step(hourly) == HOUR
    assert cal.detect_step(hourly[:3] + hourly[5:]) == HOUR  # a gap is not the step
    assert cal.detect_step([start + i * QUARTER_HOUR for i in range(9)]) == QUARTER_HOUR
    with pytest.raises(ValueError, match="does not divide an hour"):
        cal.detect_step([start + i * dt.timedelta(minutes=7) for i in range(5)])
    with pytest.raises(ValueError, match="fewer than two"):
        cal.detect_step([start])


def test_excel_times_are_rounded_to_the_second() -> None:
    drifted = dt.datetime(2026, 12, 31, 22, 59, 59, 998000)
    assert ingest._parse_timestamp(drifted) == dt.datetime(2026, 12, 31, 23, 0, tzinfo=dt.UTC)


def test_hourly_data_in_MWh_and_tonnes_is_read_as_it_is(tmp_path: Path) -> None:
    """Hourly rows stay hourly; MWh and tonnes become kWh and kg; the conversion and the blank
    trailing row are reported."""
    path = tmp_path / "hourly.csv"
    rows = ["Time,Solar Power (MWh),H2 Production (tonne)"]
    start = dt.datetime(2026, 9, 30, 23)  # 1 October, 00:00 in London (BST)
    for i in range(2209):  # 2026-Q4 in Europe/London, the extra hour in October included
        rows.append(f"{(start + i * HOUR).isoformat()},{0.5},{0.004}")
    rows.append(",#REF!,#REF!")  # a trailing row with no time and no numbers
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    source = ingest.DataSource(
        format="csv",
        time_column="Time",
        columns={"solar_kWh": "Solar Power (MWh)", "h2_kg": "H2 Production (tonne)"},
        units={"solar_kWh": "MWh", "h2_kg": "tonne"},
    )
    raw = ingest.load(path, source)
    assert len(raw.timestamps) == 2209
    assert raw.series["solar_kWh"][0] == pytest.approx(500.0)
    assert raw.series["h2_kg"][0] == pytest.approx(4.0)
    assert any("x 1000 per 1:00:00" in c for c in raw.conversions)
    assert any("skipped" in c for c in raw.conversions)

    frame, flags = quality.align(raw, 2026, 4, "Europe/London")
    assert frame.interval == HOUR and len(frame) == 2209
    assert not quality.has_errors(flags)
    assert frame.end - frame.index[0] == 2209 * HOUR
    assert frame.position(dt.datetime(2026, 10, 1, 23, tzinfo=dt.UTC)) == 24
