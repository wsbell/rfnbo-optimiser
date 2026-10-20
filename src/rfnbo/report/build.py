"""Reports from a sealed run: recalculate from the sealed files, then write tables."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from rfnbo.report.csv_export import write_csv
from rfnbo.report.excel import write_xlsx
from rfnbo.report.tables import all_tables
from rfnbo.trace import run as sealed


@dataclass
class Written:
    xlsx: Path
    csv: list[Path] = field(default_factory=list)


def report_sealed(
    run_dir: str | Path,
    out: str | Path,
    baseline_dir: str | Path | None = None,
    with_csv: bool = False,
) -> Written:
    """Write the report for a sealed run (and its baseline, if given) into ``out``.

    Numbers come from re-running the calculator on the sealed files, never from the record.
    """
    rec = sealed.load_record(run_dir)
    run = sealed.recalculate(run_dir)
    baseline = sealed.recalculate(baseline_dir) if baseline_dir else None
    prov = rec["provenance"]
    tables = all_tables(
        run, rec["run_id"], prov["quality_flags"], baseline, prov.get("corrections", ())
    )
    out = Path(out)
    xlsx = write_xlsx(tables, out / f"report_{rec['run_id']}.xlsx")
    paths = write_csv(tables, out / f"csv_{rec['run_id']}") if with_csv else []
    return Written(xlsx, paths)
