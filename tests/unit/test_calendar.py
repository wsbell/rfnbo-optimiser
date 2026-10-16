from __future__ import annotations

import datetime as dt
import itertools
from collections import Counter

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rfnbo.data import calendar as cal

TZ = "Europe/Amsterdam"
UTC = dt.UTC


def test_q4_2026_has_8836_intervals() -> None:
    ivs = cal.quarter_intervals(2026, 4, TZ)
    assert len(ivs) == 8836
    assert ivs[0] == dt.datetime(2026, 9, 30, 22, 0, tzinfo=UTC)
    assert ivs[-1] == dt.datetime(2026, 12, 31, 22, 45, tzinfo=UTC)


def test_25_hour_day_on_25_october_2026() -> None:
    per_day = Counter(cal.local_date(t, TZ) for t in cal.quarter_intervals(2026, 4, TZ))
    assert per_day[dt.date(2026, 10, 25)] == 100
    assert {n for d, n in per_day.items() if d != dt.date(2026, 10, 25)} == {96}


def test_23_hour_day_in_spring() -> None:
    per_day = Counter(cal.local_date(t, TZ) for t in cal.quarter_intervals(2026, 1, TZ))
    assert per_day[dt.date(2026, 3, 29)] == 92


def test_month_key_uses_local_time() -> None:
    # 22:00 UTC on 30 Sep is 00:00 CEST on 1 Oct.
    assert cal.month_key(dt.datetime(2026, 9, 30, 21, 45, tzinfo=UTC), TZ) == "2026-09"
    assert cal.month_key(dt.datetime(2026, 9, 30, 22, 0, tzinfo=UTC), TZ) == "2026-10"
    assert cal.quarter_key(dt.datetime(2026, 9, 30, 22, 0, tzinfo=UTC), TZ) == "2026-Q4"


def test_repeated_hour_gives_two_clock_hours() -> None:
    # 02:00-03:00 local occurs twice on 25 Oct 2026: 00:00 UTC (CEST) and 01:00 UTC (CET).
    first = cal.hour_key(dt.datetime(2026, 10, 25, 0, 30, tzinfo=UTC), TZ)
    second = cal.hour_key(dt.datetime(2026, 10, 25, 1, 30, tzinfo=UTC), TZ)
    assert first == dt.datetime(2026, 10, 25, 0, 0, tzinfo=UTC)
    assert second == dt.datetime(2026, 10, 25, 1, 0, tzinfo=UTC)


def test_half_hour_offset_zone_aligns_to_local_hours() -> None:
    key = cal.hour_key(dt.datetime(2026, 1, 1, 0, 15, tzinfo=UTC), "Asia/Kolkata")
    assert key == dt.datetime(2025, 12, 31, 23, 30, tzinfo=UTC)


def test_hourly_correlation_switch_uses_local_date() -> None:
    switch = dt.date(2030, 1, 1)
    # 23:00 UTC on 31 Dec 2029 is 00:00 CET on 1 Jan 2030.
    assert not cal.hourly_correlation_applies(
        dt.datetime(2029, 12, 31, 22, 45, tzinfo=UTC), TZ, switch
    )
    assert cal.hourly_correlation_applies(dt.datetime(2029, 12, 31, 23, 0, tzinfo=UTC), TZ, switch)


def test_naive_timestamps_rejected() -> None:
    with pytest.raises(ValueError, match="UTC"):
        cal.month_key(dt.datetime(2026, 10, 1), TZ)
    with pytest.raises(ValueError, match="UTC"):
        cal.intervals(dt.datetime(2026, 10, 1), dt.datetime(2026, 10, 2))


def test_partial_interval_rejected() -> None:
    start = dt.datetime(2026, 10, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="whole number"):
        cal.intervals(start, start + dt.timedelta(minutes=20))


@given(
    year=st.integers(2024, 2032),
    quarter=st.integers(1, 4),
    tz=st.sampled_from([TZ, "Europe/London", "UTC", "Australia/Sydney", "Asia/Kolkata"]),
)
def test_quarter_intervals_partition_into_whole_local_months(
    year: int, quarter: int, tz: str
) -> None:
    ivs = cal.quarter_intervals(year, quarter, tz)
    months = [cal.month_key(t, tz) for t in ivs]
    first = 3 * (quarter - 1) + 1
    assert sorted(set(months)) == [f"{year:04d}-{m:02d}" for m in range(first, first + 3)]
    # Months are contiguous runs: each month key changes exactly twice across the quarter.
    assert sum(a != b for a, b in itertools.pairwise(months)) == 2
    assert {cal.quarter_key(t, tz) for t in ivs} == {f"{year:04d}-Q{quarter}"}
    for m in range(first, first + 3):
        start, end = cal.month_bounds(year, m, tz)
        assert months.count(f"{year:04d}-{m:02d}") == (end - start) // cal.INTERVAL
