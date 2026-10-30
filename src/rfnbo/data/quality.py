"""Align raw data to the quarter's interval grid and flag data-quality problems.

Nothing is filled or corrected: missing intervals stay NaN and are flagged. Errors block
the calculator; warnings are reported in the calculation log.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from rfnbo.config.schema import InputBlock, PlantConfig, ProcessingUnit, RfnboSupply, StorageUnit
from rfnbo.data import calendar as cal
from rfnbo.data.ingest import FloatArray, RawData

Severity = Literal["error", "warning", "info"]  # info: what was done to the data, e.g. units
_SHOW = 5  # timestamps listed per flag


@dataclass(frozen=True)
class QualityFlag:
    code: str
    severity: Severity
    message: str
    series: str | None = None
    count: int = 1
    examples: tuple[dt.datetime, ...] = ()

    def __str__(self) -> str:
        where = f" [{self.series}]" if self.series else ""
        ex = ", ".join(t.isoformat() for t in self.examples)
        return f"{self.severity} {self.code}{where}: {self.message}" + (
            f" (e.g. {ex})" if ex else ""
        )


@dataclass(frozen=True)
class Frame:
    """Series on a complete, ordered UTC grid for one quarter, at the data's own time step
    (``interval``: 15 minutes, an hour, ...). Values are amounts per interval (kWh, kg)."""

    index: tuple[dt.datetime, ...]
    series: Mapping[str, FloatArray]
    timezone: str
    quarter: str
    source_sha256: tuple[str, ...] = field(default=())
    interval: dt.timedelta = cal.INTERVAL
    # Quarter to date: the data ends before the quarter does, so the run closes as if the
    # quarter ended at the data's end (decided 2026-10-30). A provisional view, never a closing.
    provisional: bool = False

    def __len__(self) -> int:
        return len(self.index)

    def position(self, ts: dt.datetime) -> int:
        """Grid position of an interval start, or ``len(self)`` for the period end."""
        start, n = self.index[0], len(self.index)
        offset = ts - start
        if offset % self.interval:
            raise ValueError(f"{ts.isoformat()} is not on the {self.interval} grid")
        i = offset // self.interval
        if not 0 <= i <= n:
            raise ValueError(f"{ts.isoformat()} is outside {self.quarter}")
        return i

    @property
    def end(self) -> dt.datetime:
        return self.index[-1] + self.interval


def required_series(cfg: PlantConfig) -> list[str]:
    names: list[str] = []
    for b in cfg.blocks:
        if isinstance(b, InputBlock) and b.series:
            names.append(b.series)
        elif isinstance(b, ProcessingUnit):
            names += [s for s in (b.production_series, b.electricity_series) if s]
        elif isinstance(b, StorageUnit) and b.inventory_series:
            names.append(b.inventory_series)
    names += [f.series for f in cfg.flows if f.series]
    return list(dict.fromkeys(names))


def align(
    raw: RawData, year: int, quarter: int, tz: str, step: dt.timedelta | None = None
) -> tuple[Frame, list[QualityFlag]]:
    """Put the data on the quarter's grid at its own time step (``step``, else detected from
    the timestamps: hourly data stays hourly, 15-minute data 15-minute).

    Data that ends before the quarter does gives a provisional, quarter-to-date frame: from the
    quarter's start to the data's last interval, flagged; gaps within it are still errors."""
    step = step or cal.detect_step(raw.timestamps)
    try:
        grid = cal.quarter_intervals(year, quarter, tz, step)
    except ValueError as exc:
        raise ValueError(f"{year}-Q{quarter} in {tz} does not start on a {step} interval") from exc
    start, end = grid[0], grid[-1] + step
    within = [ts for ts in raw.timestamps if start <= ts < end and not (ts - start) % step]
    provisional = bool(within) and max(within) + step < end
    if provisional:
        end = max(within) + step
        grid = [ts for ts in grid if ts < end]
    n = len(grid)
    flags: list[QualityFlag] = []
    if provisional:
        until = cal.to_local(end, tz).isoformat(timespec="minutes")
        flags.append(
            QualityFlag(
                "provisional_period",
                "warning",
                f"the data ends at {until}, before {year}-Q{quarter} does: provisional, "
                "quarter to date, closed as if the quarter ended there",
            )
        )

    rows_for: dict[int, list[int]] = {}
    off_grid: list[dt.datetime] = []
    outside: list[dt.datetime] = []
    for r, ts in enumerate(raw.timestamps):
        if (ts - start) % step:
            off_grid.append(ts)
        elif not start <= ts < end:
            outside.append(ts)
        else:
            rows_for.setdefault((ts - start) // step, []).append(r)

    if off_grid:
        flags.append(
            _flag("off_grid_timestamp", "error", f"timestamp not on the {step} grid", off_grid)
        )
    if outside:
        flags.append(
            _flag("outside_period", "warning", f"rows outside {year}-Q{quarter} ignored", outside)
        )
    dupes = [grid[i] for i, rs in sorted(rows_for.items()) if len(rs) > 1]
    if dupes:
        flags.append(_flag("duplicate_interval", "error", "interval appears more than once", dupes))
    missing = [grid[i] for i in range(n) if i not in rows_for]
    if missing:
        flags.append(_flag("missing_interval", "error", "interval missing from the data", missing))

    take = np.full(n, -1, dtype=np.int64)
    for i, rs in rows_for.items():
        if len(rs) == 1:
            take[i] = rs[0]
    series: dict[str, FloatArray] = {}
    for name, values in raw.series.items():
        out = np.full(n, np.nan)
        ok = take >= 0
        out[ok] = values[take[ok]]
        out.setflags(write=False)
        series[name] = out

    frame = Frame(
        index=tuple(grid),
        series=series,
        timezone=tz,
        quarter=f"{year:04d}-Q{quarter}",
        source_sha256=(raw.sha256,),
        interval=step,
        provisional=provisional,
    )
    return frame, flags


def check(cfg: PlantConfig, frame: Frame) -> list[QualityFlag]:
    flags: list[QualityFlag] = []
    for name in required_series(cfg):
        values = frame.series.get(name)
        if values is None:
            flags.append(QualityFlag("missing_series", "error", "series not in the data", name))
            continue
        nan = np.flatnonzero(np.isnan(values))
        if nan.size:
            flags.append(
                _flag(
                    "missing_value",
                    "error",
                    "no value for interval",
                    [frame.index[i] for i in nan],
                    name,
                )
            )
        neg = np.flatnonzero(values < 0)
        if neg.size:
            flags.append(
                _flag(
                    "negative_value", "error", "negative value", [frame.index[i] for i in neg], name
                )
            )
    flags += _storage_balance(cfg, frame)
    return flags


def _storage_balance(cfg: PlantConfig, frame: Frame) -> list[QualityFlag]:
    """Flag intervals where measured inventory moves differently from metered flows.

    Losses (boil-off, venting) show up here; they are not modelled, only reported. A
    pass-through storage has inventory zero, so what flows in must flow out each interval.
    Product from a unit that splits its output is counted by the metered flow, otherwise by
    the unit's production series; deliveries from an RFNBO supplier by their metered flow.
    """
    flags: list[QualityFlag] = []
    tol = cfg.settings.balance_tolerance_kg_per_interval
    for s in cfg.blocks_of(StorageUnit):
        if s.pass_through:
            inv: FloatArray | None = np.zeros(len(frame))
        else:
            inv = frame.series.get(s.inventory_series or "")
        if inv is None:
            continue
        net = np.zeros(len(frame))
        parts: list[FloatArray | None] = []
        for f in cfg.flows_into(s.id):
            unit = cfg.block(f.source)
            if isinstance(unit, ProcessingUnit):
                parts.append(frame.series.get(f.series or unit.production_series or ""))
            elif isinstance(unit, RfnboSupply):
                parts.append(frame.series.get(f.series or ""))
        for f in cfg.flows_out_of(s.id):
            out = frame.series.get(f.series or "")
            parts.append(None if out is None else -out)
        if any(p is None for p in parts):
            continue
        for p in parts:
            assert p is not None
            net = net + p
        previous = np.concatenate(([s.opening_inventory_kg], inv[:-1]))
        residual = inv - previous - net
        bad = np.flatnonzero(np.abs(residual) > tol)
        if bad.size:
            flags.append(
                _flag(
                    "meter_balance",
                    "warning",
                    f"inventory change differs from metered flows by more than {tol} kg "
                    f"(largest {float(np.nanmax(np.abs(residual))):.3f} kg)",
                    [frame.index[i] for i in bad],
                    s.inventory_series or s.id,
                )
            )
    return flags


def prepare(
    cfg: PlantConfig, raw: RawData, year: int, quarter: int
) -> tuple[Frame, list[QualityFlag]]:
    frame, flags = align(raw, year, quarter, cfg.timezone)
    notes = [QualityFlag("non_numeric", "error", note) for note in raw.notes]
    return frame, read_notes(raw) + flags + notes + check(cfg, frame)


def read_notes(raw: RawData) -> list[QualityFlag]:
    """What reading did to the data (units converted, blank rows skipped), as info flags: they
    are listed with every run and sealed with it."""
    return [QualityFlag("data_read", "info", note) for note in raw.conversions]


def has_errors(flags: list[QualityFlag]) -> bool:
    return any(f.severity == "error" for f in flags)


def _flag(
    code: str,
    severity: Severity,
    message: str,
    stamps: list[dt.datetime],
    series: str | None = None,
) -> QualityFlag:
    return QualityFlag(code, severity, message, series, len(stamps), tuple(stamps[:_SHOW]))
