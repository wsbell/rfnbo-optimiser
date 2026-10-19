"""Purchased RFNBO: the proof of sustainability register (build plan §11.1).

Each PoS's quantity is allocated to suppliers' metered deliveries in the quarter: into a unit
per batch, into a storage per interval range. What is not allocated by quarter end is written
off (decision 2). For each supplier, metered delivery is reconciled against the PoS
quantities and the credentials allocated; delivered product without credentials is non-RFNBO.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass

from rfnbo.calc.batch import BatchResult, CalculationError, supply_flow
from rfnbo.calc.tolerance import meets_threshold
from rfnbo.config.schema import PlantConfig, ProcessingUnit, RfnboSupply
from rfnbo.data.quality import Frame
from rfnbo.model.entities import Decisions, PoSAllocation


def quarter_of(date: dt.date) -> str:
    return f"{date.year:04d}-Q{(date.month - 1) // 3 + 1}"


@dataclass(frozen=True)
class PoSAllocationResult:
    flow: str
    supply: str
    target: str  # unit batch, or storage for a delivery into storage
    kind: str  # "unit" or "storage"
    start_index: int
    end_index: int
    kg: float
    metered_kg: float  # the flow's delivery over the batch or range


@dataclass(frozen=True)
class PoSResult:
    id: str
    supplier: str
    product: str
    dispatch_date: dt.date
    quarter: str
    month: str  # dispatch month: the place of passed-through RFNBO in aggregation and write-off
    quantity_kg: float
    ghg_gCO2e_per_MJ: float
    threshold_gCO2e_per_MJ: float
    allocations: tuple[PoSAllocationResult, ...]

    @property
    def compliant(self) -> bool:
        return meets_threshold(self.ghg_gCO2e_per_MJ, self.threshold_gCO2e_per_MJ)

    @property
    def allocated_kg(self) -> float:
        return sum(a.kg for a in self.allocations)

    @property
    def unallocated_kg(self) -> float:
        """Not allocated by quarter end: written off."""
        return max(0.0, self.quantity_kg - self.allocated_kg)


@dataclass(frozen=True)
class SupplyReconciliation:
    supply: str
    supplier: str
    product: str
    metered_kg: float  # every flow from the supplier, over the quarter
    pos_kg: float  # PoS from this supplier and product dispatched in the quarter
    allocated_kg: float

    @property
    def non_rfnbo_kg(self) -> float:
        """Delivered product without credentials allocated to it."""
        return self.metered_kg - self.allocated_kg

    @property
    def unallocated_pos_kg(self) -> float:
        return self.pos_kg - self.allocated_kg


def _allocation(
    cfg: PlantConfig,
    frame: Frame,
    batches: Mapping[str, BatchResult],
    a: PoSAllocation,
) -> PoSAllocationResult:
    f, sup = supply_flow(cfg, a.flow)
    if isinstance(cfg.block(f.target), ProcessingUnit):
        b = batches.get(a.batch or "")
        if b is None:
            raise CalculationError(f"PoS allocation of {a.pos}: unknown batch {a.batch!r}")
        s, e, target, kind = b.start_index, b.end_index, b.id, "unit"
    else:
        assert a.start is not None and a.end is not None
        try:
            s, e = frame.position(a.start), frame.position(a.end)
        except ValueError as exc:
            raise CalculationError(f"PoS allocation of {a.pos}: {exc}") from exc
        target, kind = f.target, "storage"
    metered = float(frame.series[f.series or ""][s:e].sum()) if f.series in frame.series else 0.0
    return PoSAllocationResult(a.flow, sup.id, target, kind, s, e, a.kg, metered)


def compute_purchases(
    cfg: PlantConfig, frame: Frame, decisions: Decisions, batches: Mapping[str, BatchResult]
) -> tuple[list[PoSResult], list[SupplyReconciliation]]:
    threshold = cfg.settings.rfnbo_threshold_gCO2e_per_MJ
    by_pos: dict[str, list[PoSAllocationResult]] = {p.id: [] for p in decisions.pos}
    for a in decisions.pos_allocations:
        if a.pos not in by_pos:
            raise CalculationError(f"PoS allocation to {a.flow}: unknown PoS {a.pos!r}")
        by_pos[a.pos].append(_allocation(cfg, frame, batches, a))
    register = [
        PoSResult(
            id=p.id,
            supplier=p.supplier,
            product=p.product,
            dispatch_date=p.dispatch_date,
            quarter=quarter_of(p.dispatch_date),
            month=f"{p.dispatch_date.year:04d}-{p.dispatch_date.month:02d}",
            quantity_kg=p.quantity_kg,
            ghg_gCO2e_per_MJ=p.ghg_gCO2e_per_MJ,
            threshold_gCO2e_per_MJ=threshold,
            allocations=tuple(by_pos[p.id]),
        )
        for p in decisions.pos
    ]
    supplies: list[SupplyReconciliation] = []
    for sup in cfg.blocks_of(RfnboSupply):
        metered = sum(
            float(frame.series[f.series].sum())
            for f in cfg.flows_out_of(sup.id)
            if f.series and f.series in frame.series
        )
        mine = [
            r
            for r in register
            if r.supplier == sup.supplier
            and r.product == sup.product
            and r.quarter == frame.quarter
        ]
        allocated = sum(a.kg for r in register for a in r.allocations if a.supply == sup.id)
        supplies.append(
            SupplyReconciliation(
                sup.id,
                sup.supplier,
                sup.product,
                metered,
                sum(r.quantity_kg for r in mine),
                allocated,
            )
        )
    return register, supplies
