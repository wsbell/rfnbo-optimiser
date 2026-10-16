"""The baseline method: one batch per unit per month, first-in-first-out allocation.

Feed: RFNBO batches in production order fill downstream batches in time order, up to what
each consumes, with no regard to GHG. Consignments: compliant stock, oldest first, in ship
date order. Write-offs: the §2.7 rule. Reported next to every optimised result.
"""

from __future__ import annotations

from collections.abc import Sequence

from rfnbo.calc import calculate
from rfnbo.calc.batch import unit_order
from rfnbo.calc.consign import consignment_route
from rfnbo.config.schema import PlantConfig
from rfnbo.data import calendar as cal
from rfnbo.data.quality import Frame
from rfnbo.model.entities import (
    AggregatedBatchSpec,
    BatchSpec,
    ConsignmentSpec,
    Decisions,
    FeedAllocation,
    Part,
)
from rfnbo.optimise import allocate
from rfnbo.optimise.batching import windows


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


def baseline_decisions(
    cfg: PlantConfig, frame: Frame, consignments: Sequence[ConsignmentSpec]
) -> Decisions:
    decisions = Decisions(batches=monthly_batches(cfg, frame), consignments=list(consignments))
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
            need = t.feed_consumed_kg
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
