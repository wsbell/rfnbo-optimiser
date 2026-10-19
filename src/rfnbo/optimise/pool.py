"""Stage A with a shared renewable pool (build plan §10.4, phase 13).

When the electrolyser and conditioning units (liquefier, compressor) draw on one renewable
pool, renewable kWh is split per interval. Matched renewable raises the electrolyser's RFNBO
share; for a conditioning unit it only removes grid emissions (§10.2 rules 2 and 6). Every
unit matches within its own batch; conditioning units keep one batch per month.

The split alternates stage A with an allocation until stable:

1. Batch the electrolyser on the pool it has (stage A's dynamic programme).
2. Give each electrolyser batch the renewable it can match, from its own intervals; the rest
   of each interval is surplus.
3. Share the surplus between the conditioning units, up to each one's electricity in its
   month (averaging within the month-long batch).
4. A conditioning batch whose product would miss the RFNBO threshold with that cover takes the
   shortfall from the electrolyser (from the end of the month backwards), but only when its
   compliant product is worth more, in final-product MJ, than the RFNBO H₂ the electrolyser
   loses (objective 2, §2.8). Then back to 1 with the electrolyser's reduced pool.

The pool is never over-allocated. With no other unit on the pool the electrolyser keeps the
whole pool and stage A is unchanged. The split is a proposal: the calculator re-runs it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from rfnbo.calc.batch import output_flows, storage_lhv
from rfnbo.calc.weights import unit_output_weight
from rfnbo.config.schema import ELECTRICITY, InputBlock, PlantConfig, ProcessingUnit, StorageUnit
from rfnbo.data import calendar as cal
from rfnbo.data.ingest import FloatArray
from rfnbo.data.quality import Frame
from rfnbo.model.entities import RenewableAllocation
from rfnbo.optimise.batching import BatchingError, UnitBatching, windows

ROUNDS = 6
MARGIN = 1e-6  # gCO2e/MJ kept below the threshold so the calculator's check always passes
TOL_KWH = 1e-6


@dataclass(frozen=True)
class PoolSplit:
    batching: UnitBatching
    allocations: list[RenewableAllocation]
    reserved_kWh: dict[str, float]  # kWh each conditioning unit took from the electrolyser
    log: list[str] = field(default_factory=list)


def renewable_source(cfg: PlantConfig, unit: ProcessingUnit) -> InputBlock | None:
    for f in cfg.flows_into(unit.id):
        b = cfg.block(f.source)
        if isinstance(b, InputBlock) and b.renewable:
            return b
    return None


def shares_pool(cfg: PlantConfig, unit: ProcessingUnit) -> bool:
    src = renewable_source(cfg, unit)
    return src is not None and len(cfg.flows_out_of(src.id)) > 1


def _series(frame: Frame, name: str | None) -> FloatArray:
    return np.asarray(frame.series[name or ""], dtype=np.float64)


def electrolyser_allocation(
    pool: FloatArray, electricity: FloatArray, cuts: list[tuple[int, int]]
) -> tuple[FloatArray, FloatArray]:
    """What each electrolyser batch can match, taken from its own intervals: first each
    interval's own use, then (to cover dark intervals by averaging) spare renewable from the
    batch's other intervals in time order. Returns (allocation, surplus) per interval."""
    alloc = np.minimum(pool, electricity)
    for s, e in cuts:
        need = min(float(pool[s:e].sum()), float(electricity[s:e].sum())) - float(alloc[s:e].sum())
        for i in range(s, e):
            if need <= TOL_KWH:
                break
            extra = min(need, float(pool[i] - alloc[i]))
            alloc[i] += extra
            need -= extra
    return alloc, pool - alloc


@dataclass
class _Conditioning:
    unit: ProcessingUnit
    electricity: FloatArray
    months: list[tuple[int, int]]


def _grid_ef(cfg: PlantConfig, frame: Frame, unit: ProcessingUnit, s: int) -> float:
    for f in cfg.flows_into(unit.id):
        b = cfg.block(f.source)
        if isinstance(b, InputBlock) and b.material == ELECTRICITY and not b.renewable:
            return b.emission_factor_at(cal.local_date(frame.index[s], frame.timezone)).value
    raise BatchingError(f"{unit.id}: no non-renewable electricity input")


def _other_emissions(cfg: PlantConfig, frame: Frame, unit: ProcessingUnit, s: int, e: int) -> float:
    on = cal.local_date(frame.index[s], frame.timezone)
    total = 0.0
    for f in cfg.flows_into(unit.id):
        b = cfg.block(f.source)
        if isinstance(b, InputBlock) and f.material != ELECTRICITY:
            total += float(_series(frame, f.series)[s:e].sum()) * b.emission_factor_at(on).value
    return total


def plan(
    cfg: PlantConfig,
    frame: Frame,
    electrolyser: ProcessingUnit,
    batch: Callable[[FloatArray], UnitBatching],
    repair: bool = True,
) -> PoolSplit:
    """Split the electrolyser's renewable pool with the conditioning units sharing it.

    ``repair=False`` (the baseline) skips step 4: the electrolyser first, the surplus to the
    others, whatever their compliance."""
    src = renewable_source(cfg, electrolyser)
    assert src is not None
    users = [cfg.block(f.target) for f in cfg.flows_out_of(src.id)]
    others = [u for u in users if isinstance(u, ProcessingUnit) and u.id != electrolyser.id]
    if any(u.electricity_adds_to_lhv for u in others):
        raise BatchingError(f"{src.id}: stage A splits a pool with one electrolytic unit only")
    pool = _series(frame, src.series)
    e_elec = _series(frame, electrolyser.electricity_series)
    e_prod = _series(frame, electrolyser.production_series)
    w_e = unit_output_weight(cfg, frame, electrolyser) or 0.0
    cond = [
        _Conditioning(u, _series(frame, u.electricity_series), windows(cfg, frame, u)[0])
        for u in sorted(others, key=lambda u: u.id)
    ]
    threshold = cfg.settings.rfnbo_threshold_gCO2e_per_MJ
    reserved = {c.unit.id: np.zeros(len(frame)) for c in cond}  # taken from the electrolyser
    log: list[str] = []

    def taken() -> FloatArray:
        return sum((r for r in reserved.values()), np.zeros(len(frame)))

    for rnd in range(ROUNDS):
        e_pool = pool - taken()
        ub = batch(e_pool)
        e_alloc, surplus = electrolyser_allocation(e_pool, e_elec, ub.cuts)
        alloc = {c.unit.id: reserved[c.unit.id].copy() for c in cond}
        # 3. Surplus to the conditioning units, up to each one's monthly electricity.
        for c in cond:
            for s, e in c.months:
                need = float(c.electricity[s:e].sum()) - float(alloc[c.unit.id][s:e].sum())
                for i in range(s, e):
                    if need <= TOL_KWH:
                        break
                    give = min(need, float(surplus[i]))
                    if give > 0:
                        alloc[c.unit.id][i] += give
                        surplus[i] -= give
                        need -= give
        # 4. Shortfalls that keep a conditioning batch from complying.
        changed = False
        e_ghg = _batch_ghg(cfg, frame, electrolyser, ub.cuts, e_alloc)
        for c in cond if repair else []:
            for s, e in c.months:
                short = _compliance_shortfall(
                    cfg, frame, c, s, e, float(alloc[c.unit.id][s:e].sum()), e_ghg, threshold
                )
                if short is None or short <= TOL_KWH:
                    continue
                gain, loss = _worth(
                    cfg, frame, c.unit, s, e, short, e_elec, e_prod, w_e, electrolyser
                )
                if gain <= loss:
                    log.append(
                        f"stage A pool: {c.unit.id} {cal.month_key(frame.index[s], frame.timezone)}"
                        f" left non-compliant ({short:,.1f} kWh short; worth {gain:,.0f} MJ < "
                        f"{loss:,.0f} MJ of electrolyser RFNBO)"
                    )
                    continue
                left = short
                free = pool - taken()
                for i in range(e - 1, s - 1, -1):  # from the end of the month backwards
                    take = min(left, float(free[i]))
                    if take > TOL_KWH:
                        reserved[c.unit.id][i] += take
                        left -= take
                    if left <= TOL_KWH:
                        break
                changed = True
                log.append(
                    f"stage A pool round {rnd + 1}: {c.unit.id} "
                    f"{cal.month_key(frame.index[s], frame.timezone)} takes {short - left:,.1f} "
                    "kWh from the electrolyser to comply"
                )
        if not changed:
            allocations = [RenewableAllocation(unit=electrolyser.id, kWh=e_alloc.tolist())]
            allocations += [
                RenewableAllocation(unit=c.unit.id, kWh=alloc[c.unit.id].tolist()) for c in cond
            ]
            over = (sum(np.asarray(a.kWh) for a in allocations) - pool).max()
            if over > 1e-6:
                raise BatchingError(f"pool split over-allocates by {over} kWh")  # pragma: no cover
            return PoolSplit(
                ub,
                allocations,
                {k: float(v.sum()) for k, v in reserved.items()},
                log,
            )
    raise BatchingError(f"{src.id}: pool split did not settle in {ROUNDS} rounds")


def _batch_ghg(
    cfg: PlantConfig,
    frame: Frame,
    unit: ProcessingUnit,
    cuts: list[tuple[int, int]],
    alloc: FloatArray,
) -> list[tuple[int, int, float, float]]:
    """(start, end, GHG, RFNBO kg) of each electrolyser batch, as the calculator computes."""
    elec = _series(frame, unit.electricity_series)
    prod = _series(frame, unit.production_series)
    out = []
    for s, e in cuts:
        el = float(elec[s:e].sum())
        matched = min(float(alloc[s:e].sum()), el)
        mj = float(prod[s:e].sum()) * unit.lhv_MJ_per_kg
        if mj <= 0 or el <= 0:
            continue
        own = (el - matched) * _grid_ef(cfg, frame, unit, s) + _other_emissions(
            cfg, frame, unit, s, e
        )
        out.append(
            (s, e, own / mj + unit.fixed_adder_gCO2e_per_MJ, matched / el * mj / unit.lhv_MJ_per_kg)
        )
    return out


def _compliance_shortfall(
    cfg: PlantConfig,
    frame: Frame,
    c: _Conditioning,
    s: int,
    e: int,
    allocated: float,
    e_batches: list[tuple[int, int, float, float]],
    threshold: float,
) -> float | None:
    """Renewable kWh the conditioning batch [s, e) still needs to reach the threshold, fed from
    the lowest-GHG electrolyser batches of the same month; None if it cannot comply anyway."""
    u = c.unit
    elec = float(c.electricity[s:e].sum())
    out_kg = float(_series(frame, u.production_series)[s:e].sum())
    feeds = [f for f in cfg.flows_into(u.id) if isinstance(cfg.block(f.source), StorageUnit)]
    if elec <= 0 or out_kg <= 0 or len(feeds) != 1:
        return None
    feed_kg = float(_series(frame, feeds[0].series)[s:e].sum())
    lhv_in = storage_lhv(cfg, feeds[0].source)
    # Share of the electrolyser's output that reaches this unit's feed storage.
    store = feeds[0].source
    flows = output_flows(cfg, cfg.flows_into(store)[0].source)
    branch = next((f for f in flows if f.target == store), None)
    if branch is None:
        return None
    total_out = (
        sum(float(_series(frame, f.series)[s:e].sum()) for f in flows) if len(flows) > 1 else None
    )
    frac = 1.0 if total_out is None else float(_series(frame, branch.series)[s:e].sum()) / total_out
    month = [b for b in e_batches if s <= b[0] < e]
    need, em, got = feed_kg, 0.0, 0.0
    for _, _, ghg, rf in sorted(month, key=lambda b: b[2]):
        take = min(need - got, rf * frac)
        if take <= 0:
            break
        em += take * ghg
        got += take
    if got <= 0:
        return None
    f = em / got
    out_mj = out_kg * u.lhv_MJ_per_kg
    room = threshold - MARGIN - u.fixed_adder_gCO2e_per_MJ - f * feed_kg * lhv_in / out_mj
    room -= _other_emissions(cfg, frame, u, s, e) / out_mj
    if room < 0:
        return None
    grid_allowed = room * out_mj / _grid_ef(cfg, frame, u, s)
    required = max(0.0, elec - grid_allowed)
    return max(0.0, required - min(allocated, elec))


def _worth(
    cfg: PlantConfig,
    frame: Frame,
    unit: ProcessingUnit,
    s: int,
    e: int,
    short_kWh: float,
    e_elec: FloatArray,
    e_prod: FloatArray,
    w_e: float,
    electrolyser: ProcessingUnit,
) -> tuple[float, float]:
    """(final-product MJ gained by the conditioning batch complying, final-product MJ of
    electrolyser RFNBO lost by giving up ``short_kWh``)."""
    out_mj = float(_series(frame, unit.production_series)[s:e].sum()) * unit.lhv_MJ_per_kg
    w_out = unit_output_weight(cfg, frame, unit) or 0.0
    el = float(e_elec[s:e].sum())
    kg_per_kwh = float(e_prod[s:e].sum()) / el if el > 0 else 0.0
    return out_mj * w_out, short_kWh * kg_per_kwh * electrolyser.lhv_MJ_per_kg * w_e
