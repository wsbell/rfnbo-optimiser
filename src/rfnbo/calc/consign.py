"""Aggregated batches and shipped consignment GHG (§2.6, revised by §12 and §12.1).

An aggregated batch holds RFNBO of one production month and one source (the plant's own, or one
supplier), every part compliant on its own; its GHG is the energy-weighted average of its parts
(ISCC EU 205-1 monthly average, build plan §12.1). Months and sources are never averaged
together: a consignment's shipped GHG is its highest aggregated batch's GHG plus the customer's
distribution adder, so every aggregated batch meets the target and the threshold.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass

from rfnbo.calc.batch import CalculationError
from rfnbo.calc.ledger import LedgerLine
from rfnbo.config.schema import ConsignmentOutput, DistributionBlock, PlantConfig, StorageUnit
from rfnbo.model.entities import AggregatedBatchSpec, ConsignmentSpec, Decisions


@dataclass(frozen=True)
class AggregatedPart:
    batch: str
    storage: str
    month: str
    kg: float
    energy_MJ: float
    ghg_gCO2e_per_MJ: float
    compliant: bool
    origin: str = "produced"  # ledger origin: "produced", "opening" or "purchased" (a PoS)
    purchased: bool = False  # purchased RFNBO, passed through or processed
    source: str = "own"  # own production, or the supplier (§12.1)


@dataclass(frozen=True)
class AggregatedBatchResult:
    id: str
    consignment: str
    parts: tuple[AggregatedPart, ...]

    @property
    def months(self) -> tuple[str, ...]:
        return tuple(sorted({p.month for p in self.parts}))

    @property
    def kg(self) -> float:
        return sum(p.kg for p in self.parts)

    @property
    def energy_MJ(self) -> float:
        return sum(p.energy_MJ for p in self.parts)

    @property
    def sources(self) -> tuple[str, ...]:
        return tuple(sorted({p.source for p in self.parts}))

    @property
    def ghg_gCO2e_per_MJ(self) -> float | None:
        """Energy-weighted average of the parts (one month, one source, all compliant)."""
        mj = self.energy_MJ
        if mj <= 0:
            return None
        return sum(p.energy_MJ * p.ghg_gCO2e_per_MJ for p in self.parts) / mj

    @property
    def max_part_ghg_gCO2e_per_MJ(self) -> float | None:
        return max((p.ghg_gCO2e_per_MJ for p in self.parts), default=None)


@dataclass(frozen=True)
class ConsignmentResult:
    id: str
    output: str
    customer: str
    distribution: str
    source_storage: str
    product: str  # the source storage's product: one consignment, one product
    ship_date: dt.date
    volume_kg: float
    target_gCO2e_per_MJ: float | None
    threshold_gCO2e_per_MJ: float
    distribution_adder_gCO2e_per_MJ: float
    aggregated: tuple[AggregatedBatchResult, ...]

    @property
    def allocated_kg(self) -> float:
        return sum(a.kg for a in self.aggregated)

    @property
    def energy_MJ(self) -> float:
        return sum(a.energy_MJ for a in self.aggregated)

    @property
    def max_ghg_gCO2e_per_MJ(self) -> float | None:
        """The highest aggregated batch GHG before distribution (months and sources are never
        averaged together)."""
        return max(
            (g for a in self.aggregated if (g := a.ghg_gCO2e_per_MJ) is not None), default=None
        )

    @property
    def shipped_ghg_gCO2e_per_MJ(self) -> float | None:
        """The highest aggregated batch's GHG as shipped: every aggregated batch must meet the
        target and threshold."""
        w = self.max_ghg_gCO2e_per_MJ
        return None if w is None else w + self.distribution_adder_gCO2e_per_MJ


def consignment_route(
    cfg: PlantConfig, output_id: str
) -> tuple[ConsignmentOutput, DistributionBlock, StorageUnit]:
    try:
        out = cfg.block(output_id)
    except KeyError as exc:
        raise CalculationError(f"unknown consignment output {output_id!r}") from exc
    if not isinstance(out, ConsignmentOutput):
        raise CalculationError(f"{output_id!r} is not a consignment output")
    dist = cfg.block(out.distribution_block)
    assert isinstance(dist, DistributionBlock)
    sources = [cfg.block(f.source) for f in cfg.flows_into(dist.id)]
    stores = [s for s in sources if isinstance(s, StorageUnit)]
    if len(stores) != 1:
        raise CalculationError(f"distribution {dist.id} must be fed by exactly one storage")
    return out, dist, stores[0]


def compute_consignments(
    cfg: PlantConfig,
    decisions: Decisions,
    ledger_lines: Mapping[tuple[str, str], LedgerLine],  # (storage, batch) -> line
) -> list[ConsignmentResult]:
    threshold = cfg.settings.rfnbo_threshold_gCO2e_per_MJ
    by_cons: dict[str, list[AggregatedBatchSpec]] = {c.id: [] for c in decisions.consignments}
    for agg in decisions.aggregated_batches:
        if agg.consignment not in by_cons:
            raise CalculationError(
                f"aggregated batch {agg.id}: unknown consignment {agg.consignment!r}"
            )
        by_cons[agg.consignment].append(agg)
    return [
        _consignment(cfg, spec, by_cons[spec.id], ledger_lines, threshold)
        for spec in decisions.consignments
    ]


def _consignment(
    cfg: PlantConfig,
    spec: ConsignmentSpec,
    aggs: list[AggregatedBatchSpec],
    ledger_lines: Mapping[tuple[str, str], LedgerLine],
    threshold: float,
) -> ConsignmentResult:
    out, dist, store = consignment_route(cfg, spec.output)
    results: list[AggregatedBatchResult] = []
    for agg in aggs:
        parts: list[AggregatedPart] = []
        for p in agg.parts:
            # The consignment's own storage first; another ledger holding the batch is
            # reported by the consignment_source check.
            held = [k for k in ledger_lines if k[1] == p.batch]
            if not held:
                raise CalculationError(f"aggregated batch {agg.id}: {p.batch} is not in a ledger")
            storage = store.id if (store.id, p.batch) in ledger_lines else held[0][0]
            line = ledger_lines[storage, p.batch]
            parts.append(
                AggregatedPart(
                    batch=p.batch,
                    storage=storage,
                    month=line.month,
                    kg=p.kg,
                    energy_MJ=p.kg * line.lhv_MJ_per_kg,
                    ghg_gCO2e_per_MJ=line.ghg_gCO2e_per_MJ,
                    compliant=line.compliant,
                    origin=line.origin,
                    purchased=line.purchased,
                    source=line.source,
                )
            )
        results.append(AggregatedBatchResult(agg.id, spec.id, tuple(parts)))
    return ConsignmentResult(
        id=spec.id,
        output=out.id,
        customer=out.customer,
        distribution=dist.id,
        source_storage=store.id,
        product=store.product,
        ship_date=spec.ship_date,
        volume_kg=spec.volume_kg,
        target_gCO2e_per_MJ=spec.target_gCO2e_per_MJ,
        threshold_gCO2e_per_MJ=threshold,
        distribution_adder_gCO2e_per_MJ=dist.fixed_adder_gCO2e_per_MJ,
        aggregated=tuple(results),
    )
