"""Batch RFNBO share and GHG intensity (build plan §2.3–2.5).

    GHG_batch = (Σ q_i · EF_i + Σ_feed MJ_b · GHG_b) / MJ_out + A_unit     [gCO₂e/MJ]

Electrolytic units (electricity adds to product LHV): RFNBO share = matched renewable kWh ÷
electricity kWh, matching within the batch's own intervals. Downstream units: RFNBO share =
RFNBO feed ÷ total feed (by energy); when short, the unit's own emissions are pro-rated to
the RFNBO portion by energy.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

import numpy as np

from rfnbo.calc.tolerance import meets_threshold
from rfnbo.config.schema import (
    ELECTRICITY,
    InputBlock,
    PlantConfig,
    ProcessingUnit,
    StorageUnit,
)
from rfnbo.data import calendar as cal
from rfnbo.data.ingest import FloatArray
from rfnbo.data.quality import Frame
from rfnbo.model.entities import BatchSpec, Decisions


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
    rfnbo_feed_kg: float
    feed_emissions_g: float
    feed_ghg_gCO2e_per_MJ: float | None
    adder_gCO2e_per_MJ: float
    adder_emissions_g: float
    rfnbo_share: float
    prorated_own_emissions_g: float
    ghg_gCO2e_per_MJ: float | None
    threshold_gCO2e_per_MJ: float
    compliant: bool
    rfnbo_kg: float
    rfnbo_MJ: float

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


def output_storage(cfg: PlantConfig, unit_id: str) -> StorageUnit:
    (flow,) = cfg.flows_out_of(unit_id)
    target = cfg.block(flow.target)
    if not isinstance(target, StorageUnit):
        raise CalculationError(f"unit {unit_id} does not output to a storage unit")
    return target


def storage_lhv(cfg: PlantConfig, storage_id: str) -> float:
    """LHV of a storage's product, taken from the unit(s) producing it."""
    lhvs = {
        b.lhv_MJ_per_kg
        for f in cfg.flows_into(storage_id)
        if isinstance(b := cfg.block(f.source), ProcessingUnit)
    }
    if len(lhvs) != 1:
        raise CalculationError(f"storage {storage_id}: cannot determine one product LHV")
    return lhvs.pop()


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

    feed_into: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for a in decisions.feed:
        feed_into[a.target].append((a.source, a.kg))

    renewable = renewable_series(cfg, frame, decisions)
    results: dict[str, BatchResult] = {}
    for unit in unit_order(cfg):
        for spec in sorted(by_unit.get(unit.id, []), key=lambda s: s.start):
            results[spec.id] = _batch(
                cfg, frame, unit, spec, renewable.get(unit.id), feed_into[spec.id], results
            )
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
    feed_alloc: list[tuple[str, float]],
    done: dict[str, BatchResult],
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

    # Product feed from storage.
    feed_flows = [f for f in into if isinstance(cfg.block(f.source), StorageUnit)]
    if unit.electricity_adds_to_lhv and feed_flows:
        raise CalculationError(f"unit {unit.id}: product feed into an electrolytic unit")
    feed_consumed_kg = sum(total(f.series) for f in feed_flows)
    feed_consumed_MJ = sum(total(f.series) * storage_lhv(cfg, f.source) for f in feed_flows)
    feed: list[FeedLine] = []
    for source_id, kg in feed_alloc:
        src_batch = done.get(source_id)
        if src_batch is None:
            raise CalculationError(f"feed into {spec.id}: unknown or downstream batch {source_id}")
        if src_batch.ghg_gCO2e_per_MJ is None:
            raise CalculationError(f"feed into {spec.id}: batch {source_id} has no GHG")
        mj = kg * src_batch.lhv_MJ_per_kg
        feed.append(
            FeedLine(
                source_id,
                kg,
                mj,
                src_batch.ghg_gCO2e_per_MJ,
                mj * src_batch.ghg_gCO2e_per_MJ,
                src_batch.compliant,
            )
        )
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
        share = rfnbo_feed_MJ / feed_consumed_MJ if feed_consumed_MJ > 0 else 0.0
        prorated = share * own
        ghg = (feed_emissions + prorated) / (share * energy) + adder if share * energy > 0 else None

    rfnbo_kg = share * product_kg if ghg is not None else 0.0
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
        month=cal.month_key(frame.index[s], tz),
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
        rfnbo_feed_kg=rfnbo_feed_kg,
        feed_emissions_g=feed_emissions,
        feed_ghg_gCO2e_per_MJ=feed_emissions / rfnbo_feed_MJ if rfnbo_feed_MJ > 0 else None,
        adder_gCO2e_per_MJ=adder,
        adder_emissions_g=adder * share * energy if kind == "downstream" else adder * energy,
        rfnbo_share=share,
        prorated_own_emissions_g=prorated,
        ghg_gCO2e_per_MJ=ghg,
        threshold_gCO2e_per_MJ=threshold,
        compliant=ghg is not None and meets_threshold(ghg, threshold),
        rfnbo_kg=rfnbo_kg,
        rfnbo_MJ=rfnbo_kg * unit.lhv_MJ_per_kg,
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
