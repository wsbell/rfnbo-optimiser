"""Property-form definitions for each block kind, flows, and the plant itself.

Plain data (no UI imports) so a test can check every schema field has an editor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

FieldType = Literal["text", "optional_text", "number", "bool", "select", "date", "block_ref"]


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    type: FieldType
    options: tuple[str, ...] = ()
    hint: str = ""


@dataclass(frozen=True)
class TableSpec:
    """A list-of-objects field edited as rows (emission factors, opening ledger)."""

    key: str
    label: str
    columns: tuple[FieldSpec, ...]


BLOCK_FIELDS: dict[str, tuple[FieldSpec | TableSpec, ...]] = {
    "input": (
        FieldSpec("material", "Material", "text", hint="electricity, water, N2, ..."),
        FieldSpec("quantity_unit", "Quantity unit", "select", ("kWh", "kg")),
        FieldSpec("renewable", "Renewable electricity", "bool"),
        FieldSpec(
            "series",
            "Renewable-available series",
            "optional_text",
            hint="renewable power inputs only",
        ),
        TableSpec(
            "emission_factors",
            "Emission factors",
            (
                FieldSpec("value", "Value", "number"),
                FieldSpec("unit", "Unit", "select", ("gCO2e/kWh", "gCO2e/kg")),
                FieldSpec("valid_from", "Valid from", "date"),
                FieldSpec("source", "Source", "text"),
            ),
        ),
    ),
    "processing_unit": (
        FieldSpec("product", "Product", "text"),
        FieldSpec("lhv_MJ_per_kg", "LHV (MJ/kg)", "number"),
        FieldSpec("electricity_adds_to_lhv", "Electricity adds to product LHV", "bool"),
        FieldSpec("fixed_adder_gCO2e_per_MJ", "Fixed GHG adder (gCO2e/MJ)", "number"),
        FieldSpec("production_series", "Production series (kg)", "optional_text"),
        FieldSpec("electricity_series", "Electricity series (kWh)", "optional_text"),
    ),
    "storage_unit": (
        FieldSpec("product", "Product", "text"),
        FieldSpec("inventory_series", "Inventory series (kg)", "optional_text"),
        FieldSpec("opening_inventory_kg", "Opening physical inventory (kg)", "number"),
        TableSpec(
            "opening_ledger",
            "Opening RFNBO ledger",
            (
                FieldSpec("batch_id", "Batch", "text"),
                FieldSpec("rfnbo_kg", "RFNBO kg", "number"),
                FieldSpec("ghg_gCO2e_per_MJ", "GHG (gCO2e/MJ)", "number"),
                FieldSpec("rfnbo_share", "RFNBO share", "number", hint="blank if unknown"),
                FieldSpec("production_month", "Month (YYYY-MM)", "text"),
            ),
        ),
    ),
    "distribution": (
        FieldSpec("customer", "Customer", "text"),
        FieldSpec("fixed_adder_gCO2e_per_MJ", "Fixed GHG adder (gCO2e/MJ)", "number"),
    ),
    "consignment_output": (
        FieldSpec("customer", "Customer", "text"),
        FieldSpec("distribution_block", "Distribution block", "block_ref", ("distribution",)),
    ),
}

FLOW_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("material", "Material", "text"),
    FieldSpec(
        "series",
        "Metered series (kg)",
        "optional_text",
        hint="inputs other than electricity, and transfers out of storage",
    ),
)

PLANT_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("plant", "Plant", "text"),
    FieldSpec("version", "Version", "text"),
    FieldSpec("effective_from", "Effective from", "date"),
    FieldSpec("timezone", "Plant time zone (IANA)", "text"),
)

SETTINGS_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("fossil_comparator_gCO2e_per_MJ", "Fossil comparator (gCO2e/MJ)", "number"),
    FieldSpec("required_saving", "Required saving (fraction)", "number"),
    FieldSpec("hourly_correlation_from", "Hourly correlation from", "date"),
    FieldSpec(
        "balance_tolerance_kg_per_interval", "Meter-balance tolerance (kg/interval)", "number"
    ),
)
# Nullable number columns: a blank cell means "not stated", never zero.
NULLABLE_NUMBERS = {("opening_ledger", "rfnbo_share")}
