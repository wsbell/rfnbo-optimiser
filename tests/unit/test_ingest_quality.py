from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

import numpy as np
import pytest

from rfnbo.config import load_config
from rfnbo.config.schema import PlantConfig
from rfnbo.data import calendar as cal
from rfnbo.data import ingest, quality
from tests.conftest import EXAMPLE

SOURCE = ingest.DataSource(
    format="csv",
    time_column="timestamp_utc",
    columns={name: name for name in quality.required_series(load_config(EXAMPLE))},
)


@pytest.fixture
def cfg() -> PlantConfig:
    return load_config(EXAMPLE)


def write_quarter(cfg: PlantConfig, path: Path) -> list[list[str]]:
    """A balanced Q4 2026 dataset: steady production, inventories consistent with flows."""
    names = list(SOURCE.columns)
    rows: list[list[str]] = []
    h2_inv, nh3_inv = 5000.0, 350000.0
    for ts in cal.quarter_intervals(2026, 4, cfg.timezone):
        h2_inv += 50 - 48.95
        nh3_inv += 275
        values = {
            "renewable_available_kWh": 3000,
            "electrolyser_electricity_kWh": 2500,
            "electrolyser_water_kg": 500,
            "electrolyser_H2_kg": 50,
            "nh3_synthesis_electricity_kWh": 137.5,
            "nh3_synthesis_H2_kg": 48.95,
            "nh3_synthesis_NH3_kg": 275,
            "h2_storage_inventory_kg": h2_inv,
            "nh3_storage_inventory_kg": nh3_inv,
        }
        rows.append([ts.isoformat()] + [repr(float(values.get(n, 0.0))) for n in names])
    save(path, rows)
    return rows


def save(path: Path, rows: list[list[str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timestamp_utc", *SOURCE.columns])
        w.writerows(rows)


def prepare(cfg: PlantConfig, path: Path) -> tuple[quality.Frame, list[quality.QualityFlag]]:
    return quality.prepare(cfg, ingest.load(path, SOURCE), 2026, 4)


def codes(flags: list[quality.QualityFlag]) -> set[str]:
    return {f.code for f in flags}


def test_clean_csv_loads(cfg: PlantConfig, tmp_path: Path) -> None:
    p = tmp_path / "q4.csv"
    write_quarter(cfg, p)
    frame, flags = prepare(cfg, p)
    assert flags == []
    assert len(frame) == 8836 and frame.quarter == "2026-Q4"
    assert frame.index[0] == dt.datetime(2026, 9, 30, 22, tzinfo=dt.UTC)
    assert len(ingest.load(p, SOURCE).sha256) == 64


def test_missing_and_duplicate_intervals(cfg: PlantConfig, tmp_path: Path) -> None:
    p = tmp_path / "q4.csv"
    rows = write_quarter(cfg, p)
    gone = rows.pop(100)
    rows.insert(200, rows[200])
    save(p, rows)
    frame, flags = prepare(cfg, p)
    assert {"missing_interval", "duplicate_interval", "missing_value"} <= codes(flags)
    missing = next(f for f in flags if f.code == "missing_interval")
    assert missing.count == 1 and missing.examples[0].isoformat() == gone[0]
    # Never filled: the gap and the duplicated interval stay NaN.
    assert np.isnan(frame.series["electrolyser_H2_kg"][100])
    assert quality.has_errors(flags)


def test_negative_blank_and_text_values(cfg: PlantConfig, tmp_path: Path) -> None:
    p = tmp_path / "q4.csv"
    rows = write_quarter(cfg, p)
    col = 1 + list(SOURCE.columns).index("electrolyser_water_kg")
    rows[5][col] = "-1"
    rows[6][col] = ""
    rows[7][col] = "n/a"
    save(p, rows)
    _, flags = prepare(cfg, p)
    assert {"negative_value", "missing_value", "non_numeric"} <= codes(flags)
    blank = next(f for f in flags if f.code == "missing_value")
    assert blank.count == 2 and blank.series == "electrolyser_water_kg"


def test_meter_balance_break_is_a_warning(cfg: PlantConfig, tmp_path: Path) -> None:
    p = tmp_path / "q4.csv"
    rows = write_quarter(cfg, p)
    col = 1 + list(SOURCE.columns).index("nh3_storage_inventory_kg")
    for r in rows[3000:]:
        r[col] = repr(float(r[col]) - 500)  # a 500 kg loss in one interval
    save(p, rows)
    _, flags = prepare(cfg, p)
    (flag,) = flags
    assert flag.code == "meter_balance" and flag.severity == "warning" and flag.count == 1
    assert not quality.has_errors(flags)


def test_non_utc_offset_rejected(cfg: PlantConfig, tmp_path: Path) -> None:
    p = tmp_path / "q4.csv"
    rows = write_quarter(cfg, p)
    rows[0][0] = "2026-10-01T00:00:00+02:00"
    save(p, rows)
    with pytest.raises(ingest.IngestError, match="not UTC"):
        ingest.load(p, SOURCE)


def test_missing_column_rejected(cfg: PlantConfig, tmp_path: Path) -> None:
    p = tmp_path / "q4.csv"
    write_quarter(cfg, p)
    source = SOURCE.model_copy(update={"columns": {**SOURCE.columns, "extra": "nope"}})
    with pytest.raises(ingest.IngestError, match="missing column"):
        ingest.load(p, source)
