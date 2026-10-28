"""A unit whose output goes straight to customers (decided 2026-10-27): test plant 4 with the
compressor filling tube trailers for Customer B directly, through a pass-through storage into
the distribution block, with no CH2 storage. Consignments are entered as usual; what no
consignment takes is written off at quarter end, as for any pass-through storage."""

from __future__ import annotations

import json
from pathlib import Path

from rfnbo.config import validate
from rfnbo.config.schema import PlantConfig
from rfnbo.data import ingest, quality
from rfnbo.model.entities import ConsignmentSpec, ProofOfSustainability
from rfnbo.optimise.pipeline import optimise
from rfnbo.trace import run as sealed

GOLDEN = Path(__file__).resolve().parents[1] / "golden"
WORKBOOK = GOLDEN / "workbooks" / "RFNBO_optimiser_test4_purchased_Q4_2026.xlsx"


def _plant() -> PlantConfig:
    raw = json.loads((GOLDEN / "tc4.plant.json").read_text("utf-8"))
    raw["blocks"] = [b for b in raw["blocks"] if b["id"] != "ch2_storage"] + [
        {
            "kind": "storage_unit",
            "id": "pt_fill",
            "name": "Tube trailer fill, straight to Customer B",
            "product": "CH2",
            "pass_through": True,
            "opening_inventory_kg": 0.0,
        }
    ]
    for f in raw["flows"]:
        if f["id"] == "f_comp_ch2s":
            f["target"] = "pt_fill"
        if f["id"] == "f_ch2s_b":
            f["source"] = "pt_fill"
    return PlantConfig.model_validate(raw)


def test_consigned_straight_from_a_unit_and_the_rest_written_off(tmp_path: Path) -> None:
    cfg = _plant()
    assert validate(cfg) == []
    data = ingest.load(WORKBOOK, ingest.load_source_spec(GOLDEN / "tc4.source.json"))
    frame, flags = quality.prepare(cfg, data, 2026, 4)
    assert not quality.has_errors(flags)
    decisions = json.loads((GOLDEN / "tc4.decisions.json").read_text("utf-8"))
    consignments = [ConsignmentSpec.model_validate(c) for c in decisions["consignments"]]
    pos = [ProofOfSustainability.model_validate(p) for p in decisions["pos"]]

    result = optimise(cfg, frame, consignments, pos=pos)
    run = result.optimised.run
    assert result.feasible and result.shortfalls == []
    assert run.failures == []

    fill = run.ledger("pt_fill")
    cb = next(c for c in consignments if c.output == "consignment_b")
    assert abs(fill.out_consignment_kg - cb.volume_kg) <= 1e-6 * cb.volume_kg
    produced = sum(line.in_kg for line in fill.lines)
    # Nothing is carried: what Customer B did not take is written off.
    assert abs(fill.carried_kg) <= 1e-6
    assert abs(fill.write_off_kg - (produced - cb.volume_kg)) <= 1e-6 * produced
    assert fill.write_off_kg > 0
    assert any(c.name == "pass_through_closes" and c.passed for c in run.checks)

    out = sealed.seal(run, tmp_path / "run")
    assert sealed.verify(out) == []
