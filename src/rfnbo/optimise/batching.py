"""Stage A: batch boundaries for units where electricity adds to product LHV (build plan §6).

For each unit and calendar month, an exact dynamic programme over the 15-minute intervals
chooses where batches start and end. Every candidate batch [s, e) inside the allowed window
(the month; one clock hour from the hourly-correlation switch) is scored from prefix sums:

    value = RFNBO_MJ · (1 − μ · (GHG − L))      floored at 0, then minus ε per batch

L is the GHG the downstream unit can absorb (blended feed GHG that keeps its own product at
the RFNBO threshold), so batches are shaped for what downstream can use. ε is the materiality
threshold (decided 2026-10-17): a batch is split off only if that gains at least
``materiality_MJ`` of final-product MJ (120 MJ, 1 kg H₂, by default), converted to the unit's
own product with its output storage's weight (``rfnbo.calc.weights``). μ is searched to
maximise the RFNBO feed that downstream can take while staying compliant, then the lowest GHG
attributed to RFNBO, then the fewest batches.

Matching follows the calculator exactly (renewable matched within the batch; emission factors
in force on the batch's first local day). The optimiser only proposes boundaries: every
result is re-run through the calculator before anything is reported.
"""

from __future__ import annotations

import functools
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import numpy.typing as npt

from rfnbo.calc.batch import output_storages, unit_order
from rfnbo.calc.weights import unit_output_weight
from rfnbo.config.schema import ELECTRICITY, InputBlock, PlantConfig, ProcessingUnit, StorageUnit
from rfnbo.data import calendar as cal
from rfnbo.data.ingest import FloatArray
from rfnbo.data.quality import Frame
from rfnbo.model.entities import BatchSpec, RenewableAllocation

IntArray = npt.NDArray[np.int64]
EPS_MJ = 0.01  # smallest per-batch penalty: far below any real gain, far above float noise
MATERIALITY_MJ = 120.0  # final-product MJ a split must gain (1 kg H₂)
MU_GRID = (0.0, *np.geomspace(1e-4, 1.0, 17).tolist())
REFINE_STEPS = 6


class BatchingError(ValueError):
    pass


@dataclass(frozen=True)
class UnitSeries:
    """Prefix sums (length n + 1) and per-start factors for one electrolytic unit."""

    unit: str
    lhv: float
    adder: float
    electricity: FloatArray
    renewable: FloatArray
    product: FloatArray
    other: tuple[tuple[FloatArray, FloatArray], ...]  # (prefix qty, EF by start interval)
    grid_ef: FloatArray  # by start interval

    def metrics(
        self, starts: IntArray, end: int | IntArray
    ) -> tuple[FloatArray, FloatArray, FloatArray]:
        """(RFNBO MJ, emissions attributed to RFNBO incl. adder [g], energy MJ) per batch."""
        e_kwh = self.electricity[end] - self.electricity[starts]
        r_kwh = self.renewable[end] - self.renewable[starts]
        matched = np.minimum(r_kwh, e_kwh)
        mj = (self.product[end] - self.product[starts]) * self.lhv
        em = (e_kwh - matched) * self.grid_ef[starts]
        for prefix, ef in self.other:
            em = em + (prefix[end] - prefix[starts]) * ef[starts]
        share = np.divide(matched, e_kwh, out=np.zeros_like(e_kwh), where=e_kwh > 0)
        return share * mj, share * (em + self.adder * mj), mj


def _prefix(values: FloatArray) -> FloatArray:
    out = np.empty(values.size + 1)
    out[0] = 0.0
    np.cumsum(values, out=out[1:])
    return out


def _ef_by_interval(src: InputBlock, frame: Frame) -> FloatArray:
    """EF in force on each interval's local day: what the calculator uses for a batch
    starting at that interval."""
    cache: dict[object, float] = {}
    out = np.empty(len(frame))
    for i, ts in enumerate(frame.index):
        day = cal.local_date(ts, frame.timezone)
        if day not in cache:
            cache[day] = src.emission_factor_at(day).value
        out[i] = cache[day]
    return out


def unit_series(
    cfg: PlantConfig, frame: Frame, unit: ProcessingUnit, renewable_kWh: FloatArray | None = None
) -> UnitSeries:
    """``renewable_kWh`` is the unit's share of a pool other units also draw on (phase 13);
    by default the unit has the whole pool."""
    if not unit.electricity_adds_to_lhv:
        raise BatchingError(f"{unit.id}: stage A batches only electrolytic units")
    into = [(f, cfg.block(f.source)) for f in cfg.flows_into(unit.id)]
    renewable = [b for _, b in into if isinstance(b, InputBlock) and b.renewable]
    grid = [
        b
        for _, b in into
        if isinstance(b, InputBlock) and b.material == ELECTRICITY and not b.renewable
    ]
    if len(renewable) != 1 or len(grid) != 1:
        raise BatchingError(f"{unit.id}: needs one renewable and one non-renewable input")
    series = frame.series
    pool = series[renewable[0].series or ""] if renewable_kWh is None else renewable_kWh
    other = tuple(
        (_prefix(series[f.series or ""]), _ef_by_interval(b, frame))
        for f, b in into
        if isinstance(b, InputBlock) and f.material != ELECTRICITY
    )
    return UnitSeries(
        unit=unit.id,
        lhv=unit.lhv_MJ_per_kg,
        adder=unit.fixed_adder_gCO2e_per_MJ,
        electricity=_prefix(series[unit.electricity_series or ""]),
        renewable=_prefix(pool),
        product=_prefix(series[unit.production_series or ""]),
        other=other,
        grid_ef=_ef_by_interval(grid[0], frame),
    )


@dataclass(frozen=True)
class Downstream:
    """What the unit's product feeds: the GHG limit L on blended feed, and the feed cap."""

    unit: str
    limit_gCO2e_per_MJ: float
    cap_MJ: float


def downstream(cfg: PlantConfig, frame: Frame, unit: ProcessingUnit) -> Downstream | None:
    """Blended-feed GHG at which the downstream unit's product sits exactly at the threshold.

    For downstream product GHG = (feed_MJ·g + own) / out_MJ + A ≤ T, the feed may average
    g ≤ (T − A − own/out_MJ) · out_MJ / feed_MJ. Pro-rating on short feed keeps the same
    limit. Totals are taken over the whole period (a stage A approximation; stage B enforces
    the limit for each downstream batch).
    """
    units = [
        u
        for store in output_storages(cfg, unit.id)
        for f in cfg.flows_out_of(store.id)
        if isinstance(u := cfg.block(f.target), ProcessingUnit)
    ]
    if not units:
        return None
    if len(units) > 1:
        # Several units take the product (a split output, or one storage feeding several):
        # the blend must suit the most demanding of them; their feed adds up (phase 13).
        parts = [downstream_of(cfg, frame, unit, u) for u in units]
        return Downstream(
            ",".join(p.unit for p in parts),
            min(p.limit_gCO2e_per_MJ for p in parts),
            sum(p.cap_MJ for p in parts),
        )
    return downstream_of(cfg, frame, unit, units[0])


def downstream_of(
    cfg: PlantConfig, frame: Frame, unit: ProcessingUnit, down: ProcessingUnit
) -> Downstream:
    feed_flows = [
        f for f in cfg.flows_into(down.id) if isinstance(cfg.block(f.source), StorageUnit)
    ]
    if len(feed_flows) != 1:
        raise BatchingError(f"{down.id}: stage A supports one product feed")
    s = frame.series
    feed_mj = float(s[feed_flows[0].series or ""].sum()) * unit.lhv_MJ_per_kg
    out_mj = float(s[down.production_series or ""].sum()) * down.lhv_MJ_per_kg
    on = cal.local_date(frame.index[0], frame.timezone)
    own = 0.0
    # A downstream unit on the renewable pool is assumed covered by the pool's surplus (the
    # pool split gives it renewable; stage B enforces each batch's actual limit).
    on_pool = any(
        isinstance(b, InputBlock) and b.renewable
        for b in (cfg.block(f.source) for f in cfg.flows_into(down.id))
    )
    for f in cfg.flows_into(down.id):
        b = cfg.block(f.source)
        if not isinstance(b, InputBlock):
            continue
        if f.material == ELECTRICITY:
            if not b.renewable and not on_pool:
                own += (
                    float(s[down.electricity_series or ""].sum()) * b.emission_factor_at(on).value
                )
        else:
            own += float(s[f.series or ""].sum()) * b.emission_factor_at(on).value
    if feed_mj <= 0 or out_mj <= 0:
        return Downstream(down.id, -math.inf, 0.0)
    threshold = cfg.settings.rfnbo_threshold_gCO2e_per_MJ
    limit = (threshold - down.fixed_adder_gCO2e_per_MJ - own / out_mj) * out_mj / feed_mj
    return Downstream(down.id, limit, feed_mj)


def windows(
    cfg: PlantConfig, frame: Frame, unit: ProcessingUnit
) -> tuple[list[tuple[int, int]], IntArray]:
    """Month slices [a, b) and, for each end e, the earliest start a batch ending at e may have."""
    tz, switch = frame.timezone, cfg.settings.hourly_correlation_from
    months = [cal.month_key(t, tz) for t in frame.index]
    slices: list[tuple[int, int]] = []
    a = 0
    for i in range(1, len(months) + 1):
        if i == len(months) or months[i] != months[a]:
            slices.append((a, i))
            a = i
    lo = np.zeros(len(frame) + 1, dtype=np.int64)
    hour_start = 0
    for i, ts in enumerate(frame.index):
        if i == 0 or cal.hour_key(ts, tz) != cal.hour_key(frame.index[i - 1], tz):
            hour_start = i
        month_start = next(s for s, b in slices if s <= i < b)
        hourly = unit.electricity_adds_to_lhv and cal.hourly_correlation_applies(ts, tz, switch)
        lo[i + 1] = max(month_start, hour_start) if hourly else month_start
    return slices, lo


ValueFn = Callable[[IntArray, int], FloatArray]


def segment(
    slices: Sequence[tuple[int, int]], lo: IntArray, value: ValueFn, eps: float = EPS_MJ
) -> list[tuple[int, int]]:
    """Exact DP: the segmentation of each slice maximising Σ (value − eps)."""
    out: list[tuple[int, int]] = []
    for a, b in slices:
        n = b - a
        best = np.full(n + 1, -np.inf)
        best[0] = 0.0
        prev = np.zeros(n + 1, dtype=np.int64)
        for k in range(1, n + 1):
            e = a + k
            starts = np.arange(lo[e], e, dtype=np.int64)
            scores = best[starts - a] + value(starts, e) - eps
            j = int(np.argmax(scores))  # first maximum: the longest batch on exact ties
            best[k] = scores[j]
            prev[k] = starts[j] - a
        cuts: list[tuple[int, int]] = []
        k = n
        while k > 0:
            cuts.append((a + int(prev[k]), a + k))
            k = int(prev[k])
        out.extend(reversed(cuts))
    return out


def value_fn(
    us: UnitSeries, mode: Literal["blend", "final"], mu: float, limit: float, threshold: float
) -> ValueFn:
    def blend(starts: IntArray, end: int) -> FloatArray:
        rf, attributed, _ = us.metrics(starts, end)
        return np.maximum(0.0, rf - mu * (attributed - limit * rf))

    def final(starts: IntArray, end: int) -> FloatArray:
        rf, attributed, _ = us.metrics(starts, end)
        ok = attributed <= (threshold + 1e-9) * rf
        return np.where(ok, rf, 0.0)

    return blend if mode == "blend" else final


@dataclass(frozen=True)
class Score:
    """Lexicographic: more feedable RFNBO, then less GHG attributed to RFNBO, then fewer batches."""

    feedable_MJ: float
    attributed_g: float
    batches: int

    def key(self) -> tuple[float, float, int]:
        # Rounded so that float noise never decides a comparison.
        return (round(self.feedable_MJ, 3), -round(self.attributed_g, 0), -self.batches)


def feedable_MJ(rf: FloatArray, attributed: FloatArray, limit: float, cap: float) -> float:
    """Most RFNBO MJ a downstream unit can take with blended feed GHG ≤ limit and feed ≤ cap.

    Taking batches in ascending GHG is optimal (moving feed from a higher- to a lower-GHG
    batch keeps the total and lowers the blend), with the last batch taken in part.
    """
    ghg = np.divide(attributed, rf, out=np.full_like(rf, np.inf), where=rf > 0)
    fed = cost = 0.0
    for i in np.argsort(ghg, kind="stable"):
        if rf[i] <= 0 or fed >= cap:
            break
        take = min(float(rf[i]), cap - fed)
        per_mj = float(ghg[i]) - limit
        if cost + take * per_mj <= 0:
            fed += take
            cost += take * per_mj
        else:  # per_mj > 0 here: take what keeps the blend at the limit, then stop
            fed += -cost / per_mj
            break
    return fed


@dataclass(frozen=True)
class UnitBatching:
    unit: str
    cuts: list[tuple[int, int]]
    mu: float | None
    limit_gCO2e_per_MJ: float | None
    score: Score
    search: list[tuple[float, Score]] = field(default_factory=list)


def score_of(
    us: UnitSeries, cuts: Sequence[tuple[int, int]], down: Downstream | None, threshold: float
) -> Score:
    s = np.array([c[0] for c in cuts], dtype=np.int64)
    e = np.array([c[1] for c in cuts], dtype=np.int64)
    rf, attributed, _ = us.metrics(s, e)
    if down is None:
        ok = attributed <= (threshold + 1e-9) * rf
        fed = float(rf[ok].sum())
    else:
        fed = feedable_MJ(rf, attributed, down.limit_gCO2e_per_MJ, down.cap_MJ)
    return Score(fed, float(attributed.sum()), len(cuts))


def batch_unit(
    cfg: PlantConfig,
    frame: Frame,
    unit: ProcessingUnit,
    mus: Sequence[float] = MU_GRID,
    eps: float = EPS_MJ,
    renewable_kWh: FloatArray | None = None,
) -> UnitBatching:
    us = unit_series(cfg, frame, unit, renewable_kWh)
    slices, lo = windows(cfg, frame, unit)
    threshold = cfg.settings.rfnbo_threshold_gCO2e_per_MJ
    down = downstream(cfg, frame, unit)
    if down is None:
        cuts = segment(slices, lo, value_fn(us, "final", 0.0, threshold, threshold), eps)
        return UnitBatching(unit.id, cuts, None, None, score_of(us, cuts, None, threshold))

    tried: dict[float, tuple[Score, list[tuple[int, int]]]] = {}

    def run(mu: float) -> Score:
        if mu not in tried:
            cuts = segment(
                slices, lo, value_fn(us, "blend", mu, down.limit_gCO2e_per_MJ, threshold), eps
            )
            tried[mu] = (score_of(us, cuts, down, threshold), cuts)
        return tried[mu][0]

    def better(a: float, b: float) -> bool:
        return run(a).key() > run(b).key() or (run(a).key() == run(b).key() and a < b)

    best = mus[0]
    for mu in mus:
        if better(mu, best):
            best = mu
    # Refine geometrically between the best grid point and its neighbours.
    grid = sorted(mus)
    i = grid.index(best)
    left = grid[i - 1] if i > 0 else best
    right = grid[i + 1] if i + 1 < len(grid) else best
    for _ in range(REFINE_STEPS):
        for cand in (
            math.sqrt(left * best) if left > 0 else best / 2,
            math.sqrt(best * right) if best > 0 else right / 2,
        ):
            if cand > 0 and better(cand, best):
                best = cand
        left, right = (
            math.sqrt(left * best) if left > 0 else best / 2,
            math.sqrt(best * right) if best > 0 else right / 2,
        )
    score, cuts = tried[best]
    search = sorted((mu, s) for mu, (s, _) in tried.items())
    return UnitBatching(unit.id, cuts, best, down.limit_gCO2e_per_MJ, score, search)


@dataclass(frozen=True)
class BatchingResult:
    units: list[UnitBatching]
    batches: list[BatchSpec]
    # Per-interval renewable split when several units share a pool (phase 13).
    renewable_allocations: list[RenewableAllocation] = field(default_factory=list)
    log: list[str] = field(default_factory=list)


def batch_ids(prefix: str, months: Sequence[str]) -> list[str]:
    counter: dict[str, int] = {}
    out = []
    for m in months:
        counter[m] = counter.get(m, 0) + 1
        out.append(f"{prefix}-{m}-{counter[m]:03d}")
    return out


def materiality_eps(
    cfg: PlantConfig, frame: Frame, unit: ProcessingUnit, materiality_MJ: float
) -> float:
    """Per-batch penalty in the unit's own product MJ for a final-product materiality."""
    w = unit_output_weight(cfg, frame, unit)
    return max(EPS_MJ, materiality_MJ / w if w else materiality_MJ)


def optimise_batching(
    cfg: PlantConfig,
    frame: Frame,
    mus: Sequence[float] = MU_GRID,
    materiality_MJ: float = MATERIALITY_MJ,
) -> BatchingResult:
    """Batches for every unit: stage A for electrolytic units, one per month for the rest."""
    from rfnbo.optimise import pool  # pool builds on this module

    end = frame.end
    tz = frame.timezone
    results: list[UnitBatching] = []
    specs: list[BatchSpec] = []
    allocations: list[RenewableAllocation] = []
    log: list[str] = []
    for unit in unit_order(cfg):
        if unit.electricity_adds_to_lhv:
            eps = materiality_eps(cfg, frame, unit, materiality_MJ)
            if pool.shares_pool(cfg, unit):
                split = pool.plan(
                    cfg,
                    frame,
                    unit,
                    functools.partial(batch_unit, cfg, frame, unit, mus, eps),
                )
                ub = split.batching
                allocations += split.allocations
                log += split.log
            else:
                ub = batch_unit(cfg, frame, unit, mus, eps)
            results.append(ub)
            cuts = ub.cuts
        else:
            cuts = windows(cfg, frame, unit)[0]
        months = [cal.month_key(frame.index[s], tz) for s, _ in cuts]
        ids = (
            batch_ids(unit.id, months)
            if unit.electricity_adds_to_lhv
            else [f"{unit.id}-{m}" for m in months]
        )
        for bid, (s, e) in zip(ids, cuts, strict=True):
            specs.append(
                BatchSpec(
                    id=bid,
                    unit=unit.id,
                    start=frame.index[s],
                    end=frame.index[e] if e < len(frame) else end,
                )
            )
    return BatchingResult(results, specs, allocations, log)
