"""Data corrections: explicit, reasoned replacements applied on top of the original data
(build plan §11.2).

A correction sets one series to a value over an interval range [start, end) in UTC. It needs a
reason; it never changes the original file. Corrections are kept as a log, applied in order
after the data is aligned to the quarter's grid, and recorded in the sealed run with the
original file's hash and what each one replaced.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from rfnbo.data.ingest import FloatArray
from rfnbo.data.quality import Frame


class CorrectionError(ValueError):
    pass


class Correction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    series: str = Field(min_length=1)
    start: AwareDatetime  # first interval corrected (UTC interval start)
    end: AwareDatetime  # exclusive
    value: float
    reason: str = Field(min_length=1)

    @field_validator("start", "end")
    @classmethod
    def _utc(cls, v: dt.datetime) -> dt.datetime:
        return v.astimezone(dt.UTC)

    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("a correction needs a reason")
        return v.strip()

    @model_validator(mode="after")
    def _check(self) -> Correction:
        if self.end <= self.start:
            raise ValueError(f"correction {self.id}: end must be after start")
        if not math.isfinite(self.value):
            raise ValueError(f"correction {self.id}: value must be a number")
        return self


@dataclass(frozen=True)
class Applied:
    """What one correction replaced."""

    correction: Correction
    intervals: int
    missing_before: int  # intervals that had no value
    original_sum: float  # of the values that were present
    corrected_sum: float

    def record(self) -> dict[str, Any]:
        c = self.correction
        return {
            "id": c.id,
            "series": c.series,
            "start_utc": c.start.isoformat(),
            "end_utc": c.end.isoformat(),
            "value": c.value,
            "reason": c.reason,
            "intervals": self.intervals,
            "missing_before": self.missing_before,
            "original_sum": self.original_sum,
            "corrected_sum": self.corrected_sum,
        }


def apply(frame: Frame, corrections: Sequence[Correction]) -> tuple[Frame, list[Applied]]:
    """The frame with every correction applied, and what each replaced. Two corrections may
    not touch the same interval of the same series: each value has one stated origin."""
    ids = [c.id for c in corrections]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise CorrectionError(f"correction id(s) used more than once: {dupes}")
    series: dict[str, FloatArray] = {}
    touched: dict[str, np.ndarray] = {}
    applied: list[Applied] = []
    for c in corrections:
        if c.series not in frame.series:
            raise CorrectionError(f"correction {c.id}: series {c.series!r} is not in the data")
        try:
            s, e = frame.position(c.start), frame.position(c.end)
        except ValueError as exc:
            raise CorrectionError(f"correction {c.id}: {exc}") from exc
        if s >= e:
            raise CorrectionError(f"correction {c.id}: covers no interval of {frame.quarter}")
        values = series.setdefault(c.series, np.array(frame.series[c.series], dtype=np.float64))
        mask = touched.setdefault(c.series, np.zeros(len(frame), dtype=bool))
        if mask[s:e].any():
            raise CorrectionError(f"correction {c.id} overlaps another correction of {c.series}")
        mask[s:e] = True
        before = values[s:e]
        present = ~np.isnan(before)
        applied.append(
            Applied(
                c,
                e - s,
                int((~present).sum()),
                float(before[present].sum()),
                c.value * (e - s),
            )
        )
        values[s:e] = c.value
    out = dict(frame.series)
    for name, values in series.items():
        values.setflags(write=False)
        out[name] = values
    corrected = Frame(frame.index, out, frame.timezone, frame.quarter, frame.source_sha256)
    return corrected, applied
