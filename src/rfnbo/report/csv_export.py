"""Write report tables as CSV files, one per table, with units in the header."""

from __future__ import annotations

import csv
import datetime as dt
import re
from collections.abc import Sequence
from pathlib import Path

from rfnbo.report.tables import Table


def file_name(table: Table) -> str:
    return re.sub(r"[^a-z0-9]+", "_", table.name.lower()).strip("_") + ".csv"


def write_csv(tables: Sequence[Table], directory: str | Path) -> list[Path]:
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for table in tables:
        path = out / file_name(table)
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([c.title for c in table.columns])
            for row in table.rows:
                w.writerow([_cell(row.get(c.key)) for c in table.columns])
        written.append(path)
    return written


def _cell(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float):
        return repr(v)  # full precision, reads back exactly
    if isinstance(v, dt.datetime):
        return v.isoformat(timespec="minutes")
    return str(v)
