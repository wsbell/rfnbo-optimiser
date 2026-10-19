"""Storage RFNBO ledgers, quarter-end write-off and physical reconciliation (§2.7).

Each storage keeps an RFNBO ledger per calendar quarter, checked at quarter end only. Non-
compliant RFNBO (§2.5) cannot be carried into the next quarter, so it is written off in full
(decided 2026-10-17). The compliant closing ledger may not exceed measured physical inventory;
the excess is written off from the highest-GHG batches first (equal GHG: newest production
month first). Non-compliant batches come first in that order. Carried batches keep their ID,
GHG, RFNBO share and production month.

Purchased RFNBO allocated to a supplier's delivery into a storage enters its ledger under the
PoS's id, with the PoS's GHG and its dispatch month in place of a production month
(§11.1 decision 8); it then follows §2.7 like any other batch.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from rfnbo.calc.batch import (
    BatchResult,
    CalculationError,
    output_storages,
    storage_lhv,
    supply_flow,
)
from rfnbo.calc.tolerance import RANK_DECIMALS, meets_threshold
from rfnbo.config.schema import PlantConfig, ProcessingUnit, RfnboSupply, StorageUnit
from rfnbo.data.quality import Frame
from rfnbo.model.entities import Decisions


@dataclass(frozen=True)
class LedgerLine:
    batch: str
    origin: Literal["opening", "produced", "purchased"]
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
    def purchased_kg(self) -> float:
        return sum(x.in_kg for x in self.lines if x.origin == "purchased")

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
        """Closing ledger above measured physical inventory."""
        return max(0.0, self.closing_before_kg - self.physical_kg)

    @property
    def noncompliant_closing_kg(self) -> float:
        """Non-compliant RFNBO left at quarter end: it may not be carried."""
        return sum(max(0.0, x.closing_before_kg) for x in self.lines if not x.compliant)

    @property
    def required_write_off_kg(self) -> float:
        """What the rules require to be written off: all non-compliant RFNBO, and at least the
        excess over physical inventory (non-compliant batches are taken first)."""
        return max(self.excess_kg, self.noncompliant_closing_kg)

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
    delivered_kg: float = 0.0  # from RFNBO suppliers, metered

    @property
    def expected_kg(self) -> float:
        return (
            self.opening_physical_kg
            + self.produced_kg
            + self.delivered_kg
            - sum(kg for _, kg in self.outflows_kg)
        )

    @property
    def implied_loss_kg(self) -> float:
        return self.expected_kg - self.measured_kg


def priority_key(ghg: float, month: str, compliant: bool = True) -> tuple[bool, float, str]:
    """Sort descending on this key: non-compliant first, then highest GHG, then newest month."""
    return (not compliant, round(ghg, RANK_DECIMALS), month)


def rule_write_offs(
    lines: Iterable[tuple[str, float, str, float]],
    required_kg: float,
    noncompliant: Iterable[str] = (),
) -> dict[str, float]:
    """Write-off required by §2.7 for (batch, GHG, month, closing kg) lines.

    ``noncompliant`` names the batches above the threshold; they go first. Within an exact
    tie (same rounded GHG and month), batches are taken in descending id order, so the result
    is deterministic.
    """
    bad = set(noncompliant)
    ordered = sorted(
        lines, key=lambda x: (*priority_key(x[1], x[2], x[0] not in bad), x[0]), reverse=True
    )
    out: dict[str, float] = {}
    left = required_kg
    for batch, _, _, closing in ordered:
        if left <= 0:
            break
        take = min(closing, left)
        if take > 0:
            out[batch] = take
            left -= take
    return out


def batch_storages(
    cfg: PlantConfig, batches: Mapping[str, BatchResult], decisions: Decisions | None = None
) -> dict[str, tuple[str, ...]]:
    """Which storage ledger(s) each batch (produced, opening or a PoS) belongs to. A batch of a
    unit that splits its output is in every storage its unit outputs to (§10.2 rule 4); a PoS
    is in every storage its credentials were allocated into."""
    where = {b.id: tuple(s.id for s in output_storages(cfg, b.unit)) for b in batches.values()}
    for s in cfg.blocks_of(StorageUnit):
        for entry in s.opening_ledger:
            if entry.batch_id in where:
                raise CalculationError(
                    f"opening ledger batch {entry.batch_id} clashes with a batch"
                )
            where[entry.batch_id] = (s.id,)
    bought: dict[str, list[str]] = defaultdict(list)
    for a in decisions.pos_allocations if decisions else ():
        f, _ = supply_flow(cfg, a.flow)
        if isinstance(cfg.block(f.target), StorageUnit) and f.target not in bought[a.pos]:
            bought[a.pos].append(f.target)
    for pos_id, stores in bought.items():
        if pos_id in where:
            raise CalculationError(f"PoS {pos_id} clashes with a batch or opening entry")
        where[pos_id] = tuple(sorted(stores))
    return where


def feed_storage_for(
    cfg: PlantConfig, homes: Mapping[str, tuple[str, ...]], source: str, target_unit: str
) -> str:
    """The storage a feed allocation draws on: the one holding ``source`` that feeds
    ``target_unit``. If none does, the batch's first storage (the feed check then fails)."""
    feeding = {f.source for f in cfg.flows_into(target_unit)}
    held = homes[source]
    return next((s for s in held if s in feeding), held[0])


def consignment_storage(cfg: PlantConfig, output_id: str) -> str:
    from rfnbo.calc.consign import consignment_route  # consign imports this module

    return consignment_route(cfg, output_id)[2].id


def compute_ledgers(
    cfg: PlantConfig,
    frame: Frame,
    batches: Mapping[str, BatchResult],
    decisions: Decisions,
) -> list[LedgerResult]:
    homes = batch_storages(cfg, batches, decisions)
    threshold = cfg.settings.rfnbo_threshold_gCO2e_per_MJ

    def held(batch: str, context: str) -> tuple[str, ...]:
        if batch not in homes:
            raise CalculationError(f"{context}: unknown batch {batch!r}")
        return homes[batch]

    Key = tuple[str, str]  # (storage, batch)
    feed_out: dict[Key, float] = defaultdict(float)
    for a in decisions.feed:
        held(a.source, f"feed into {a.target}")
        if a.target not in batches:
            raise CalculationError(f"feed allocated to unknown batch {a.target!r}")
        store = feed_storage_for(cfg, homes, a.source, batches[a.target].unit)
        feed_out[store, a.source] += a.kg
    cons_store = {c.id: c.output for c in decisions.consignments}
    cons_out: dict[Key, float] = defaultdict(float)
    for agg in decisions.aggregated_batches:
        output = cons_store.get(agg.consignment)
        for p in agg.parts:
            stores = held(p.batch, f"aggregated batch {agg.id}")
            store = consignment_storage(cfg, output) if output else stores[0]
            cons_out[store if store in stores else stores[0], p.batch] += p.kg
    written: dict[Key, float] = defaultdict(float)
    for w in decisions.write_offs:
        if w.storage not in held(w.batch, "write-off"):
            raise CalculationError(
                f"write-off of {w.batch} names storage {w.storage}, "
                f"but the batch is in {', '.join(homes[w.batch])}"
            )
        written[w.storage, w.batch] += w.kg

    # RFNBO entering each storage from each batch: all of it for a single output; the
    # optimiser's assignment for a split output.
    assigned: dict[Key, float] = defaultdict(float)
    flows = {f.id: f for f in cfg.flows}
    for o in decisions.output_allocations:
        f = flows.get(o.flow)
        if o.batch not in batches:
            raise CalculationError(f"output allocation: unknown batch {o.batch!r}")
        if f is None or f.source != batches[o.batch].unit or len(homes[o.batch]) < 2:
            raise CalculationError(
                f"output allocation of {o.batch}: {o.flow!r} is not a split output of its unit"
            )
        assigned[f.target, o.batch] += o.kg

    bought: dict[Key, float] = defaultdict(float)
    for pa in decisions.pos_allocations:
        f, _ = supply_flow(cfg, pa.flow)
        if isinstance(cfg.block(f.target), StorageUnit):
            bought[f.target, pa.pos] += pa.kg
    pos = {p.id: p for p in decisions.pos}

    def entering(s: StorageUnit, b: BatchResult) -> float:
        return b.rfnbo_kg if len(homes[b.id]) == 1 else assigned[s.id, b.id]

    results: list[LedgerResult] = []
    for s in cfg.blocks_of(StorageUnit):
        lhv = storage_lhv(cfg, s.id)
        lines: list[LedgerLine] = []
        for e in s.opening_ledger:
            k = (s.id, e.batch_id)
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
                    out_feed_kg=feed_out[k],
                    out_consignment_kg=cons_out[k],
                    write_off_kg=written[k],
                )
            )
        for b in batches.values():
            if s.id not in homes[b.id]:
                continue
            k = (s.id, b.id)
            in_kg = entering(s, b)
            touched = k in feed_out or k in cons_out or k in written
            if in_kg <= 0 and not touched:
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
                    in_kg=in_kg,
                    out_feed_kg=feed_out[k],
                    out_consignment_kg=cons_out[k],
                    write_off_kg=written[k],
                )
            )
        for (sid, pos_id), kg in sorted(bought.items()):
            if sid != s.id:
                continue
            bp = pos[pos_id]
            k = (s.id, pos_id)
            lines.append(
                LedgerLine(
                    batch=pos_id,
                    origin="purchased",
                    month=f"{bp.dispatch_date.year:04d}-{bp.dispatch_date.month:02d}",
                    ghg_gCO2e_per_MJ=bp.ghg_gCO2e_per_MJ,
                    rfnbo_share=1.0,
                    compliant=meets_threshold(bp.ghg_gCO2e_per_MJ, threshold),
                    lhv_MJ_per_kg=lhv,
                    in_kg=kg,
                    out_feed_kg=feed_out[k],
                    out_consignment_kg=cons_out[k],
                    write_off_kg=written[k],
                )
            )
        lines.sort(
            key=lambda x: (*priority_key(x.ghg_gCO2e_per_MJ, x.month, x.compliant), x.batch),
            reverse=True,
        )
        physical = 0.0 if s.pass_through else float(frame.series[s.inventory_series or ""][-1])
        results.append(LedgerResult(s.id, frame.quarter, tuple(lines), physical))
    return results


def compute_reconciliation(cfg: PlantConfig, frame: Frame) -> list[Reconciliation]:
    out: list[Reconciliation] = []
    for s in cfg.blocks_of(StorageUnit):
        produced = delivered = 0.0
        for f in cfg.flows_into(s.id):
            unit = cfg.block(f.source)
            if isinstance(unit, ProcessingUnit):
                # A split output is counted by its metered flow (§10.2 rule 4).
                produced += float(frame.series[f.series or unit.production_series or ""].sum())
            elif isinstance(unit, RfnboSupply):
                delivered += float(frame.series[f.series or ""].sum())
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
                measured_kg=0.0
                if s.pass_through
                else float(frame.series[s.inventory_series or ""][-1]),
                delivered_kg=delivered,
            )
        )
    return out
