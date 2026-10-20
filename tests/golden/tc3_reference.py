"""Test case 3: independent reference calculation for a multi-step plant (build plan §10.4).

Plant: electrolyser → H₂ buffer storage → liquefier → LH₂ storage → Customer A, and in parallel
electrolyser → (direct, pass-through) → compressor → CH₂ storage → Customer B. One renewable pool
is shared by all three units. The electrolyser's output is split between two metered flows.

This module is plain Python: it does not import the app. It builds the synthetic quarter of
15-minute data, runs an explicit decision set over it with the rules of build plan §2 and
§10.2, and returns every number the workbook's formulas compute. The workbook's Checks sheet
compares the two; ``tests/golden/test_tc3_workbook.py`` re-runs this module on the data read
back from the workbook.

Rules applied (§10.2 and §10.3 decisions):
- Each unit matches renewable within its own batch: matched = min(Σ allocated, Σ electricity).
- Electrolyser: RFNBO share = matched ÷ electricity; GHG = (water + grid) ÷ all H₂ energy + adder.
  A batch above the threshold is still RFNBO (§2.5).
- Split output (rule 4): each output flow receives at most its metered quantity of the batch's
  RFNBO; the parts add up to the batch's RFNBO.
- Conditioning units (rule 2): share = RFNBO feed ÷ feed consumed (same LHV, so by kg). Batches
  are never averaged (build plan §12, decided 2026-10-20): the RFNBO output is split into parts,
  one per source GHG, each with GHG = (its feed emissions + its share × own emissions) ÷ its
  product energy + adder. Losses (rule 3) make the RFNBO product energy smaller than the RFNBO
  feed energy, so they raise GHG. A part from a non-compliant source stays non-compliant.
- Consignments: no average either. Each part, plus the distribution adder, must meet the target
  and the threshold; the shipped GHG reported is the highest part's.
- Storage ledgers close at quarter end by the §2.7 rule. Non-compliant RFNBO cannot be carried
  into the next quarter, so all of it is written off (decided 2026-10-17); beyond that, the
  excess over measured inventory is written off, highest GHG first, newest month first on ties
  (GHG rounded to 9 dp). A pass-through storage (rule 5) has physical inventory zero.
- Objectives (decided 2026-10-17): consignments met; then compliant RFNBO carried forward, in
  MJ of final product: each storage's carried MJ times its weight (1 for a storage that feeds a
  distribution block; otherwise the downstream unit's energy yield over the quarter times the
  weight of the storage it outputs to); then carried emissions; then fewest batches.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Amsterdam")
STEP = dt.timedelta(minutes=15)
LHV_MJ_PER_KG = 120.0  # H₂, LH₂ and CH₂
EF_GRID = 400.0  # gCO2e/kWh
EF_WATER = 2.0  # gCO2e/kg
ADDER = {"electrolyser": 1.0, "liquefier": 0.5, "compressor": 0.5}
THRESHOLD = 28.2
UNITS = ("electrolyser", "liquefier", "compressor")
DEMAND_KWH = {"electrolyser": 2500.0, "liquefier": 300.0, "compressor": 40.0}
BASE_SPLIT_KWH = {"electrolyser": 2550.0, "liquefier": 400.0, "compressor": 50.0}
OPENING_KG = {
    "h2_buffer": 2000.0,
    "pt_compressor": 0.0,
    "lh2_storage": 1000.0,
    "ch2_storage": 500.0,
}
CONSIGNMENTS = {
    "CA": {
        "customer": "Customer A",
        "storage": "lh2_storage",
        "volume_kg": 150000.0,
        "ship_day": 15,
        "target": 5.0,
        "adder": 2.0,
    },
    "CB": {
        "customer": "Customer B",
        "storage": "ch2_storage",
        "volume_kg": 120000.0,
        "ship_day": 20,
        "target": 4.0,
        "adder": 1.0,
    },
}
SPOT_KG, SPOT_DAY = 60000.0, 29
TRIP_DAY = 8  # liquefier off for the whole local day, 8 December
V2_DEC_RENEWABLE_KWH = 2500.0
V3_CA_TARGET = 3.5

# Upstream unit, storage and metered flow for each conditioning unit.
FEEDS = {"liquefier": ("h2_buffer", "to_buffer"), "compressor": ("pt_compressor", "to_compressor")}
PRODUCT_STORAGE = {"liquefier": "lh2_storage", "compressor": "ch2_storage"}


@dataclass
class Data:
    utc: list[dt.datetime]
    local: list[dt.datetime]
    month: list[str]
    s: dict[str, list[float]]

    def __len__(self) -> int:
        return len(self.utc)


def _tenths(x: float) -> int:
    return round(x * 10)


def build_data(dec_renewable_kWh: float | None = None, spot: bool = True) -> Data:
    """The synthetic Q4 2026 quarter. Variant V2 sets December renewable and drops the spot sale."""
    start = dt.datetime(2026, 10, 1, tzinfo=TZ).astimezone(dt.UTC)
    end = dt.datetime(2027, 1, 1, tzinfo=TZ).astimezone(dt.UTC)
    utc, local = [], []
    t = start
    while t < end:
        utc.append(t)
        local.append(t.astimezone(TZ))
        t += STEP
    names = [
        "renewable_kWh",
        "electrolyser_kWh",
        "water_kg",
        "h2_kg",
        "to_buffer_kg",
        "to_compressor_kg",
        "liquefier_kWh",
        "liquefier_h2_kg",
        "lh2_kg",
        "compressor_kWh",
        "compressor_h2_kg",
        "ch2_kg",
        "buffer_inv_kg",
        "ca_kg",
        "spot_kg",
        "lh2_inv_kg",
        "cb_kg",
        "ch2_inv_kg",
    ]
    s: dict[str, list[float]] = {n: [] for n in names}
    inv = {k: _tenths(v) for k, v in OPENING_KG.items()}  # exact, in tenths of kg
    for lt in local:
        ren = 3000.0
        if lt.month == 11 and lt.day == 18 and 6 <= lt.hour < 18:
            ren = 0.0
        if lt.month == 12 and dec_renewable_kWh is not None:
            ren = dec_renewable_kWh
        trip = lt.month == 12 and lt.day == TRIP_DAY
        lh2_out = 0.0 if trip else (29.1 if lt.month == 11 else 29.4)
        row = {
            "renewable_kWh": ren,
            "electrolyser_kWh": 2500.0,
            "water_kg": 500.0,
            "h2_kg": 50.0,
            "to_buffer_kg": 30.0,
            "to_compressor_kg": 20.0,
            "liquefier_kWh": 0.0 if trip else 300.0,
            "liquefier_h2_kg": 0.0 if trip else 30.0,
            "lh2_kg": lh2_out,
            "compressor_kWh": 40.0,
            "compressor_h2_kg": 20.0,
            "ch2_kg": 20.0,
            "ca_kg": 150000.0 / 96 if (lt.month, lt.day) == (12, 15) else 0.0,
            "spot_kg": SPOT_KG / 96 if spot and (lt.month, lt.day) == (12, SPOT_DAY) else 0.0,
            "cb_kg": 120000.0 / 96 if (lt.month, lt.day) == (12, 20) else 0.0,
        }
        inv["h2_buffer"] += _tenths(row["to_buffer_kg"]) - _tenths(row["liquefier_h2_kg"])
        inv["lh2_storage"] += _tenths(lh2_out) - _tenths(row["ca_kg"]) - _tenths(row["spot_kg"])
        inv["ch2_storage"] += _tenths(row["ch2_kg"]) - _tenths(row["cb_kg"])
        row["buffer_inv_kg"] = inv["h2_buffer"] / 10
        row["lh2_inv_kg"] = inv["lh2_storage"] / 10
        row["ch2_inv_kg"] = inv["ch2_storage"] / 10
        for n in names:
            s[n].append(row[n])
    return Data(utc, local, [f"{x.year:04d}-{x.month:02d}" for x in local], s)


# ---------------------------------------------------------------- decisions


@dataclass
class Decisions:
    """Batches as [i0, i1) interval ranges; everything else keyed by batch id."""

    batches: dict[str, list[tuple[str, int, int]]]
    renewable: dict[str, list[float]]
    outputs: dict[tuple[str, str], float]  # (electrolyser batch, flow) -> RFNBO kg
    feed: dict[tuple[str, str], float]  # (upstream batch, downstream batch) -> RFNBO kg
    parts: dict[str, dict[str, float]]  # consignment -> batch -> kg
    targets: dict[str, float] = field(default_factory=dict)


def _monthly(data: Data, prefix: str) -> list[tuple[str, int, int]]:
    out: list[tuple[str, int, int]] = []
    for i, m in enumerate(data.month):
        if out and out[-1][0] == f"{prefix}-{m}":
            out[-1] = (out[-1][0], out[-1][1], i + 1)
        else:
            out.append((f"{prefix}-{m}", i, i + 1))
    return out


def _hourly(data: Data, prefix: str) -> list[tuple[str, int, int]]:
    """One batch per clock hour (Amsterdam hours start on UTC hours)."""
    out: list[tuple[str, int, int]] = []
    count: dict[str, int] = {}
    for i, t in enumerate(data.utc):
        if out and t.minute != 0:
            out[-1] = (out[-1][0], out[-1][1], i + 1)
            continue
        m = data.month[i]
        count[m] = count.get(m, 0) + 1
        out.append((f"{prefix}-{m}-{count[m]:03d}", i, i + 1))
    return out


def base_allocation(data: Data) -> dict[str, list[float]]:
    """2,550 / 400 / 50 kWh whenever 3,000 kWh is available; nothing in the dark spell."""
    alloc: dict[str, list[float]] = {u: [] for u in UNITS}
    for r in data.s["renewable_kWh"]:
        for u in UNITS:
            alloc[u].append(BASE_SPLIT_KWH[u] if r == 3000.0 else 0.0)
    return alloc


def _range_sum(xs: list[float], i0: int, i1: int) -> float:
    return sum(xs[i0:i1])


def route(data: Data, d: Decisions, buffer_carry_kg: float) -> None:
    """Default routing used by every case: each electrolyser batch sends its RFNBO to the
    compressor branch first (no loss) and the rest to the buffer, both within metering; each
    conditioning batch draws from same-month electrolyser batches in time order. The liquefier's
    last batch leaves ``buffer_carry_kg`` of RFNBO H₂ in the buffer."""
    e_batches = d.batches["electrolyser"]
    rf = {}
    for bid, i0, i1 in e_batches:
        alloc = _range_sum(d.renewable["electrolyser"], i0, i1)
        elec = _range_sum(data.s["electrolyser_kWh"], i0, i1)
        share = min(alloc, elec) / elec
        rf[bid] = share * _range_sum(data.s["h2_kg"], i0, i1)
        to_c = min(rf[bid], _range_sum(data.s["to_compressor_kg"], i0, i1))
        to_b = rf[bid] - to_c
        if to_c > 0:
            d.outputs[(bid, "to_compressor")] = to_c
        if to_b > 0:
            d.outputs[(bid, "to_buffer")] = to_b
    for unit, (_, flow) in FEEDS.items():
        avail = {bid: d.outputs.get((bid, flow), 0.0) for bid, _, _ in e_batches}
        month_of = {bid: data.month[i0] for bid, i0, _ in e_batches}
        targets = d.batches[unit]
        for k, (tid, i0, i1) in enumerate(targets):
            need = _range_sum(data.s[f"{unit}_h2_kg"], i0, i1)
            if unit == "liquefier" and k == len(targets) - 1:
                left = sum(avail.values()) - buffer_carry_kg
                need = min(need, left)
            for bid, _, _ in e_batches:
                if need <= 1e-9 or month_of[bid] != data.month[i0]:
                    continue
                take = min(need, avail[bid])
                if take > 0:
                    d.feed[(bid, tid)] = d.feed.get((bid, tid), 0.0) + take
                    avail[bid] -= take
                    need -= take


def base_decisions(data: Data) -> Decisions:
    d = Decisions(
        batches={u: _monthly(data, p) for u, p in zip(UNITS, "ELC", strict=True)},
        renewable=base_allocation(data),
        outputs={},
        feed={},
        parts={
            # Highest-GHG material first (November LH₂ has the most boil-off), then the
            # oldest month among equal GHG; the targets do not bind.
            "CA": {"L-2026-11": 83808.0, "L-2026-10": 66192.0},
            "CB": {"C-2026-10": 59600.0, "C-2026-11": 57600.0, "C-2026-12": 2800.0},
        },
    )
    route(data, d, buffer_carry_kg=data.s["buffer_inv_kg"][-1])
    return d


def hourly_decisions(data: Data) -> Decisions:
    """V1: electrolyser batches within clock hours (2030); conditioning units stay monthly."""
    d = base_decisions(data)
    d.batches["electrolyser"] = _hourly(data, "E")
    d.outputs, d.feed = {}, {}
    route(data, d, buffer_carry_kg=data.s["buffer_inv_kg"][-1])
    res = run(data, Decisions(d.batches, d.renewable, d.outputs, d.feed, parts={}))
    lh2 = {b: res["units"]["liquefier"][b]["rfnbo_kg"] for b in ("L-2026-10", "L-2026-11")}
    d.parts = {
        "CA": {"L-2026-11": lh2["L-2026-11"], "L-2026-10": 150000.0 - lh2["L-2026-11"]},
        "CB": {},
    }
    ch2 = {b: res["units"]["compressor"][b]["rfnbo_kg"] for b in res["units"]["compressor"]}
    left = 120000.0
    for b in sorted(ch2):
        take = min(left, ch2[b])
        d.parts["CB"][b] = take
        left -= take
    return d


def v2_decisions(data: Data, all_to_electrolyser: bool = False) -> Decisions:
    """V2: December renewable 2,500 kWh per interval, no LH₂ spot sale.

    Non-compliant RFNBO cannot be carried (decided 2026-10-17), so the liquefier gets enough
    renewable for its December batch to comply: the whole last intervals of December, which
    become a 0% electrolyser batch. A part interval would be no better: kept apart its RFNBO is
    non-compliant, and in the main batch it raises the GHG the liquefier's part is set by (no
    averaging, §12). LH₂ storage is not full, and December's liquefier yield (0.98) beats the
    buffer's weight (0.977), so the liquefier takes all the RFNBO it can.

    ``all_to_electrolyser`` is the rejected alternative: December LH₂ is non-compliant and is
    written off."""
    d = base_decisions(data)
    dec = [i for i, m in enumerate(data.month) if m == "2026-12"]
    first, last = dec[0], dec[-1] + 1
    for i in dec:
        d.renewable["electrolyser"][i] = V2_DEC_RENEWABLE_KWH
        d.renewable["liquefier"][i] = 0.0
        d.renewable["compressor"][i] = 0.0
    carry = data.s["buffer_inv_kg"][-1]
    if not all_to_electrolyser:
        carry = 0.0
        dark = math.ceil(v2_min_liquefier_kWh(data) / V2_DEC_RENEWABLE_KWH)
        p = last - dark
        for i in range(p, last):
            d.renewable["electrolyser"][i] = 0.0
            d.renewable["liquefier"][i] = V2_DEC_RENEWABLE_KWH
        d.batches["electrolyser"] = [b for b in d.batches["electrolyser"] if b[1] < first] + [
            ("E-2026-12-01", first, p),
            ("E-2026-12-02", p, last),
        ]
    d.outputs, d.feed = {}, {}
    route(data, d, buffer_carry_kg=carry)
    # December CH₂ (8.33 gCO₂e/MJ: no renewable for the compressor) is above CB's limit, and
    # without averaging (§12) it cannot be blended down: CB is not met.
    d.parts["CB"] = {"C-2026-10": 59600.0, "C-2026-11": 57600.0, "C-2026-12": 2800.0}
    return d


def v2_min_liquefier_kWh(data: Data) -> float:
    """Least renewable for the liquefier's December batch to reach the threshold, fed only
    from 100% electrolyser batches (water and adder only)."""
    dec = [i for i, m in enumerate(data.month) if m == "2026-12"]
    elec = sum(data.s["liquefier_kWh"][i] for i in dec)
    feed = sum(data.s["liquefier_h2_kg"][i] for i in dec)
    out = sum(data.s["lh2_kg"][i] for i in dec)
    f = EF_WATER * 500.0 / (50.0 * LHV_MJ_PER_KG) + ADDER["electrolyser"]
    room = THRESHOLD - ADDER["liquefier"] - f * feed / out
    return elec - room * out * LHV_MJ_PER_KG / EF_GRID


def v3_decisions(data: Data) -> Decisions:
    """V3: Customer A's target cut to 3.5 gCO₂e/MJ, below the lowest LH₂ (1.69) plus 2.0
    distribution: infeasible. Customer B is unchanged."""
    d = base_decisions(data)
    d.parts = {"CB": d.parts["CB"]}
    d.targets = {"CA": V3_CA_TARGET}
    return d


# ---------------------------------------------------------------- calculation


def _ghg_key(ghg: float) -> float:
    return round(ghg, 9)


def split_parts(
    bid: str,
    sources: dict[str, tuple[float, float, bool]],  # source -> (kg, GHG, purchased)
    consumed_kg: float,
    product_kg: float,
    own_g: float,
    adder: float,
) -> dict[str, dict[str, Any]]:
    """The parts of a conditioning batch (§12): one per origin and source GHG, ascending GHG,
    named ``bid`` when there is one and ``bid.1``, ``bid.2``, … otherwise. Same LHV in and out."""
    groups: dict[tuple[float, bool], list[str]] = {}
    for src, (kg, g, purchased) in sources.items():
        if kg > 0:
            groups.setdefault((_ghg_key(g), purchased), []).append(src)
    keys = sorted(groups, key=lambda k: (k[0], k[1]))
    out: dict[str, dict[str, Any]] = {}
    for n, key in enumerate(keys, 1):
        srcs = groups[key]
        kg = sum(sources[s][0] for s in srcs)
        em = sum(sources[s][0] * LHV_MJ_PER_KG * sources[s][1] for s in srcs)
        share = kg / consumed_kg
        rf = share * product_kg
        ghg = (em + share * own_g) / (rf * LHV_MJ_PER_KG) + adder
        out[bid if len(keys) == 1 else f"{bid}.{n}"] = {
            "sources": srcs,
            "purchased": key[1],
            "feed_kg": kg,
            "feed_emissions_g": em,
            "share": share,
            "rfnbo_kg": rf,
            "ghg": ghg,
            "compliant": ghg <= THRESHOLD + 1e-9,
        }
    return out


def _close(storage: str, lines: dict[str, dict[str, Any]], physical: float) -> None:
    """§2.7: write off all non-compliant RFNBO, and the excess over physical inventory, highest
    GHG first, newest first."""
    closing = sum(x["closing_kg"] for x in lines.values())
    bad = [b for b in lines if lines[b]["ghg"] > THRESHOLD + 1e-9]
    excess = max(0.0, closing - physical, sum(lines[b]["closing_kg"] for b in bad))
    newest_first = sorted(lines, key=lambda b: lines[b]["month"], reverse=True)
    by_ghg = sorted(newest_first, key=lambda b: -_ghg_key(lines[b]["ghg"]))
    for b in sorted(by_ghg, key=lambda b: b not in bad):
        take = min(lines[b]["closing_kg"], excess)
        lines[b]["write_off_kg"] = take
        lines[b]["carried_kg"] = lines[b]["closing_kg"] - take
        excess -= take


def run(data: Data, d: Decisions) -> dict[str, Any]:
    s = data.s
    units: dict[str, dict[str, dict[str, Any]]] = {u: {} for u in UNITS}
    # Electrolyser batches
    for bid, i0, i1 in d.batches["electrolyser"]:
        alloc = _range_sum(d.renewable["electrolyser"], i0, i1)
        elec = _range_sum(s["electrolyser_kWh"], i0, i1)
        matched = min(alloc, elec)
        h2 = _range_sum(s["h2_kg"], i0, i1)
        water_g = _range_sum(s["water_kg"], i0, i1) * EF_WATER
        grid_g = (elec - matched) * EF_GRID
        ghg = (water_g + grid_g) / (h2 * LHV_MJ_PER_KG) + ADDER["electrolyser"]
        share = matched / elec
        metered = {f: _range_sum(s[f"{f}_kg"], i0, i1) for f in ("to_buffer", "to_compressor")}
        assigned = {f: d.outputs.get((bid, f), 0.0) for f in metered}
        units["electrolyser"][bid] = {
            "month": data.month[i0],
            "start": i0,
            "end": i1,
            "intervals": i1 - i0,
            "renewable_allocated_kWh": alloc,
            "electricity_kWh": elec,
            "renewable_matched_kWh": matched,
            "rfnbo_share": share,
            "product_kg": h2,
            "energy_MJ": h2 * LHV_MJ_PER_KG,
            "water_emissions_g": water_g,
            "grid_emissions_g": grid_g,
            "ghg": ghg,
            "compliant": ghg <= THRESHOLD + 1e-9,
            "rfnbo_kg": share * h2,
            "metered_kg": metered,
            "assigned_kg": assigned,
        }
    # Conditioning units, in topological order (both draw only on the electrolyser)
    for unit in ("liquefier", "compressor"):
        for bid, i0, i1 in d.batches[unit]:
            consumed = _range_sum(s[f"{unit}_h2_kg"], i0, i1)
            sources = {src: kg for (src, tgt), kg in d.feed.items() if tgt == bid}
            rf_feed = sum(sources.values())
            feed_em = sum(
                kg * LHV_MJ_PER_KG * units["electrolyser"][src]["ghg"]
                for src, kg in sources.items()
            )
            feed_ghg = feed_em / (rf_feed * LHV_MJ_PER_KG) if rf_feed else None
            share = rf_feed / consumed if consumed else 0.0
            product = _range_sum(s["lh2_kg" if unit == "liquefier" else "ch2_kg"], i0, i1)
            alloc = _range_sum(d.renewable[unit], i0, i1)
            elec = _range_sum(s[f"{unit}_kWh"], i0, i1)
            matched = min(alloc, elec)
            own_g = (elec - matched) * EF_GRID
            rf_kg = share * product
            parts = split_parts(
                bid,
                {
                    src: (kg, units["electrolyser"][src]["ghg"], False)
                    for src, kg in sources.items()
                },
                consumed,
                product,
                own_g,
                ADDER[unit],
            )
            ghg = next(iter(parts.values()))["ghg"] if len(parts) == 1 else None
            units[unit][bid] = {
                "month": data.month[i0],
                "start": i0,
                "end": i1,
                "intervals": i1 - i0,
                "feed_consumed_kg": consumed,
                "rfnbo_feed_kg": rf_feed,
                "sources": sources,
                "feed_ghg": feed_ghg,
                "rfnbo_share": share,
                "product_kg": product,
                "loss_kg": consumed - product,
                "renewable_allocated_kWh": alloc,
                "electricity_kWh": elec,
                "renewable_matched_kWh": matched,
                "own_emissions_g": own_g,
                "feed_emissions_g": feed_em,
                "rfnbo_kg": rf_kg,
                "energy_MJ": rf_kg * LHV_MJ_PER_KG,
                "ghg": ghg,
                "parts": parts,
                "compliant": bool(parts) and all(x["compliant"] for x in parts.values()),
            }
    # Ledgers
    ledgers: dict[str, dict[str, Any]] = {}
    for unit, (storage, flow) in FEEDS.items():
        lines = {}
        for bid, e in units["electrolyser"].items():
            inn = e["assigned_kg"][flow]
            out = sum(kg for (src, _), kg in d.feed.items() if src == bid and _ in units[unit])
            if inn or out:
                lines[bid] = {
                    "month": e["month"],
                    "ghg": e["ghg"],
                    "in_kg": inn,
                    "out_kg": out,
                    "closing_kg": inn - out,
                }
        physical = 0.0 if storage == "pt_compressor" else s["buffer_inv_kg"][-1]
        _close(storage, lines, physical)
        ledgers[storage] = {"lines": lines, "physical_kg": physical}
    by_storage = {c["storage"]: cid for cid, c in CONSIGNMENTS.items()}
    for unit, storage in PRODUCT_STORAGE.items():
        cid = by_storage[storage]
        lines = {}
        for b in units[unit].values():
            for pid, x in b["parts"].items():
                out = d.parts.get(cid, {}).get(pid, 0.0)
                lines[pid] = {
                    "month": b["month"],
                    "ghg": x["ghg"],
                    "in_kg": x["rfnbo_kg"],
                    "out_kg": out,
                    "closing_kg": x["rfnbo_kg"] - out,
                }
        physical = s["lh2_inv_kg" if storage == "lh2_storage" else "ch2_inv_kg"][-1]
        _close(storage, lines, physical)
        ledgers[storage] = {"lines": lines, "physical_kg": physical}
    for led in ledgers.values():
        lines = led["lines"].values()
        led["in_kg"] = sum(x["in_kg"] for x in lines)
        led["out_kg"] = sum(x["out_kg"] for x in lines)
        led["closing_before_kg"] = sum(x["closing_kg"] for x in lines)
        led["write_off_kg"] = sum(x["write_off_kg"] for x in lines)
        led["carried_kg"] = sum(x["carried_kg"] for x in lines)
        led["carried_compliant_kg"] = sum(
            x["carried_kg"] for x in lines if x["ghg"] <= THRESHOLD + 1e-9
        )
        led["carried_emissions_g"] = sum(x["carried_kg"] * LHV_MJ_PER_KG * x["ghg"] for x in lines)
        led["carried_ghg"] = (
            led["carried_emissions_g"] / (led["carried_kg"] * LHV_MJ_PER_KG)
            if led["carried_kg"]
            else None
        )
    # Consignments
    cons: dict[str, dict[str, Any]] = {}
    for cid, c in CONSIGNMENTS.items():
        parts = d.parts.get(cid, {})
        lines = ledgers[c["storage"]]["lines"]
        kg = sum(parts.values())
        target = d.targets.get(cid, c["target"])
        if not kg:
            cons[cid] = {"volume_kg": c["volume_kg"], "allocated_kg": 0.0, "met": False}
            cons[cid]["target"] = target
            continue
        highest = max(lines[b]["ghg"] for b, v in parts.items() if v > 0)
        shipped = highest + c["adder"]
        by_month: dict[str, float] = {}
        for b, v in parts.items():
            by_month[lines[b]["month"]] = by_month.get(lines[b]["month"], 0.0) + v
        cons[cid] = {
            "volume_kg": c["volume_kg"],
            "allocated_kg": kg,
            "target": target,
            "adder": c["adder"],
            "parts": dict(parts),
            "by_month": by_month,
            "highest_ghg": highest,
            "shipped_ghg": shipped,
            "parts_compliant": all(lines[b]["ghg"] <= THRESHOLD + 1e-9 for b in parts),
            "met": abs(kg - c["volume_kg"]) <= 1e-6 * c["volume_kg"]
            and shipped <= target + 1e-9
            and shipped <= THRESHOLD + 1e-9,
        }
    weights = storage_weights(data)
    stored = {k: v["carried_compliant_kg"] * LHV_MJ_PER_KG for k, v in ledgers.items()}
    objectives = {
        "consignments_met": sum(1 for c in cons.values() if c["met"]),
        "carried_value_MJ": sum(mj * weights[k] for k, mj in stored.items()),
        "stored_MJ_by_storage": stored,
        "weights": weights,
        "carried_emissions_g": sum(v["carried_emissions_g"] for v in ledgers.values()),
        "batches": sum(len(v) for v in d.batches.values()),
    }
    return {
        "intervals": len(data),
        "units": units,
        "ledgers": ledgers,
        "consignments": cons,
        "objectives": objectives,
    }


def storage_weights(data: Data) -> dict[str, float]:
    """MJ of final product per MJ carried in each storage, from the quarter's metered data."""
    s = data.s
    return {
        "h2_buffer": sum(s["lh2_kg"]) / sum(s["liquefier_h2_kg"]),  # liquefier energy yield
        "pt_compressor": sum(s["ch2_kg"]) / sum(s["compressor_h2_kg"]),
        "lh2_storage": 1.0,
        "ch2_storage": 1.0,
    }


def pool_ok(data: Data, d: Decisions) -> bool:
    """Renewable allocated in each interval never exceeds what was available."""
    return all(
        sum(d.renewable[u][i] for u in UNITS) <= data.s["renewable_kWh"][i] + 1e-9
        for i in range(len(data))
    )


def splits_ok(result: dict[str, Any]) -> bool:
    """Rule 4: each flow gets at most its metered quantity; the parts add up to the RFNBO."""
    for e in result["units"]["electrolyser"].values():
        a, m = e["assigned_kg"], e["metered_kg"]
        if any(a[f] > m[f] + 1e-9 for f in a) or abs(sum(a.values()) - e["rfnbo_kg"]) > 1e-6:
            return False
    return True


def all_cases() -> dict[str, dict[str, Any]]:
    """Every case the workbook covers, from freshly built data."""
    base = build_data()
    v2 = build_data(dec_renewable_kWh=V2_DEC_RENEWABLE_KWH, spot=False)
    return {
        "base": run(base, base_decisions(base)),
        "v1_hourly": run(base, hourly_decisions(base)),
        "v2_shortfall": run(v2, v2_decisions(v2)),
        "v2_all_to_electrolyser": run(v2, v2_decisions(v2, all_to_electrolyser=True)),
        "v3_infeasible": run(base, v3_decisions(base)),
    }
