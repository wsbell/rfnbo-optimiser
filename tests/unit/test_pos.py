"""PoS records: CSV and JSON import, and the rules a record must meet (build plan §11.1)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from rfnbo.config import load_config
from rfnbo.model.pos import PoSError, load_pos, pos_rules

GOLDEN = Path(__file__).resolve().parents[1] / "golden"


def test_csv_import_matches_the_test_case_4_records() -> None:
    records = load_pos(GOLDEN / "tc4.pos.csv")
    fixture = json.loads((GOLDEN / "tc4.decisions.json").read_text("utf-8"))["pos"]
    assert [r.model_dump(mode="json") for r in records] == [
        {
            **p,
            "quantity_kg": float(p["quantity_kg"]),
            "ghg_gCO2e_per_MJ": float(p["ghg_gCO2e_per_MJ"]),
        }
        for p in fixture
    ]
    assert records[0].dispatch_date == dt.date(2026, 10, 20)
    cfg = load_config(GOLDEN / "tc4.plant.json")
    assert list(pos_rules(cfg, records, "2026-Q4")) == []


def test_json_import(tmp_path: Path) -> None:
    path = tmp_path / "pos.json"
    fixture = json.loads((GOLDEN / "tc4.decisions.json").read_text("utf-8"))["pos"]
    path.write_text(json.dumps(fixture), encoding="utf-8")
    assert [p.id for p in load_pos(path)] == ["POS-A", "POS-B", "POS-C"]


@pytest.mark.parametrize(
    ("row", "rule"),
    [
        ("POS-X,H2 Trailers BV,H2,2026-10-20,100,28.3", "pos_compliant"),
        ("POS-X,H2 Trailers BV,H2,2027-01-02,100,3.0", "pos_in_quarter"),
        ("POS-X,Other Ltd,H2,2026-10-20,100,3.0", "pos_supplier"),
        ("POS-X,H2 Trailers BV,NH3,2026-10-20,100,3.0", "pos_supplier"),
        ("POS-A,H2 Trailers BV,H2,2026-10-20,100,3.0", "pos_unique"),
    ],
)
def test_pos_rules_fail(tmp_path: Path, row: str, rule: str) -> None:
    path = tmp_path / "pos.csv"
    path.write_text((GOLDEN / "tc4.pos.csv").read_text("utf-8") + row + "\n", encoding="utf-8")
    cfg = load_config(GOLDEN / "tc4.plant.json")
    assert {i.rule for i in pos_rules(cfg, load_pos(path), "2026-Q4")} == {rule}


@pytest.mark.parametrize(
    "text",
    [
        "id,supplier,product,dispatch_date,quantity_kg\nP,S,H2,2026-10-01,1\n",
        "id,supplier,product,dispatch_date,quantity_kg,ghg_gCO2e_per_MJ\nP,S,H2,2026-10-01,,3\n",
        "id,supplier,product,dispatch_date,quantity_kg,ghg_gCO2e_per_MJ\nP,S,H2,01/10/2026,1,3\n",
    ],
)
def test_malformed_csv_is_refused_not_filled(tmp_path: Path, text: str) -> None:
    path = tmp_path / "pos.csv"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(PoSError):
        load_pos(path)
