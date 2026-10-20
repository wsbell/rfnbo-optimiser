"""Batch RFNBO share and GHG intensity (build plan §2.3–2.5).

    GHG_batch = (Σ q_i · EF_i + Σ_feed MJ_b · GHG_b) / MJ_out + A_unit     [gCO₂e/MJ]

Electrolytic units (electricity adds to product LHV): RFNBO share = matched renewable kWh ÷
electricity kWh, matching within the batch's own intervals. Downstream units: RFNBO share =
RFNBO feed ÷ total feed (by energy); when short, the unit's own emissions are pro-rated to
the RFNBO portion by energy. Purchased RFNBO (§11.1 rule 4) is RFNBO feed like any other:
credentials allocated to a supplier's metered delivery, at the PoS's GHG intensity; the whole
metered delivery counts in the feed consumed.

No averaging across batches (build plan §12): a downstream batch's RFNBO output is a set of
parts, one per source GHG and origin. Part j, fed MJ_j at g_j, with C the feed consumed and E the
product energy, has share_j = MJ_j ÷ C and

    GHG_j = (MJ_j · g_j + share_j · own) / (share_j · E) + A = g_j · C / E + own / E + A

so a non-compliant source gives a non-compliant part: nothing is saved by blending.

Monthly averaging (build plan §12.1): feed lines carrying the same ``pool`` label make one part
at their energy-weighted GHG. The checks require them to share a source (the plant's own
production, or one supplier) and a production month, and each to be compliant.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

import numpy as np

from rfnbo.calc.tolerance import RANK_DECIMALS, meets_threshold, snap_unit_share
from rfnbo.config.schema import (
    ELECTRICITY,
    Flow,
    InputBlock,
    PlantConfig,
    ProcessingUnit,
    RfnboSupply,
    StorageUnit,
)
from rfnbo.data import calendar as cal
from rfnbo.data.ingest import FloatArray
from rfnbo.data.quality import Frame
from rfnbo.model.entities import BatchSpec, Decisions, ProofOfSustainability

OWN = "own"  # the source of RFNBO the plant produced; purchased RFNBO's source is its supplier


def dispatch_month(d: dt.date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


class CalculationError(ValueError):
    """The decision set or data cannot be calculated at all (as opposed to a failed check)."""


@dataclass(frozen=True)
class InputLine:
    name: str
    source: str  # input block id
    qty: float
    uom: str
    ef: float
    ef_uom: str
    ef_source: str
    emissions_g: float


@dataclass(frozen=True)
class FeedLine:
    source_batch: str
    kg: float
    energy_MJ: float
    ghg_gCO2e_per_MJ: float
    emissions_g: float
    compliant: bool
    origin: Literal["batch", "pos"] = "batch"  # a produced or opening batch part, or a PoS
    flow: str | None = None  # supply flow, for PoS credentials allocated to the batch directly
    purchased: bool = False  # a PoS, or a part made from purchased RFNBO
    source: str = OWN  # own production, or the supplier (§12.1)
    month: str = ""  # the input's production month (dispatch month if purchased)
    pool: str | None = None  # feed lines with one label are averaged into one part


@dataclass(frozen=True)
class BatchPart:
    """RFNBO of one batch from one source GHG and origin (build plan §12): the unit of the
    ledgers, feed, consignments and write-offs. An electrolytic batch has one part."""

    id: str
    batch: str
    unit: str
    month: str
    lhv_MJ_per_kg: float
    purchased: bool
    sources: tuple[str, ...]  # upstream part ids or PoS ids (none for an electrolytic batch)
    share: float  # of the batch's product
    kg: float
    feed_emissions_g: float
    own_emissions_g: float  # the batch's own emissions pro-rated to this part
    adder_emissions_g: float
    ghg_gCO2e_per_MJ: float
    compliant: bool
    source: str = OWN  # own production, or the supplier (§12.1)
    pool: str | None = None  # the pool label when several inputs were averaged

    @property
    def energy_MJ(self) -> float:
        return self.kg * self.lhv_MJ_per_kg


@dataclass(frozen=True)
class BatchResult:
    id: str
    unit: str
    unit_name: str
    kind: Literal["electrolytic", "downstream"]
    product: str
    start: dt.datetime  # UTC, inclusive
    end: dt.datetime  # UTC, exclusive
    start_index: int
    end_index: int
    month: str
    quarter: str
    product_kg: float
    lhv_MJ_per_kg: float
    energy_MJ: float
    electricity_kWh: float
    renewable_available_kWh: float
    renewable_matched_kWh: float
    nonrenewable_kWh: float
    inputs: tuple[InputLine, ...]
    own_emissions_g: float
    feed: tuple[FeedLine, ...]
    feed_consumed_kg: float
    feed_consumed_MJ: float
    rfnbo_feed_kg: float
    feed_emissions_g: float
    feed_ghg_gCO2e_per_MJ: float | None
    adder_gCO2e_per_MJ: float
    adder_emissions_g: float
    rfnbo_share: float
    prorated_own_emissions_g: float
    ghg_gCO2e_per_MJ: float | None
    threshold_gCO2e_per_MJ: float
    compliant: bool  # every part compliant
    rfnbo_kg: float
    rfnbo_MJ: float
    parts: tuple[BatchPart, ...] = ()

    @property
    def max_ghg_gCO2e_per_MJ(self) -> float | None:
        return max((p.ghg_gCO2e_per_MJ for p in self.parts), default=None)

    @property
    def intervals(self) -> int:
        return self.end_index - self.start_index

    @property
    def is_rfnbo(self) -> bool:
        return self.rfnbo_kg > 0


def unit_order(cfg: PlantConfig) -> list[ProcessingUnit]:
    """Processing units, upstream first (flows run one way, so this always exists)."""
    units = {u.id: u for u in cfg.blocks_of(ProcessingUnit)}
    upstream: dict[str, set[str]] = {u: set() for u in units}
    for u in units:
        for f in cfg.flows_into(u):
            if isinstance(cfg.block(f.source), StorageUnit):
                upstream[u] |= {g.source for g in cfg.flows_into(f.source) if g.source in units}
    order: list[ProcessingUnit] = []
    done: set[str] = set()
    while len(order) < len(units):
        ready = sorted(u for u in units if u not in done and upstream[u] <= done)
        if not ready:
            raise CalculationError("processing units form a cycle")
        for u in ready:
            order.append(units[u])
            done.add(u)
    return order


def output_flows(cfg: PlantConfig, unit_id: str) -> list[Flow]:
    """The unit's product flows, each into a storage (several when the output is split)."""
    flows = cfg.flows_out_of(unit_id)
    for f in flows:
        if not isinstance(cfg.block(f.target), StorageUnit):
            raise CalculationError(f"unit {unit_id} does not output to a storage unit")
    if not flows:
        raise CalculationError(f"unit {unit_id} has no product output")
    return flows


def output_storages(cfg: PlantConfig, unit_id: str) -> list[StorageUnit]:
    out = []
    for f in output_flows(cfg, unit_id):
        s = cfg.block(f.target)
        assert isinstance(s, StorageUnit)
        out.append(s)
    return out


def output_storage(cfg: PlantConfig, unit_id: str) -> StorageUnit:
    """The unit's one output storage (single-chain code; a split output raises)."""
    stores = output_storages(cfg, unit_id)
    if len(stores) != 1:
        raise CalculationError(f"unit {unit_id} splits its output between {len(stores)} storages")
    return stores[0]


def storage_lhv(cfg: PlantConfig, storage_id: str) -> float:
    """LHV of a storage's product, taken from the unit(s) or supplier(s) feeding it."""
    lhvs = {
        b.lhv_MJ_per_kg
        for f in cfg.flows_into(storage_id)
        if isinstance(b := cfg.block(f.source), (ProcessingUnit, RfnboSupply))
    }
    if len(lhvs) != 1:
        raise CalculationError(f"storage {storage_id}: cannot determine one product LHV")
    return lhvs.pop()


def supply_flow(cfg: PlantConfig, flow_id: str) -> tuple[Flow, RfnboSupply]:
    """A flow from an RFNBO supplier, and the supplier."""
    f = next((x for x in cfg.flows if x.id == flow_id), None)
    if f is None:
        raise CalculationError(f"unknown flow {flow_id!r}")
    src = cfg.block(f.source)
    if not isinstance(src, RfnboSupply):
        raise CalculationError(f"flow {flow_id} is not from an RFNBO supplier")
    return f, src


@dataclass(frozen=True)
class Credit:
    """A PoS as RFNBO feed: its GHG and the LHV of the product it was delivered as."""

    pos: ProofOfSustainability
    lhv_MJ_per_kg: float
    compliant: bool


def credits(cfg: PlantConfig, decisions: Decisions) -> dict[str, Credit]:
    """Each PoS that has an allocation, with the LHV of the supplier it was allocated to."""
    threshold = cfg.settings.rfnbo_threshold_gCO2e_per_MJ
    known = {p.id: p for p in decisions.pos}
    out: dict[str, Credit] = {}
    for a in decisions.pos_allocations:
        if a.pos not in known:
            raise CalculationError(f"PoS allocation to {a.flow}: unknown PoS {a.pos!r}")
        _, sup = supply_flow(cfg, a.flow)
        if a.pos not in out:
            p = known[a.pos]
            out[a.pos] = Credit(
                p, sup.lhv_MJ_per_kg, meets_threshold(p.ghg_gCO2e_per_MJ, threshold)
            )
    return out


def renewable_series(cfg: PlantConfig, frame: Frame, decisions: Decisions) -> dict[str, FloatArray]:
    """Renewable kWh per interval available to each unit with a renewable input."""
    given = {r.unit: r for r in decisions.renewable_allocations}
    out: dict[str, FloatArray] = {}
    for src in cfg.blocks_of(InputBlock):
        if not src.renewable:
            continue
        consumers = [f.target for f in cfg.flows_out_of(src.id)]
        pool = _series(frame, src.series)
        for unit in consumers:
            if unit in given:
                alloc = np.asarray(given[unit].kWh, dtype=np.float64)
                if alloc.shape != pool.shape:
                    raise CalculationError(
                        f"renewable allocation for {unit} has {alloc.size} values, "
                        f"expected {pool.size}"
                    )
                out[unit] = alloc
            elif len(consumers) == 1:
                out[unit] = pool
            else:
                raise CalculationError(
                    f"renewable input {src.id} feeds several units; the decision set must "
                    f"allocate it (missing for {unit})"
                )
    return out


def _series(frame: Frame, name: str | None) -> FloatArray:
    if name is None or name not in frame.series:
        raise CalculationError(f"series {name!r} is not in the data")
    values = frame.series[name]
    if np.isnan(values).any():
        raise CalculationError(f"series {name!r} has missing values; resolve quality flags first")
    return values


def compute_batches(cfg: PlantConfig, frame: Frame, decisions: Decisions) -> dict[str, BatchResult]:
    by_unit: dict[str, list[BatchSpec]] = defaultdict(list)
    units = {u.id for u in cfg.blocks_of(ProcessingUnit)}
    for spec in decisions.batches:
        if spec.unit not in units:
            raise CalculationError(f"batch {spec.id}: unknown processing unit {spec.unit!r}")
        by_unit[spec.unit].append(spec)

    feed_into: dict[str, list[tuple[str, float, str | None]]] = defaultdict(list)
    for a in decisions.feed:
        feed_into[a.target].append((a.source, a.kg, a.pool))
    bought = credits(cfg, decisions)
    # batch -> PoS, flow, kg, pool
    direct: dict[str, list[tuple[str, str, float, str | None]]] = defaultdict(list)
    for pa in decisions.pos_allocations:
        f, _ = supply_flow(cfg, pa.flow)
        target = cfg.block(f.target)
        if isinstance(target, ProcessingUnit):
            spec_unit = next((s.unit for s in decisions.batches if s.id == pa.batch), None)
            if pa.batch is None or spec_unit != target.id:
                raise CalculationError(
                    f"PoS allocation of {pa.pos} to {pa.flow} must name a batch of {target.id}"
                )
            direct[pa.batch].append((pa.pos, pa.flow, pa.kg, pa.pool))
        elif pa.batch is not None:
            raise CalculationError(
                f"PoS allocation of {pa.pos} to {pa.flow} (into a storage) takes a start and end"
            )

    renewable = renewable_series(cfg, frame, decisions)
    results: dict[str, BatchResult] = {}
    parts: dict[str, BatchPart] = {}
    for unit in unit_order(cfg):
        for spec in sorted(by_unit.get(unit.id, []), key=lambda s: s.start):
            b = _batch(
                cfg,
                frame,
                unit,
                spec,
                renewable.get(unit.id),
                feed_into[spec.id],
                parts,
                direct[spec.id],
                bought,
            )
            results[spec.id] = b
            for part in b.parts:
                if part.id in parts and part.id != b.id:
                    raise CalculationError(f"part id {part.id} clashes with another batch")
                parts[part.id] = part
    unknown = {a.target for a in decisions.feed} - results.keys()
    if unknown:
        raise CalculationError(f"feed allocated to unknown batch(es): {sorted(unknown)}")
    return results


def _batch(
    cfg: PlantConfig,
    frame: Frame,
    unit: ProcessingUnit,
    spec: BatchSpec,
    renewable: FloatArray | None,
    feed_alloc: list[tuple[str, float, str | None]],
    done: dict[str, BatchPart],
    direct: list[tuple[str, str, float, str | None]],
    bought: dict[str, Credit],
) -> BatchResult:
    try:
        s, e = frame.position(spec.start), frame.position(spec.end)
    except ValueError as exc:
        raise CalculationError(f"batch {spec.id}: {exc}") from exc
    if not 0 <= s < e <= len(frame):
        raise CalculationError(f"batch {spec.id}: empty or outside the period")

    def total(name: str | None) -> float:
        return float(_series(frame, name)[s:e].sum())

    tz = cfg.timezone
    on = cal.local_date(frame.index[s], tz)
    threshold = cfg.settings.rfnbo_threshold_gCO2e_per_MJ
    into = cfg.flows_into(unit.id)

    # Electricity: renewable matched within the batch, the rest at the grid factor.
    elec_sources = [cfg.block(f.source) for f in into if f.material == ELECTRICITY]
    re_src = next((b for b in elec_sources if isinstance(b, InputBlock) and b.renewable), None)
    grid_src = next(
        (b for b in elec_sources if isinstance(b, InputBlock) and not b.renewable), None
    )
    electricity = total(unit.electricity_series) if elec_sources else 0.0
    available = float(renewable[s:e].sum()) if renewable is not None and re_src else 0.0
    matched = min(available, electricity)
    unmatched = electricity - matched

    lines: list[InputLine] = []
    if re_src is not None:
        lines.append(_line("Renewable electricity (matched in batch)", re_src, matched, on))
    if grid_src is not None:
        lines.append(_line("Non-renewable electricity", grid_src, unmatched, on))
    elif unmatched > 0:
        raise CalculationError(
            f"batch {spec.id}: {unmatched} kWh unmatched but no non-renewable input"
        )
    for f in into:
        src = cfg.block(f.source)
        if isinstance(src, InputBlock) and f.material != ELECTRICITY:
            lines.append(_line(src.name, src, total(f.series), on))
    own = sum(line.emissions_g for line in lines)

    # Product feed from storage and from suppliers (all of the metered delivery, RFNBO or not).
    feed_flows = [f for f in into if isinstance(cfg.block(f.source), StorageUnit)]
    supplies = [(f, b) for f in into if isinstance(b := cfg.block(f.source), RfnboSupply)]
    if unit.electricity_adds_to_lhv and (feed_flows or supplies):
        raise CalculationError(f"unit {unit.id}: product feed into an electrolytic unit")
    feed_consumed_kg = sum(total(f.series) for f in feed_flows) + sum(
        total(f.series) for f, _ in supplies
    )
    feed_consumed_MJ = sum(total(f.series) * storage_lhv(cfg, f.source) for f in feed_flows) + sum(
        total(f.series) * b.lhv_MJ_per_kg for f, b in supplies
    )
    feed: list[FeedLine] = []

    def credit_line(pos_id: str, kg: float, flow: str | None, pool: str | None) -> FeedLine:
        c = bought[pos_id]
        mj = kg * c.lhv_MJ_per_kg
        g = c.pos.ghg_gCO2e_per_MJ
        return FeedLine(
            pos_id,
            kg,
            mj,
            g,
            mj * g,
            c.compliant,
            "pos",
            flow,
            purchased=True,
            source=c.pos.supplier,
            month=dispatch_month(c.pos.dispatch_date),
            pool=pool,
        )

    for source_id, kg, pool in feed_alloc:
        up = done.get(source_id)
        if up is None and source_id in bought:
            feed.append(credit_line(source_id, kg, None, pool))  # a PoS held in the feed storage
            continue
        if up is None:
            raise CalculationError(f"feed into {spec.id}: unknown or downstream batch {source_id}")
        mj = kg * up.lhv_MJ_per_kg
        g = up.ghg_gCO2e_per_MJ
        feed.append(
            FeedLine(
                source_id,
                kg,
                mj,
                g,
                mj * g,
                up.compliant,
                purchased=up.purchased,
                source=up.source,
                month=up.month,
                pool=pool,
            )
        )
    for pos_id, flow, kg, pool in direct:
        feed.append(credit_line(pos_id, kg, flow, pool))
    rfnbo_feed_kg = sum(x.kg for x in feed)
    rfnbo_feed_MJ = sum(x.energy_MJ for x in feed)
    feed_emissions = sum(x.emissions_g for x in feed)

    product_kg = total(unit.production_series)
    energy = product_kg * unit.lhv_MJ_per_kg
    adder = unit.fixed_adder_gCO2e_per_MJ

    ghg: float | None
    if unit.electricity_adds_to_lhv:
        kind: Literal["electrolytic", "downstream"] = "electrolytic"
        share = matched / electricity if electricity > 0 else 0.0
        prorated = own
        ghg = own / energy + adder if energy > 0 else None
    else:
        kind = "downstream"
        share = snap_unit_share(rfnbo_feed_MJ / feed_consumed_MJ) if feed_consumed_MJ > 0 else 0.0
        prorated = share * own
        ghg = (feed_emissions + prorated) / (share * energy) + adder if share * energy > 0 else None

    rfnbo_kg = share * product_kg if ghg is not None else 0.0
    month = cal.month_key(frame.index[s], tz)

    def part(pid: str, xs: list[FeedLine], sh: float, fem: float, own_g: float) -> BatchPart:
        mj = sh * energy
        a_g = adder * mj
        g = (fem + own_g + a_g) / mj
        purchased = any(x.purchased for x in xs)
        sources = sorted({x.source for x in xs}) or [OWN]
        # Production month (§12.1 decision 8): own RFNBO, this batch's month; purchased, the
        # dispatch month it keeps through processing.
        months = sorted({x.month for x in xs if x.purchased})
        return BatchPart(
            pid,
            spec.id,
            unit.id,
            months[0] if purchased and months else month,
            unit.lhv_MJ_per_kg,
            purchased,
            tuple(dict.fromkeys(x.source_batch for x in xs)),
            sh,
            sh * product_kg,
            fem,
            own_g,
            a_g,
            g,
            meets_threshold(g, threshold),
            source="+".join(sources),
            pool=next((x.pool for x in xs if x.pool), None),
        )

    parts: list[BatchPart] = []
    if kind == "electrolytic":
        if rfnbo_kg > 0 and ghg is not None:
            # Matched renewable sets the share; emissions are over all the batch's product.
            parts.append(part(spec.id, [], share, 0.0, own * share))
    elif rfnbo_kg > 0:
        # One part per pool (averaged, §12.1), and otherwise per source, production month and
        # source GHG (identical characteristics may be summarised).
        groups: dict[tuple[str, ...], list[FeedLine]] = defaultdict(list)
        for x in feed:
            if x.energy_MJ > 0:
                key: tuple[str, ...] = (
                    ("pool", x.pool)
                    if x.pool
                    else (
                        "line",
                        x.source,
                        x.month if x.purchased else "",
                        repr(round(x.ghg_gCO2e_per_MJ, RANK_DECIMALS)),
                    )
                )
                groups[key].append(x)
        shares = {
            # Ratio first: share × energy can underflow into subnormals for tiny feeds.
            k: share * (sum(x.energy_MJ for x in xs) / rfnbo_feed_MJ)
            for k, xs in groups.items()
        }

        def group_ghg(k: tuple[str, ...]) -> float:
            xs = groups[k]
            return round(sum(x.emissions_g for x in xs) / sum(x.energy_MJ for x in xs), 9)

        # A part too small to carry any energy in floating point (underflow) is no part.
        keys = sorted(
            (k for k, sh in shares.items() if sh * energy > 0),
            key=lambda k: (group_ghg(k), any(x.purchased for x in groups[k]), k),
        )
        for n, key in enumerate(keys, 1):
            xs = groups[key]
            sh = shares[key]
            pid = spec.id if len(keys) == 1 else f"{spec.id}.{n}"
            parts.append(part(pid, xs, sh, sum(x.emissions_g for x in xs), sh * own))
    if kind == "downstream":
        ghg = parts[0].ghg_gCO2e_per_MJ if len(parts) == 1 else None
    compliant = bool(parts) and all(p.compliant for p in parts)
    if kind == "electrolytic":
        compliant = ghg is not None and meets_threshold(ghg, threshold)
    return BatchResult(
        id=spec.id,
        unit=unit.id,
        unit_name=unit.name,
        kind=kind,
        product=unit.product,
        start=spec.start,
        end=spec.end,
        start_index=s,
        end_index=e,
        month=month,
        quarter=cal.quarter_key(frame.index[s], tz),
        product_kg=product_kg,
        lhv_MJ_per_kg=unit.lhv_MJ_per_kg,
        energy_MJ=energy,
        electricity_kWh=electricity,
        renewable_available_kWh=available,
        renewable_matched_kWh=matched,
        nonrenewable_kWh=unmatched,
        inputs=tuple(lines),
        own_emissions_g=own,
        feed=tuple(feed),
        feed_consumed_kg=feed_consumed_kg,
        feed_consumed_MJ=feed_consumed_MJ,
        rfnbo_feed_kg=rfnbo_feed_kg,
        feed_emissions_g=feed_emissions,
        feed_ghg_gCO2e_per_MJ=feed_emissions / rfnbo_feed_MJ if rfnbo_feed_MJ > 0 else None,
        adder_gCO2e_per_MJ=adder,
        adder_emissions_g=adder * share * energy if kind == "downstream" else adder * energy,
        rfnbo_share=share,
        prorated_own_emissions_g=prorated,
        ghg_gCO2e_per_MJ=ghg,
        threshold_gCO2e_per_MJ=threshold,
        compliant=compliant,
        rfnbo_kg=rfnbo_kg,
        rfnbo_MJ=rfnbo_kg * unit.lhv_MJ_per_kg,
        parts=tuple(parts),
    )


def _line(name: str, src: InputBlock, qty: float, on: dt.date) -> InputLine:
    ef = src.emission_factor_at(on)
    return InputLine(
        name=name,
        source=src.id,
        qty=qty,
        uom=src.quantity_unit,
        ef=ef.value,
        ef_uom=ef.unit,
        ef_source=ef.source,
        emissions_g=qty * ef.value,
    )
