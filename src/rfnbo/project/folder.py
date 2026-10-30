"""A project folder: every input of a quarter's run as plain files (build plan §11.2).

    project.json        plant config, quarter, data files and their column mapping
    consignments.json   consignment requirements (a JSON list, as ``rfnbo optimise``)
    pos.csv             proofs of sustainability (as ``rfnbo optimise --pos``)
    corrections.json    data corrections, each with a reason
    opening.json        manual opening ledger per storage (or ``opening_from`` a sealed run)
    data/               the data files as uploaded, never changed
    runs/<label>/       sealed runs made from the folder

The CLI (``rfnbo optimise --project``) and the Data screen (``rfnbo data``) read the same files.
Nothing is filled silently: the data is aligned, the corrections applied, and every quality
flag reported.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from rfnbo.config import load_config
from rfnbo.config.schema import OpeningLedgerEntry, PlantConfig, StorageUnit
from rfnbo.data import ingest, quality
from rfnbo.data.ingest import DataSource
from rfnbo.data.quality import Frame, QualityFlag
from rfnbo.model.entities import ConsignmentSpec, ProofOfSustainability
from rfnbo.model.pos import load_pos
from rfnbo.project.corrections import Applied, Correction, apply

PROJECT_FILE = "project.json"
CONSIGNMENTS_FILE = "consignments.json"
POS_FILE = "pos.csv"
CORRECTIONS_FILE = "corrections.json"
OPENING_FILE = "opening.json"
DATA_DIR = "data"
RUNS_DIR = "runs"
QUARTER = re.compile(r"(\d{4})-Q([1-4])")


class ProjectError(ValueError):
    pass


class SourceFile(DataSource):
    """A data file in the project, with how to read it. Its mapping may still be empty while
    it is being set up; such a file is not read."""

    file: str  # relative to the project folder
    columns: dict[str, str] = Field(default_factory=dict)  # series name -> column header


class OpeningEntry(BaseModel):
    """A storage's opening position entered by hand."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    opening_inventory_kg: float = Field(ge=0)
    opening_ledger: list[OpeningLedgerEntry] = Field(default_factory=list)


class ProjectFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    config: str  # plant config JSON, relative to the project folder or absolute
    quarter: str
    sources: list[SourceFile] = Field(default_factory=list)
    opening_from: str | None = None  # a sealed run of the previous quarter
    batch_materiality_MJ: float = Field(default=120.0, gt=0)

    @field_validator("quarter")
    @classmethod
    def _quarter(cls, v: str) -> str:
        if not QUARTER.fullmatch(v):
            raise ValueError(f"quarter must look like 2026-Q4, got {v!r}")
        return v

    @property
    def year_quarter(self) -> tuple[int, int]:
        m = QUARTER.fullmatch(self.quarter)
        assert m
        return int(m.group(1)), int(m.group(2))


_CONS = TypeAdapter(list[ConsignmentSpec])
_CORR = TypeAdapter(list[Correction])
_OPEN = TypeAdapter(dict[str, OpeningEntry])


@dataclass
class Project:
    """A project folder's contents, read and written as plain files."""

    root: Path
    file: ProjectFile
    consignments: list[ConsignmentSpec] = field(default_factory=list)
    pos: list[ProofOfSustainability] = field(default_factory=list)
    corrections: list[Correction] = field(default_factory=list)
    opening: dict[str, OpeningEntry] = field(default_factory=dict)

    @classmethod
    def new(cls, root: str | Path, config: str, quarter: str) -> Project:
        return cls(Path(root), ProjectFile(config=config, quarter=quarter))

    @classmethod
    def load(cls, root: str | Path) -> Project:
        r = Path(root)
        try:
            pf = ProjectFile.model_validate_json((r / PROJECT_FILE).read_text("utf-8"))
        except FileNotFoundError as exc:
            raise ProjectError(f"{r}: no {PROJECT_FILE}") from exc

        def read(name: str) -> str | None:
            p = r / name
            return p.read_text("utf-8") if p.exists() else None

        cons, corr, opening = read(CONSIGNMENTS_FILE), read(CORRECTIONS_FILE), read(OPENING_FILE)
        return cls(
            r,
            pf,
            _CONS.validate_json(cons) if cons else [],
            load_pos(r / POS_FILE) if (r / POS_FILE).exists() else [],
            _CORR.validate_json(corr) if corr else [],
            _OPEN.validate_json(opening) if opening else {},
        )

    def save(self) -> None:
        """Write every file (sorted, indented JSON; CSV for PoS) so the folder diffs cleanly."""
        self.root.mkdir(parents=True, exist_ok=True)

        def write(name: str, data: Any) -> None:
            text = json.dumps(data, indent=1, ensure_ascii=False) + "\n"
            (self.root / name).write_text(text, encoding="utf-8")

        write(PROJECT_FILE, self.file.model_dump(mode="json"))
        write(CONSIGNMENTS_FILE, [c.model_dump(mode="json") for c in self.consignments])
        write(CORRECTIONS_FILE, [c.model_dump(mode="json") for c in self.corrections])
        write(OPENING_FILE, {k: v.model_dump(mode="json") for k, v in sorted(self.opening.items())})
        from rfnbo.model.pos import CSV_COLUMNS

        lines = [",".join(CSV_COLUMNS)]
        for p in self.pos:
            d = p.model_dump(mode="json")
            lines.append(",".join(_csv_cell(d[c]) for c in CSV_COLUMNS))
        (self.root / POS_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.root / p

    def config(self) -> PlantConfig:
        """The plant config, with this quarter's opening position applied."""
        from rfnbo.trace import run as sealed

        cfg = load_config(self.path(self.file.config))
        if self.file.opening_from and self.opening:
            raise ProjectError("opening ledger: give a previous sealed run or manual entries")
        if self.file.opening_from:
            return sealed.with_opening_from(
                cfg, self.path(self.file.opening_from), self.file.quarter
            )
        if not self.opening:
            return cfg
        stores = {s.id for s in cfg.blocks_of(StorageUnit)}
        unknown = sorted(set(self.opening) - stores)
        if unknown:
            raise ProjectError(f"opening ledger for unknown storage(s) {unknown}")
        blocks = []
        for b in cfg.blocks:
            if isinstance(b, StorageUnit) and b.id in self.opening:
                o = self.opening[b.id]
                b = StorageUnit.model_validate(
                    b.model_dump()
                    | {
                        "opening_inventory_kg": o.opening_inventory_kg,
                        "opening_ledger": [e.model_dump() for e in o.opening_ledger],
                    }
                )
            blocks.append(b)
        return cfg.model_copy(update={"blocks": blocks})


def _csv_cell(v: Any) -> str:
    s = str(v)
    return f'"{s.replace(chr(34), chr(34) * 2)}"' if any(c in s for c in ',"\n') else s


# ---- reading the data -----------------------------------------------------------------------


def _norm(text: str) -> str:
    sub = str.maketrans("₂₃", "23")
    return re.sub(r"[^a-z0-9]", "", text.translate(sub).lower())


def headers(path: str | Path, fmt: str, sheet: str | None = None) -> list[str]:
    """The header row of a data file."""
    if fmt == "excel":
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb[sheet] if sheet else wb.worksheets[0]
        row = next(ws.iter_rows(max_row=1, values_only=True), ())
        wb.close()
        return [str(c).strip() for c in row if c is not None]
    with Path(path).open(encoding="utf-8-sig") as f:
        first = f.readline()
    import csv

    return [h.strip() for h in next(csv.reader([first]), []) if h.strip()]


def sheets(path: str | Path) -> list[str]:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True)
    names = list(wb.sheetnames)
    wb.close()
    return names


def auto_map(required: Sequence[str], heads: Sequence[str]) -> dict[str, str]:
    """Series whose name agrees with a header (ignoring case, spaces and punctuation)."""
    by_norm = {_norm(h): h for h in heads}
    return {s: by_norm[_norm(s)] for s in required if _norm(s) in by_norm}


def guess_time_column(heads: Sequence[str]) -> str | None:
    for h in heads:
        n = _norm(h)
        if "utc" in n or n in ("timestamp", "time", "timestamputc", "start"):
            return h
    return None


@dataclass(frozen=True)
class Prepared:
    """A project's data as the calculator will use it."""

    config: PlantConfig
    frame: Frame
    original_flags: list[QualityFlag]  # before corrections
    flags: list[QualityFlag]  # after corrections
    applied: list[Applied]

    @property
    def ok(self) -> bool:
        return not quality.has_errors(self.flags)

    def correction_records(self) -> list[dict[str, Any]]:
        return [a.record() for a in self.applied]


def prepare(project: Project) -> Prepared:
    """Read, align, correct and check the project's data (build plan §11.2)."""
    cfg = project.config()
    if not project.file.sources:
        raise ProjectError("no data file in the project")
    year, q = project.file.year_quarter
    series: dict[str, Any] = {}
    shas: list[str] = []
    align_flags: list[QualityFlag] = []
    index: tuple[Any, ...] | None = None
    step = None
    for src in project.file.sources:
        if not src.columns:
            continue
        spec = DataSource.model_validate(src.model_dump(exclude={"file"}))
        raw = ingest.load(project.path(src.file), spec)
        try:
            frame, flags = quality.align(raw, year, q, cfg.timezone)
        except ValueError as exc:
            raise ProjectError(f"{src.file}: {exc}") from exc
        if index is not None and frame.index != index:
            raise ProjectError(
                f"{src.file} covers {len(frame)} intervals, the other data {len(index)}: every "
                "data file of a project covers the same period"
            )
        if step is not None and frame.interval != step:
            raise ProjectError(
                f"{src.file} has a {frame.interval} time step, the other data {step}: "
                "every data file of a project has the same step"
            )
        step = frame.interval
        provisional = frame.provisional
        align_flags += (
            quality.read_notes(raw)
            + flags
            + [QualityFlag("non_numeric", "error", n) for n in raw.notes]
        )
        index = frame.index
        shas.append(raw.sha256)
        for name, values in frame.series.items():
            if name in series:
                raise ProjectError(f"series {name!r} is mapped in more than one data file")
            series[name] = values
    if index is None:
        raise ProjectError("no column of any data file is mapped to a series")
    assert step is not None
    original = Frame(
        index, series, cfg.timezone, f"{year:04d}-Q{q}", tuple(shas), step, provisional
    )
    corrected, applied = apply(original, project.corrections)
    before = align_flags + quality.check(cfg, original)
    after_check = quality.check(cfg, corrected)
    # Missing intervals that every required series now has a value for were corrected
    # explicitly; the corrections log says so. Anything still missing stays an error.
    still_missing = any(f.code in ("missing_value", "missing_series") for f in after_check)
    after = [f for f in align_flags if f.code != "missing_interval" or still_missing]
    return Prepared(cfg, corrected, before, after + after_check, applied)
