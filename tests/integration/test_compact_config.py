"""The compact plant config (build plan §13): electricity as plant settings and direct unit
links, expanded on loading into the full form the engine reads. A plant written either way
gives the same run."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest
from tests.integration.test_time_step_split import COLUMNS, PLANT, _csv

from rfnbo.config import validate
from rfnbo.config.schema import (
    ELECTRICITY,
    InputBlock,
    PlantConfig,
    StorageUnit,
    compact,
    expand,
    synthetic_owner,
)
from rfnbo.optimise.pipeline import optimise
from rfnbo.project.folder import prepare
from rfnbo.ui.data.workspace import Workspace

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "golden"


def _full() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(PLANT.read_text("utf-8"))
    return data


def test_compact_form_of_the_simple_plant() -> None:
    small = compact(_full())
    assert [b["id"] for b in small["blocks"]] == [
        "electrolyser",
        "compressor",
        "dist_customer",
        "consignment_customer",
    ]
    assert [(f["source"], f["target"]) for f in small["flows"]] == [
        ("electrolyser", "compressor"),
        ("compressor", "dist_customer"),
        ("dist_customer", "consignment_customer"),
    ]
    assert small["electricity"]["renewable_available_series"] == "solar_available_kWh"
    assert small["electricity"]["grid_gCO2e_per_kWh"] == 200.0
    assert not set(small.get("layout", {})) - {b["id"] for b in small["blocks"]}
    cfg = PlantConfig.model_validate(small)
    assert validate(cfg) == [] and cfg.electricity is None  # read, then expanded
    # Each unit draws on the renewable pool and the grid; the links are pass-throughs.
    for unit in ("electrolyser", "compressor"):
        into = {f.source for f in cfg.flows_into(unit) if f.material == ELECTRICITY}
        assert into == {"renewable_power", "grid_power"}
    links = [b for b in cfg.blocks_of(StorageUnit) if b.pass_through]
    assert len(links) == 2
    # The dump reloads as it is (a sealed run stores it).
    again = PlantConfig.model_validate(cfg.model_dump(mode="json"))
    assert again == cfg


@pytest.mark.parametrize("plant", ["tc3.plant.json", "tc4.plant.json"])
def test_golden_plants_stay_valid_when_compacted(plant: str) -> None:
    raw = json.loads((GOLDEN / plant).read_text("utf-8"))
    assert validate(PlantConfig.model_validate(compact(raw))) == []


def test_the_workbook_plant_keeps_its_grid_only_unit() -> None:
    """NH₃ synthesis draws grid power only, so its electricity cannot go into the plant's
    settings (every unit there shares the renewable pool): the blocks are kept."""
    raw = json.loads((ROOT / "examples" / "test_plant.json").read_text("utf-8"))
    small = compact(raw)
    assert "electricity" not in small
    assert {"renewable_power", "grid_power"} <= {b["id"] for b in small["blocks"]}


def test_electricity_section_and_electricity_blocks_conflict() -> None:
    raw = json.loads((ROOT / "examples" / "test_plant.json").read_text("utf-8"))
    raw["electricity"] = {"grid_gCO2e_per_kWh": 200.0, "grid_source": "grid"}
    with pytest.raises(ValueError, match="remove the electricity input blocks"):
        PlantConfig.model_validate(raw)


def test_electricity_section_needs_its_grid_factor() -> None:
    small = compact(_full())
    del small["electricity"]["grid_source"]
    with pytest.raises(ValueError, match="grid_source"):
        PlantConfig.model_validate(small)


def test_reserved_ids() -> None:
    small = compact(_full())
    small["blocks"][2]["id"] = "grid_power"
    small["flows"][1]["target"] = small["flows"][2]["source"] = "grid_power"
    small["blocks"][3]["distribution_block"] = "grid_power"
    with pytest.raises(ValueError, match="reserved"):
        PlantConfig.model_validate(small)


def test_issues_on_added_items_point_at_what_was_given() -> None:
    small = compact(_full())
    assert synthetic_owner("f_el_pt__link", small) == "f_el_pt"
    assert synthetic_owner("f_el_pt__out", small) == "f_el_pt"
    assert synthetic_owner("compressor__grid_power", small) == "compressor"
    assert synthetic_owner("grid_power", small) is None
    assert synthetic_owner("compressor", small) == "compressor"


def test_grid_only_plant() -> None:
    small = compact(_full())
    del small["electricity"]["renewable_available_series"]
    cfg = PlantConfig.model_validate(small)
    assert [b.id for b in cfg.blocks_of(InputBlock)] == ["grid_power"]
    # An electrolyser still needs renewable power (its rule is unchanged).
    assert [(i.rule, i.ref) for i in validate(cfg)] == [("unit_feeds", "electrolyser")]
    assert expand({"blocks": [], "flows": []}) == {"blocks": [], "flows": []}


def _final(tmp: Path, plant: dict[str, Any]) -> tuple[float, int, float]:
    ws = Workspace(tmp)
    (tmp / "plant.json").write_text(json.dumps(plant), "utf-8")
    ws.setup("plant.json", "2026-Q4")
    data, heads = _csv(1)
    i = ws.add_data_file("data.csv", data)
    for series, head in zip(COLUMNS, heads, strict=True):
        ws.map_series(series, i, head)
    ws.add_consignment(
        id="C1", output="consignment_customer", volume_kg=100_000, ship_date="2026-11-15"
    )
    ws.save()
    assert ws.project is not None
    prep = prepare(ws.project)
    assert prep.ok
    result = optimise(prep.config, prep.frame, ws.project.consignments)
    assert result.feasible and result.optimised.run.failures == []
    o = result.optimised.objectives
    return o.final_rfnbo_kg, len(result.optimised.decisions.batches), o.carried_value_MJ


def test_compact_and_full_forms_give_the_same_run(tmp_path: Path) -> None:
    (tmp_path / "full").mkdir()
    (tmp_path / "small").mkdir()
    full = _final(tmp_path / "full", _full())
    small = _final(tmp_path / "small", compact(_full()))
    assert full[0] > 0 and full[1] == small[1]
    assert math.isclose(full[0], small[0], rel_tol=1e-6)
    assert math.isclose(full[2], small[2], rel_tol=1e-6, abs_tol=1e-6)
