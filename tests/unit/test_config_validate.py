"""Each validation rule has at least one failing case built from the example plant."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from rfnbo.config import ConfigError, load_config, validate
from rfnbo.config.schema import PlantConfig
from rfnbo.config.validate import RULES
from tests.conftest import EXAMPLE

Raw = dict[str, Any]


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


CASES: dict[str, list[Callable[[Raw], None]]] = {
    "unique_ids": [_dup_block],
    "flow_endpoints": [_unknown_endpoint, _self_loop],
    "connection_types": [_input_to_storage, _unit_to_unit],
    "no_orphans": [_orphan],
    "flow_materials": [_wrong_material_from_input, _wrong_material_into_storage],
    "distribution_single_product": [_two_products_into_distribution],
    "unit_series": [_no_production_series, _no_electricity_series],
    "flow_series": [_no_input_series, _no_transfer_series, _series_on_electricity_flow],
    "storage_series": [_no_inventory_series],
    "unit_outputs": [_no_unit_output],
    "unit_feeds": [_electrolyser_without_renewable, _downstream_without_feed],
    "electricity_inputs": [_second_grid_input, _grid_ef_varies, _renewable_without_grid],
    "emission_factor_coverage": [_ef_starts_late],
    "consignment_links": [
        _consignment_missing_distribution,
        _consignment_customer_mismatch,
        _consignment_fed_by_other_distribution,
    ],
    "no_cycles": [_cycle],
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
