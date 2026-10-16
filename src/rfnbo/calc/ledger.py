"""Storage RFNBO ledgers, quarter-end write-off and physical reconciliation (§2.7).

Each storage keeps an RFNBO ledger per calendar quarter, checked at quarter end only. The
closing ledger may not exceed measured physical inventory; the excess is written off from
the highest-GHG batches first (equal GHG: newest production month first). Carried batches
keep their ID, GHG, RFNBO share and production month.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from rfnbo.calc.batch import BatchResult, CalculationError, output_storage, storage_lhv
from rfnbo.calc.tolerance import RANK_DECIMALS, meets_threshold
from rfnbo.config.schema import PlantConfig, ProcessingUnit, StorageUnit
from rfnbo.data.quality import Frame
from rfnbo.model.entities import Decisions


@dataclass(frozen=True)
class LedgerLine:
    batch: str
    origin: Literal["opening", "produced"]
    month: str
    ghg_gCO2e_per_MJ: float
    rfnbo_share: float | None
    compliant: bool
    lhv_MJ_per_kg: float
    in_kg: float
    out_feed_kg: float
    out_consignment_kg: float
    write_off_kg: float

    @property
    def closing_before_kg(self) -> float:
        return self.in_kg - self.out_feed_kg - self.out_consignment_kg

    @property
    def carried_kg(self) -> float:
        return self.closing_before_kg - self.write_off_kg


@dataclass(frozen=True)
class LedgerResult:
    storage: str
    quarter: str
    lines: tuple[LedgerLine, ...]  # write-off priority order
    physical_kg: float

    @property
    def opening_kg(self) -> float:
        return sum(x.in_kg for x in self.lines if x.origin == "opening")

    @property
    def produced_kg(self) -> float:
        return sum(x.in_kg for x in self.lines if x.origin == "produced")

    @property
    def out_feed_kg(self) -> float:
        return sum(x.out_feed_kg for x in self.lines)

    @property
    def out_consignment_kg(self) -> float:
        return sum(x.out_consignment_kg for x in self.lines)

    @property
    def closing_before_kg(self) -> float:
        return sum(x.closing_before_kg for x in self.lines)

    @property
    def excess_kg(self) -> float:
        """What the rules require to be written off."""
        return max(0.0, self.closing_before_kg - self.physical_kg)

    @property
    def write_off_kg(self) -> float:
        return sum(x.write_off_kg for x in self.lines)

    @property
    def carried_kg(self) -> float:
        return sum(x.carried_kg for x in self.lines)

    @property
    def carried_ghg_gCO2e_per_MJ(self) -> float | None:
        mj = sum(x.carried_kg * x.lhv_MJ_per_kg for x in self.lines)
        if mj <= 0:
            return None
        em = sum(x.carried_kg * x.lhv_MJ_per_kg * x.ghg_gCO2e_per_MJ for x in self.lines)
        return em / mj


@dataclass(frozen=True)
class Reconciliation:
    storage: str
    quarter: str
    opening_physical_kg: float
    produced_kg: float
    outflows_kg: tuple[tuple[str, float], ...]  # (flow id, kg)
    measured_kg: float

    @property
    def expected_kg(self) -> float:
        return self.opening_physical_kg + self.produced_kg - sum(kg for _, kg in self.outflows_kg)

    @property
    def implied_loss_kg(self) -> float:
        return self.expected_kg - self.measured_kg


def priority_key(ghg: float, month: str) -> tuple[float, str]:
    """Sort descending on this key: highest GHG first, then newest month first."""
    return (round(ghg, RANK_DECIMALS), month)


def rule_write_offs(
    lines: Iterable[tuple[str, float, str, float]], excess_kg: float
) -> dict[str, float]:
    """Write-off required by §2.7 for (batch, GHG, month, closing kg) lines.

    Within an exact tie (same rounded GHG and month), batches are taken in descending id
    order, so the result is deterministic.
    """
    ordered = sorted(lines, key=lambda x: (*priority_key(x[1], x[2]), x[0]), reverse=True)
    out: dict[str, float] = {}
    left = excess_kg
    for batch, _, _, closing in ordered:
        if left <= 0:
            break
        take = min(closing, left)
        if take > 0:
            out[batch] = take
            left -= take
    return out


def batch_storage(cfg: PlantConfig, batches: Mapping[str, BatchResult]) -> dict[str, str]:
    """Which storage ledger each batch (produced or opening) belongs to."""
    where = {b.id: output_storage(cfg, b.unit).id for b in batches.values()}
    for s in cfg.blocks_of(StorageUnit):
        for entry in s.opening_ledger:
            if entry.batch_id in where:
                raise CalculationError(
                    f"opening ledger batch {entry.batch_id} clashes with a batch"
                )
            where[entry.batch_id] = s.id
    return where


def compute_ledgers(
    cfg: PlantConfig,
    frame: Frame,
    batches: Mapping[str, BatchResult],
    decisions: Decisions,
) -> list[LedgerResult]:
    where = batch_storage(cfg, batches)
    threshold = cfg.settings.rfnbo_threshold_gCO2e_per_MJ

    def home(batch: str, context: str) -> str:
        if batch not in where:
            raise CalculationError(f"{context}: unknown batch {batch!r}")
        return where[batch]

    feed_out: dict[str, float] = defaultdict(float)
    for a in decisions.feed:
        home(a.source, f"feed into {a.target}")
        feed_out[a.source] += a.kg
    cons_out: dict[str, float] = defaultdict(float)
    for agg in decisions.aggregated_batches:
        for p in agg.parts:
            home(p.batch, f"aggregated batch {agg.id}")
            cons_out[p.batch] += p.kg
    written: dict[str, float] = defaultdict(float)
    for w in decisions.write_offs:
        if home(w.batch, "write-off") != w.storage:
            raise CalculationError(
                f"write-off of {w.batch} names storage {w.storage}, "
                f"but the batch is in {where[w.batch]}"
            )
        written[w.batch] += w.kg

    results: list[LedgerResult] = []
    for s in cfg.blocks_of(StorageUnit):
        lhv = storage_lhv(cfg, s.id)
        lines: list[LedgerLine] = []
        for e in s.opening_ledger:
            lines.append(
                LedgerLine(
                    batch=e.batch_id,
                    origin="opening",
                    month=e.production_month,
                    ghg_gCO2e_per_MJ=e.ghg_gCO2e_per_MJ,
                    rfnbo_share=e.rfnbo_share,
                    compliant=meets_threshold(e.ghg_gCO2e_per_MJ, threshold),
                    lhv_MJ_per_kg=lhv,
                    in_kg=e.rfnbo_kg,
                    out_feed_kg=feed_out[e.batch_id],
                    out_consignment_kg=cons_out[e.batch_id],
                    write_off_kg=written[e.batch_id],
                )
            )
        for b in batches.values():
            if where[b.id] != s.id:
                continue
            touched = b.id in feed_out or b.id in cons_out or b.id in written
            if not b.is_rfnbo and not touched:
                continue
            lines.append(
                LedgerLine(
                    batch=b.id,
                    origin="produced",
                    month=b.month,
                    ghg_gCO2e_per_MJ=b.ghg_gCO2e_per_MJ if b.ghg_gCO2e_per_MJ is not None else 0.0,
                    rfnbo_share=b.rfnbo_share,
                    compliant=b.compliant,
                    lhv_MJ_per_kg=lhv,
                    in_kg=b.rfnbo_kg,
                    out_feed_kg=feed_out[b.id],
                    out_consignment_kg=cons_out[b.id],
                    write_off_kg=written[b.id],
                )
            )
        lines.sort(
            key=lambda x: (*priority_key(x.ghg_gCO2e_per_MJ, x.month), x.batch), reverse=True
        )
        physical = float(frame.series[s.inventory_series or ""][-1])
        results.append(LedgerResult(s.id, frame.quarter, tuple(lines), physical))
    return results


def compute_reconciliation(cfg: PlantConfig, frame: Frame) -> list[Reconciliation]:
    out: list[Reconciliation] = []
    for s in cfg.blocks_of(StorageUnit):
        produced = 0.0
        for f in cfg.flows_into(s.id):
            unit = cfg.block(f.source)
            if isinstance(unit, ProcessingUnit):
                produced += float(frame.series[unit.production_series or ""].sum())
        outflows = tuple(
            (f.id, float(frame.series[f.series or ""].sum())) for f in cfg.flows_out_of(s.id)
        )
        out.append(
            Reconciliation(
                storage=s.id,
                quarter=frame.quarter,
                opening_physical_kg=s.opening_inventory_kg,
                produced_kg=produced,
                outflows_kg=outflows,
                measured_kg=float(frame.series[s.inventory_series or ""][-1]),
            )
        )
    return out
