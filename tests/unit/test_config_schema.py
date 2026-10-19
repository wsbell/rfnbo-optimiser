from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from pydantic import ValidationError

from rfnbo.config.schema import InputBlock, PlantConfig, select_version


def _block(raw: dict[str, Any], block_id: str) -> dict[str, Any]:
    return next(b for b in raw["blocks"] if b["id"] == block_id)


def test_example_parses(raw: dict[str, Any]) -> None:
    cfg = PlantConfig.model_validate(raw)
    assert cfg.settings.rfnbo_threshold_gCO2e_per_MJ == pytest.approx(28.2, rel=1e-9)
    assert len(cfg.blocks_of(InputBlock)) == 3


def test_json_round_trip(raw: dict[str, Any]) -> None:
    cfg = PlantConfig.model_validate(raw)
    again = PlantConfig.model_validate_json(cfg.model_dump_json())
    assert again == cfg


def test_unknown_field_rejected(raw: dict[str, Any]) -> None:
    raw["blocks"][0]["colour"] = "green"
    with pytest.raises(ValidationError):
        PlantConfig.model_validate(raw)


def test_unknown_block_kind_rejected(raw: dict[str, Any]) -> None:
    raw["blocks"][0]["kind"] = "reactor"
    with pytest.raises(ValidationError):
        PlantConfig.model_validate(raw)


def test_unknown_timezone_rejected(raw: dict[str, Any]) -> None:
    raw["timezone"] = "Europe/Atlantis"
    with pytest.raises(ValidationError, match="time zone"):
        PlantConfig.model_validate(raw)


def test_emission_factor_unit_must_match_quantity(raw: dict[str, Any]) -> None:
    _block(raw, "water")["emission_factors"][0]["unit"] = "gCO2e/kWh"
    with pytest.raises(ValidationError, match="does not match quantity unit"):
        PlantConfig.model_validate(raw)


def test_emission_factor_dates_unique(raw: dict[str, Any]) -> None:
    efs = _block(raw, "water")["emission_factors"]
    efs.append(dict(efs[0], value=3.0))
    with pytest.raises(ValidationError, match="same valid_from"):
        PlantConfig.model_validate(raw)


def test_negative_emission_factor_rejected(raw: dict[str, Any]) -> None:
    _block(raw, "water")["emission_factors"][0]["value"] = -1.0
    with pytest.raises(ValidationError):
        PlantConfig.model_validate(raw)


def test_non_positive_lhv_rejected(raw: dict[str, Any]) -> None:
    _block(raw, "electrolyser")["lhv_MJ_per_kg"] = 0
    with pytest.raises(ValidationError):
        PlantConfig.model_validate(raw)


def test_only_electricity_can_be_renewable(raw: dict[str, Any]) -> None:
    _block(raw, "water")["renewable"] = True
    with pytest.raises(ValidationError, match="only electricity"):
        PlantConfig.model_validate(raw)


def test_electricity_measured_in_kwh(raw: dict[str, Any]) -> None:
    grid = _block(raw, "grid_power")
    grid["quantity_unit"] = "kg"
    grid["emission_factors"][0]["unit"] = "gCO2e/kg"
    with pytest.raises(ValidationError, match="kWh"):
        PlantConfig.model_validate(raw)


def test_renewable_input_needs_series(raw: dict[str, Any]) -> None:
    del _block(raw, "renewable_power")["series"]
    with pytest.raises(ValidationError, match="renewable-available series"):
        PlantConfig.model_validate(raw)


def test_opening_ledger_ids_unique(raw: dict[str, Any]) -> None:
    entry = {
        "batch_id": "E-2026-09-001",
        "rfnbo_kg": 10.0,
        "ghg_gCO2e_per_MJ": 5.0,
        "rfnbo_share": 1.0,
        "production_month": "2026-09",
    }
    _block(raw, "h2_storage")["opening_ledger"] = [entry, entry]
    with pytest.raises(ValidationError, match="repeats a batch id"):
        PlantConfig.model_validate(raw)


def test_opening_ledger_month_format(raw: dict[str, Any]) -> None:
    _block(raw, "h2_storage")["opening_ledger"] = [
        {
            "batch_id": "X",
            "rfnbo_kg": 1.0,
            "ghg_gCO2e_per_MJ": 1.0,
            "rfnbo_share": 1.0,
            "production_month": "2026-13",
        }
    ]
    with pytest.raises(ValidationError):
        PlantConfig.model_validate(raw)


def test_emission_factor_at_picks_latest_valid(raw: dict[str, Any]) -> None:
    _block(raw, "water")["emission_factors"].append(
        {"value": 3.0, "unit": "gCO2e/kg", "valid_from": "2026-11-01", "source": "update"}
    )
    water = PlantConfig.model_validate(raw).block("water")
    assert isinstance(water, InputBlock)
    assert water.emission_factor_at(dt.date(2026, 10, 31)).value == 2.0
    assert water.emission_factor_at(dt.date(2026, 11, 1)).value == 3.0
    with pytest.raises(LookupError):
        water.emission_factor_at(dt.date(2025, 12, 31))


def test_select_version(raw: dict[str, Any]) -> None:
    v1 = PlantConfig.model_validate(raw)
    v2 = PlantConfig.model_validate(raw | {"version": "2", "effective_from": "2026-11-01"})
    assert select_version([v1, v2], dt.date(2026, 10, 31)) is v1
    assert select_version([v2, v1], dt.date(2026, 11, 1)) is v2
    with pytest.raises(LookupError):
        select_version([v1, v2], dt.date(2025, 1, 1))
    clash = PlantConfig.model_validate(raw | {"version": "1b"})
    with pytest.raises(ValueError, match="share effective_from"):
        select_version([v1, clash], dt.date(2026, 6, 1))


def test_pos_allocation_names_a_batch_or_a_range() -> None:
    from rfnbo.model.entities import PoSAllocation

    t0 = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
    PoSAllocation(pos="P", flow="f", batch="B", kg=1.0)
    PoSAllocation(pos="P", flow="f", start=t0, end=t0 + dt.timedelta(hours=1), kg=1.0)
    for bad in (
        {},
        {"batch": "B", "start": t0, "end": t0 + dt.timedelta(hours=1)},
        {"start": t0},
        {"start": t0, "end": t0},
    ):
        with pytest.raises(ValueError):
            PoSAllocation(pos="P", flow="f", kg=1.0, **bad)
