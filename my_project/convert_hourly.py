"""Convert the hourly test data spreadsheet into the 15-minute data file the optimiser reads.

    uv run python my_project/convert_hourly.py my_project/test_data.xlsx

The spreadsheet (one sheet: Time, Solar Power (MWh), Electrolyser Power (MWh), H2 Production
(tonne), Compressor Power (MWh), Compressor Flow (tonne)) has one row per hour. The optimiser
reads 15-minute intervals in kWh and kg with UTC interval starts, so each hour becomes four
intervals, each a quarter of the hour's energy or mass:

- MWh x 1000 / 4 = kWh per interval; tonne x 1000 / 4 = kg per interval.
- Times are read as UTC interval starts and rounded to the minute: Excel stores them as
  fractions of a day, and a few have drifted by a millisecond (e.g. 22:59:59.998).
- A row without a time and with no numbers in it (the sheet's last row holds only #REF!
  errors) is skipped and reported; any other unreadable row stops the conversion.

Splitting an hour evenly is an assumption: the true 15-minute profile is not in the data.
The spreadsheet is never changed; the output's header records its SHA-256.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import sys
from pathlib import Path

import openpyxl

COLUMNS = (  # spreadsheet header -> (data file header, factor to the 15-minute unit)
    ("Solar Power (MWh)", "Solar power available (kWh)", 1000 / 4),
    ("Electrolyser Power (MWh)", "Electrolyser power (kWh)", 1000 / 4),
    ("H2 Production (tonne)", "H2 production (kg)", 1000 / 4),
    ("Compressor Power (MWh)", "Compressor power (kWh)", 1000 / 4),
    ("Compressor Flow (tonne)", "Compressor flow (kg)", 1000 / 4),
)
QUARTER_HOUR = dt.timedelta(minutes=15)


def convert(source: Path, out: Path) -> None:
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    wb = openpyxl.load_workbook(source, read_only=True, data_only=True)
    rows = list(wb.worksheets[0].iter_rows(values_only=True))
    header = [str(h).strip() if h is not None else "" for h in rows[0]]
    try:
        time_col = header.index("Time")
        cols = [header.index(name) for name, _, _ in COLUMNS]
    except ValueError as exc:
        raise SystemExit(f"{source}: expected columns Time and {[c[0] for c in COLUMNS]}") from exc

    out_rows = []
    skipped = []
    for n, row in enumerate(rows[1:], start=2):
        t = row[time_col]
        values = [row[c] for c in cols]
        if t is None and not any(isinstance(v, int | float) for v in values):
            skipped.append(n)
            continue
        if not isinstance(t, dt.datetime) or not all(isinstance(v, int | float) for v in values):
            raise SystemExit(f"{source} row {n}: unreadable time or value {row[: len(cols) + 1]}")
        start = (t + dt.timedelta(seconds=30)).replace(second=0, microsecond=0)
        if start.minute:
            raise SystemExit(f"{source} row {n}: {t} is not on the hour")
        per_interval = [
            repr(float(v) * factor) for v, (_, _, factor) in zip(values, COLUMNS, strict=True)
        ]
        for q in range(4):
            stamp = (start + q * QUARTER_HOUR).strftime("%Y-%m-%dT%H:%M:%SZ")
            out_rows.append([stamp, *per_interval])

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["Start (UTC)"] + [name for _, name, _ in COLUMNS])
        w.writerows(out_rows)
    print(f"{source} (sha256 {sha}): {len(rows) - 1 - len(skipped)} hours")
    print(f"wrote {out}: {len(out_rows)} intervals, {out_rows[0][0]} to {out_rows[-1][0]}")
    if skipped:
        print(f"skipped rows with no time and no numbers: {skipped}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    src = Path(sys.argv[1])
    convert(src, Path(__file__).parent / "data" / "test_data_15min.csv")
