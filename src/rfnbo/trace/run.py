"""Sealed run records.

A sealed run is a directory that holds everything needed to reproduce a run exactly:

    frame.csv       the aligned 15-minute data the calculator used (UTC, full precision)
    batches.jsonl   one traceability record per batch (build plan §5)
    run.json        config, decisions, provenance, environment, hashes and all results

The run id is a hash of the inputs (config, data, decisions), so the same inputs always give
the same id. Nothing in a record depends on the wall clock. :func:`verify` re-runs the
calculation from the sealed files and checks the outputs are byte-identical.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import io
import json
import platform
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

from rfnbo.calc import RunResult, calculate
from rfnbo.config.schema import OpeningLedgerEntry, PlantConfig, StorageUnit
from rfnbo.data import ingest, quality
from rfnbo.data.quality import Frame, QualityFlag
from rfnbo.model.entities import Decisions
from rfnbo.optimise.lp import solver_info
from rfnbo.trace import json_log, lineage

RECORD_VERSION = 3  # 2: storage weights, non-compliant write-off; 3: purchased RFNBO (PoS)
FRAME_FILE, BATCHES_FILE, RUN_FILE = "frame.csv", "batches.jsonl", "run.json"
_PACKAGES = ("rfnbo", "numpy", "pydantic", "openpyxl", "tzdata", "highspy")


class SealError(ValueError):
    pass


def canonical(obj: Any) -> bytes:
    """Canonical JSON bytes for hashing: sorted keys, no whitespace, no NaN."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pretty(obj: Any) -> bytes:
    return (json.dumps(obj, indent=1, ensure_ascii=False, allow_nan=False) + "\n").encode()


def frame_csv(frame: Frame) -> bytes:
    """The frame as CSV with full float precision (``repr``), so it reads back exactly."""
    names = sorted(frame.series)
    buf = io.StringIO()
    buf.write(",".join(["timestamp_utc", *names]) + "\n")
    columns = [frame.series[n] for n in names]
    for i, ts in enumerate(frame.index):
        cells = [ts.isoformat()]
        for col in columns:
            v = float(col[i])
            cells.append("" if np.isnan(v) else repr(v))
        buf.write(",".join(cells) + "\n")
    return buf.getvalue().encode()


def read_frame(path: Path, timezone: str, quarter: str) -> Frame:
    header = path.read_text(encoding="utf-8").split("\n", 1)[0].split(",")
    source = ingest.DataSource(
        format="csv", time_column=header[0], columns={n: n for n in header[1:]}
    )
    year, q = quarter.split("-Q")
    frame, flags = quality.align(ingest.load(path, source), int(year), int(q), timezone)
    if quality.has_errors(flags):
        raise SealError(
            f"{path}: sealed frame does not cover {quarter}: " + "; ".join(str(f) for f in flags)
        )
    return frame


def environment() -> dict[str, Any]:
    versions: dict[str, str | None] = {}
    for pkg in _PACKAGES:
        try:
            versions[pkg] = importlib.metadata.version(pkg)
        except importlib.metadata.PackageNotFoundError:
            versions[pkg] = None
    return {
        "python": platform.python_version(),
        "packages": versions,
        "solver": solver_info(),
    }


def run_id_for(config_hash: str, frame_hash: str, decisions_hash: str) -> str:
    digest = sha256(f"{RECORD_VERSION}:{config_hash}:{frame_hash}:{decisions_hash}".encode())
    return f"run-{digest[:16]}"


def _flag_json(f: QualityFlag) -> dict[str, Any]:
    d = asdict(f)
    d["examples"] = [t.isoformat() for t in f.examples]
    return d


def _ledgers(run: RunResult) -> list[dict[str, Any]]:
    out = []
    for led in run.ledgers:
        out.append(
            {
                "storage": led.storage,
                "quarter": led.quarter,
                "opening_kg": led.opening_kg,
                "produced_kg": led.produced_kg,
                "purchased_kg": led.purchased_kg,
                "out_feed_kg": led.out_feed_kg,
                "out_consignment_kg": led.out_consignment_kg,
                "closing_before_kg": led.closing_before_kg,
                "physical_kg": led.physical_kg,
                "excess_kg": led.excess_kg,
                "noncompliant_closing_kg": led.noncompliant_closing_kg,
                "required_write_off_kg": led.required_write_off_kg,
                "write_off_kg": led.write_off_kg,
                "carried_kg": led.carried_kg,
                "carried_ghg": led.carried_ghg_gCO2e_per_MJ,
                "lines": [
                    {
                        "batch": x.batch,
                        "origin": x.origin,
                        "month": x.month,
                        "ghg": x.ghg_gCO2e_per_MJ,
                        "rfnbo_share": x.rfnbo_share,
                        "compliant": x.compliant,
                        "in_kg": x.in_kg,
                        "out_feed_kg": x.out_feed_kg,
                        "out_consignment_kg": x.out_consignment_kg,
                        "closing_before_kg": x.closing_before_kg,
                        "write_off_kg": x.write_off_kg,
                        "carried_kg": x.carried_kg,
                        "purchased": x.purchased,
                        "source": x.source,
                    }
                    for x in led.lines
                ],
            }
        )
    return out


def _reconciliation(run: RunResult) -> list[dict[str, Any]]:
    return [
        {
            "storage": r.storage,
            "quarter": r.quarter,
            "opening_physical_kg": r.opening_physical_kg,
            "produced_kg": r.produced_kg,
            "delivered_kg": r.delivered_kg,
            "outflows_kg": dict(r.outflows_kg),
            "expected_kg": r.expected_kg,
            "measured_kg": r.measured_kg,
            "implied_loss_kg": r.implied_loss_kg,
        }
        for r in run.reconciliation
    ]


def _consignments(run: RunResult) -> list[dict[str, Any]]:
    out = []
    for c in run.consignments:
        shipped = c.shipped_ghg_gCO2e_per_MJ
        out.append(
            {
                "consignment": c.id,
                "output": c.output,
                "customer": c.customer,
                "distribution": c.distribution,
                "source_storage": c.source_storage,
                "ship_date": c.ship_date.isoformat(),
                "volume_kg": c.volume_kg,
                "allocated_kg": c.allocated_kg,
                "energy_MJ": c.energy_MJ,
                "highest_aggregated_ghg": c.max_ghg_gCO2e_per_MJ,
                "distribution_adder": c.distribution_adder_gCO2e_per_MJ,
                "shipped_ghg": shipped,
                "target": c.target_gCO2e_per_MJ,
                "threshold": c.threshold_gCO2e_per_MJ,
                "margin_to_target": (
                    None
                    if shipped is None or c.target_gCO2e_per_MJ is None
                    else c.target_gCO2e_per_MJ - shipped
                ),
                "margin_to_threshold": None
                if shipped is None
                else c.threshold_gCO2e_per_MJ - shipped,
                "aggregated_batches": [
                    {
                        "id": a.id,
                        "months": list(a.months),
                        "kg": a.kg,
                        "energy_MJ": a.energy_MJ,
                        "ghg": a.ghg_gCO2e_per_MJ,
                        "sources": list(a.sources),
                        "parts": [
                            {
                                "batch": p.batch,
                                "month": p.month,
                                "kg": p.kg,
                                "energy_MJ": p.energy_MJ,
                                "ghg": p.ghg_gCO2e_per_MJ,
                                "compliant": p.compliant,
                                "purchased": p.purchased,
                                "source": p.source,
                            }
                            for p in a.parts
                        ],
                    }
                    for a in c.aggregated
                ],
            }
        )
    return out


def _purchases(run: RunResult) -> dict[str, Any]:
    return {
        "register": [
            {
                "pos": p.id,
                "supplier": p.supplier,
                "product": p.product,
                "dispatch_date": p.dispatch_date.isoformat(),
                "quarter": p.quarter,
                "quantity_kg": p.quantity_kg,
                "ghg": p.ghg_gCO2e_per_MJ,
                "compliant": p.compliant,
                "allocated_kg": p.allocated_kg,
                "written_off_kg": p.unallocated_kg,
                "allocations": [
                    {
                        "flow": a.flow,
                        "target": a.target,
                        "kind": a.kind,
                        "start_utc": run.frame.index[a.start_index].isoformat(),
                        "intervals": a.end_index - a.start_index,
                        "kg": a.kg,
                        "metered_kg": a.metered_kg,
                    }
                    for a in p.allocations
                ],
            }
            for p in run.purchases
        ],
        "supplies": [
            {
                "supply": s.supply,
                "supplier": s.supplier,
                "product": s.product,
                "metered_kg": s.metered_kg,
                "pos_kg": s.pos_kg,
                "allocated_kg": s.allocated_kg,
                "non_rfnbo_kg": s.non_rfnbo_kg,
            }
            for s in run.supplies
        ],
    }


def _summary(run: RunResult) -> dict[str, Any]:
    units: dict[str, dict[str, Any]] = {}
    for b in run.batches.values():
        u = units.setdefault(
            b.unit,
            {
                "batches": 0,
                "rfnbo_batches": 0,
                "compliant_batches": 0,
                "product_kg": 0.0,
                "rfnbo_kg": 0.0,
                "compliant_rfnbo_kg": 0.0,
            },
        )
        u["batches"] += 1
        u["product_kg"] += b.product_kg
        if b.is_rfnbo:
            u["rfnbo_batches"] += 1
            u["rfnbo_kg"] += b.rfnbo_kg
            if b.compliant:
                u["compliant_batches"] += 1
                u["compliant_rfnbo_kg"] += b.rfnbo_kg
    return {
        "passed": run.passed,
        "checks": len(run.checks),
        "failed_checks": len(run.failures),
        "units": units,
    }


def build(
    run: RunResult,
    source_sha256: Sequence[str] | None = None,
    flags: Sequence[dict[str, Any]] | None = None,
    corrections: Sequence[dict[str, Any]] | None = None,
) -> tuple[bytes, bytes, bytes]:
    """Serialise a run: (run.json, batches.jsonl, frame.csv) bytes."""
    frame_bytes = frame_csv(run.frame)
    # Layout is presentation only: moving a block must not change the run id.
    config_json = run.config.model_dump(mode="json", exclude={"layout"})
    decisions_json = run.decisions.model_dump(mode="json")
    hashes = {
        "config_sha256": sha256(canonical(config_json)),
        "frame_sha256": sha256(frame_bytes),
        "decisions_sha256": sha256(canonical(decisions_json)),
    }
    run_id = run_id_for(hashes["config_sha256"], hashes["frame_sha256"], hashes["decisions_sha256"])

    all_edges = lineage.edges(run)
    records = json_log.batch_records(run, run_id, all_edges)
    batches_bytes = b"".join(
        json.dumps(r, ensure_ascii=False, allow_nan=False).encode() + b"\n" for r in records
    )

    record = {
        "record_version": RECORD_VERSION,
        "run_id": run_id,
        "plant": run.config.plant,
        "config_version": run.config.version,
        "quarter": run.frame.quarter,
        "timezone": run.frame.timezone,
        "intervals": len(run.frame),
        "hashes": {**hashes, "batches_sha256": sha256(batches_bytes)},
        "provenance": {
            "source_files_sha256": list(
                source_sha256 if source_sha256 is not None else run.frame.source_sha256
            ),
            "quality_flags": list(flags or []),
            # Data corrections applied over the original files (build plan §11.2); frame.csv
            # holds the corrected data, source_files_sha256 the originals.
            **({"corrections": list(corrections)} if corrections else {}),
        },
        "environment": environment(),
        "config": config_json,
        "decisions": decisions_json,
        "results": {
            "summary": _summary(run),
            "ledgers": _ledgers(run),
            "reconciliation": _reconciliation(run),
            "storage_weights": {
                k: {"weight": w.weight, "source": w.source, "detail": w.detail}
                for k, w in run.storage_weights.items()
            },
            "consignments": _consignments(run),
            "purchases": _purchases(run),
            "lineage": lineage.graph(run),
            "checks": [
                {
                    "check": c.name,
                    "reference": c.reference,
                    "subject": c.subject,
                    "passed": c.passed,
                    "detail": c.detail,
                }
                for c in run.checks
            ],
        },
    }
    return pretty(record), batches_bytes, frame_bytes


def seal(
    run: RunResult,
    out_dir: str | Path,
    flags: Sequence[QualityFlag] = (),
    corrections: Sequence[dict[str, Any]] = (),
) -> Path:
    """Write a sealed run directory. Refuses to overwrite a different sealed run."""
    out = Path(out_dir)
    run_bytes, batches_bytes, frame_bytes = build(
        run, flags=[_flag_json(f) for f in flags], corrections=corrections
    )
    files = {RUN_FILE: run_bytes, BATCHES_FILE: batches_bytes, FRAME_FILE: frame_bytes}
    if out.exists() and any(out.iterdir()):
        existing = {n: (out / n).read_bytes() for n in files if (out / n).exists()}
        if existing != files:
            raise SealError(f"{out} already holds a different run; choose another directory")
        return out
    out.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (out / name).write_bytes(data)
    return out


def load_record(run_dir: str | Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((Path(run_dir) / RUN_FILE).read_text(encoding="utf-8"))
    return data


def recalculate(run_dir: str | Path) -> RunResult:
    """Re-run the calculation from a sealed run's own files."""
    rd = Path(run_dir)
    rec = load_record(rd)
    cfg = PlantConfig.model_validate(rec["config"])
    decisions = Decisions.model_validate(rec["decisions"])
    frame = read_frame(rd / FRAME_FILE, rec["timezone"], rec["quarter"])
    return calculate(cfg, frame, decisions)


def verify(run_dir: str | Path) -> list[str]:
    """Re-run a sealed run and compare outputs byte for byte. Returns a list of problems."""
    rd = Path(run_dir)
    rec = load_record(rd)
    problems: list[str] = []
    frame_bytes = (rd / FRAME_FILE).read_bytes()
    if sha256(frame_bytes) != rec["hashes"]["frame_sha256"]:
        problems.append(f"{FRAME_FILE} does not match its recorded hash")
    batches_bytes = (rd / BATCHES_FILE).read_bytes()
    if sha256(batches_bytes) != rec["hashes"]["batches_sha256"]:
        problems.append(f"{BATCHES_FILE} does not match its recorded hash")

    env_now = environment()
    if env_now != rec["environment"]:
        problems.append(f"environment differs: sealed {rec['environment']}, now {env_now}")

    run = recalculate(rd)
    new_run, new_batches, new_frame = build(
        run,
        source_sha256=rec["provenance"]["source_files_sha256"],
        flags=rec["provenance"]["quality_flags"],
        corrections=rec["provenance"].get("corrections"),
    )
    if new_frame != frame_bytes:
        problems.append(f"{FRAME_FILE} does not reproduce")
    if new_batches != batches_bytes:
        problems.append(f"{BATCHES_FILE} does not reproduce")
    if new_run != (rd / RUN_FILE).read_bytes():
        new = json.loads(new_run)
        diff = sorted(k for k in set(new) | set(rec) if new.get(k) != rec.get(k))
        problems.append(f"{RUN_FILE} does not reproduce (differs in {diff})")
    return problems


def carried_ledger(run_dir: str | Path) -> dict[str, list[OpeningLedgerEntry]]:
    """Closing ledger of a sealed run, as opening entries for the next quarter (§2.7).

    Non-compliant RFNBO is never carried (decided 2026-10-17), so a floating-point residue left
    on a non-compliant line (within the checks' tolerance) does not open the next quarter."""
    rec = load_record(run_dir)
    if not rec["results"]["summary"]["passed"]:
        raise SealError(f"{run_dir}: sealed run has failed checks; it cannot open a quarter")
    out: dict[str, list[OpeningLedgerEntry]] = {}
    for led in rec["results"]["ledgers"]:
        out[led["storage"]] = [
            OpeningLedgerEntry(
                batch_id=x["batch"],
                rfnbo_kg=x["carried_kg"],
                ghg_gCO2e_per_MJ=x["ghg"],
                rfnbo_share=x["rfnbo_share"],
                production_month=x["month"],
                origin="purchased" if x.get("purchased") else "produced",
                supplier=x["source"] if x.get("purchased") else None,
            )
            for x in led["lines"]
            if x["compliant"] and x["carried_kg"] > 0
        ]
    return out


def with_opening_from(cfg: PlantConfig, run_dir: str | Path, quarter: str) -> PlantConfig:
    """``cfg`` for ``quarter`` with each storage's opening ledger from a prior sealed run.

    The prior run must be for the same plant and the quarter immediately before.
    Opening physical inventory is taken from the prior run's measured closing inventory, and
    each storage's carried-weight fallback from the prior run's storage weight.
    """
    rec = load_record(run_dir)
    if rec["plant"] != cfg.plant:
        raise SealError(f"sealed run is for plant {rec['plant']!r}, not {cfg.plant!r}")
    if next_quarter(rec["quarter"]) != quarter:
        raise SealError(f"sealed run is for {rec['quarter']}; it cannot open {quarter}")
    carried = carried_ledger(run_dir)
    measured = {r["storage"]: r["measured_kg"] for r in rec["results"]["reconciliation"]}
    weights = {k: v["weight"] for k, v in rec["results"].get("storage_weights", {}).items()}
    blocks = []
    for b in cfg.blocks:
        if isinstance(b, StorageUnit):
            if b.id not in carried:
                raise SealError(f"sealed run has no ledger for storage {b.id!r}")
            update: dict[str, Any] = {
                "opening_ledger": carried[b.id],
                "opening_inventory_kg": measured[b.id],
            }
            if weights.get(b.id) is not None:
                update["carried_weight_fallback"] = weights[b.id]
            b = b.model_copy(update=update)
        blocks.append(b)
    return cfg.model_copy(update={"blocks": blocks})


def next_quarter(quarter: str) -> str:
    year, q = (int(x) for x in quarter.split("-Q"))
    return f"{year + q // 4:04d}-Q{q % 4 + 1}"
