"""Per-batch traceability records (build plan §5).

Each record carries every input, quantity, unit, emission factor, factor unit and emissions
figure, so the batch GHG can be recomputed from the record alone (:func:`recompute_ghg`).
Numbers are not rounded: JSON floats round-trip exactly.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from rfnbo.calc.batch import BatchResult
from rfnbo.trace.lineage import Edge

if TYPE_CHECKING:
    from rfnbo.calc.calculator import RunResult

FORMULA_ELECTROLYTIC = (
    "ghg = (sum(inputs.emissions_g) + sum(adders.emissions_g)) / output.energy_MJ; "
    "rfnbo_qty = rfnbo_share * output.qty; compliant = ghg <= threshold"
)
FORMULA_DOWNSTREAM = (
    "per part p (one per source GHG and origin; parts are never averaged): "
    "share_p = sum(feed[p].energy_MJ) / feed_consumed_MJ; "
    "ghg_p = (sum(feed[p].emissions_g) + share_p * sum(inputs.emissions_g) "
    "+ adder * share_p * output.energy_MJ) / (share_p * output.energy_MJ); "
    "qty_p = share_p * output.qty; compliant_p = ghg_p <= threshold"
)


def local_iso(ts: dt.datetime, tz: str) -> str:
    return ts.astimezone(ZoneInfo(tz)).isoformat(timespec="minutes")


def batch_record(
    run: RunResult, b: BatchResult, run_id: str, downstream: list[Edge]
) -> dict[str, Any]:
    tz = run.frame.timezone
    downstream_unit = b.kind == "downstream"
    inputs = []
    for line in b.inputs:
        item: dict[str, Any] = {
            "input": line.name,
            "block": line.source,
            "qty": line.qty,
            "uom": line.uom,
            "ef": line.ef,
            "ef_uom": line.ef_uom,
            "ef_source": line.ef_source,
            "emissions_g": line.emissions_g,
        }
        if downstream_unit:
            item["prorated_to_rfnbo"] = b.rfnbo_share
        inputs.append(item)

    rec: dict[str, Any] = {
        "batch": b.id,
        "run_id": run_id,
        "config_version": f"{run.config.version} ({run.config.effective_from.isoformat()})",
        "unit": b.unit,
        "unit_name": b.unit_name,
        "kind": b.kind,
        "start": local_iso(b.start, tz),
        "end": local_iso(b.end, tz),
        "start_utc": b.start.isoformat(),
        "end_utc": b.end.isoformat(),
        "month": b.month,
        "quarter": b.quarter,
        "intervals": b.intervals,
        "electricity_kWh": b.electricity_kWh,
        "renewable_available_kWh": b.renewable_available_kWh,
        "renewable_matched_kWh": b.renewable_matched_kWh,
        "inputs": inputs,
    }
    if downstream_unit:
        rec["feed"] = [
            {
                "batch": f.source_batch,
                "origin": f.origin,
                "via": run.feed_via(f, b.id),
                "qty": f.kg,
                "uom": "kg",
                "energy_MJ": f.energy_MJ,
                "ghg": f.ghg_gCO2e_per_MJ,
                "ghg_uom": "gCO2e/MJ",
                "emissions_g": f.emissions_g,
                "compliant": f.compliant,
                "source": f.source,
                "month": f.month,
            }
            | ({"pool": f.pool} if f.pool else {})
            for f in b.feed
        ]
        rec["feed_consumed_kg"] = b.feed_consumed_kg
        rec["feed_consumed_MJ"] = b.feed_consumed_MJ
        rec["rfnbo_feed_kg"] = b.rfnbo_feed_kg
        rec["feed_ghg"] = b.feed_ghg_gCO2e_per_MJ
    rec["adders"] = [
        {
            "name": f"{b.unit_name} fixed adder",
            "value": b.adder_gCO2e_per_MJ,
            "uom": "gCO2e/MJ",
            "applies_to_MJ": b.rfnbo_MJ if downstream_unit else b.energy_MJ,
            "emissions_g": b.adder_emissions_g,
        }
    ]
    rec["output"] = {
        "product": b.product,
        "qty": b.product_kg,
        "uom": "kg",
        "lhv": b.lhv_MJ_per_kg,
        "lhv_uom": "MJ/kg",
        "energy_MJ": b.energy_MJ,
        # Where the batch's RFNBO entered a ledger: one storage, or each part of a split output.
        "to": [
            {"storage": s, "rfnbo_kg": x.in_kg}
            for s in run.storages_of.get(b.id, ())
            if (x := run.line(s, b.id)) is not None
        ],
    }
    rec["parts"] = [
        {
            "part": p.id,
            "sources": list(p.sources),
            "purchased": p.purchased,
            "source": p.source,
            "share": p.share,
            "qty": p.kg,
            "uom": "kg",
            "energy_MJ": p.energy_MJ,
            "feed_emissions_g": p.feed_emissions_g,
            "own_emissions_g": p.own_emissions_g,
            "adder_emissions_g": p.adder_emissions_g,
            "ghg": p.ghg_gCO2e_per_MJ,
            "compliant": p.compliant,
        }
        | ({"pool": p.pool} if p.pool else {})
        for p in b.parts
    ]
    rec.update(
        {
            "rfnbo_share": b.rfnbo_share,
            "ghg": b.ghg_gCO2e_per_MJ,
            "ghg_uom": "gCO2e/MJ",
            "threshold": b.threshold_gCO2e_per_MJ,
            "compliant": b.compliant,
            "rfnbo": b.is_rfnbo,
            "rfnbo_qty": b.rfnbo_kg,
            "rfnbo_uom": "kg",
            "rfnbo_MJ": b.rfnbo_MJ,
            "formula": FORMULA_DOWNSTREAM if downstream_unit else FORMULA_ELECTROLYTIC,
            "lineage": {
                "upstream": [
                    {
                        "batch": f.source_batch,
                        "origin": f.origin,
                        "via": run.feed_via(f, b.id),
                        "kg": f.kg,
                    }
                    for f in b.feed
                ],
                "downstream": [{"to": e.target, "kind": e.kind, "kg": e.kg} for e in downstream],
            },
            "checks": [
                {"check": c.name, "reference": c.reference, "passed": c.passed, "detail": c.detail}
                for c in run.checks
                if c.subject == b.id
            ],
        }
    )
    return rec


def batch_records(run: RunResult, run_id: str, all_edges: list[Edge]) -> list[dict[str, Any]]:
    out_edges: dict[str, list[Edge]] = defaultdict(list)
    for e in all_edges:
        out_edges[e.source].append(e)
    return [batch_record(run, b, run_id, out_edges[b.id]) for b in run.batches.values()]


def recompute_parts(rec: dict[str, Any]) -> dict[str, float]:
    """Recompute every part's GHG from the record alone (no app state)."""
    if rec["kind"] == "electrolytic":
        g = recompute_ghg(rec)
        return {p["part"]: g for p in rec["parts"]} if g is not None else {}
    own = sum(line["emissions_g"] for line in rec["inputs"])
    adder = sum(a["value"] for a in rec["adders"])
    energy = rec["output"]["energy_MJ"]
    out: dict[str, float] = {}
    for p in rec["parts"]:
        # A pool's lines (averaged, §12.1); otherwise the part's sources outside any pool.
        lines = (
            [f for f in rec["feed"] if f.get("pool") == p["pool"]]
            if p.get("pool")
            else [f for f in rec["feed"] if f["batch"] in p["sources"] and not f.get("pool")]
        )
        share = sum(f["energy_MJ"] for f in lines) / rec["feed_consumed_MJ"]
        feed = sum(f["emissions_g"] for f in lines)
        mj = share * energy
        out[p["part"]] = float((feed + share * own + adder * mj) / mj)
    return out


def recompute_ghg(rec: dict[str, Any]) -> float | None:
    """Recompute a batch's GHG from its record alone (no app state)."""
    if rec["ghg"] is None:
        return None
    for line in rec["inputs"]:
        if line["emissions_g"] != line["qty"] * line["ef"]:
            raise ValueError(f"{rec['batch']}: {line['input']} emissions != qty * ef")
    own = sum(line["emissions_g"] for line in rec["inputs"])
    adders = sum(a["emissions_g"] for a in rec["adders"])
    energy = rec["output"]["energy_MJ"]
    if rec["kind"] == "electrolytic":
        return float((own + adders) / energy)
    share = sum(f["energy_MJ"] for f in rec["feed"]) / rec["feed_consumed_MJ"]
    feed = sum(f["emissions_g"] for f in rec["feed"])
    return float((feed + share * own + adders) / (share * energy))
