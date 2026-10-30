"""Optimise a quarter and seal it with its baseline: shared by ``rfnbo optimise`` and the
Data screen, so both write the same outputs from the same inputs."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rfnbo.data.quality import QualityFlag
from rfnbo.optimise.pipeline import OptimisationResult, optimise
from rfnbo.project.folder import RUNS_DIR, Prepared, Project, ProjectError, prepare
from rfnbo.trace import run as sealed


@dataclass(frozen=True)
class Sealed:
    out: Path
    run_ids: dict[str, str]  # "optimised" / "baseline" -> run id
    report: Path
    summary: dict[str, Any]


def seal_optimisation(
    result: OptimisationResult,
    out: str | Path,
    flags: Sequence[QualityFlag] = (),
    corrections: Sequence[dict[str, Any]] = (),
) -> Sealed:
    """Seal the optimised run under ``out`` (and the baseline beside it when one was
    calculated for comparison), write ``summary.json`` and the report."""
    from rfnbo.report.build import report_sealed

    out = Path(out)
    outcomes = [("optimised", result.optimised)]
    if result.baseline is not None:
        outcomes.append(("baseline", result.baseline))
    ids = {}
    for name, outcome in outcomes:
        d = sealed.seal(outcome.run, out / name, flags, corrections)
        ids[name] = sealed.load_record(d)["run_id"]
    bound, vb = result.bound(), result.value_bound()
    summary = {
        "run_ids": ids,
        "feasible": result.feasible,
        "shortfalls": [
            {"consignment": s.consignment, "reason": s.reason, "achievable_kg": s.achievable_kg}
            for s in result.shortfalls
        ],
        "objectives": {
            name: {
                "consignments_met": o.objectives.consignments_met,
                "carried_value_MJ": o.objectives.carried_value_MJ,
                "final_rfnbo_kg": o.objectives.final_rfnbo_kg,
                "carried_emissions_g": o.objectives.carried_emissions_g,
                "carried_ghg": o.objectives.carried_ghg,
                "batches": o.objectives.batches,
                "pos_written_off_kg": o.objectives.pos_written_off_kg,
                "failed_checks": len(o.run.failures),
            }
            for name, o in outcomes
        },
        "bound": None
        if bound is None
        else {"upper_kg": bound.upper_kg, "achieved_kg": bound.achieved_kg, "gap": bound.gap},
        "value_bound": {"upper_MJ": vb.upper_MJ, "achieved_MJ": vb.achieved_MJ, "gap": vb.gap},
        "corrections": len(corrections),
        "log": result.log,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    baseline = out / "baseline" if result.baseline is not None else None
    written = report_sealed(out / "optimised", out, baseline_dir=baseline)
    return Sealed(out, ids, written.xlsx, summary)


def run_label(project: Project, label: str | None) -> str:
    """A folder name under runs/: the given label, or the quarter numbered from 1."""
    if label:
        if not re.fullmatch(r"[A-Za-z0-9_\-.]+", label):
            raise ProjectError(f"run label {label!r}: letters, digits, '-', '_' and '.' only")
        return label
    n = 1
    while (project.root / RUNS_DIR / f"{project.file.quarter}-{n}").exists():
        n += 1
    return f"{project.file.quarter}-{n}"


def run_project(
    project: Project,
    label: str | None = None,
    prepared: Prepared | None = None,
    compare: bool = False,
) -> tuple[OptimisationResult, Sealed]:
    """Optimise the project's quarter and seal it under ``runs/<label>``. Refuses to run on
    data with quality errors (nothing is filled silently) or invalid PoS records."""
    from rfnbo.model.pos import pos_rules

    prep = prepared or prepare(project)
    if not prep.ok:
        errors = [str(f) for f in prep.flags if f.severity == "error"]
        raise ProjectError("data quality errors: resolve them first: " + "; ".join(errors[:5]))
    issues = list(pos_rules(prep.config, project.pos, project.file.quarter))
    if issues:
        raise ProjectError("PoS records: " + "; ".join(str(i) for i in issues))
    if not project.consignments:
        raise ProjectError("no consignments: add at least one")
    result = optimise(
        prep.config,
        prep.frame,
        project.consignments,
        batch_materiality_MJ=project.file.batch_materiality_MJ,
        pos=project.pos,
        compare=compare,
    )
    out = project.root / RUNS_DIR / run_label(project, label)
    return result, seal_optimisation(result, out, prep.flags, prep.correction_records())
