"""Decision sets: everything the optimiser chooses, in a form the calculator can re-run.

A decision set plus the plant config and the 15-minute data fully determine every reported
number. Decision sets are plain JSON so auditors can inspect and re-run them.
"""

from __future__ import annotations

import datetime as dt

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BatchSpec(_Model):
    """A contiguous run of intervals on one unit: [start, end) in UTC."""

    id: str = Field(min_length=1)
    unit: str
    start: AwareDatetime
    end: AwareDatetime

    @field_validator("start", "end")
    @classmethod
    def _utc(cls, v: dt.datetime) -> dt.datetime:
        return v.astimezone(dt.UTC)

    @model_validator(mode="after")
    def _order(self) -> BatchSpec:
        if self.end <= self.start:
            raise ValueError(f"batch {self.id}: end must be after start")
        return self


class FeedAllocation(_Model):
    """RFNBO kg of an upstream batch allocated as feed to a downstream batch."""

    source: str
    target: str
    kg: float = Field(gt=0)
    # Feed lines into one target with the same label are averaged into one part (build plan
    # §12.1): same source and production month, each compliant.
    pool: str | None = Field(default=None, min_length=1)


class OutputAllocation(_Model):
    """RFNBO kg of a batch assigned to one of its unit's metered output flows, when the unit
    splits its output between several destinations (build plan §10.2 rule 4)."""

    batch: str
    flow: str  # flow id from the unit to a storage
    kg: float = Field(ge=0)


class RenewableAllocation(_Model):
    """Renewable kWh per interval given to one unit, when several units share the pool."""

    unit: str
    kWh: list[float]


class ConsignmentSpec(_Model):
    id: str = Field(min_length=1)
    output: str  # consignment_output block id
    volume_kg: float = Field(gt=0)
    ship_date: dt.date
    # None in minimise mode: no target, only the RFNBO threshold applies.
    target_gCO2e_per_MJ: float | None = None


class ProofOfSustainability(_Model):
    """A PoS for RFNBO bought from a supplier (build plan §11.1 rule 2). An input to the run,
    like a consignment: the optimiser decides only where its quantity is allocated.

    The GHG intensity includes distribution to this plant (decision 3). The PoS belongs to the
    mass balance period (calendar quarter) of its dispatch date (decision 1).
    """

    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_\-.]+$")
    supplier: str = Field(min_length=1)
    product: str = Field(min_length=1)
    dispatch_date: dt.date
    quantity_kg: float = Field(gt=0)
    ghg_gCO2e_per_MJ: float = Field(ge=0)


class PoSAllocation(_Model):
    """kg of a PoS's RFNBO credentials allocated to a supply flow (§11.1 rule 3): into a unit,
    for one of its batches (``batch``); into a storage, for the interval range [start, end),
    where it enters the storage's ledger under the PoS's id."""

    pos: str
    flow: str  # flow id from an RFNBO supply block
    kg: float = Field(gt=0)
    batch: str | None = None
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    pool: str | None = Field(default=None, min_length=1)  # averaged into one part (§12.1)

    @field_validator("start", "end")
    @classmethod
    def _utc(cls, v: dt.datetime | None) -> dt.datetime | None:
        return None if v is None else v.astimezone(dt.UTC)

    @model_validator(mode="after")
    def _target(self) -> PoSAllocation:
        ranged = self.start is not None and self.end is not None
        if (self.batch is None) == (not ranged) or (self.start is None) != (self.end is None):
            raise ValueError(
                f"PoS allocation of {self.pos} to {self.flow}: give a batch (flow into a unit) "
                "or a start and end (flow into a storage), not both"
            )
        if ranged and self.end <= self.start:  # type: ignore[operator]
            raise ValueError(f"PoS allocation of {self.pos} to {self.flow}: end before start")
        return self


class Part(_Model):
    batch: str
    kg: float = Field(gt=0)


class AggregatedBatchSpec(_Model):
    """Compliant sub-batches from one production month, assigned to one consignment."""

    id: str = Field(min_length=1)
    consignment: str
    parts: list[Part] = Field(min_length=1)


class WriteOff(_Model):
    storage: str
    batch: str
    kg: float = Field(gt=0)


class Decisions(_Model):
    batches: list[BatchSpec]
    feed: list[FeedAllocation] = Field(default_factory=list)
    renewable_allocations: list[RenewableAllocation] = Field(default_factory=list)
    output_allocations: list[OutputAllocation] = Field(default_factory=list)
    consignments: list[ConsignmentSpec] = Field(default_factory=list)
    aggregated_batches: list[AggregatedBatchSpec] = Field(default_factory=list)
    write_offs: list[WriteOff] = Field(default_factory=list)
    # Purchased RFNBO (§11.1): the PoS records are inputs; their allocation is a decision.
    pos: list[ProofOfSustainability] = Field(default_factory=list)
    pos_allocations: list[PoSAllocation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> Decisions:
        for label, ids in (
            ("batch", [b.id for b in self.batches]),
            ("PoS", [p.id for p in self.pos]),
            ("consignment", [c.id for c in self.consignments]),
            ("aggregated batch", [a.id for a in self.aggregated_batches]),
            ("renewable allocation unit", [r.unit for r in self.renewable_allocations]),
        ):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            if dupes:
                raise ValueError(f"duplicate {label} id(s): {dupes}")
        return self
