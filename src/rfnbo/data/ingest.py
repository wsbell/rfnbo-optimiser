"""Read plant data from CSV or Excel, at its own time step (15 minutes, an hour, ...).

Ingestion only reads and labels: blank or non-numeric cells become NaN and are noted,
never filled. Alignment to the interval grid and quality flags are in
:mod:`rfnbo.data.quality`.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict, Field

from rfnbo.data import calendar as cal
from rfnbo.data import units

FloatArray = npt.NDArray[np.float64]


class IngestError(ValueError):
    pass


class DataSource(BaseModel):
    """How to read one data file: where the timestamps are and which column feeds which series."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["excel", "csv"]
    sheet: str | None = None
    time_column: str
    # Timestamps without an offset are read as UTC; offsets other than UTC are rejected.
    timestamps: Literal["UTC"] = "UTC"
    columns: dict[str, str] = Field(min_length=1)  # series name -> column header
    # series name -> the column's unit (e.g. "MWh", "t/h"), converted on reading to the unit
    # the series is named in (rfnbo.data.units); a series not listed is read as it is.
    units: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True)
class RawData:
    """Rows as read, in file order, before alignment to the interval grid."""

    timestamps: tuple[dt.datetime, ...]
    series: Mapping[str, FloatArray]
    source_file: str
    sha256: str
    notes: tuple[str, ...]  # e.g. non-numeric cells read as NaN
    conversions: tuple[str, ...] = ()  # units converted, rows skipped: reported, never silent


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_source_spec(path: str | Path) -> DataSource:
    return DataSource.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def load(path: str | Path, source: DataSource) -> RawData:
    if source.format == "excel":
        header, rows = _read_excel(path, source.sheet)
    else:
        header, rows = _read_csv(path)
    return _build(path, source, header, rows)


def _read_excel(
    path: str | Path, sheet: str | None
) -> tuple[list[str], Iterable[Sequence[object]]]:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[sheet] if sheet else wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    try:
        header = [str(c) if c is not None else "" for c in next(it)]
    except StopIteration as exc:
        raise IngestError(f"{path}: sheet is empty") from exc
    rows = [r for r in it if any(c is not None for c in r)]
    wb.close()
    return header, rows


def _read_csv(path: str | Path) -> tuple[list[str], Iterable[Sequence[object]]]:
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise IngestError(f"{path}: file is empty") from exc
        rows = [r for r in reader if any(c.strip() for c in r)]
    return header, rows


def _parse_timestamp(value: object) -> dt.datetime:
    if isinstance(value, dt.datetime):
        # Excel keeps times as fractions of a day: they can read back a millisecond off
        # (22:59:59.998). Rounded to the second; data steps are whole minutes.
        ts = (value + dt.timedelta(microseconds=500_000)).replace(microsecond=0)
    elif isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        ts = dt.datetime.fromisoformat(text)
    else:
        raise ValueError(f"not a timestamp: {value!r}")
    if ts.tzinfo is None:
        return ts.replace(tzinfo=dt.UTC)
    if ts.utcoffset() != dt.timedelta(0):
        raise ValueError(f"timestamp {value!r} is not UTC")
    return ts.astimezone(dt.UTC)


def _parse_number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _build(
    path: str | Path,
    source: DataSource,
    header: list[str],
    rows: Iterable[Sequence[object]],
) -> RawData:
    pos = {name.strip(): i for i, name in enumerate(header)}
    wanted = [source.time_column, *source.columns.values()]
    missing = [c for c in wanted if c not in pos]
    if missing:
        raise IngestError(f"{path}: missing column(s) {missing}")

    t_col = pos[source.time_column]
    data_cols = [pos[c] for c in source.columns.values()]
    kept: list[Sequence[object]] = []
    timestamps: list[dt.datetime] = []
    blank: list[int] = []
    for n, row in enumerate(rows, start=2):
        when = row[t_col] if t_col < len(row) else None
        if when in (None, "") and not any(
            _parse_number(row[c] if c < len(row) else None) is not None for c in data_cols
        ):
            blank.append(n)  # a trailing or spacer row: no time and no numbers
            continue
        try:
            timestamps.append(_parse_timestamp(when))
        except ValueError as exc:
            raise IngestError(f"{path}: row {n}: {exc}") from exc
        kept.append(row)
    rows = kept

    notes: list[str] = []
    series: dict[str, FloatArray] = {}
    for name, column in source.columns.items():
        c = pos[column]
        values = np.full(len(rows), np.nan)
        bad = 0
        for i, row in enumerate(rows):
            raw = row[c] if c < len(row) else None
            num = _parse_number(raw)
            if num is None:
                if raw not in (None, "") and not (isinstance(raw, str) and not raw.strip()):
                    bad += 1
            else:
                values[i] = num
        if bad:
            notes.append(f"{name}: {bad} non-numeric cell(s) read as missing")
        series[name] = values

    conversions: list[str] = []
    if source.units:
        step = cal.detect_step(timestamps)
        for name, unit in sorted(source.units.items()):
            if name not in series:
                continue
            try:
                f = units.factor(unit, name, step)
            except ValueError as exc:
                raise IngestError(f"{path}: column {source.columns[name]!r}: {exc}") from exc
            if f != 1.0:
                series[name] = series[name] * f
                conversions.append(
                    f"{name}: column {source.columns[name]!r} in {unit}, "
                    f"x {f:g} per {step} interval"
                )
    if blank:
        conversions.append(f"rows without a time or numbers skipped: {_rows(blank)}")

    return RawData(
        timestamps=tuple(timestamps),
        series=series,
        source_file=str(path),
        sha256=file_sha256(path),
        notes=tuple(notes),
        conversions=tuple(conversions),
    )


def _rows(numbers: list[int]) -> str:
    shown = ", ".join(str(n) for n in numbers[:5])
    return shown + (f" and {len(numbers) - 5} more" if len(numbers) > 5 else "")
