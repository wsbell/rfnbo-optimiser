"""Storage weights: the MJ of final product that 1 MJ of RFNBO carried in a storage becomes.

Objective 2 (decided 2026-10-17) values compliant RFNBO carried forward at each storage by its
expected final-product yield, so that holding product upstream of a lossy step is never worth
more than the product it would become:

- a storage that feeds a distribution block holds final product: weight 1;
- otherwise weight = Σ over its outflows of (share of the storage's metered outflow) × (the
  receiving unit's energy yield, product MJ ÷ feed MJ, over the quarter) × (weight of the
  storage that unit outputs to); applied recursively, so a chain multiplies its yields.

Yields come from the quarter's own metered data. When a storage or a downstream unit has no
metered flow this quarter, the storage's ``carried_weight_fallback`` is used (``--opening-from``
sets it from the previous sealed run); without one the weight is unknown (None) and anything
that needs it must say so rather than guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from rfnbo.calc.batch import output_storage, storage_lhv
from rfnbo.config.schema import DistributionBlock, PlantConfig, ProcessingUnit, StorageUnit
from rfnbo.data.quality import Frame


@dataclass(frozen=True)
class StorageWeight:
    storage: str
    weight: float | None
    source: Literal["final", "data", "fallback", "unknown"]
    detail: str


def _kg(frame: Frame, series: str | None) -> float:
    return float(frame.series[series].sum()) if series and series in frame.series else 0.0


def unit_energy_yield(cfg: PlantConfig, frame: Frame, unit: ProcessingUnit) -> float | None:
    """Product MJ ÷ product feed MJ over the quarter; None when the unit took no feed."""
    feed_mj = sum(
        _kg(frame, f.series) * storage_lhv(cfg, f.source)
        for f in cfg.flows_into(unit.id)
        if isinstance(cfg.block(f.source), StorageUnit)
    )
    if feed_mj <= 0:
        return None
    return _kg(frame, unit.production_series) * unit.lhv_MJ_per_kg / feed_mj


def storage_weights(cfg: PlantConfig, frame: Frame) -> dict[str, StorageWeight]:
    out: dict[str, StorageWeight] = {}

    def fallback(s: StorageUnit, why: str) -> StorageWeight:
        if s.carried_weight_fallback is not None:
            return StorageWeight(s.id, s.carried_weight_fallback, "fallback", why)
        return StorageWeight(s.id, None, "unknown", why)

    def weigh(s: StorageUnit) -> StorageWeight:
        if s.id in out:
            return out[s.id]
        flows = cfg.flows_out_of(s.id)
        if any(isinstance(cfg.block(f.target), DistributionBlock) for f in flows) and not any(
            isinstance(cfg.block(f.target), ProcessingUnit) for f in flows
        ):
            result = StorageWeight(s.id, 1.0, "final", "feeds a distribution block")
            out[s.id] = result
            return result
        total = sum(_kg(frame, f.series) for f in flows)
        if total <= 0:
            result = fallback(s, "no metered outflow this quarter")
            out[s.id] = result
            return result
        value = 0.0
        parts = []
        for f in flows:
            share = _kg(frame, f.series) / total
            if share <= 0:
                continue
            target = cfg.block(f.target)
            if isinstance(target, DistributionBlock):
                value += share
                parts.append(f"{share:.4f} × final")
                continue
            if not isinstance(target, ProcessingUnit):
                continue
            y = unit_energy_yield(cfg, frame, target)
            down = weigh(output_storage(cfg, target.id))
            if y is None or down.weight is None:
                result = fallback(s, f"no yield for {target.id} this quarter")
                out[s.id] = result
                return result
            value += share * y * down.weight
            parts.append(f"{share:.4f} × {target.id} yield {y:.6f} × {down.weight:.6f}")
        result = StorageWeight(s.id, value, "data", " + ".join(parts))
        out[s.id] = result
        return result

    for s in cfg.blocks_of(StorageUnit):
        weigh(s)
    return {s.id: out[s.id] for s in cfg.blocks_of(StorageUnit)}
