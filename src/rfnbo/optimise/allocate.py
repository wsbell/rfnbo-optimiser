"""Stages B and C (build plan §6): feed allocation, then consignments and write-offs.

Stage B (LP per downstream unit). Variables x[b, m]: kg of upstream ledger batch b fed to
downstream batch m. With F_m = Σ_b x·LHV_b (RFNBO feed MJ), C_m the feed the batch consumed
(MJ), own_m its own emissions and MJ_m its output energy, the downstream batch is compliant
when GHG_m ≤ T, which is linear in x:

    Σ_b x[b, m] · LHV_b · (g_b + own_m / C_m − (T − A) · MJ_m / C_m) ≤ 0

Its RFNBO output is P_m · F_m / C_m kg. Objectives, lexicographic:
    1. maximise RFNBO output of the downstream unit (compliant by construction);
    2. minimise GHG of inventory carried forward: the upstream storage's carried remainder
       (exact) plus the downstream batches' carried shares (weights from stage C);
    3. prefer same-month feed (readability).

Stage C (LP per storage with consignments). Variables y[l, c]: kg of compliant ledger line l
to consignment c, and k[l]: kg carried forward. Each consignment gets its volume with GHG at or
below min(target, threshold) − distribution adder. Objectives: minimise-mode consignments'
GHG first, then GHG carried forward, then oldest month first into consignments. Write-offs
then follow the §2.7 rule exactly (highest GHG first, newest month on ties).

Non-compliant RFNBO (§2.5) is a valid feed source in stage B and never a consignment source.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from rfnbo.calc.batch import BatchResult
from rfnbo.calc.calculator import RunResult
from rfnbo.calc.consign import consignment_route
from rfnbo.calc.ledger import LedgerLine, LedgerResult, rule_write_offs
from rfnbo.config.schema import PlantConfig, ProcessingUnit, StorageUnit
from rfnbo.model.entities import (
    AggregatedBatchSpec,
    ConsignmentSpec,
    FeedAllocation,
    Part,
    WriteOff,
)
from rfnbo.optimise.lp import INF, LinearProgram

MARGIN = 1e-7  # gCO2e/MJ kept below every limit, so solver tolerance never breaks a check
KEEP = 1e-9  # relative slack when fixing an earlier objective
DROP_KG = 1e-7  # allocations smaller than this are solver noise


def _month_number(month: str) -> int:
    y, m = month.split("-")
    return int(y) * 12 + int(m)


@dataclass(frozen=True)
class FeedPlan:
    unit: str
    feed: list[FeedAllocation]
    rfnbo_kg: float  # downstream RFNBO output achieved
    status: str


def feed_storage(cfg: PlantConfig, unit: ProcessingUnit) -> StorageUnit | None:
    stores = [
        b for f in cfg.flows_into(unit.id) if isinstance(b := cfg.block(f.source), StorageUnit)
    ]
    if len(stores) > 1:
        raise NotImplementedError(f"{unit.id}: stage B supports one product feed")
    return stores[0] if stores else None


def stage_b(
    cfg: PlantConfig,
    run: RunResult,
    unit: ProcessingUnit,
    carried_weight: Mapping[str, float] | None = None,
    min_rfnbo_kg: float | None = None,
) -> FeedPlan:
    """Allocate RFNBO feed to one downstream unit's batches.

    ``run`` is a calculation of the current decisions without feed into ``unit``.
    ``carried_weight`` (downstream batch → share of its RFNBO expected to be carried
    forward, from stage C) steers objective 2; ``min_rfnbo_kg`` lowers objective 1 when a
    consignment can only be met with less RFNBO output (objective 1 of the run).
    """
    store = feed_storage(cfg, unit)
    if store is None:
        return FeedPlan(unit.id, [], 0.0, "no feed")
    threshold = cfg.settings.rfnbo_threshold_gCO2e_per_MJ
    ledger = run.ledger(store.id)
    sources = [x for x in ledger.lines if x.in_kg - x.out_consignment_kg > 0]
    targets = sorted((b for b in run.batches.values() if b.unit == unit.id), key=lambda b: b.start)

    lp = LinearProgram()
    x: dict[tuple[str, str], int] = {}
    for s in sources:
        for t in targets:
            if t.feed_consumed_MJ > 0:
                x[s.batch, t.id] = lp.var()
    by_source: dict[str, list[int]] = defaultdict(list)
    for (sb, _), j in x.items():
        by_source[sb].append(j)
    src = {s.batch: s for s in sources}

    for s in sources:
        if by_source[s.batch]:
            lp.row(
                by_source[s.batch],
                [1.0] * len(by_source[s.batch]),
                upper=s.in_kg - s.out_consignment_kg,
            )
    rfnbo_coef: dict[int, float] = {}
    emissions_coef: dict[int, float] = {}
    for t in targets:
        cols = [j for (sb, tb), j in x.items() if tb == t.id]
        if not cols:
            continue
        lhv = [src[sb].lhv_MJ_per_kg for (sb, tb) in x if tb == t.id]
        lp.row(cols, lhv, upper=t.feed_consumed_MJ)
        own_per_mj = t.own_emissions_g / t.feed_consumed_MJ
        out_per_mj = t.energy_MJ / t.feed_consumed_MJ
        limit = (threshold - t.adder_gCO2e_per_MJ - MARGIN) * out_per_mj
        lp.row(
            cols,
            [
                src[sb].lhv_MJ_per_kg * (src[sb].ghg_gCO2e_per_MJ + own_per_mj - limit)
                for (sb, tb) in x
                if tb == t.id
            ],
            upper=0.0,
        )
        for (sb, tb), j in x.items():
            if tb != t.id:
                continue
            mj = src[sb].lhv_MJ_per_kg
            rfnbo_coef[j] = mj * t.product_kg / t.feed_consumed_MJ
            emissions_coef[j] = mj * (
                src[sb].ghg_gCO2e_per_MJ + own_per_mj + t.adder_gCO2e_per_MJ * out_per_mj
            )
    if not x:
        return FeedPlan(unit.id, [], 0.0, "no feed")

    # 1. Most RFNBO output.
    lp.set_cost(rfnbo_coef)
    best = lp.solve(maximise=True)
    if not best.optimal:
        return FeedPlan(unit.id, [], 0.0, best.status)
    floor = best.objective if min_rfnbo_kg is None else min(best.objective, min_rfnbo_kg)
    lp.row(list(rfnbo_coef), list(rfnbo_coef.values()), lower=floor * (1 - KEEP) - 1e-6)

    # 2. Least GHG carried forward: the upstream remainder (exact) and the downstream share.
    fed_kg = float(sum(best.x[j] for j in x.values()))
    available = sum(s.in_kg - s.out_consignment_kg for s in sources)
    physical = ledger.physical_kg
    carry_all = available - fed_kg <= physical
    k: dict[str, int] = {}
    for s in sources:
        k[s.batch] = lp.var()
        cols = [k[s.batch], *by_source[s.batch]]
        avail = s.in_kg - s.out_consignment_kg
        if carry_all:
            lp.row(cols, [1.0] * len(cols), lower=avail, upper=avail)
        else:
            lp.row(cols, [1.0] * len(cols), upper=avail)
    if not carry_all:
        lp.row(list(k.values()), [1.0] * len(k), lower=physical, upper=physical)
    weights = carried_weight or {}
    cost2: dict[int, float] = {k[s.batch]: s.lhv_MJ_per_kg * s.ghg_gCO2e_per_MJ for s in sources}
    for (_, tb), j in x.items():
        cost2[j] = cost2.get(j, 0.0) + weights.get(tb, 0.0) * emissions_coef[j]
    lp.set_cost(cost2)
    second = lp.solve()
    if second.optimal:
        lp.row(
            list(cost2),
            list(cost2.values()),
            upper=second.objective + abs(second.objective) * KEEP + 1e-3,
        )
        # 3. Same-month feed where it makes no difference.
        months = {t.id: t.month for t in targets}
        lp.set_cost(
            {
                j: abs(_month_number(src[sb].month) - _month_number(months[tb]))
                for (sb, tb), j in x.items()
            }
        )
        third = lp.solve()
        final = third if third.optimal else second
    else:
        final = best

    feed = [
        FeedAllocation(source=sb, target=tb, kg=float(final.x[j]))
        for (sb, tb), j in x.items()
        if final.x[j] > DROP_KG
    ]
    feed.sort(key=lambda a: (a.target, a.source))
    rfnbo = sum(rfnbo_coef[j] * final.x[j] for j in x.values() if final.x[j] > DROP_KG)
    return FeedPlan(unit.id, feed, rfnbo, final.status)


@dataclass(frozen=True)
class Shortfall:
    consignment: str
    reason: str
    achievable_kg: float


@dataclass(frozen=True)
class ConsignmentPlan:
    aggregated: list[AggregatedBatchSpec]
    feasible: bool
    shortfalls: list[Shortfall] = field(default_factory=list)


def _lines_for(ledger: LedgerResult) -> list[LedgerLine]:
    return [x for x in ledger.lines if x.in_kg - x.out_feed_kg > DROP_KG]


def stage_c(
    cfg: PlantConfig, run: RunResult, consignments: Sequence[ConsignmentSpec]
) -> ConsignmentPlan:
    """Allocate compliant ledger batches to consignments, keeping the lowest GHG carried."""
    threshold = cfg.settings.rfnbo_threshold_gCO2e_per_MJ
    by_store: dict[str, list[ConsignmentSpec]] = defaultdict(list)
    adder: dict[str, float] = {}
    for c in consignments:
        _, dist, store = consignment_route(cfg, c.output)
        by_store[store.id].append(c)
        adder[c.id] = dist.fixed_adder_gCO2e_per_MJ

    aggregated: list[AggregatedBatchSpec] = []
    shortfalls: list[Shortfall] = []
    for store_id, cons in by_store.items():
        ledger = run.ledger(store_id)
        lines = _lines_for(ledger)
        lp = LinearProgram()
        y: dict[tuple[str, str], int] = {}
        for line in lines:
            if line.compliant:
                for c in cons:
                    y[line.batch, c.id] = lp.var()
        k = {line.batch: lp.var() for line in lines}
        for line in lines:
            cols = [k[line.batch], *[y[line.batch, c.id] for c in cons if (line.batch, c.id) in y]]
            lp.row(cols, [1.0] * len(cols), upper=line.in_kg - line.out_feed_kg)
        limits: dict[str, float] = {}
        for c in cons:
            cols = [y[line.batch, c.id] for line in lines if (line.batch, c.id) in y]
            lp.row(cols, [1.0] * len(cols), lower=c.volume_kg, upper=c.volume_kg)
            target = (
                threshold
                if c.target_gCO2e_per_MJ is None
                else min(c.target_gCO2e_per_MJ, threshold)
            )
            limits[c.id] = target - adder[c.id] - MARGIN
            lp.row(
                cols,
                [line.ghg_gCO2e_per_MJ - limits[c.id] for line in lines if (line.batch, c.id) in y],
                upper=0.0,
            )
        remaining = sum(line.in_kg - line.out_feed_kg for line in lines) - sum(
            c.volume_kg for c in cons
        )
        carried = min(ledger.physical_kg, remaining)
        lp.row(list(k.values()), [1.0] * len(k), lower=carried, upper=carried)

        ghg = {line.batch: line.ghg_gCO2e_per_MJ for line in lines}
        lhv = {line.batch: line.lhv_MJ_per_kg for line in lines}
        month = {line.batch: line.month for line in lines}
        stages: list[dict[int, float]] = []
        minimise = [c.id for c in cons if c.target_gCO2e_per_MJ is None]
        if minimise:
            stages.append({j: ghg[lb] for (lb, cid), j in y.items() if cid in minimise})
        stages.append({j: lhv[lb] * ghg[lb] for lb, j in k.items()})
        stages.append({j: _month_number(month[lb]) for (lb, _), j in y.items()})

        sol = None
        for cost in stages:
            lp.set_cost(cost)
            sol = lp.solve()
            if not sol.optimal:
                break
            lp.row(
                list(cost),
                list(cost.values()),
                upper=sol.objective + abs(sol.objective) * KEEP + 1e-6,
            )
        if sol is None or not sol.optimal:
            shortfalls += diagnose(cfg, run, store_id, cons, limits)
            continue
        for c in cons:
            parts_by_month: dict[str, list[Part]] = defaultdict(list)
            for line in lines:
                j = y.get((line.batch, c.id))
                if j is not None and sol.x[j] > DROP_KG:
                    parts_by_month[line.month].append(Part(batch=line.batch, kg=float(sol.x[j])))
            for m in sorted(parts_by_month):
                aggregated.append(
                    AggregatedBatchSpec(id=f"{c.id}-{m}", consignment=c.id, parts=parts_by_month[m])
                )
    return ConsignmentPlan(aggregated, not shortfalls, shortfalls)


def diagnose(
    cfg: PlantConfig,
    run: RunResult,
    store_id: str,
    cons: Sequence[ConsignmentSpec],
    limits: Mapping[str, float],
) -> list[Shortfall]:
    """Which consignments cannot be met, why, and the most each could get at its target.

    Consignments that fail on their own are named; if every one could be met alone, they
    only fail together and all are named.
    """
    lines = _lines_for(run.ledger(store_id))
    compliant = [x for x in lines if x.compliant]
    lowest = min((x.ghg_gCO2e_per_MJ for x in compliant), default=INF)
    alone: list[Shortfall] = []
    together: list[Shortfall] = []
    for c in cons:
        lp = LinearProgram()
        cols = [lp.var(upper=x.in_kg - x.out_feed_kg, cost=1.0) for x in compliant]
        best = 0.0
        if cols:
            lp.row(cols, [x.ghg_gCO2e_per_MJ - limits[c.id] for x in compliant], upper=0.0)
            sol = lp.solve(maximise=True)
            best = sol.objective if sol.optimal else 0.0
        limit = limits[c.id] + MARGIN
        if best + 1e-6 >= c.volume_kg:
            together.append(
                Shortfall(
                    c.id,
                    "can be met alone but not together with the other consignments "
                    "drawing on the same low-GHG stock",
                    c.volume_kg,
                )
            )
        elif lowest > limit:
            alone.append(
                Shortfall(
                    c.id,
                    f"no compliant stock at or below {limit:.4g} gCO2e/MJ before "
                    f"distribution (lowest available {lowest:.4g})",
                    0.0,
                )
            )
        else:
            alone.append(
                Shortfall(c.id, f"only {best:,.1f} kg can be blended to {limit:.4g} gCO2e/MJ", best)
            )
    return alone or together


def write_offs(run: RunResult) -> list[WriteOff]:
    """Write-offs the §2.7 rule requires on the current ledgers."""
    out: list[WriteOff] = []
    for led in run.ledgers:
        lines = [
            (x.batch, x.ghg_gCO2e_per_MJ, x.month, x.closing_before_kg)
            for x in led.lines
            if x.closing_before_kg > 0
        ]
        for batch, kg in rule_write_offs(lines, led.excess_kg).items():
            out.append(WriteOff(storage=led.storage, batch=batch, kg=kg))
    return out


def carried_weights(run: RunResult, unit: str) -> dict[str, float]:
    """Share of each of ``unit``'s batches' RFNBO that is carried forward (for stage B)."""
    lines = {x.batch: x for led in run.ledgers for x in led.lines}
    out: dict[str, float] = {}
    for b in run.batches.values():
        if b.unit == unit and b.id in lines and lines[b.id].in_kg > 0:
            out[b.id] = max(0.0, lines[b.id].carried_kg) / lines[b.id].in_kg
    return out


def downstream_units(cfg: PlantConfig, order: Sequence[ProcessingUnit]) -> list[ProcessingUnit]:
    return [u for u in order if not u.electricity_adds_to_lhv]


def final_units(cfg: PlantConfig) -> list[str]:
    """Units whose product leaves through a distribution block (the final product)."""
    out = []
    for u in cfg.blocks_of(ProcessingUnit):
        store = [cfg.block(f.target) for f in cfg.flows_out_of(u.id)]
        if any(
            isinstance(s, StorageUnit)
            and any(cfg.block(g.target).kind == "distribution" for g in cfg.flows_out_of(s.id))
            for s in store
        ):
            out.append(u.id)
    return out


def batch_rfnbo(batches: Mapping[str, BatchResult], units: Sequence[str]) -> float:
    return sum(b.rfnbo_kg for b in batches.values() if b.unit in units and b.compliant)
