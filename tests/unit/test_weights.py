"""Storage weights for objective 2 (decided 2026-10-17): MJ of final product per MJ carried."""

from __future__ import annotations

import pytest

from rfnbo.calc.weights import storage_weights
from rfnbo.config import load_config
from rfnbo.config.schema import PlantConfig, StorageUnit
from tests.conftest import EXAMPLE
from tests.unit.test_calc import ELECTROLYSER, NH3, make_frame


@pytest.fixture
def cfg() -> PlantConfig:
    return load_config(EXAMPLE)


def test_final_product_storage_has_weight_one(cfg: PlantConfig) -> None:
    w = storage_weights(cfg, make_frame(cfg, {**ELECTROLYSER, **NH3}))
    assert w["nh3_storage"].weight == 1.0 and w["nh3_storage"].source == "final"


def test_intermediate_storage_weight_is_the_downstream_energy_yield(cfg: PlantConfig) -> None:
    """170 kg H₂ (120 MJ/kg) became 1,000 kg NH₃ (18.6 MJ/kg): 1 MJ of H₂ held upstream is
    worth 18,600 / 20,400 MJ of NH₃."""
    w = storage_weights(cfg, make_frame(cfg, {**ELECTROLYSER, **NH3}))["h2_storage"]
    assert w.source == "data"
    assert w.weight == pytest.approx(1000 * 18.6 / (170 * 120))


def test_weight_without_downstream_flow_needs_a_fallback(cfg: PlantConfig) -> None:
    frame = make_frame(cfg, ELECTROLYSER)  # NH₃ synthesis never ran
    w = storage_weights(cfg, frame)["h2_storage"]
    assert w.weight is None and w.source == "unknown"
    raw = cfg.model_dump(mode="json")
    next(b for b in raw["blocks"] if b["id"] == "h2_storage")["carried_weight_fallback"] = 0.87
    w = storage_weights(PlantConfig.model_validate(raw), frame)["h2_storage"]
    assert w.weight == 0.87 and w.source == "fallback"


def test_fallback_must_be_positive() -> None:
    with pytest.raises(ValueError):
        StorageUnit(
            id="s", name="s", product="H2", opening_inventory_kg=0, carried_weight_fallback=0
        )
