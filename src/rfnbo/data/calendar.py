"""Plant-time-zone calendar.

Timestamps are stored in UTC and mark the *start* of a 15-minute interval. Months, clock
hours and quarters are derived here, and only here, from the plant time zone, so a UTC
month boundary is never mistaken for a local one and DST days have 92 or 100 intervals.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

INTERVAL = dt.timedelta(minutes=15)
UTC = dt.UTC


def _require_utc(ts: dt.datetime) -> None:
    if ts.tzinfo is None or ts.utcoffset() != dt.timedelta(0):
        raise ValueError(f"timestamp must be timezone-aware UTC, got {ts!r}")


def to_local(ts: dt.datetime, tz: str) -> dt.datetime:
    _require_utc(ts)
    return ts.astimezone(ZoneInfo(tz))


def month_key(ts: dt.datetime, tz: str) -> str:
    """Local calendar month of the interval starting at ``ts``, as ``YYYY-MM``."""
    local = to_local(ts, tz)
    return f"{local.year:04d}-{local.month:02d}"


def quarter_key(ts: dt.datetime, tz: str) -> str:
    """Local calendar quarter of the interval starting at ``ts``, as ``YYYY-Qn``."""
    local = to_local(ts, tz)
    return f"{local.year:04d}-Q{(local.month - 1) // 3 + 1}"


def hour_key(ts: dt.datetime, tz: str) -> dt.datetime:
    """UTC start of the local clock hour containing ``ts``.

    Keyed in UTC so that the repeated hour on the autumn DST change gives two distinct
    clock hours, and zones with a non-whole-hour offset still align to local hours.
    """
    local = to_local(ts, tz)
    floored = local.replace(minute=0, second=0, microsecond=0)
    return floored.astimezone(UTC)


def local_date(ts: dt.datetime, tz: str) -> dt.date:
    return to_local(ts, tz).date()


def hourly_correlation_applies(ts: dt.datetime, tz: str, switch_date: dt.date) -> bool:
    """Whether hourly temporal correlation applies to the interval starting at ``ts``."""
    return local_date(ts, tz) >= switch_date


def _local_midnight_utc(year: int, month: int, tz: str) -> dt.datetime:
    # Midnight is never inside a DST gap in the zones this tool targets; zoneinfo
    # resolves it consistently with fold=0 either way.
    return dt.datetime(year, month, 1, tzinfo=ZoneInfo(tz)).astimezone(UTC)


def month_bounds(year: int, month: int, tz: str) -> tuple[dt.datetime, dt.datetime]:
    """UTC [start, end) of a local calendar month."""
    nxt = (year + month // 12, month % 12 + 1)
    return _local_midnight_utc(year, month, tz), _local_midnight_utc(*nxt, tz)


def quarter_bounds(year: int, quarter: int, tz: str) -> tuple[dt.datetime, dt.datetime]:
    """UTC [start, end) of a local calendar quarter."""
    if quarter not in (1, 2, 3, 4):
        raise ValueError(f"quarter must be 1-4, got {quarter}")
    first = 3 * (quarter - 1) + 1
    start, _ = month_bounds(year, first, tz)
    _, end = month_bounds(year, first + 2, tz)
    return start, end


def intervals(start: dt.datetime, end: dt.datetime) -> list[dt.datetime]:
    """UTC starts of every 15-minute interval in [start, end)."""
    _require_utc(start)
    _require_utc(end)
    if (end - start) % INTERVAL:
        raise ValueError("range is not a whole number of 15-minute intervals")
    n = (end - start) // INTERVAL
    return [start + i * INTERVAL for i in range(n)]


def quarter_intervals(year: int, quarter: int, tz: str) -> list[dt.datetime]:
    return intervals(*quarter_bounds(year, quarter, tz))


def from_local(ts: dt.datetime, tz: str) -> dt.datetime:
    """UTC for a time entered in the plant time zone. A time with an offset is taken as given.
    A wall time that occurs twice (the autumn clock change) or never (spring) is refused, so
    the user states the offset rather than the app guessing."""
    if ts.tzinfo is not None:
        return ts.astimezone(dt.UTC)
    zone = ZoneInfo(tz)
    found = {
        c.astimezone(dt.UTC)
        for c in (ts.replace(tzinfo=zone, fold=0), ts.replace(tzinfo=zone, fold=1))
        if c.astimezone(dt.UTC).astimezone(zone).replace(tzinfo=None) == ts
    }
    if len(found) != 1:
        what = "occurs twice" if found else "does not exist"
        raise ValueError(
            f"{ts.isoformat(sep=' ')} {what} in {tz} (clock change); give the UTC offset, "
            "e.g. 2026-10-25 02:15+02:00"
        )
    return found.pop()
