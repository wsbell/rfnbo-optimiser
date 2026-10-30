"""The Data screen's state: a project folder being set up, checked and run (build plan §11.2).

No UI imports: every action the page offers is a method here, so the screen is unit-tested
without a browser. Changes are written to the project folder on :meth:`Workspace.save`; the CLI
(``rfnbo optimise --project``) reads the same files.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import ValidationError

from rfnbo.config import ConfigError, load_config
from rfnbo.config.schema import ConsignmentOutput, OpeningLedgerEntry, PlantConfig, StorageUnit
from rfnbo.config.validate import Issue
from rfnbo.data import calendar as cal
from rfnbo.data import quality, units
from rfnbo.data.ingest import DataSource
from rfnbo.model.entities import ConsignmentSpec, ProofOfSustainability
from rfnbo.model.pos import pos_rules
from rfnbo.project import folder as F
from rfnbo.project.corrections import Correction, CorrectionError
from rfnbo.project.folder import OpeningEntry, Prepared, Project, ProjectError, SourceFile


class WorkspaceError(ValueError):
    """An action that cannot be applied; the message says why."""


def _errors(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(x) for x in e['loc']) or 'value'}: {e['msg']}" for e in exc.errors()
    )


def _unit(series: str, header: str) -> str | None:
    """The unit a header names, when the series is named in a unit it converts to. A column
    of the wrong kind (a mass column for an energy series) is refused."""
    unit = units.from_header(header)
    if unit is None or units.target_of(series) is None:
        return None
    try:
        units.factor(unit, series, cal.INTERVAL)
    except ValueError as exc:
        raise WorkspaceError(f"column {header!r}: {exc}") from exc
    return unit


def _put[T: (ConsignmentSpec, ProofOfSustainability)](
    items: Sequence[T], item: T, replace: str | None, what: str
) -> list[T]:
    """``items`` with ``item`` added, or put in the place of the one whose id is ``replace``;
    ids stay unique."""
    if replace is not None and not any(x.id == replace for x in items):
        raise WorkspaceError(f"no {what} {replace}")
    if any(x.id == item.id and x.id != replace for x in items):
        raise WorkspaceError(f"{what} {item.id} already exists")
    if replace is not None:
        return [item if x.id == replace else x for x in items]
    out = list(items)
    out.append(item)
    return out


@dataclass(frozen=True)
class MappingRow:
    series: str
    file: str | None  # data file the series is mapped in
    header: str | None

    @property
    def mapped(self) -> bool:
        return self.header is not None


@dataclass(frozen=True)
class PlantSummary:
    config: str  # as chosen: relative to the project folder or absolute
    path: Path
    plant: PlantConfig | None  # None: the file is missing, not JSON, or invalid
    issues: list[str]
    # A draft (a plant config still being drawn) can be set up, mapped and edited, but not run.
    draft: bool = False
    effective_from: dt.date | None = None

    @property
    def ok(self) -> bool:
        return self.plant is not None

    @property
    def usable(self) -> bool:
        """Valid, or a draft that can be set up and finished on the Plant tab."""
        return self.plant is not None or self.draft


@dataclass(frozen=True)
class Step:
    """One item of the Run tab's checklist, and the badge on the tab it belongs to."""

    tab: str
    ok: bool
    detail: str
    badge: str = ""  # short text for the tab, e.g. "12/14"
    required: bool = True  # an optional step never blocks a run


class Workspace:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.project: Project | None = (
            Project.load(self.root) if (self.root / F.PROJECT_FILE).exists() else None
        )
        self._prepared: Prepared | None = None

    # ---- setup -----------------------------------------------------------------------------

    @property
    def ready(self) -> bool:
        return self.project is not None

    def _p(self) -> Project:
        if self.project is None:
            raise WorkspaceError("choose the plant config and quarter first")
        return self.project

    def plant_configs(self) -> list[str]:
        """Plant configs to choose from: the JSON files at the top of the project folder (not
        the project's own files), and the one in use wherever it is."""
        own = {F.PROJECT_FILE, F.CONSIGNMENTS_FILE, F.CORRECTIONS_FILE, F.OPENING_FILE}
        found = (
            sorted(p.name for p in self.root.glob("*.json") if p.name not in own)
            if self.root.is_dir()
            else []
        )
        if self.project is not None and self.project.file.config not in found:
            found.insert(0, self.project.file.config)
        return found

    def plant_summary(self, config: str) -> PlantSummary:
        """What a plant config file holds, or why it cannot be used. Never raises."""
        path = Path(config) if Path(config).is_absolute() else self.root / config
        try:
            raw = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            return PlantSummary(config, path, None, [str(exc)])
        if not isinstance(raw, dict) or "blocks" not in raw:
            return PlantSummary(config, path, None, ["not a plant config"])
        try:
            effective: dt.date | None = dt.date.fromisoformat(str(raw.get("effective_from")))
        except ValueError:
            effective = None
        try:
            cfg = load_config(path)
        except ConfigError as exc:
            issues = [str(i) for i in exc.issues]
            return PlantSummary(config, path, None, issues, draft=True, effective_from=effective)
        except (OSError, ValueError) as exc:
            return PlantSummary(config, path, None, [str(exc)])
        return PlantSummary(config, path, cfg, [], effective_from=cfg.effective_from)

    def add_plant_config(self, name: str, content: bytes) -> str:
        """Copy an uploaded plant config into the project folder, so the folder holds every
        input; returns its name there. Drafts are kept too: they open in the editor."""
        safe = Path(name).name
        own = {F.PROJECT_FILE, F.CONSIGNMENTS_FILE, F.CORRECTIONS_FILE, F.OPENING_FILE}
        if not safe.lower().endswith(".json"):
            raise WorkspaceError(f"{safe}: a plant config is a .json file")
        if safe in own:
            raise WorkspaceError(f"{safe} is one of the project's own files; rename the upload")
        try:
            if not isinstance(json.loads(content), dict):
                raise ValueError("not a JSON object")
        except ValueError as exc:
            raise WorkspaceError(f"{safe}: not a plant config: {exc}") from exc
        dest = self.root / safe
        if dest.exists() and dest.read_bytes() != content:
            raise WorkspaceError(f"{safe} already holds a different file; rename the upload")
        self.root.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        return safe

    def new_plant_name(self) -> str:
        """A free file name in the project folder for a new plant diagram."""
        n, name = 1, "plant.json"
        while (self.root / name).exists():
            n += 1
            name = f"plant-{n}.json"
        return name

    def create_plant(self) -> str:
        """Start a new plant diagram: an empty plant config in the project folder, drawn on the
        Plant tab. Returns its name."""
        from rfnbo.ui.bfd_editor.document import EditorDocument

        name = self.new_plant_name()
        self.root.mkdir(parents=True, exist_ok=True)
        EditorDocument.new().save(self.root / name)
        return name

    @staticmethod
    def quarters(effective_from: dt.date, today: dt.date) -> tuple[list[str], str]:
        """The quarters a plant config can be accounted for, newest first: from the one it
        takes effect in to ``today``'s; and the one to suggest, the last complete quarter."""

        def key(d: dt.date) -> tuple[int, int]:
            return d.year, (d.month - 1) // 3 + 1

        (y, q), last = key(effective_from), key(today)
        out = []
        while (y, q) <= last:
            out.append(f"{y}-Q{q}")
            y, q = (y + 1, 1) if q == 4 else (y, q + 1)
        out.reverse()
        return out, (out[1] if len(out) > 1 else out[0] if out else "")

    def setup(self, config: str, quarter: str) -> None:
        """Choose the plant config (path, relative to the folder or absolute) and quarter. A
        draft is accepted: data can be uploaded and mapped while it is finished; it is run only
        once valid."""
        summary = self.plant_summary(config)
        if not summary.usable:
            raise WorkspaceError(f"{config}: {'; '.join(summary.issues)}")
        try:
            if self.project is None:
                self.project = Project.new(self.root, config, quarter)
            else:
                self.project.file = self.project.file.model_copy(
                    update={
                        "config": config,
                        "quarter": F.ProjectFile(config=config, quarter=quarter).quarter,
                    }
                )
        except ValidationError as exc:
            raise WorkspaceError(_errors(exc)) from exc
        self._changed()

    def config(self) -> PlantConfig:
        p = self._p()
        try:
            return p.config()
        except (ProjectError, ConfigError, ValueError) as exc:  # ValueError: a sealed opening
            raise WorkspaceError(str(exc)) from exc

    @property
    def timezone(self) -> str:
        return self.config().timezone

    def required_series(self) -> list[str]:
        return quality.required_series(self.config())

    def _required_or_none(self) -> list[str]:
        """The series the plant needs, or none while it is a draft."""
        try:
            return self.required_series()
        except WorkspaceError:
            return []

    def _changed(self) -> None:
        self._prepared = None

    def save(self) -> None:
        self._p().save()

    # ---- data files and mapping ------------------------------------------------------------

    def add_data_file(self, name: str, content: bytes, sheet: str | None = None) -> int:
        """Store an uploaded file under data/ (never changed afterwards), read its headers and
        map every series whose name agrees with a header. Returns the file's index."""
        p = self._p()
        safe = Path(name).name
        fmt = "excel" if safe.lower().endswith((".xlsx", ".xlsm")) else "csv"
        if fmt == "csv" and not safe.lower().endswith(".csv"):
            raise WorkspaceError(f"{safe}: upload a .csv or .xlsx file")
        dest = self.root / F.DATA_DIR / safe
        if dest.exists() and dest.read_bytes() != content:
            raise WorkspaceError(f"data/{safe} already holds a different file; rename the upload")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        rel = f"{F.DATA_DIR}/{safe}"
        if fmt == "excel" and sheet is None:
            sheet = self._best_sheet(dest)
        heads = F.headers(dest, fmt, sheet)
        time_col = F.guess_time_column(heads) or (heads[0] if heads else "")
        taken = {s for src in p.file.sources for s in src.columns}
        auto = {
            s: h for s, h in F.auto_map(self._required_or_none(), heads).items() if s not in taken
        }
        src = SourceFile(
            file=rel,
            format=fmt,
            sheet=sheet,
            time_column=time_col,
            columns=auto,
            units={s: u for s, h in auto.items() if (u := _unit(s, h))},
        )
        sources = [s for s in p.file.sources if s.file != rel] + [src]
        p.file = p.file.model_copy(update={"sources": sources})
        self._changed()
        return len(sources) - 1

    def _best_sheet(self, path: Path) -> str:
        """The sheet whose headers match the most series; on a tie (a draft plant needs none
        yet), one with a time column, then the most columns, then the first."""
        required = self._required_or_none()
        names = F.sheets(path)

        def key(n: str) -> tuple[int, bool, int, int]:
            heads = F.headers(path, "excel", n)
            matched = len(F.auto_map(required, heads))
            return matched, F.guess_time_column(heads) is not None, len(heads), -names.index(n)

        return max(names, key=key)

    def remove_data_file(self, index: int) -> None:
        p = self._p()
        sources = list(p.file.sources)
        sources.pop(index)
        p.file = p.file.model_copy(update={"sources": sources})
        self._changed()

    def headers(self, index: int) -> list[str]:
        src = self._p().file.sources[index]
        return F.headers(self._p().path(src.file), src.format, src.sheet)

    def set_source(self, index: int, **fields: Any) -> None:
        """Change how a file is read: sheet, time column."""
        p = self._p()
        sources = list(p.file.sources)
        try:
            sources[index] = SourceFile.model_validate(sources[index].model_dump() | fields)
        except ValidationError as exc:
            raise WorkspaceError(_errors(exc)) from exc
        p.file = p.file.model_copy(update={"sources": sources})
        self._changed()

    def map_series(
        self, series: str, index: int | None, header: str | None, require: bool = True
    ) -> None:
        """Map ``series`` to a column of data file ``index`` (or unmap it with None). With
        ``require`` false the series need not be one the saved plant needs yet: the Plant tab
        maps a meter as it is drawn."""
        p = self._p()
        if require and series not in self.required_series():
            raise WorkspaceError(f"{series!r} is not a series this plant needs")
        sources = []
        for i, src in enumerate(p.file.sources):
            cols = {k: v for k, v in src.columns.items() if k != series}
            found = {k: v for k, v in src.units.items() if k != series}
            if i == index and header is not None:
                if header not in self.headers(i):
                    raise WorkspaceError(f"{src.file} has no column {header!r}")
                cols[series] = header
                if unit := _unit(series, header):  # the header's unit, converted on reading
                    found[series] = unit
            sources.append(src.model_copy(update={"columns": cols, "units": found}))
        p.file = p.file.model_copy(update={"sources": sources})
        self._changed()

    def set_unit(self, series: str, unit: str | None) -> None:
        """The unit a mapped series' column is in (converted on reading), or None to read it
        as it is."""
        p = self._p()
        if unit is not None:
            try:
                units.factor(unit, series, cal.INTERVAL)
            except ValueError as exc:
                raise WorkspaceError(str(exc)) from exc
        sources = []
        for src in p.file.sources:
            found = {k: v for k, v in src.units.items() if k != series}
            if series in src.columns and unit is not None:
                found[series] = unit
            sources.append(src.model_copy(update={"units": found}))
        p.file = p.file.model_copy(update={"sources": sources})
        self._changed()

    def unit_of(self, series: str) -> str | None:
        p = self._p()
        return next((s.units[series] for s in p.file.sources if series in s.units), None)

    def import_mapping(self, index: int, spec: dict[str, Any]) -> int:
        """Take a column mapping from a saved source spec (as ``rfnbo run --source``): every
        series whose column the file has. Returns how many were mapped."""
        try:
            ds = DataSource.model_validate(spec)
        except ValidationError as exc:
            raise WorkspaceError(_errors(exc)) from exc
        heads = set(self.headers(index))
        n = 0
        for series, header in ds.columns.items():
            if header in heads and series in self.required_series():
                self.map_series(series, index, header)
                n += 1
        if ds.time_column in heads:
            self.set_source(index, time_column=ds.time_column)
        for series, unit in ds.units.items():
            if series in ds.columns and ds.columns[series] in heads:
                self.set_unit(series, unit)
        return n

    def columns(self) -> list[tuple[int, str]]:
        """Every data column that can feed a series: (data file index, header), time columns
        left out."""
        if self.project is None:
            return []
        out = []
        for i, src in enumerate(self.project.file.sources):
            try:
                heads = self.headers(i)
            except (OSError, ValueError):
                continue
            out += [(i, h) for h in heads if h and h != src.time_column]
        return out

    def column_of(self, series: str) -> tuple[int, str] | None:
        """The data column a series is mapped to, if any."""
        if self.project is None:
            return None
        for i, src in enumerate(self.project.file.sources):
            if series in src.columns:
                return i, src.columns[series]
        return None

    def mapping(self) -> list[MappingRow]:
        p = self._p()
        where = {s: (src.file, h) for src in p.file.sources for s, h in src.columns.items()}
        return [MappingRow(s, *where.get(s, (None, None))) for s in self.required_series()]

    # ---- check and preview -----------------------------------------------------------------

    def prepared(self) -> Prepared:
        if self._prepared is None:
            try:
                self._prepared = F.prepare(self._p())
            except (ProjectError, CorrectionError, ConfigError) as exc:
                raise WorkspaceError(str(exc)) from exc
            except (OSError, ValueError) as exc:
                raise WorkspaceError(f"cannot read the data: {exc}") from exc
        return self._prepared

    def flags(self) -> list[quality.QualityFlag]:
        return self.prepared().flags

    def preview(
        self, start: int = 0, n: int = 96, series: Sequence[str] | None = None
    ) -> dict[str, Any]:
        """Rows of the corrected data in the plant time zone, with each value's origin."""
        prep = self.prepared()
        frame, tz = prep.frame, prep.config.timezone
        names = list(series or self.required_series())
        corrected = self._corrected_mask(prep)
        rows = []
        for i in range(start, min(start + n, len(frame))):
            row: dict[str, Any] = {
                "i": i,
                "local": cal.to_local(frame.index[i], tz).isoformat(timespec="minutes"),
                "utc": frame.index[i].isoformat(),
            }
            for s in names:
                v = frame.series[s][i] if s in frame.series else math.nan
                row[s] = None if np.isnan(v) else float(v)
                row[f"{s}__corrected"] = bool(corrected.get(s, np.zeros(0, bool))[i : i + 1].any())
            rows.append(row)
        return {"columns": names, "rows": rows, "intervals": len(frame)}

    @staticmethod
    def _corrected_mask(prep: Prepared) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        for a in prep.applied:
            c = a.correction
            m = out.setdefault(c.series, np.zeros(len(prep.frame), bool))
            m[prep.frame.position(c.start) : prep.frame.position(c.end)] = True
        return out

    def first_flagged(self) -> int:
        """Index of the first interval a flag names, to jump the preview to it."""
        prep = self.prepared()
        stamps = [t for f in prep.flags for t in f.examples]
        if not stamps:
            return 0
        return min(
            prep.frame.position(t) for t in stamps if prep.frame.index[0] <= t < prep.frame.end
        )

    # ---- status ----------------------------------------------------------------------------

    def status(self) -> list[Step]:
        """Where the project stands, tab by tab: the Run tab's checklist and the tab badges.
        Never raises; a step that cannot be checked says why."""
        if self.project is None:
            return [Step("Setup", False, "choose the plant config and quarter")]
        p = self.project
        steps = [Step("Setup", True, f"plant config {p.file.config}, quarter {p.file.quarter}")]
        summary = self.plant_summary(p.file.config)
        if not summary.ok:
            n = len(summary.issues)
            detail = (
                f"{n} issue(s) to fix: finish the diagram and map its meters"
                if summary.draft
                else f"plant config: {'; '.join(summary.issues)}"
            )
            return [*steps, Step("Plant", False, detail, badge=str(n) if summary.draft else "!")]
        try:
            self.config()
        except WorkspaceError as exc:
            return [*steps, Step("Plant", False, str(exc), badge="!")]
        steps.append(Step("Plant", True, "the diagram is valid", badge="✓"))
        rows = self.mapping()
        done = sum(r.mapped for r in rows)
        steps.append(
            Step(
                "Mapping",
                done == len(rows),
                f"{done} of {len(rows)} series mapped",
                badge=f"{done}/{len(rows)}",
            )
        )
        if done < len(rows):
            steps.append(Step("Preview", False, "map every series to check the data"))
        else:
            try:
                errors = sum(f.severity == "error" for f in self.flags())
            except WorkspaceError as exc:
                steps.append(Step("Preview", False, str(exc), badge="!"))
            else:
                steps.append(
                    Step(
                        "Preview",
                        errors == 0,
                        f"{errors} data quality error(s): correct them with a reason"
                        if errors
                        else "no data quality errors",
                        badge=str(errors) if errors else "✓",
                    )
                )
        try:
            frame = self.prepared().frame
        except WorkspaceError:
            frame = None
        if frame is not None and frame.provisional:
            until = cal.to_local(frame.end, frame.timezone).isoformat(sep=" ", timespec="minutes")
            steps.append(
                Step(
                    "Period",
                    True,
                    f"PROVISIONAL: the data ends {until}, so the run is quarter to date, closed "
                    "as if the quarter ended there; it cannot open the next quarter",
                    required=False,
                )
            )
        n = len(p.corrections)
        steps.append(
            Step("Corrections", True, f"{n} correction(s)", badge=str(n or ""), required=False)
        )
        n = len(p.consignments)
        steps.append(
            Step(
                "Consignments",
                n > 0,
                f"{n} consignment(s)" if n else "add at least one consignment",
                badge=str(n),
            )
        )
        issues = list(pos_rules(self.config(), p.pos, p.file.quarter))
        steps.append(
            Step(
                "PoS",
                not issues,
                "; ".join(str(i) for i in issues) or f"{len(p.pos)} PoS record(s)",
                badge=str(len(p.pos) or ""),
                required=bool(issues),
            )
        )
        if p.file.opening_from:
            opening = f"from sealed run {p.file.opening_from}"
        elif p.opening:
            opening = f"{len(p.opening)} storage(s) entered by hand"
        else:
            opening = "no opening entries in this project"
        steps.append(
            Step("Opening", True, opening, badge=str(len(p.opening) or ""), required=False)
        )
        return steps

    @staticmethod
    def blocking(steps: Iterable[Step]) -> list[Step]:
        """The steps that stop a run."""
        return [s for s in steps if s.required and not s.ok]

    def local(self, t: dt.datetime) -> str:
        """A UTC instant in the plant time zone with its UTC offset, as the correction form
        takes it back."""
        return cal.to_local(t, self.timezone).isoformat(sep=" ", timespec="minutes")

    # ---- corrections -----------------------------------------------------------------------

    def add_correction(
        self,
        series: str,
        start: str,
        end: str,
        value: float,
        reason: str,
        replace: str | None = None,
    ) -> str:
        """Correct ``series`` to ``value`` over [start, end), times in the plant time zone
        (``YYYY-MM-DD HH:MM``, or with a UTC offset). Needs a reason. ``replace`` edits the
        correction with that id in place, keeping its id."""
        p = self._p()
        tz = self.timezone
        try:
            s = cal.from_local(dt.datetime.fromisoformat(start), tz)
            e = cal.from_local(dt.datetime.fromisoformat(end), tz)
        except ValueError as exc:
            raise WorkspaceError(str(exc)) from exc
        before = list(p.corrections)
        others = [c for c in before if c.id != replace]
        if replace is not None and len(others) == len(before):
            raise WorkspaceError(f"no correction {replace}")
        n = 1
        while any(c.id == f"C{n:03d}" for c in p.corrections):
            n += 1
        try:
            c = Correction(
                id=replace or f"C{n:03d}", series=series, start=s, end=e, value=value, reason=reason
            )
        except ValidationError as exc:
            raise WorkspaceError(_errors(exc)) from exc
        if series not in self.required_series():
            raise WorkspaceError(f"{series!r} is not a series this plant needs")
        at = next((i for i, x in enumerate(before) if x.id == replace), len(before))
        p.corrections = [*others[:at], c, *others[at:]]
        self._changed()
        try:
            self.prepared()
        except WorkspaceError:
            p.corrections = before
            self._changed()
            raise
        return c.id

    def remove_correction(self, correction_id: str) -> None:
        p = self._p()
        p.corrections = [c for c in p.corrections if c.id != correction_id]
        self._changed()

    # ---- consignments, PoS, opening ledger --------------------------------------------------

    def outputs(self) -> list[str]:
        return [b.id for b in self.config().blocks_of(ConsignmentOutput)]

    def add_consignment(self, replace: str | None = None, **fields: Any) -> None:
        """Add a consignment, or with ``replace`` edit the one with that id in place."""
        p = self._p()
        try:
            c = ConsignmentSpec.model_validate(fields)
        except ValidationError as exc:
            raise WorkspaceError(_errors(exc)) from exc
        if c.output not in self.outputs():
            raise WorkspaceError(f"{c.output!r} is not a consignment output of this plant")
        p.consignments = _put(p.consignments, c, replace, "consignment")

    def remove_consignment(self, consignment_id: str) -> None:
        p = self._p()
        p.consignments = [c for c in p.consignments if c.id != consignment_id]

    def add_pos(self, replace: str | None = None, **fields: Any) -> list[Issue]:
        """Add a PoS record, or with ``replace`` edit the one with that id in place; refused
        with the rule it breaks (§11.1: compliant, in the quarter, from a supplier of its
        product in the plant)."""
        p = self._p()
        try:
            rec = ProofOfSustainability.model_validate(fields)
        except ValidationError as exc:
            raise WorkspaceError(_errors(exc)) from exc
        records = _put(p.pos, rec, replace, "PoS")
        issues = list(pos_rules(self.config(), records, p.file.quarter))
        if issues:
            raise WorkspaceError("; ".join(str(i) for i in issues))
        p.pos = records
        return []

    def remove_pos(self, pos_id: str) -> None:
        p = self._p()
        p.pos = [x for x in p.pos if x.id != pos_id]

    def storages(self) -> list[str]:
        return [s.id for s in self.config().blocks_of(StorageUnit) if not s.pass_through]

    def set_opening_from(self, run_dir: str | None) -> None:
        """Open the quarter from a previous quarter's sealed run (its closing ledger and
        measured inventory), replacing any manual entries."""
        p = self._p()
        if run_dir:
            p.opening = {}
        p.file = p.file.model_copy(update={"opening_from": run_dir or None})
        self._changed()
        try:
            self.config()
        except WorkspaceError:
            p.file = p.file.model_copy(update={"opening_from": None})
            raise

    def set_opening(
        self, storage: str, inventory_kg: float, entries: Sequence[dict[str, Any]]
    ) -> None:
        """Enter a storage's opening position by hand."""
        p = self._p()
        if storage not in self.storages():
            raise WorkspaceError(f"{storage!r} is not a storage with inventory")
        try:
            entry = OpeningEntry(
                opening_inventory_kg=inventory_kg,
                opening_ledger=[OpeningLedgerEntry.model_validate(e) for e in entries],
            )
        except ValidationError as exc:
            raise WorkspaceError(_errors(exc)) from exc
        p.file = p.file.model_copy(update={"opening_from": None})
        p.opening = {**p.opening, storage: entry}
        self._changed()
        self.config()

    def clear_opening(self, storage: str) -> None:
        p = self._p()
        p.opening = {k: v for k, v in p.opening.items() if k != storage}
        self._changed()

    def remove_opening_entry(self, storage: str, batch_id: str) -> None:
        """Remove one ledger entry from a storage's hand-entered opening position."""
        p = self._p()
        old = p.opening.get(storage)
        if old is None or not any(e.batch_id == batch_id for e in old.opening_ledger):
            raise WorkspaceError(f"no opening entry {batch_id} in {storage}")
        self.set_opening(
            storage,
            old.opening_inventory_kg,
            [e.model_dump() for e in old.opening_ledger if e.batch_id != batch_id],
        )

    # ---- run -------------------------------------------------------------------------------

    def run(self, label: str | None = None) -> Any:
        """Save, optimise and seal under runs/<label>; returns the :class:`Sealed` outputs."""
        from rfnbo.project.run import run_project

        self.save()
        try:
            _, out = run_project(self._p(), label, self.prepared())
        except ProjectError as exc:
            raise WorkspaceError(str(exc)) from exc
        return out
