"""Each validation rule has at least one failing case built from the example plant."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from rfnbo.config import ConfigError, load_config, validate
from rfnbo.config.schema import PlantConfig, StorageUnit
from rfnbo.config.validate import RULES
from tests.conftest import EXAMPLE

Raw = dict[str, Any]
PLANT_3 = Path(__file__).resolve().parents[1] / "golden" / "tc3.plant.json"
PLANT_4 = PLANT_3.with_name("tc4.plant.json")


def _block(raw: Raw, block_id: str) -> dict[str, Any]:
    return next(b for b in raw["blocks"] if b["id"] == block_id)


def _flow(raw: Raw, flow_id: str) -> dict[str, Any]:
    return next(f for f in raw["flows"] if f["id"] == flow_id)


def _drop_flow(raw: Raw, flow_id: str) -> None:
    raw["flows"] = [f for f in raw["flows"] if f["id"] != flow_id]


def _rules(raw: Raw) -> set[str]:
    return {i.rule for i in validate(PlantConfig.model_validate(raw))}


def test_example_is_valid() -> None:
    cfg = load_config(EXAMPLE)
    assert validate(cfg) == []


def test_multi_step_plant_is_valid() -> None:
    """Test plant 3: split electrolyser output, pass-through link, liquefier and compressor."""
    cfg = load_config(PLANT_3)
    assert validate(cfg) == []
    pt = cfg.block("pt_compressor")
    assert isinstance(pt, StorageUnit) and pt.pass_through


def test_plant_with_purchased_rfnbo_is_valid() -> None:
    """Test plant 4: a supplier delivering H₂ into the liquefier and the compressor; a supplier
    may also deliver into a storage, which then needs no producing unit."""
    cfg = load_config(PLANT_4)
    assert validate(cfg) == []
    raw = json.loads(PLANT_4.read_text(encoding="utf-8"))
    raw["flows"].append(
        {
            "id": "f_sup_buf",
            "source": "h2_supplier",
            "target": "h2_buffer",
            "material": "H2",
            "series": "supplier_to_buffer_kg",
        }
    )
    assert validate(PlantConfig.model_validate(raw)) == []


def _plant_3(r: Raw) -> None:
    """Replace the test plant with test plant 3 (the network cases mutate that)."""
    r.clear()
    r.update(json.loads(PLANT_3.read_text(encoding="utf-8")))


def test_pass_through_storage_has_no_inventory() -> None:
    raw = json.loads(PLANT_3.read_text(encoding="utf-8"))
    _block(raw, "pt_compressor")["inventory_series"] = "pt_kg"
    with pytest.raises(ValueError, match="no inventory series"):
        PlantConfig.model_validate(raw)
    raw = json.loads(PLANT_3.read_text(encoding="utf-8"))
    _block(raw, "pt_compressor")["opening_inventory_kg"] = 10.0
    with pytest.raises(ValueError, match="opens empty"):
        PlantConfig.model_validate(raw)


def _direct_to_distribution(raw: Raw) -> None:
    """Test plant 3 with the compressor filling tube trailers straight for Customer B: no CH2
    storage, a pass-through storage into the distribution block instead."""
    raw["blocks"] = [b for b in raw["blocks"] if b["id"] != "ch2_storage"] + [
        {
            "kind": "storage_unit",
            "id": "pt_fill",
            "name": "Tube trailer fill",
            "product": "CH2",
            "pass_through": True,
            "opening_inventory_kg": 0.0,
        }
    ]
    _flow(raw, "f_comp_ch2s")["target"] = "pt_fill"
    _flow(raw, "f_ch2s_b")["source"] = "pt_fill"


def test_unit_output_straight_to_distribution_through_a_pass_through() -> None:
    """A unit whose output goes straight to customers (decided 2026-10-27)."""
    raw = json.loads(PLANT_3.read_text(encoding="utf-8"))
    _direct_to_distribution(raw)
    assert validate(PlantConfig.model_validate(raw)) == []


def _dup_block(r: Raw) -> None:
    r["blocks"].append(dict(_block(r, "water")))


def _unknown_endpoint(r: Raw) -> None:
    _flow(r, "f_water_el")["target"] = "missing"


def _self_loop(r: Raw) -> None:
    _flow(r, "f_el_h2s")["target"] = "electrolyser"


def _input_to_storage(r: Raw) -> None:
    _flow(r, "f_water_el")["target"] = "h2_storage"


def _unit_to_unit(r: Raw) -> None:
    _flow(r, "f_h2s_nh3")["source"] = "electrolyser"


def _orphan(r: Raw) -> None:
    r["blocks"].append(
        {
            "kind": "distribution",
            "id": "dist_unused",
            "name": "Unused",
            "customer": "Nobody",
            "fixed_adder_gCO2e_per_MJ": 0.0,
        }
    )


def _wrong_material_from_input(r: Raw) -> None:
    _flow(r, "f_water_el")["material"] = "N2"


def _wrong_material_into_storage(r: Raw) -> None:
    _flow(r, "f_nh3_nh3s")["target"] = "h2_storage"


def _two_products_into_distribution(r: Raw) -> None:
    r["flows"].append(
        {
            "id": "f_h2s_a",
            "source": "h2_storage",
            "target": "dist_customer_a",
            "material": "H2",
            "series": "h2_shipped_a_kg",
        }
    )


def _no_production_series(r: Raw) -> None:
    del _block(r, "nh3_synthesis")["production_series"]


def _no_electricity_series(r: Raw) -> None:
    del _block(r, "electrolyser")["electricity_series"]


def _no_input_series(r: Raw) -> None:
    del _flow(r, "f_water_el")["series"]


def _no_transfer_series(r: Raw) -> None:
    del _flow(r, "f_h2s_nh3")["series"]


def _series_on_electricity_flow(r: Raw) -> None:
    _flow(r, "f_grid_el")["series"] = "grid_kWh"


def _no_inventory_series(r: Raw) -> None:
    del _block(r, "nh3_storage")["inventory_series"]


def _no_unit_output(r: Raw) -> None:
    _drop_flow(r, "f_nh3_nh3s")


def _electrolyser_without_renewable(r: Raw) -> None:
    _drop_flow(r, "f_re_el")


def _downstream_without_feed(r: Raw) -> None:
    _drop_flow(r, "f_h2s_nh3")


def _second_grid_input(r: Raw) -> None:
    extra = dict(_block(r, "grid_power"), id="grid_power_2")
    r["blocks"].append(extra)
    r["flows"].append(
        {
            "id": "f_grid2_el",
            "source": "grid_power_2",
            "target": "electrolyser",
            "material": "electricity",
        }
    )


def _grid_ef_varies(r: Raw) -> None:
    efs = _block(r, "grid_power")["emission_factors"]
    efs.append(dict(efs[0], valid_from="2026-07-01", value=380.0))


def _renewable_without_grid(r: Raw) -> None:
    r["blocks"] = [b for b in r["blocks"] if b["id"] != "grid_power"]
    _drop_flow(r, "f_grid_el")
    _drop_flow(r, "f_grid_nh3")


def _ef_starts_late(r: Raw) -> None:
    _block(r, "water")["emission_factors"][0]["valid_from"] = "2026-02-01"


def _consignment_missing_distribution(r: Raw) -> None:
    _block(r, "consignment_a")["distribution_block"] = "dist_missing"


def _consignment_customer_mismatch(r: Raw) -> None:
    _block(r, "consignment_a")["customer"] = "Customer B"


def _consignment_fed_by_other_distribution(r: Raw) -> None:
    _flow(r, "f_a_cons")["source"] = "dist_customer_b"


def _cycle(r: Raw) -> None:
    # A storage feeding an upstream unit closes a loop electrolyser → H2 → NH3 → NH3s → el.
    r["flows"].append(
        {
            "id": "f_loop",
            "source": "nh3_storage",
            "target": "electrolyser",
            "material": "NH3",
            "series": "loop_kg",
        }
    )


def _split_output_unmetered(r: Raw) -> None:
    _plant_3(r)
    del _flow(r, "f_el_buf")["series"]


def _pass_through_to_storage(r: Raw) -> None:
    _plant_3(r)
    _flow(r, "f_pt_comp")["target"] = "ch2_storage"


def _pass_through_two_sources(r: Raw) -> None:
    _plant_3(r)
    r["flows"].append(
        {"id": "f_liq_pt", "source": "liquefier", "target": "pt_compressor", "material": "LH2"}
    )


def _unit_takes_two_products(r: Raw) -> None:
    _plant_3(r)
    r["flows"].append(
        {
            "id": "f_lh2s_comp",
            "source": "lh2_storage",
            "target": "compressor",
            "material": "LH2",
            "series": "compressor_LH2_kg",
        }
    )


def _storage_not_produced(r: Raw) -> None:
    _drop_flow(r, "f_el_h2s")


def _plant_4(r: Raw) -> None:
    """Test plant 4: test plant 3 with a supplier of purchased RFNBO H₂."""
    r.clear()
    r.update(json.loads(PLANT_4.read_text(encoding="utf-8")))


def _supply_flow_unmetered(r: Raw) -> None:
    _plant_4(r)
    del _flow(r, "f_sup_liq")["series"]


def _supply_into_electrolyser(r: Raw) -> None:
    _plant_4(r)
    _flow(r, "f_sup_liq")["target"] = "electrolyser"


def _supply_to_distribution(r: Raw) -> None:
    _plant_4(r)
    _flow(r, "f_sup_liq")["target"] = "dist_customer_a"


def _supply_wrong_product(r: Raw) -> None:
    _plant_4(r)
    _block(r, "h2_supplier")["product"] = "NH3"


def _layout_for_missing_block(r: Raw) -> None:
    r["layout"] = {"electrolyser": {"x": 0, "y": 0}, "ghost": {"x": 10, "y": 10}}


CASES: dict[str, list[Callable[[Raw], None]]] = {
    "unique_ids": [_dup_block],
    "flow_endpoints": [_unknown_endpoint, _self_loop],
    "connection_types": [_input_to_storage, _unit_to_unit, _supply_to_distribution],
    "no_orphans": [_orphan],
    "flow_materials": [
        _wrong_material_from_input,
        _wrong_material_into_storage,
        _supply_wrong_product,
    ],
    "distribution_single_product": [_two_products_into_distribution],
    "unit_series": [_no_production_series, _no_electricity_series],
    "flow_series": [_no_input_series, _no_transfer_series, _series_on_electricity_flow],
    "storage_series": [_no_inventory_series],
    "unit_outputs": [_no_unit_output],
    "split_outputs_metered": [_split_output_unmetered],
    "pass_through_links": [_pass_through_to_storage, _pass_through_two_sources],
    "unit_feed_products": [_unit_takes_two_products],
    "storages_produced": [_storage_not_produced],
    "unit_feeds": [_electrolyser_without_renewable, _downstream_without_feed],
    "supply_flows": [_supply_flow_unmetered, _supply_into_electrolyser],
    "electricity_inputs": [_second_grid_input, _grid_ef_varies, _renewable_without_grid],
    "emission_factor_coverage": [_ef_starts_late],
    "consignment_links": [
        _consignment_missing_distribution,
        _consignment_customer_mismatch,
        _consignment_fed_by_other_distribution,
    ],
    "no_cycles": [_cycle],
    "layout_blocks": [_layout_for_missing_block],
}


def test_every_rule_has_a_failing_case() -> None:
    assert set(CASES) == {r.__name__ for r in RULES}


@pytest.mark.parametrize(
    ("rule", "mutate"),
    [(rule, m) for rule, ms in CASES.items() for m in ms],
    ids=lambda x: x if isinstance(x, str) else x.__name__.lstrip("_"),
)
def test_rule_fails(raw: Raw, rule: str, mutate: Callable[[Raw], None]) -> None:
    mutate(raw)
    assert rule in _rules(raw)


def test_load_config_reports_schema_and_rule_errors(raw: Raw, tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    raw["blocks"][0]["kind"] = "reactor"
    bad.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(bad)
    assert {i.rule for i in exc.value.issues} == {"schema"}

    raw["blocks"][0]["kind"] = "input"
    _no_inventory_series(raw)
    bad.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(bad)
    assert [i.rule for i in exc.value.issues] == ["storage_series"]


def test_pass_through_outflow_is_metered_by_its_inflow() -> None:
    """A pass-through holds nothing, so an outflow given no series takes the inflow's
    (decided 2026-10-28): the flow into it if the unit splits its output, else the unit's
    production series. A series given on the outflow is kept."""
    raw = json.loads(PLANT_3.read_text(encoding="utf-8"))
    del _flow(raw, "f_pt_comp")["series"]  # the electrolyser splits: its flow in is metered
    _direct_to_distribution(raw)
    del _flow(raw, "f_ch2s_b")["series"]  # the compressor does not: its production series
    cfg = PlantConfig.model_validate(raw)
    assert validate(cfg) == []
    series = {f.id: f.series for f in cfg.flows}
    assert series["f_pt_comp"] == _flow(raw, "f_el_pt")["series"]
    assert series["f_ch2s_b"] == _block(raw, "compressor")["production_series"]
    raw = json.loads(PLANT_3.read_text(encoding="utf-8"))
    kept = PlantConfig.model_validate(raw)
    assert {f.id: f.series for f in kept.flows}["f_pt_comp"] == _flow(raw, "f_pt_comp")["series"]
