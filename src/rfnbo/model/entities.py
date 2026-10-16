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
    consignments: list[ConsignmentSpec] = Field(default_factory=list)
    aggregated_batches: list[AggregatedBatchSpec] = Field(default_factory=list)
    write_offs: list[WriteOff] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> Decisions:
        for label, ids in (
            ("batch", [b.id for b in self.batches]),
            ("consignment", [c.id for c in self.consignments]),
            ("aggregated batch", [a.id for a in self.aggregated_batches]),
            ("renewable allocation unit", [r.unit for r in self.renewable_allocations]),
        ):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            if dupes:
                raise ValueError(f"duplicate {label} id(s): {dupes}")
        return self
