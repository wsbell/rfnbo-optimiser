"""Pydantic models for the plant block flow diagram (build plan §2.1).

The saved diagram is the single definition of the plant. It is versioned with an
effective-from date so that a plant change mid-year does not alter past results.

Structural rules that span several blocks (orphans, flow materials, cycles, ...)
live in :mod:`rfnbo.config.validate`, so each rule can be reported by name.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ELECTRICITY = "electricity"

QuantityUnit = Literal["kWh", "kg"]
EmissionFactorUnit = Literal["gCO2e/kWh", "gCO2e/kg"]

EF_UNIT_FOR_QUANTITY: dict[str, str] = {"kWh": "gCO2e/kWh", "kg": "gCO2e/kg"}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EmissionFactor(_Model):
    """An emission factor with its validity start and source reference."""

    value: float = Field(ge=0)
    unit: EmissionFactorUnit
    valid_from: dt.date
    source: str = Field(min_length=1)


class _Block(_Model):
    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_\-]+$")
    name: str = Field(min_length=1)


class InputBlock(_Block):
    """A utility or feedstock entering the plant (renewable power, grid power, water, N₂)."""

    kind: Literal["input"] = "input"
    material: str = Field(min_length=1)
    quantity_unit: QuantityUnit
    renewable: bool = False
    emission_factors: list[EmissionFactor] = Field(min_length=1)
    # Renewable power available (kWh per interval). Other inputs are metered per unit,
    # on the flow into that unit.
    series: str | None = None

    @model_validator(mode="after")
    def _check(self) -> InputBlock:
        expected = EF_UNIT_FOR_QUANTITY[self.quantity_unit]
        for ef in self.emission_factors:
            if ef.unit != expected:
                raise ValueError(
                    f"emission factor unit {ef.unit!r} does not match quantity unit "
                    f"{self.quantity_unit!r} (expected {expected!r})"
                )
        dates = [ef.valid_from for ef in self.emission_factors]
        if len(set(dates)) != len(dates):
            raise ValueError("two emission factors share the same valid_from date")
        if self.renewable and self.material != ELECTRICITY:
            raise ValueError("only electricity inputs can be flagged renewable")
        if self.material == ELECTRICITY and self.quantity_unit != "kWh":
            raise ValueError("electricity inputs must be measured in kWh")
        if self.renewable and self.series is None:
            raise ValueError("a renewable power input needs a renewable-available series")
        return self

    def emission_factor_at(self, date: dt.date) -> EmissionFactor:
        """The emission factor in force on ``date`` (latest ``valid_from`` on or before it)."""
        valid = [ef for ef in self.emission_factors if ef.valid_from <= date]
        if not valid:
            raise LookupError(f"input {self.id!r} has no emission factor valid on {date}")
        return max(valid, key=lambda ef: ef.valid_from)


class ProcessingUnit(_Block):
    """Converts inputs to one product (electrolyser, NH₃ synthesis, compression)."""

    kind: Literal["processing_unit"] = "processing_unit"
    product: str = Field(min_length=1)
    lhv_MJ_per_kg: float = Field(gt=0)
    electricity_adds_to_lhv: bool
    fixed_adder_gCO2e_per_MJ: float = Field(ge=0)
    production_series: str | None = None  # kg per interval; required by the validator
    electricity_series: str | None = None  # total kWh consumed per interval


class OpeningLedgerEntry(_Model):
    """A batch carried into the first quarter of a run (manual opening ledger)."""

    batch_id: str = Field(min_length=1)
    rfnbo_kg: float = Field(ge=0)
    ghg_gCO2e_per_MJ: float = Field(ge=0)
    # None when the source record does not state it; never assumed.
    rfnbo_share: float | None = Field(default=None, ge=0, le=1)
    production_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")


class StorageUnit(_Block):
    """Holds product between units and across periods; keeps a quarterly RFNBO ledger.

    A pass-through storage models a direct unit → unit link (build plan §10.2 rule 5): it has
    no inventory meter, its physical inventory is zero, and its RFNBO ledger must close at
    zero each quarter.
    """

    kind: Literal["storage_unit"] = "storage_unit"
    product: str = Field(min_length=1)
    pass_through: bool = False
    inventory_series: str | None = None  # kg at interval end; required unless pass-through
    opening_inventory_kg: float = Field(ge=0)
    opening_ledger: list[OpeningLedgerEntry] = Field(default_factory=list)
    # MJ of final product per MJ carried here, used only when this quarter's data has no
    # downstream flow to measure it from (rfnbo.calc.weights); --opening-from sets it.
    carried_weight_fallback: float | None = Field(default=None, gt=0)

    @field_validator("inventory_series")
    @classmethod
    def _blank_is_none(cls, v: str | None) -> str | None:
        return v or None

    @model_validator(mode="after")
    def _check(self) -> StorageUnit:
        ids = [e.batch_id for e in self.opening_ledger]
        if len(set(ids)) != len(ids):
            raise ValueError("opening ledger repeats a batch id")
        if self.pass_through:
            if self.inventory_series:
                raise ValueError("a pass-through storage has no inventory series")
            if self.opening_inventory_kg or self.opening_ledger:
                raise ValueError("a pass-through storage opens empty (inventory and ledger zero)")
        return self


class RfnboSupply(_Block):
    """RFNBO bought from a third-party supplier (build plan §11.1 rule 1).

    Each flow out of it carries the metered kg delivered per interval into a unit or a
    storage. Delivered product is RFNBO only where a proof of sustainability from this
    supplier is allocated to it; the rest is non-RFNBO.
    """

    kind: Literal["rfnbo_supply"] = "rfnbo_supply"
    supplier: str = Field(min_length=1)
    product: str = Field(min_length=1)
    lhv_MJ_per_kg: float = Field(gt=0)


class DistributionBlock(_Block):
    """Fixed transport and conversion emissions to one customer, applied after shipping."""

    kind: Literal["distribution"] = "distribution"
    customer: str = Field(min_length=1)
    fixed_adder_gCO2e_per_MJ: float = Field(ge=0)


class ConsignmentOutput(_Block):
    """The delivery point a consignment is claimed against."""

    kind: Literal["consignment_output"] = "consignment_output"
    customer: str = Field(min_length=1)
    distribution_block: str = Field(min_length=1)


Block = Annotated[
    InputBlock | ProcessingUnit | StorageUnit | RfnboSupply | DistributionBlock | ConsignmentOutput,
    Field(discriminator="kind"),
]


class Flow(_Model):
    """A one-way connection carrying one material between two blocks."""

    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_\-]+$")
    source: str
    target: str
    material: str = Field(min_length=1)
    # Metered quantity per interval: other inputs into a unit (kg) or transfers out of
    # storage (kg). Electricity flows carry no series; units meter total electricity.
    series: str | None = None


class Settings(_Model):
    """Reference data and rule settings (scope doc: Inputs, Batching)."""

    fossil_comparator_gCO2e_per_MJ: float = Field(default=94.0, gt=0)
    required_saving: float = Field(default=0.70, gt=0, lt=1)
    # Hourly temporal correlation for units whose electricity adds to product LHV.
    hourly_correlation_from: dt.date = dt.date(2030, 1, 1)
    # Data quality: a storage meter-balance residual above this is flagged (not corrected).
    balance_tolerance_kg_per_interval: float = Field(default=1.0, ge=0)

    @property
    def rfnbo_threshold_gCO2e_per_MJ(self) -> float:
        """28.2 gCO₂e/MJ with the defaults (70% saving against 94)."""
        return self.fossil_comparator_gCO2e_per_MJ * (1.0 - self.required_saving)


class Position(_Model):
    """Where a block sits on the diagram canvas (presentation only)."""

    x: float
    y: float


class PlantConfig(_Model):
    """One version of the plant block flow diagram."""

    schema_version: Literal[1] = 1
    plant: str = Field(min_length=1)
    version: str = Field(min_length=1)
    effective_from: dt.date
    timezone: str
    settings: Settings = Field(default_factory=Settings)
    blocks: list[Block]
    flows: list[Flow]
    # Diagram positions written by the editor. Presentation only: never read by the engine
    # and left out of the config hash of a sealed run.
    layout: dict[str, Position] = Field(default_factory=dict)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA time zone {v!r}") from exc
        return v

    def block(self, block_id: str) -> Block:
        for b in self.blocks:
            if b.id == block_id:
                return b
        raise KeyError(block_id)

    def blocks_of[T](self, cls: type[T]) -> list[T]:
        return [b for b in self.blocks if isinstance(b, cls)]

    def flows_into(self, block_id: str) -> list[Flow]:
        return [f for f in self.flows if f.target == block_id]

    def flows_out_of(self, block_id: str) -> list[Flow]:
        return [f for f in self.flows if f.source == block_id]


def select_version(configs: list[PlantConfig], on: dt.date) -> PlantConfig:
    """The config version in force on ``on``: the latest ``effective_from`` on or before it."""
    valid = [c for c in configs if c.effective_from <= on]
    if not valid:
        raise LookupError(f"no plant config version is effective on {on}")
    dates = [c.effective_from for c in valid]
    latest = max(dates)
    if dates.count(latest) > 1:
        raise ValueError(f"two plant config versions share effective_from {latest}")
    return next(c for c in valid if c.effective_from == latest)
