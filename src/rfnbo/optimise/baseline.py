"""The baseline method: one batch per unit per month, first-in-first-out allocation.

A renewable pool shared by several units goes to the electrolyser first and the surplus to the
others. Purchased RFNBO: PoS credentials in dispatch order fill each supplier's metered
deliveries into units, batch by batch in time order, then into storages. Feed: RFNBO batches in
production order fill downstream batches in time order, up to what each consumes less the
credentials it took, with no regard to GHG. Consignments: compliant stock, oldest first, in ship
date order. Write-offs: the §2.7 rule. Reported next to every optimised result.
"""

from __future__ import annotations

from collections.abc import Sequence

from rfnbo.calc import calculate
from rfnbo.calc.batch import unit_order
from rfnbo.calc.consign import consignment_route
from rfnbo.config.schema import Flow, PlantConfig, RfnboSupply, StorageUnit
from rfnbo.data import calendar as cal
from rfnbo.data.ingest import FloatArray
from rfnbo.data.quality import Frame
from rfnbo.model.entities import (
    AggregatedBatchSpec,
    BatchSpec,
    ConsignmentSpec,
    Decisions,
    FeedAllocation,
    OutputAllocation,
    Part,
    PoSAllocation,
    ProofOfSustainability,
    RenewableAllocation,
)
from rfnbo.optimise import allocate, pool
from rfnbo.optimise.batching import Score, UnitBatching, windows


def monthly_batches(cfg: PlantConfig, frame: Frame) -> list[BatchSpec]:
    tz, specs = frame.timezone, []
    for unit in unit_order(cfg):
        slices, _ = windows(cfg, frame, unit)
        for s, e in slices:
            month = cal.month_key(frame.index[s], tz)
            specs.append(
                BatchSpec(
                    id=f"{unit.id}-{month}",
                    unit=unit.id,
                    start=frame.index[s],
                    end=frame.index[e] if e < len(frame) else frame.end,
                )
            )
    return specs


def monthly_split(cfg: PlantConfig, frame: Frame) -> list[RenewableAllocation]:
    """A shared renewable pool, baseline style: the electrolyser's monthly batch first, the
    surplus to the other units, with no regard to their compliance."""
    out: list[RenewableAllocation] = []
    for unit in unit_order(cfg):
        if unit.electricity_adds_to_lhv and pool.shares_pool(cfg, unit):
            cuts = windows(cfg, frame, unit)[0]
            fixed = UnitBatching(unit.id, cuts, None, None, Score(0.0, 0.0, len(cuts)))

            def monthly(_pool: FloatArray, b: UnitBatching = fixed) -> UnitBatching:
                return b

            out += pool.plan(cfg, frame, unit, monthly, repair=False).allocations
    return out


def pro_rata_outputs(
    cfg: PlantConfig, frame: Frame, decisions: Decisions
) -> list[OutputAllocation]:
    """A split output, baseline style: each batch's RFNBO in proportion to the metered flows."""
    run = calculate(cfg, frame, decisions)
    out: list[OutputAllocation] = []
    for b in run.batches.values():
        flows = cfg.flows_out_of(b.unit)
        if len(flows) < 2 or b.rfnbo_kg <= 0:
            continue
        metered = [
            float(frame.series[f.series or ""][b.start_index : b.end_index].sum()) for f in flows
        ]
        total = sum(metered)
        for f, m in zip(flows, metered, strict=True):
            if total > 0 and m > 0:
                out.append(OutputAllocation(batch=b.id, flow=f.id, kg=b.rfnbo_kg * m / total))
    return out


def fifo_credits(cfg: PlantConfig, frame: Frame, decisions: Decisions) -> list[PoSAllocation]:
    """PoS credentials in dispatch order into suppliers' metered deliveries: into units batch
    by batch in time order, then into storages over the whole quarter."""
    run = calculate(cfg, frame, decisions)
    left = {p.id: p.quantity_kg for p in decisions.pos}
    targets: list[tuple[Flow, RfnboSupply, str | None, float]] = []  # flow, supplier, batch, kg
    for unit in unit_order(cfg):
        for f, sup in allocate.supply_flows_into(cfg, unit.id):
            for b in sorted(
                (b for b in run.batches.values() if b.unit == unit.id), key=lambda b: b.start
            ):
                kg = float(frame.series[f.series or ""][b.start_index : b.end_index].sum())
                targets.append((f, sup, b.id, kg))
    for f in cfg.flows:
        src = cfg.block(f.source)
        if isinstance(src, RfnboSupply) and isinstance(cfg.block(f.target), StorageUnit):
            targets.append((f, src, None, float(frame.series[f.series or ""].sum())))
    out: list[PoSAllocation] = []
    for f, sup, batch, metered in targets:
        for p in sorted(allocate.usable_pos(run, sup), key=lambda p: (p.dispatch_date, p.id)):
            take = min(metered, left[p.id])
            if take <= 1e-9:
                continue
            if batch is None:
                out.append(
                    PoSAllocation(pos=p.id, flow=f.id, kg=take, start=frame.index[0], end=frame.end)
                )
            else:
                out.append(PoSAllocation(pos=p.id, flow=f.id, kg=take, batch=batch))
            left[p.id] -= take
            metered -= take
    return out


def baseline_decisions(
    cfg: PlantConfig,
    frame: Frame,
    consignments: Sequence[ConsignmentSpec],
    pos: Sequence[ProofOfSustainability] = (),
) -> Decisions:
    decisions = Decisions(
        batches=monthly_batches(cfg, frame),
        renewable_allocations=monthly_split(cfg, frame),
        consignments=list(consignments),
        pos=list(pos),
    )
    decisions = decisions.model_copy(
        update={"output_allocations": pro_rata_outputs(cfg, frame, decisions)}
    )
    if pos:
        decisions = decisions.model_copy(
            update={"pos_allocations": fifo_credits(cfg, frame, decisions)}
        )
    credited: dict[str, float] = {}
    for a in decisions.pos_allocations:
        if a.batch:
            credited[a.batch] = credited.get(a.batch, 0.0) + a.kg
    for unit in allocate.downstream_units(cfg, unit_order(cfg)):
        store = allocate.feed_storage(cfg, unit)
        if store is None:
            continue
        run = calculate(cfg, frame, decisions)
        sources = sorted(
            (x for x in run.ledger(store.id).lines if x.in_kg > 0), key=lambda x: (x.month, x.batch)
        )
        left = {x.batch: x.in_kg for x in sources}
        feed: list[FeedAllocation] = []
        for t in sorted(
            (b for b in run.batches.values() if b.unit == unit.id), key=lambda b: b.start
        ):
            need = t.feed_consumed_kg - credited.get(t.id, 0.0)
            for s in sources:
                if need <= 1e-9:
                    break
                take = min(left[s.batch], need)
                if take > 1e-9:
                    feed.append(FeedAllocation(source=s.batch, target=t.id, kg=take))
                    left[s.batch] -= take
                    need -= take
        decisions = decisions.model_copy(update={"feed": decisions.feed + feed})

    run = calculate(cfg, frame, decisions)
    aggregated: list[AggregatedBatchSpec] = []
    left_line: dict[str, float] = {}
    for c in sorted(consignments, key=lambda c: (c.ship_date, c.id)):
        _, _, store = consignment_route(cfg, c.output)
        stock = sorted(
            (x for x in run.ledger(store.id).lines if x.compliant), key=lambda x: (x.month, x.batch)
        )
        need = c.volume_kg
        by_month: dict[str, list[Part]] = {}
        for x in stock:
            avail = left_line.setdefault(x.batch, x.in_kg - x.out_feed_kg)
            take = min(avail, need)
            if take > 1e-9:
                by_month.setdefault(x.month, []).append(Part(batch=x.batch, kg=take))
                left_line[x.batch] -= take
                need -= take
            if need <= 1e-9:
                break
        aggregated += [
            AggregatedBatchSpec(id=f"{c.id}-{m}", consignment=c.id, parts=parts)
            for m, parts in sorted(by_month.items())
        ]
    return decisions.model_copy(update={"aggregated_batches": aggregated})
