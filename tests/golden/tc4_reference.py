"""Test case 4: independent reference calculation for purchased RFNBO (build plan §11.1).

Plant: test plant 3 plus a third-party supplier of H₂ (tube trailers) whose deliveries are
metered into the compressor (1–10 November) and the liquefier (11–20 December). Not all of
the delivered H₂ is RFNBO: only what proofs of sustainability (PoS) are allocated to.

Three PoS land in the quarter (decisions 2026-10-17):
- POS-A, dispatched 20 Oct, 8,000 kg at 3.0 gCO₂e/MJ: low GHG.
- POS-B, dispatched 20 Dec, 9,000 kg at 12.0 gCO₂e/MJ: partly allocated to flow metered before
  its dispatch date, which is allowed within the quarter (decision 7).
- POS-C, dispatched 25 Nov, 6,000 kg at 27.9 gCO₂e/MJ: near the threshold, and only part
  covered by metered delivery: the quarter's 19,200 kg of metered supply is less than the
  23,000 kg of PoS, so 3,800 kg of POS-C is not allocated and is written off (decision 2).

Like ``tc3_reference`` this module is plain Python and does not import the app. It reuses
test case 3's data and decisions (the electrolyser, its split output and the H₂ buffer are
unchanged); the base case has no LH₂ spot sale, so LH₂ storage has room for more RFNBO.

Rules applied, in addition to test case 3's:
- Allocated PoS kg is RFNBO feed with the PoS's GHG intensity (rule 4): it counts in the
  batch's RFNBO share by energy and adds its emissions to the feed emissions. The supplier's
  metered delivery counts in the feed the unit consumed whether or not it is RFNBO.
- Per batch and supply flow, allocated kg ≤ metered kg; per PoS, allocated ≤ quantity.
- Storage weights count the supplier's delivery in a unit's feed (energy yield = product MJ ÷
  all feed MJ).
- Objectives as test case 3. Where the objectives tie, the optimiser uses the lowest-GHG
  credentials and then the credentials dispatched nearest the month of the batch they feed.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from tests.golden import tc3_reference as R

LHV = R.LHV_MJ_PER_KG
THRESHOLD = R.THRESHOLD
SUPPLY_KG = 10.0  # per interval, on delivery days
SUPPLY_DAYS = {"compressor": (11, range(1, 11)), "liquefier": (12, range(11, 21))}
DEC_YIELD = 0.98  # liquefier, October and December
POS = {
    "POS-A": {"dispatch": dt.date(2026, 10, 20), "kg": 8000.0, "ghg": 3.0},
    "POS-B": {"dispatch": dt.date(2026, 12, 20), "kg": 9000.0, "ghg": 12.0},
    "POS-C": {"dispatch": dt.date(2026, 11, 25), "kg": 6000.0, "ghg": 27.9},
}
SUPPLIER = "H2 Trailers BV"
# H₂ buffer physical inventory at quarter end (4,880 kg) less the 2,880 kg built up on the trip
# day: what November's liquefier batch leaves in the buffer.
NOV_HELD_KG = 2000.0


def build_data(spot: bool = False) -> R.Data:
    """Test case 3's quarter with the supplier's deliveries; ``spot`` keeps the LH₂ spot sale
    (variant V1)."""
    data = R.build_data(spot=spot)
    s = data.s
    for unit in SUPPLY_DAYS:
        s[f"sup_{unit}_kg"] = []
    lh2_add = ch2_add = 0  # cumulative tenths of a kg
    for i, lt in enumerate(data.local):
        for unit, (month, days) in SUPPLY_DAYS.items():
            s[f"sup_{unit}_kg"].append(SUPPLY_KG if lt.month == month and lt.day in days else 0.0)
        if s["sup_liquefier_kg"][i]:
            s["lh2_kg"][i] = round(s["lh2_kg"][i] + DEC_YIELD * SUPPLY_KG, 1)
            lh2_add += round(DEC_YIELD * SUPPLY_KG * 10)
        if s["sup_compressor_kg"][i]:
            s["ch2_kg"][i] += SUPPLY_KG
            ch2_add += round(SUPPLY_KG * 10)
        s["lh2_inv_kg"][i] = (round(s["lh2_inv_kg"][i] * 10) + lh2_add) / 10
        s["ch2_inv_kg"][i] = (round(s["ch2_inv_kg"][i] * 10) + ch2_add) / 10
    return data


@dataclass
class Decisions:
    base: R.Decisions
    pos: dict[tuple[str, str], float]  # (PoS, unit batch) -> kg
    parts: dict[str, dict[str, float]] = field(default_factory=dict)


SUPPLY_FLOW = {"liquefier": "f_sup_liq", "compressor": "f_sup_comp"}


def base_decisions(data: R.Data) -> Decisions:
    """Every metered kg of supply is covered by a PoS (objective 2: carried value). The H₂
    buffer ends full of RFNBO (4,880 kg): 2,880 kg the liquefier's buffer flow did not take on
    the trip day, and 2,000 kg that the November liquefier batch does not take, because
    November's liquefier yield (0.97) is below the buffer's weight (0.9768); October's and
    December's (0.98) are above it, so those batches are fully RFNBO. Which credentials: the
    lowest GHG (A and B in full, C 2,200 kg), placed nearest their dispatch month: A (Oct) and
    C (Nov) into the November compressor batch, B (Dec) and the rest of C into the December
    liquefier batch."""
    d = R.base_decisions(data)
    d.outputs, d.feed = {}, {}
    R.route(data, d, buffer_carry_kg=0.0)
    d.feed[("E-2026-11", "L-2026-11")] -= NOV_HELD_KG
    return Decisions(
        d,
        pos={
            ("POS-A", "C-2026-11"): 8000.0,
            ("POS-C", "C-2026-11"): 1600.0,
            ("POS-B", "L-2026-12"): 9000.0,
            ("POS-C", "L-2026-12"): 600.0,
        },
        # Highest GHG first: the batches holding credentials, then the next highest.
        parts={
            "CA": {"L-2026-12": 94080.0, "L-2026-11": 55920.0},
            "CB": {"C-2026-11": 67200.0, "C-2026-10": 52800.0},
        },
    )


def v1_decisions(data: R.Data) -> Decisions:
    """V1: test case 3's 60,000 kg LH₂ spot sale is back, so LH₂ storage holds less than its
    RFNBO ledger and the excess is written off. A credential allocated to the liquefier would
    only add LH₂ that is written off, so none is: the compressor takes the lowest-GHG 9,600
    kg (A, then B), the H₂ buffer stays full of RFNBO as in test case 3, and 13,400 kg of
    credentials are written off unallocated."""
    d = R.base_decisions(data)  # test case 3's routing: the buffer stays full
    return Decisions(
        d,
        pos={("POS-A", "C-2026-11"): 8000.0, ("POS-B", "C-2026-11"): 1600.0},
        parts={
            "CA": {"L-2026-11": 83808.0, "L-2026-10": 66192.0},
            "CB": {"C-2026-11": 67200.0, "C-2026-10": 52800.0},
        },
    )


def run(data: R.Data, dd: Decisions) -> dict[str, Any]:
    """Test case 3's calculation with purchased RFNBO feed into the conditioning units."""
    s = data.s
    d = dd.base
    res = R.run(data, R.Decisions(d.batches, d.renewable, d.outputs, d.feed, parts={}))
    e = res["units"]["electrolyser"]
    units: dict[str, dict[str, dict[str, Any]]] = {"electrolyser": e}
    for unit in ("liquefier", "compressor"):
        units[unit] = {}
        for bid, i0, i1 in d.batches[unit]:
            from_storage = R._range_sum(s[f"{unit}_h2_kg"], i0, i1)
            supply = R._range_sum(s[f"sup_{unit}_kg"], i0, i1)
            consumed = from_storage + supply
            sources = {src: kg for (src, tgt), kg in d.feed.items() if tgt == bid}
            credits = {p: kg for (p, tgt), kg in dd.pos.items() if tgt == bid}
            storage_rf = sum(sources.values())
            pos_rf = sum(credits.values())
            storage_em = sum(kg * LHV * e[src]["ghg"] for src, kg in sources.items())
            pos_em = sum(kg * LHV * POS[p]["ghg"] for p, kg in credits.items())
            rf_feed = storage_rf + pos_rf
            feed_em = storage_em + pos_em
            share = rf_feed / consumed if consumed else 0.0
            product = R._range_sum(s["lh2_kg" if unit == "liquefier" else "ch2_kg"], i0, i1)
            alloc = R._range_sum(d.renewable[unit], i0, i1)
            elec = R._range_sum(s[f"{unit}_kWh"], i0, i1)
            matched = min(alloc, elec)
            own_g = (elec - matched) * R.EF_GRID
            rf_kg = share * product
            ghg = (feed_em + share * own_g) / (rf_kg * LHV) + R.ADDER[unit] if rf_kg else None
            units[unit][bid] = {
                "month": data.month[i0],
                "start": i0,
                "end": i1,
                "intervals": i1 - i0,
                "storage_feed_kg": from_storage,
                "supply_kg": supply,
                "feed_consumed_kg": consumed,
                "storage_rfnbo_feed_kg": storage_rf,
                "pos_feed_kg": pos_rf,
                "rfnbo_feed_kg": rf_feed,
                "sources": sources,
                "credits": credits,
                "storage_feed_emissions_g": storage_em,
                "pos_emissions_g": pos_em,
                "feed_emissions_g": feed_em,
                "feed_ghg": feed_em / (rf_feed * LHV) if rf_feed else None,
                "rfnbo_share": share,
                "product_kg": product,
                "loss_kg": consumed - product,
                "renewable_allocated_kWh": alloc,
                "electricity_kWh": elec,
                "renewable_matched_kWh": matched,
                "own_emissions_g": own_g,
                "rfnbo_kg": rf_kg,
                "energy_MJ": rf_kg * LHV,
                "ghg": ghg,
                "compliant": ghg is not None and ghg <= THRESHOLD + 1e-9,
            }
    ledgers = {k: res["ledgers"][k] for k in ("h2_buffer", "pt_compressor")}
    by_storage = {c["storage"]: cid for cid, c in R.CONSIGNMENTS.items()}
    for unit, storage in R.PRODUCT_STORAGE.items():
        cid = by_storage[storage]
        lines = {}
        for bid, b in units[unit].items():
            if not b["rfnbo_kg"]:
                continue
            out = dd.parts.get(cid, {}).get(bid, 0.0)
            lines[bid] = {
                "month": b["month"],
                "ghg": b["ghg"],
                "in_kg": b["rfnbo_kg"],
                "out_kg": out,
                "closing_kg": b["rfnbo_kg"] - out,
            }
        physical = s["lh2_inv_kg" if storage == "lh2_storage" else "ch2_inv_kg"][-1]
        R._close(storage, lines, physical)
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
        led["carried_emissions_g"] = sum(x["carried_kg"] * LHV * x["ghg"] for x in lines)
        led["carried_ghg"] = (
            led["carried_emissions_g"] / (led["carried_kg"] * LHV) if led["carried_kg"] else None
        )
    cons: dict[str, dict[str, Any]] = {}
    for cid, c in R.CONSIGNMENTS.items():
        parts = dd.parts.get(cid, {})
        lines = ledgers[c["storage"]]["lines"]
        kg = sum(parts.values())
        weighted = sum(v * lines[b]["ghg"] for b, v in parts.items()) / kg
        shipped = weighted + c["adder"]
        cons[cid] = {
            "volume_kg": c["volume_kg"],
            "allocated_kg": kg,
            "target": c["target"],
            "adder": c["adder"],
            "parts": dict(parts),
            "weighted_ghg": weighted,
            "shipped_ghg": shipped,
            "met": abs(kg - c["volume_kg"]) <= 1e-6 * c["volume_kg"]
            and shipped <= c["target"] + 1e-9
            and all(lines[b]["ghg"] <= THRESHOLD + 1e-9 for b in parts),
        }
    # The PoS register: allocated, and what is left at quarter end (written off).
    pos: dict[str, dict[str, Any]] = {}
    for p, rec in POS.items():
        to = {tgt: kg for (q, tgt), kg in dd.pos.items() if q == p}
        allocated = sum(to.values())
        pos[p] = {
            **rec,
            "allocated_kg": allocated,
            "written_off_kg": rec["kg"] - allocated,
            "to": to,
            "emissions_allocated_g": allocated * LHV * rec["ghg"],
        }
    delivery = {}
    for unit in ("liquefier", "compressor"):
        for bid, b in units[unit].items():
            if b["supply_kg"] or b["pos_feed_kg"]:
                delivery[bid] = {"metered_kg": b["supply_kg"], "allocated_kg": b["pos_feed_kg"]}
    weights = storage_weights(data)
    stored = {k: v["carried_compliant_kg"] * LHV for k, v in ledgers.items()}
    objectives = {
        "consignments_met": sum(1 for c in cons.values() if c["met"]),
        "carried_value_MJ": sum(mj * weights[k] for k, mj in stored.items()),
        "stored_MJ_by_storage": stored,
        "weights": weights,
        "carried_emissions_g": sum(v["carried_emissions_g"] for v in ledgers.values()),
        "batches": sum(len(v) for v in d.batches.values()),
        "purchased_emissions_g": sum(x["emissions_allocated_g"] for x in pos.values()),
    }
    return {
        "intervals": len(data),
        "units": units,
        "ledgers": ledgers,
        "consignments": cons,
        "pos": pos,
        "delivery": delivery,
        "metered_supply_kg": sum(sum(s[f"sup_{u}_kg"]) for u in SUPPLY_DAYS),
        "objectives": objectives,
    }


def storage_weights(data: R.Data) -> dict[str, float]:
    """As test case 3, with the supplier's delivery in each unit's feed."""
    s = data.s
    return {
        "h2_buffer": sum(s["lh2_kg"]) / (sum(s["liquefier_h2_kg"]) + sum(s["sup_liquefier_kg"])),
        "pt_compressor": sum(s["ch2_kg"])
        / (sum(s["compressor_h2_kg"]) + sum(s["sup_compressor_kg"])),
        "lh2_storage": 1.0,
        "ch2_storage": 1.0,
    }


def checks_ok(result: dict[str, Any]) -> bool:
    """Every PoS rule: compliant, in the quarter, within quantity, within metered delivery."""
    for p in result["pos"].values():
        if p["ghg"] > THRESHOLD + 1e-9 or not (dt.date(2026, 10, 1) <= p["dispatch"]):
            return False
        if p["dispatch"] >= dt.date(2027, 1, 1) or p["allocated_kg"] > p["kg"] + 1e-6:
            return False
    return all(x["allocated_kg"] <= x["metered_kg"] + 1e-6 for x in result["delivery"].values())


def all_cases() -> dict[str, dict[str, Any]]:
    base = build_data()
    v1 = build_data(spot=True)
    return {"base": run(base, base_decisions(base)), "v1_spot": run(v1, v1_decisions(v1))}
