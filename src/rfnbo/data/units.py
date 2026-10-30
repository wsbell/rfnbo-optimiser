"""Units of data columns, converted on reading to the unit a series is named in.

A series name ends in its unit: ``_kWh`` (energy per interval) or ``_kg`` (mass per
interval). A data column may be in another unit of the same kind (MWh, tonnes), or a rate
(MW, t/h) that becomes an amount by multiplying by the interval's length. The column's unit is
read from its header, as in "Solar Power (MWh)" or "H2 flow [t/h]", or set in the mapping;
every conversion is reported (decided 2026-10-30), never applied silently.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Unit:
    kind: str  # "energy" or "mass"
    factor: float  # to kWh or kg
    rate: bool = False  # per hour: multiplied by the interval's length in hours


TARGETS = {"kWh": "energy", "kg": "mass"}

UNITS: dict[str, Unit] = {
    "wh": Unit("energy", 1e-3),
    "kwh": Unit("energy", 1.0),
    "mwh": Unit("energy", 1e3),
    "gwh": Unit("energy", 1e6),
    "w": Unit("energy", 1e-3, rate=True),
    "kw": Unit("energy", 1.0, rate=True),
    "mw": Unit("energy", 1e3, rate=True),
    "gw": Unit("energy", 1e6, rate=True),
    "g": Unit("mass", 1e-3),
    "kg": Unit("mass", 1.0),
    "t": Unit("mass", 1e3),
    "tonne": Unit("mass", 1e3),
    "tonnes": Unit("mass", 1e3),
    "kg/h": Unit("mass", 1.0, rate=True),
    "t/h": Unit("mass", 1e3, rate=True),
    "tonne/h": Unit("mass", 1e3, rate=True),
    "tonnes/h": Unit("mass", 1e3, rate=True),
}

_BRACKETED = re.compile(r"[\(\[]\s*([^\)\]]+?)\s*[\)\]]")


def normalise(unit: str) -> str:
    u = unit.strip().lower().replace(" ", "").replace("per", "/").replace("hr", "h")
    return u


def from_header(header: str) -> str | None:
    """The unit a header names, in brackets ("Solar Power (MWh)") or as its last word
    ("H2 flow t/h"), if it is one this module knows; as written in the header."""
    candidates = [str(c) for c in _BRACKETED.findall(header)]
    words = header.split()
    if words:
        candidates.append(words[-1])
    for c in reversed(candidates):
        if normalise(c) in UNITS:
            return c.strip()
    return None


def target_of(series: str) -> str | None:
    """The unit a series is named in (its last ``_`` part), if it is one data converts to."""
    tail = series.rsplit("_", 1)[-1]
    return tail if tail in TARGETS else None


def factor(unit: str, series: str, interval: dt.timedelta) -> float:
    """What a column in ``unit`` is multiplied by to give ``series`` per interval."""
    u = UNITS.get(normalise(unit))
    if u is None:
        raise ValueError(f"unknown unit {unit!r} (known: {', '.join(sorted(UNITS))})")
    target = target_of(series)
    if target is None:
        raise ValueError(f"series {series!r} does not end in a unit (_kWh or _kg) to convert to")
    if TARGETS[target] != u.kind:
        raise ValueError(f"{unit} is {u.kind}, but {series} is {TARGETS[target]} ({target})")
    hours = interval.total_seconds() / 3600 if u.rate else 1.0
    return u.factor * hours
