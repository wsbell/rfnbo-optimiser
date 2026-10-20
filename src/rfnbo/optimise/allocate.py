"""Stages B and C (build plan §6): feed allocation, then consignments and write-offs.

Stage B (LP per downstream unit). Variables x[b, m]: kg of upstream ledger part b fed to
downstream batch m. Batches are never averaged (build plan §12): each source becomes its own
part of m, with GHG g_b · C_m / MJ_m + own_m / MJ_m + A (C_m the feed m consumed, MJ_m its
output energy, own_m its own emissions), so a variable exists only where that part complies
(T, or the back-propagated limit for an intermediate product). Feed energy is at most C_m.

Its RFNBO output is P_m · F_m / C_m kg. Objectives, lexicographic:
    1. maximise RFNBO output of the downstream unit (compliant by construction); from the
       second pipeline round, maximise value instead: output MJ that stage C kept (consigned
       or carried) plus compliant RFNBO left upstream, each times its storage weight;
    2. minimise GHG of inventory carried forward: the upstream storage's carried remainder
       (exact) plus the downstream batches' carried shares (weights from stage C);
    3. prefer same-month feed (readability).

Stage C (LP per storage with consignments). Variables y[l, c]: kg of compliant ledger part l
to consignment c, and k[l]: kg carried forward. Each consignment gets its volume, and every
part it takes is at or below min(target, threshold) − distribution adder (parts are never
averaged, §12). Objectives: minimise-mode consignments'
GHG first, then GHG carried forward, then oldest month first into consignments. Write-offs
then follow the §2.7 rule exactly (highest GHG first, newest month on ties).

Non-compliant RFNBO (§2.5) is a valid feed source in stage B and never a consignment source;
it cannot be carried forward either, so what is not fed is written off.

Purchased RFNBO (§11.1 rule 5) is chosen with the feed: variables p[pos, flow, m] are the
credentials of a PoS allocated to a supplier's metered delivery into downstream batch m, within
the metered kg and the PoS's quantity; they enter every row exactly like feed from storage. A
credential is worth only what it adds: into a batch whose storage was already over its
physical inventory, nothing. Where the objectives tie, the lowest-GHG credentials are used
(least purchased emissions), then those dispatched nearest the batch's month. Credentials for
a supplier's delivery into a storage are allocated after stage B, lowest GHG first, up to the
storage's room (:func:`storage_credits`).
"""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass, field

from rfnbo.calc.batch import (
    OWN,
    BatchPart,
    BatchResult,
    output_storage,
    output_storages,
    storage_lhv,
)
from rfnbo.calc.calculator import RunResult
from rfnbo.calc.consign import consignment_route
from rfnbo.calc.ledger import LedgerLine, LedgerResult, rule_write_offs
from rfnbo.calc.purchase import quarter_of
from rfnbo.calc.tolerance import meets_threshold
from rfnbo.config.schema import Flow, PlantConfig, ProcessingUnit, RfnboSupply, StorageUnit
from rfnbo.data.quality import Frame
from rfnbo.model.entities import (
    AggregatedBatchSpec,
    ConsignmentSpec,
    FeedAllocation,
    OutputAllocation,
    Part,
    PoSAllocation,
    ProofOfSustainability,
    WriteOff,
)
from rfnbo.optimise.batching import blend_capacity, downstream_of
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


def part_ghg(t: BatchResult, g: float) -> float:
    """GHG of the part a source at ``g`` makes in downstream batch ``t`` (§12)."""
    return (
        g * t.feed_consumed_MJ / t.energy_MJ
        + t.own_emissions_g / t.energy_MJ
        + t.adder_gCO2e_per_MJ
    )


def part_ok(t: BatchResult, g: float, limit: float) -> bool:
    """Whether feeding a source at ``g`` into ``t`` gives a part within ``limit``."""
    return t.energy_MJ > 0 and part_ghg(t, g) <= limit - MARGIN


@dataclass(frozen=True)
class PoolMember:
    key: Hashable
    lhv_MJ_per_kg: float
    ghg_gCO2e_per_MJ: float
    month: str
    source: str


def pooled_feed(
    lp: LinearProgram, t: BatchResult, members: Sequence[PoolMember], limit: float
) -> dict[Hashable, tuple[int, str]]:
    """Pooled feed into ``t`` (§12.1): for each production month and source, one variable per
    compliant member and one row holding the pool's energy-weighted part GHG to ``limit``.

    A pool is made only where it can help: some member is within the limit alone and some is
    not (all within needs no pool; all above cannot average below it). Returns member key →
    (variable, pool label)."""
    out: dict[Hashable, tuple[int, str]] = {}
    if t.energy_MJ <= 0:
        return out
    groups: dict[tuple[str, str], list[PoolMember]] = defaultdict(list)
    for m in members:
        groups[m.month, m.source].append(m)
    cap = limit - MARGIN
    for (month, source), ms in sorted(groups.items()):
        pg = [part_ghg(t, m.ghg_gCO2e_per_MJ) for m in ms]
        if not (any(g <= cap for g in pg) and any(g > cap for g in pg)):
            continue
        label = f"{t.id}|{month}|{source}"
        cols = []
        for m, g in zip(ms, pg, strict=True):
            j = lp.var()
            out[m.key] = (j, label)
            cols.append((j, m.lhv_MJ_per_kg * (g - cap)))
        lp.row([j for j, _ in cols], [c for _, c in cols], upper=0.0)
    return out


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
    kept_share: Mapping[str, float] | None = None,
) -> FeedPlan:
    """Allocate RFNBO feed to one downstream unit's batches.

    ``run`` is a calculation of the current decisions without feed into ``unit``.
    ``carried_weight`` (downstream batch → share of its RFNBO expected to be carried
    forward, from stage C) steers objective 2; ``min_rfnbo_kg`` lowers objective 1 when a
    consignment can only be met with less RFNBO output (objective 1 of the run).

    Objective 1 is the most RFNBO output, unless ``kept_share`` (downstream batch → share of
    its RFNBO that stage C consigned or carried) is given: then it is the most value, with
    each batch's output in MJ times its kept share and times its storage weight, plus the
    compliant RFNBO left upstream (within measured inventory) times the upstream storage
    weight. Feeding product that would only be written off is then worth less than keeping
    the feed.
    """
    store = feed_storage(cfg, unit)
    if store is None:
        return FeedPlan(unit.id, [], 0.0, "no feed")
    ledger = run.ledger(store.id)
    sources = [x for x in ledger.lines if x.in_kg - x.out_consignment_kg > 0]
    targets = sorted((b for b in run.batches.values() if b.unit == unit.id), key=lambda b: b.start)

    lp = LinearProgram()
    limit_t = ghg_limit(cfg, run.frame, unit)
    x: dict[tuple[str, str], int] = {}
    for s in sources:
        for t in targets:
            if t.feed_consumed_MJ > 0 and part_ok(t, s.ghg_gCO2e_per_MJ, limit_t):
                x[s.batch, t.id] = lp.var()
    # Pooled feed (§12.1): compliant lines of one month and source averaged into one part.
    pool_of: dict[int, str] = {}
    z: dict[tuple[str, str], int] = {}
    for t in targets:
        if t.feed_consumed_MJ <= 0:
            continue
        members = [
            PoolMember(s.batch, s.lhv_MJ_per_kg, s.ghg_gCO2e_per_MJ, s.month, s.source)
            for s in sources
            if s.compliant
        ]
        for key, (j, label) in pooled_feed(lp, t, members, limit_t).items():
            assert isinstance(key, str)
            z[key, t.id] = j
            pool_of[j] = label
    if not x and not z:
        return FeedPlan(unit.id, [], 0.0, "no feed")
    feed_vars = [*x.items(), *z.items()]
    by_source: dict[str, list[int]] = defaultdict(list)
    for (sb, _), j in feed_vars:
        by_source[sb].append(j)
    src = {s.batch: s for s in sources}

    # k[s]: compliant RFNBO left in the upstream storage and carried forward. Non-compliant
    # RFNBO cannot be carried: whatever is not fed is written off.
    k = {s.batch: lp.var() for s in sources if s.compliant}
    for s in sources:
        cols = [*by_source[s.batch], *([k[s.batch]] if s.batch in k else [])]
        lp.row(cols, [1.0] * len(cols), upper=s.in_kg - s.out_consignment_kg)
    if k:
        lp.row(list(k.values()), [1.0] * len(k), upper=ledger.physical_kg)

    rfnbo_coef: dict[int, float] = {}
    emissions_coef: dict[int, float] = {}
    for t in targets:
        cols = [j for (sb, tb), j in feed_vars if tb == t.id]
        if not cols:
            continue
        lhv = [src[sb].lhv_MJ_per_kg for (sb, tb), _ in feed_vars if tb == t.id]
        lp.row(cols, lhv, upper=t.feed_consumed_MJ)
        own_per_mj = t.own_emissions_g / t.feed_consumed_MJ
        out_per_mj = t.energy_MJ / t.feed_consumed_MJ
        for (sb, tb), j in feed_vars:
            if tb != t.id:
                continue
            mj = src[sb].lhv_MJ_per_kg
            rfnbo_coef[j] = mj * t.product_kg / t.feed_consumed_MJ
            emissions_coef[j] = mj * (
                src[sb].ghg_gCO2e_per_MJ + own_per_mj + t.adder_gCO2e_per_MJ * out_per_mj
            )

    # 1. Most RFNBO output, or most value.
    if kept_share is None:
        cost1 = dict(rfnbo_coef)
    else:
        w_up = run.storage_weights[store.id].weight
        w_down = run.storage_weights[output_storage(cfg, unit.id).id].weight
        if w_up is None or w_down is None:
            raise ValueError(f"stage B {unit.id}: storage weight unknown; set a fallback")
        unit_of = {j: tb for (_, tb), j in feed_vars}
        cost1 = {
            j: c * unit.lhv_MJ_per_kg * w_down * kept_share.get(unit_of[j], 1.0)
            for j, c in rfnbo_coef.items()
        }
        cost1.update({kj: src[sb].lhv_MJ_per_kg * w_up for sb, kj in k.items()})
    lp.set_cost(cost1)
    best = lp.solve(maximise=True)
    if not best.optimal:
        return FeedPlan(unit.id, [], 0.0, best.status)
    if kept_share is None:
        floor = best.objective if min_rfnbo_kg is None else min(best.objective, min_rfnbo_kg)
        lp.row(list(rfnbo_coef), list(rfnbo_coef.values()), lower=floor * (1 - KEEP) - 1e-6)
        fed = {sb: sum(best.x[j] for j in by_source[sb]) for sb in k}
        left = sum(src[sb].in_kg - src[sb].out_consignment_kg - fed[sb] for sb in k)
        carry = min(ledger.physical_kg, left)
    else:
        lp.row(list(cost1), list(cost1.values()), lower=best.objective * (1 - KEEP) - 1e-6)
        carry = sum(best.x[kj] for kj in k.values())
    # Keep the upstream storage as full of compliant RFNBO as objective 1 left it.
    if k:
        lp.row(list(k.values()), [1.0] * len(k), lower=carry * (1 - KEEP) - 1e-6)

    # 2. Least GHG carried forward: the upstream remainder and the downstream share.
    weights = carried_weight or {}
    cost2: dict[int, float] = {
        kj: src[sb].lhv_MJ_per_kg * src[sb].ghg_gCO2e_per_MJ for sb, kj in k.items()
    }
    for (_, tb), j in feed_vars:
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
        cost3: dict[int, float] = {
            j: abs(_month_number(src[sb].month) - _month_number(months[tb]))
            for (sb, tb), j in feed_vars
        }
        lp.set_cost(cost3)
        third = lp.solve()
        final = third if third.optimal else second
        if third.optimal and pool_of:
            # 4. Pool only where it is beneficial: the least pooled feed.
            lp.row(list(cost3), list(cost3.values()), upper=third.objective + 1e-6)
            lp.set_cost(dict.fromkeys(pool_of, 1.0))
            fourth = lp.solve()
            final = fourth if fourth.optimal else third
    else:
        final = best

    feed = [
        FeedAllocation(source=sb, target=tb, kg=float(final.x[j]), pool=pool_of.get(j))
        for (sb, tb), j in feed_vars
        if final.x[j] > DROP_KG
    ]
    feed.sort(key=lambda a: (a.target, a.source, a.pool or ""))
    rfnbo = sum(rfnbo_coef[j] * final.x[j] for _, j in feed_vars if final.x[j] > DROP_KG)
    return FeedPlan(unit.id, feed, rfnbo, final.status)


@dataclass(frozen=True)
class NetworkPlan:
    units: list[str]
    feed: list[FeedAllocation]
    outputs: list[OutputAllocation]
    rfnbo_kg: float  # RFNBO output of the stage's units
    status: str
    pos: list[PoSAllocation] = field(default_factory=list)


def supply_flows_into(cfg: PlantConfig, unit_id: str) -> list[tuple[Flow, RfnboSupply]]:
    return [
        (f, b) for f in cfg.flows_into(unit_id) if isinstance(b := cfg.block(f.source), RfnboSupply)
    ]


def usable_pos(run: RunResult, sup: RfnboSupply) -> list[ProofOfSustainability]:
    """PoS whose credentials may go to this supplier's deliveries: same supplier and product,
    compliant, dispatched in the quarter (§11.1 decisions 1, 4, 7)."""
    threshold = run.config.settings.rfnbo_threshold_gCO2e_per_MJ
    return [
        p
        for p in run.decisions.pos
        if (p.supplier, p.product) == (sup.supplier, sup.product)
        and quarter_of(p.dispatch_date) == run.frame.quarter
        and meets_threshold(p.ghg_gCO2e_per_MJ, threshold)
    ]


def pos_left(run: RunResult, exclude_targets: set[str]) -> dict[str, float]:
    """Each PoS's quantity not allocated to batches outside ``exclude_targets`` (or storages)."""
    left = {p.id: p.quantity_kg for p in run.decisions.pos}
    for a in run.decisions.pos_allocations:
        if a.batch not in exclude_targets:
            left[a.pos] -= a.kg
    return left


def _month_of(date: dt.date) -> str:
    return f"{date.year:04d}-{date.month:02d}"


def feed_storages(cfg: PlantConfig, unit: ProcessingUnit) -> list[StorageUnit]:
    return [b for f in cfg.flows_into(unit.id) if isinstance(b := cfg.block(f.source), StorageUnit)]


def producers(cfg: PlantConfig, unit: ProcessingUnit) -> frozenset[str]:
    """Units that make the product ``unit`` takes from storage."""
    return frozenset(
        f.source
        for s in feed_storages(cfg, unit)
        for f in cfg.flows_into(s.id)
        if isinstance(cfg.block(f.source), ProcessingUnit)
    )


def stages(cfg: PlantConfig, order: Sequence[ProcessingUnit]) -> list[list[ProcessingUnit]]:
    """Downstream units grouped by the units that feed them, upstream first: parallel units
    on one producer are allocated together (one LP); units in series one stage after another."""
    groups: dict[frozenset[str], list[ProcessingUnit]] = {}
    for u in downstream_units(cfg, order):
        groups.setdefault(producers(cfg, u), []).append(u)
    return list(groups.values())


def needs_network(cfg: PlantConfig, stage: Sequence[ProcessingUnit]) -> bool:
    """A stage that is not a single chain: several units, several feed storages, or a
    producer that splits its output."""
    if len(stage) > 1 or any(len(feed_storages(cfg, u)) > 1 for u in stage):
        return True
    if any(supply_flows_into(cfg, u.id) for u in stage):
        return True  # purchased RFNBO is allocated in the network LP
    return any(len(cfg.flows_out_of(p)) > 1 for u in stage for p in producers(cfg, u))


def ghg_limit(cfg: PlantConfig, frame: Frame, unit: ProcessingUnit) -> float:
    """GHG the unit's product may reach: the threshold for a final product; for an
    intermediate, also the blended-feed limit of every unit it feeds (back-propagated)."""
    threshold = cfg.settings.rfnbo_threshold_gCO2e_per_MJ
    limit = threshold
    for s in output_storages(cfg, unit.id):
        for f in cfg.flows_out_of(s.id):
            down = cfg.block(f.target)
            if isinstance(down, ProcessingUnit):
                limit = min(limit, downstream_of(cfg, frame, unit, down).limit_gCO2e_per_MJ)
    return limit


def stage_b_network(
    cfg: PlantConfig,
    run: RunResult,
    units: Sequence[ProcessingUnit],
    carried_weight: Mapping[str, float] | None = None,
    min_rfnbo_kg: float | None = None,
    kept_share: Mapping[str, float] | None = None,
    credit_share: Mapping[str, float] | None = None,
) -> NetworkPlan:
    """Feed allocation for a stage of parallel units in one LP (build plan §10.4 phase 14).

    Variables: feed x[(s, b), m] from batch b in storage s into downstream batch m; carried
    k[(s, b)] (compliant only); and, for a producer that splits its output, o[b, flow]: the
    RFNBO of b assigned to each output flow (§10.2 rule 4), within the flow's metered kg and
    adding up to b's RFNBO. Availability rows are joint across the units, so the problem stays
    linear and exact. Each downstream batch is held to its unit's GHG limit (threshold, or the
    back-propagated limit for an intermediate product). Objectives as in :func:`stage_b`:
    output (round 1) or value (later rounds), then carried value, then carried emissions, then
    same-month feed. PoS credentials (§11.1) are variables like feed; ``credit_share`` (batch →
    share of an extra kg of its RFNBO that would be kept) values them in later rounds.
    """
    ids = [u.id for u in units]
    lp = LinearProgram()
    frame = run.frame
    stores = {s.id: s for u in units for s in feed_storages(cfg, u)}
    targets = sorted(
        (b for b in run.batches.values() if b.unit in ids and b.feed_consumed_MJ > 0),
        key=lambda b: (b.unit, b.start),
    )
    fed_by = {u.id: {s.id for s in feed_storages(cfg, u)} for u in units}

    # Sources: (storage, batch) with availability either fixed or an output-allocation variable.
    o: dict[tuple[str, str], int] = {}
    split_batches: dict[str, list[tuple[str, int]]] = defaultdict(list)
    sources: dict[tuple[str, str], tuple[LedgerLine | None, BatchPart | None]] = {}
    for sid in stores:
        for ln in run.ledger(sid).lines:
            if ln.origin == "opening" or len(run.storages_of.get(ln.batch, ())) == 1:
                sources[sid, ln.batch] = (ln, None)
        for f in cfg.flows_into(sid):
            prod = cfg.block(f.source)
            if not isinstance(prod, ProcessingUnit) or len(cfg.flows_out_of(prod.id)) < 2:
                continue
            for pb in run.batches.values():
                if pb.unit != prod.id or not pb.parts:
                    continue
                metered = float(frame.series[f.series or ""][pb.start_index : pb.end_index].sum())
                shared = []
                for part in pb.parts:  # the batch's parts share the flow's metered kg
                    jo = lp.var(upper=metered)
                    o[part.id, f.id] = jo
                    split_batches[part.id].append((f.id, jo))
                    sources[sid, part.id] = (None, part)
                    shared.append(jo)
                if len(shared) > 1:
                    lp.row(shared, [1.0] * len(shared), upper=metered)
    for pid, parts in split_batches.items():
        rf = run.parts[pid].kg
        lp.row([j for _, j in parts], [1.0] * len(parts), lower=rf, upper=rf)

    def ghg(key: tuple[str, str]) -> float:
        line, b = sources[key]
        if line is not None:
            return line.ghg_gCO2e_per_MJ
        assert b is not None
        return b.ghg_gCO2e_per_MJ

    def compliant(key: tuple[str, str]) -> bool:
        line, b = sources[key]
        return line.compliant if line is not None else bool(b and b.compliant)

    def month(key: tuple[str, str]) -> str:
        line, b = sources[key]
        return line.month if line is not None else (b.month if b else "")

    def source(key: tuple[str, str]) -> str:
        line, b = sources[key]
        return line.source if line is not None else (b.source if b else OWN)

    lhv = {sid: storage_lhv(cfg, sid) for sid in stores}

    def lhv_of(key: tuple[str, str]) -> float:
        return lhv[key[0]]

    limits = {u.id: ghg_limit(cfg, frame, u) for u in units}
    x: dict[tuple[tuple[str, str], str], int] = {}
    for key in sources:
        for t in targets:
            if key[0] in fed_by[t.unit] and part_ok(t, ghg(key), limits[t.unit]):
                x[key, t.id] = lp.var()
    # Pooled feed (§12.1): compliant storage lines, and credentials, of one production month
    # and one source averaged into one part of a target batch.
    left = pos_left(run, {t.id for t in targets})
    threshold = cfg.settings.rfnbo_threshold_gCO2e_per_MJ
    pool_of: dict[int, str] = {}
    zs: dict[tuple[tuple[str, str], str], int] = {}
    zp: dict[tuple[str, str, str], int] = {}
    zp_lhv: dict[int, float] = {}
    pos_ghg: dict[str, float] = {}
    pos_month: dict[str, str] = {}
    for t in targets:
        members = [
            PoolMember(("s", key), lhv_of(key), ghg(key), month(key), source(key))
            for key in sources
            if key[0] in fed_by[t.unit] and compliant(key)
        ]
        for f, sup in supply_flows_into(cfg, t.unit):
            metered = float(frame.series[f.series or ""][t.start_index : t.end_index].sum())
            for pp in usable_pos(run, sup):
                if metered <= 0 or left.get(pp.id, 0.0) <= DROP_KG:
                    continue
                if not meets_threshold(pp.ghg_gCO2e_per_MJ, threshold):
                    continue
                members.append(
                    PoolMember(
                        ("p", pp.id, f.id),
                        sup.lhv_MJ_per_kg,
                        pp.ghg_gCO2e_per_MJ,
                        _month_of(pp.dispatch_date),
                        pp.supplier,
                    )
                )
                pos_ghg[pp.id] = pp.ghg_gCO2e_per_MJ
                pos_month[pp.id] = _month_of(pp.dispatch_date)
        for mkey, (j, label) in pooled_feed(lp, t, members, limits[t.unit]).items():
            assert isinstance(mkey, tuple)
            pool_of[j] = label
            if mkey[0] == "s":
                zs[mkey[1], t.id] = j
            else:
                zp[mkey[1], mkey[2], t.id] = j
                zp_lhv[j] = next(m.lhv_MJ_per_kg for m in members if m.key == mkey)
    k = {key: lp.var() for key in sources if compliant(key)}
    by_source: dict[tuple[str, str], list[int]] = defaultdict(list)
    for (key, _), j in [*x.items(), *zs.items()]:
        by_source[key].append(j)
    flow_into = {(f.target, f.source): f.id for f in cfg.flows if f.target in stores}
    for key, (src_line, src_batch) in sources.items():
        use = [*by_source[key], *([k[key]] if key in k else [])]
        if not use:
            continue
        if src_line is not None:
            lp.row(use, [1.0] * len(use), upper=src_line.in_kg - src_line.out_consignment_kg)
        else:
            assert src_batch is not None
            j_o = o[src_batch.id, flow_into[key[0], src_batch.unit]]
            lp.row([*use, j_o], [1.0] * len(use) + [-1.0], upper=0.0)
    for sid in stores:
        ks = [j for key, j in k.items() if key[0] == sid]
        if ks:
            lp.row(ks, [1.0] * len(ks), upper=run.ledger(sid).physical_kg)

    # Purchased RFNBO: credentials into the stage's batches through suppliers' deliveries,
    # within each batch's metered delivery and each PoS's quantity left.
    pv: dict[tuple[str, str, str], int] = {}  # (PoS, flow, batch) -> var
    pv_lhv: dict[int, float] = dict(zp_lhv)
    by_pos: dict[str, list[int]] = defaultdict(list)
    for (pid, _, _), j in zp.items():
        by_pos[pid].append(j)
    for t in targets:
        for f, sup in supply_flows_into(cfg, t.unit):
            metered = float(frame.series[f.series or ""][t.start_index : t.end_index].sum())
            cols_f = [j for (_, fid, tb), j in zp.items() if fid == f.id and tb == t.id]
            for pp in usable_pos(run, sup):
                if metered <= 0 or left.get(pp.id, 0.0) <= DROP_KG:
                    continue
                if not part_ok(t, pp.ghg_gCO2e_per_MJ, limits[t.unit]):
                    continue  # its part would not comply, and no blend can save it (§12)
                j = lp.var()
                pv[pp.id, f.id, t.id] = j
                pv_lhv[j] = sup.lhv_MJ_per_kg
                by_pos[pp.id].append(j)
                cols_f.append(j)
                pos_ghg[pp.id] = pp.ghg_gCO2e_per_MJ
                pos_month[pp.id] = _month_of(pp.dispatch_date)
            if cols_f:
                lp.row(cols_f, [1.0] * len(cols_f), upper=metered)
    for pid, cols_p in by_pos.items():
        lp.row(cols_p, [1.0] * len(cols_p), upper=max(0.0, left[pid]))

    rfnbo_coef: dict[int, float] = {}
    emissions_coef: dict[int, float] = {}
    unit_of: dict[int, str] = {}
    for t in targets:
        # (energy per kg, GHG of the source, var) for storage feed and credentials alike
        cols = [
            (lhv[key[0]], ghg(key), j) for (key, tb), j in [*x.items(), *zs.items()] if tb == t.id
        ]
        cols += [
            (pv_lhv[j], pos_ghg[k[0]], j) for k, j in [*pv.items(), *zp.items()] if k[2] == t.id
        ]
        if not cols:
            continue
        lp.row([j for _, _, j in cols], [lv for lv, _, _ in cols], upper=t.feed_consumed_MJ)
        own_per_mj = t.own_emissions_g / t.feed_consumed_MJ
        out_per_mj = t.energy_MJ / t.feed_consumed_MJ
        for lv, g, j in cols:
            rfnbo_coef[j] = lv * t.product_kg / t.feed_consumed_MJ
            emissions_coef[j] = lv * (g + own_per_mj + t.adder_gCO2e_per_MJ * out_per_mj)
            unit_of[j] = t.id
    if not x and not pv and not pool_of:
        return NetworkPlan(ids, [], [], 0.0, "no feed")
    credit_vars = set(pv.values()) | set(zp.values())

    weight = {sid: run.storage_weights[sid].weight for sid in stores}
    carry_value = {kj: lhv[key[0]] * (weight[key[0]] or 0.0) for key, kj in k.items()}
    # 1. Most RFNBO output, or most value.
    if kept_share is None:
        cost1 = dict(rfnbo_coef)
    else:
        cost1 = {}
        shares = credit_share or {}
        for j, c in rfnbo_coef.items():
            tgt = run.batches[unit_of[j]]
            w_down = run.storage_weights[output_storage(cfg, tgt.unit).id].weight or 0.0
            kept = shares.get(tgt.id, 1.0) if j in credit_vars else kept_share.get(tgt.id, 1.0)
            cost1[j] = c * tgt.lhv_MJ_per_kg * w_down * kept
        cost1.update(carry_value)
    lp.set_cost(cost1)
    best = lp.solve(maximise=True)
    if not best.optimal:
        return NetworkPlan(ids, [], [], 0.0, best.status)
    if kept_share is None:
        floor = best.objective if min_rfnbo_kg is None else min(best.objective, min_rfnbo_kg)
        lp.row(list(rfnbo_coef), list(rfnbo_coef.values()), lower=floor * (1 - KEEP) - 1e-6)
        # 2. Most carried value within that output.
        lp.set_cost(carry_value)
        second = lp.solve(maximise=True)
        if second.optimal and carry_value:
            lp.row(
                list(carry_value),
                list(carry_value.values()),
                lower=second.objective * (1 - KEEP) - 1e-6,
            )
    else:
        lp.row(list(cost1), list(cost1.values()), lower=best.objective * (1 - KEEP) - 1e-6)

    # 3. Least GHG carried forward.
    weights = carried_weight or {}
    cost3: dict[int, float] = {kj: lhv[key[0]] * ghg(key) for key, kj in k.items()}
    for j, tb in unit_of.items():
        cost3[j] = cost3.get(j, 0.0) + weights.get(tb, 0.0) * emissions_coef[j]
    lp.set_cost(cost3)
    third = lp.solve()
    final = best
    if third.optimal:
        final = third
        lp.row(
            list(cost3),
            list(cost3.values()),
            upper=third.objective + abs(third.objective) * KEEP + 1e-3,
        )
        # 4. Least purchased emissions: the lowest-GHG credentials where they tie.
        if pv or zp:
            cost_p = {j: pv_lhv[j] * pos_ghg[k[0]] for k, j in [*pv.items(), *zp.items()]}
            lp.set_cost(cost_p)
            purchased = lp.solve()
            if purchased.optimal:
                final = purchased
                lp.row(
                    list(cost_p),
                    list(cost_p.values()),
                    upper=purchased.objective + abs(purchased.objective) * KEEP + 1e-3,
                )
        # 5. Same-month feed, and credentials nearest their dispatch month, where it makes no
        # difference.
        months = {t.id: t.month for t in targets}
        cost_m: dict[int, float] = {
            j: abs(_month_number(month(key)) - _month_number(months[tb]))
            for (key, tb), j in [*x.items(), *zs.items()]
        }
        cost_m.update(
            {
                j: abs(_month_number(pos_month[k[0]]) - _month_number(months[k[2]]))
                for k, j in [*pv.items(), *zp.items()]
            }
        )
        lp.set_cost(cost_m)
        fourth = lp.solve()
        if fourth.optimal:
            final = fourth
            if pool_of:
                # 6. Pool only where it is beneficial: the least pooled feed.
                lp.row(list(cost_m), list(cost_m.values()), upper=fourth.objective + 1e-6)
                lp.set_cost(dict.fromkeys(pool_of, 1.0))
                sixth = lp.solve()
                if sixth.optimal:
                    final = sixth

    feed = [
        FeedAllocation(source=key[1], target=tb, kg=float(final.x[j]), pool=pool_of.get(j))
        for (key, tb), j in [*x.items(), *zs.items()]
        if final.x[j] > DROP_KG
    ]
    feed.sort(key=lambda a: (a.target, a.source, a.pool or ""))
    outputs = [
        OutputAllocation(batch=bid, flow=fid, kg=float(final.x[j]))
        for (bid, fid), j in sorted(o.items())
        if final.x[j] > DROP_KG
    ]
    credits = [
        PoSAllocation(pos=pid, flow=fid, batch=tb, kg=float(final.x[j]), pool=pool_of.get(j))
        for (pid, fid, tb), j in sorted([*pv.items(), *zp.items()])
        if final.x[j] > DROP_KG
    ]
    rfnbo = sum(rfnbo_coef[j] * final.x[j] for j in rfnbo_coef if final.x[j] > DROP_KG)
    return NetworkPlan(ids, feed, outputs, rfnbo, final.status, credits)


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
    """Allocate compliant ledger batches to consignments, keeping the lowest GHG carried.

    One aggregated batch per consignment, production month and source (§12.1): its parts'
    energy-weighted GHG is held to the consignment's limit by one linear row, so compliant
    parts above the limit may ship with lower ones of the same month and source."""
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
        limits: dict[str, float] = {}
        for c in cons:
            target = (
                threshold
                if c.target_gCO2e_per_MJ is None
                else min(c.target_gCO2e_per_MJ, threshold)
            )
            limits[c.id] = target - adder[c.id] - MARGIN
        y: dict[tuple[str, str], int] = {}
        for line in lines:
            if line.compliant:
                for c in cons:
                    y[line.batch, c.id] = lp.var()
        # Only compliant RFNBO can be carried forward; non-compliant is written off.
        k = {line.batch: lp.var() for line in lines if line.compliant}
        for line in lines:
            cols = [
                *([k[line.batch]] if line.batch in k else []),
                *[y[line.batch, c.id] for c in cons if (line.batch, c.id) in y],
            ]
            if cols:
                lp.row(cols, [1.0] * len(cols), upper=line.in_kg - line.out_feed_kg)
        for c in cons:
            cols = [y[line.batch, c.id] for line in lines if (line.batch, c.id) in y]
            lp.row(cols, [1.0] * len(cols), lower=c.volume_kg, upper=c.volume_kg)
            # Each aggregated batch's average at or below the limit: Σ MJ · (g − limit) ≤ 0.
            groups: dict[tuple[str, str], list[LedgerLine]] = defaultdict(list)
            for line in lines:
                if (line.batch, c.id) in y:
                    groups[line.month, line.source].append(line)
            for group in groups.values():
                if any(x.ghg_gCO2e_per_MJ > limits[c.id] for x in group):
                    lp.row(
                        [y[x.batch, c.id] for x in group],
                        [x.lhv_MJ_per_kg * (x.ghg_gCO2e_per_MJ - limits[c.id]) for x in group],
                        upper=0.0,
                    )
        remaining = sum(line.in_kg - line.out_feed_kg for line in lines if line.compliant) - sum(
            c.volume_kg for c in cons
        )
        carried = max(0.0, min(ledger.physical_kg, remaining))
        if k:
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
            # One production month and one source per aggregated batch (§2.6, §12.1).
            chosen: dict[tuple[str, str], list[Part]] = defaultdict(list)
            for line in lines:
                j = y.get((line.batch, c.id))
                if j is not None and sol.x[j] > DROP_KG:
                    chosen[line.month, line.source].append(
                        Part(batch=line.batch, kg=float(sol.x[j]))
                    )
            for m, source in sorted(chosen):
                aggregated.append(
                    AggregatedBatchSpec(
                        id=aggregated_id(c.id, m, source),
                        consignment=c.id,
                        parts=chosen[m, source],
                    )
                )
    return ConsignmentPlan(aggregated, not shortfalls, shortfalls)


def aggregated_id(consignment: str, month: str, source: str) -> str:
    """``<consignment>-<month>``, plus the supplier for purchased RFNBO."""
    if source == OWN:
        return f"{consignment}-{month}"
    return f"{consignment}-{month}-" + re.sub(r"[^A-Za-z0-9]+", "-", source).strip("-")


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
    groups: dict[tuple[str, str], list[LedgerLine]] = defaultdict(list)
    for x in compliant:
        groups[x.month, x.source].append(x)
    for c in cons:
        # Averaged within each production month and source only (§12.1).
        best = sum(
            blend_capacity(
                [x.in_kg - x.out_feed_kg for x in g], [x.ghg_gCO2e_per_MJ for x in g], limits[c.id]
            )
            for g in groups.values()
        )
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
                Shortfall(c.id, f"only {best:,.1f} kg is at or below {limit:.4g} gCO2e/MJ", best)
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
        bad = [x.batch for x in led.lines if not x.compliant]
        for batch, kg in rule_write_offs(lines, led.required_write_off_kg, bad).items():
            out.append(WriteOff(storage=led.storage, batch=batch, kg=kg))
    return out


def _batch_lines(run: RunResult, b: BatchResult) -> list[LedgerLine]:
    """The ledger lines of every part of ``b``."""
    ids = {p.id for p in b.parts}
    return [x for led in run.ledgers for x in led.lines if x.batch in ids]


def carried_weights(run: RunResult, unit: str) -> dict[str, float]:
    """Share of each of ``unit``'s batches' RFNBO (over its parts) that is carried forward
    (for stage B)."""
    out: dict[str, float] = {}
    for b in run.batches.values():
        lines = _batch_lines(run, b) if b.unit == unit else []
        total = sum(x.in_kg for x in lines)
        if total > 0:
            out[b.id] = max(0.0, sum(x.carried_kg for x in lines)) / total
    return out


def kept_shares(run: RunResult, unit: str) -> dict[str, float]:
    """Share of each of ``unit``'s batches' RFNBO (over its parts) that is consigned or carried
    forward, not written off (for stage B's value objective)."""
    out: dict[str, float] = {}
    for b in run.batches.values():
        lines = _batch_lines(run, b) if b.unit == unit else []
        total = sum(x.in_kg for x in lines)
        if total > 0:
            kept = sum(x.out_consignment_kg + x.carried_kg for x in lines)
            out[b.id] = max(0.0, kept) / total
    return out


def credit_shares(run: RunResult, unit: str) -> dict[str, float]:
    """Share of one more kg of each of ``unit``'s batches' RFNBO that would be kept: none if
    the batch's storage already holds more RFNBO than physical inventory (the extra would be
    written off), else all of it. Values PoS credentials in stage B, so that credentials are
    not pushed into product that would be written off (§11.1 rule 5)."""
    full = {led.storage for led in run.ledgers if led.excess_kg > DROP_KG}
    return {
        b.id: 0.0 if {s.id for s in output_storages(run.config, b.unit)} & full else 1.0
        for b in run.batches.values()
        if b.unit == unit
    }


def storage_credits(cfg: PlantConfig, frame: Frame, run: RunResult) -> list[PoSAllocation]:
    """Credentials for suppliers' deliveries into storages: lowest GHG first (oldest dispatch
    on ties), over the whole quarter, up to the metered delivery and the storage's room for
    more RFNBO (measured inventory less its compliant closing ledger)."""
    left = pos_left(run, set())
    out: list[PoSAllocation] = []
    for f in cfg.flows:
        sup, store = cfg.block(f.source), cfg.block(f.target)
        if not isinstance(sup, RfnboSupply) or not isinstance(store, StorageUnit):
            continue
        led = run.ledger(store.id)
        room = led.physical_kg - sum(
            max(0.0, x.closing_before_kg) for x in led.lines if x.compliant
        )
        room = min(room, float(frame.series[f.series or ""].sum()))
        for p in sorted(usable_pos(run, sup), key=lambda p: (p.ghg_gCO2e_per_MJ, p.dispatch_date)):
            take = min(room, left[p.id])
            if take <= DROP_KG:
                continue
            out.append(
                PoSAllocation(pos=p.id, flow=f.id, kg=take, start=frame.index[0], end=frame.end)
            )
            left[p.id] -= take
            room -= take
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
    """Compliant RFNBO of the units' batches (every compliant part)."""
    return sum(p.kg for b in batches.values() if b.unit in units for p in b.parts if p.compliant)
