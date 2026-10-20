"""Export a test workbook's decision set and expected values to JSON fixtures.

    uv run python tests/golden/export_workbook.py <workbook.xlsx> <out-prefix> [--rule-2-5]
        [--rule-12]

Writes ``<out-prefix>.decisions.json`` and ``<out-prefix>.expected.json``. Values are read from
the workbook's cached results; nothing here uses the calculator.

``--rule-2-5`` regenerates what the non-compliant RFNBO rule (build plan §2.5) changes: an
electrolyser sub-batch above 28.2 gCO2e/MJ is still RFNBO and enters the H2 ledger, so its RFNBO
kg and the H2 ledger and write-off are recomputed here, independently of the app. Non-compliant
RFNBO cannot be carried into the next quarter (decided 2026-10-17), so all of it left at quarter
end is written off, ahead of compliant batches. Workbook values that change are kept under
``superseded``.

``--rule-12`` regenerates what build plan §12 and §12.1 (decided 2026-10-20) change. An NH₃
batch's RFNBO is split into parts: the compliant H₂ of one production month is averaged into one
part (the workbook averaged all its feed; §12.1 allows that only for compliant RFNBO of one month
and source), and each non-compliant H₂ GHG makes a part of its own (never averaged). A part's
GHG is g × C ÷ E + own ÷ E + adder, with g the energy-weighted GHG of its H₂, C the H₂ consumed
and E the NH₃ energy (MJ). The NH₃ ledger works on parts; a consignment's GHG is the highest
energy-weighted average of its aggregated batches (one per production month) plus the
distribution adder. The workbook's allocations average September with October NH₃, which
§12.1 does not allow (different months), so they are re-derived: each consignment takes the
parts at or below its limit (target − adder, and the threshold) in ascending GHG, oldest first;
what is still missing comes from the lowest-GHG parts left, and the consignment then misses its
target. Workbook values that change are kept under ``superseded``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any

import openpyxl

UTC = dt.UTC
STEP = dt.timedelta(minutes=15)
CUSTOMER_OUTPUT = {"Customer A": "consignment_a", "Customer B": "consignment_b"}
MONTHS = {
    m: i
    for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1
    )
}
THRESHOLD = 28.2
H2_LHV, NH3_LHV, NH3_ADDER = 120.0, 18.6, 2.0  # examples/test_plant.json


def rows(wb: Any, sheet: str) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in wb[sheet].iter_rows(values_only=True)]


def labelled(wb: Any, sheet: str) -> dict[str, Any]:
    return {str(r[0]).strip(): r[1] for r in rows(wb, sheet) if r and r[0] is not None}


def pick(table: dict[str, Any], *prefixes: str) -> Any:
    for p in prefixes:
        for k, v in table.items():
            if k.startswith(p):
                return v
    raise KeyError(prefixes)


def iso(ts: dt.datetime) -> str:
    return ts.replace(tzinfo=UTC).isoformat()


def runs(
    stamps: list[dt.datetime], labels: list[str]
) -> list[tuple[str, dt.datetime, dt.datetime]]:
    out: list[tuple[str, dt.datetime, dt.datetime]] = []
    for ts, label in zip(stamps, labels, strict=True):
        if out and out[-1][0] == label and out[-1][2] == ts:
            out[-1] = (label, out[-1][1], ts + STEP)
        else:
            if any(o[0] == label for o in out):
                raise ValueError(f"batch {label} is not contiguous")
            out.append((label, ts, ts + STEP))
    return out


def export(
    path: Path, rule_2_5: bool, rule_12: bool = False
) -> tuple[dict[str, Any], dict[str, Any]]:
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)

    # --- 15-minute data: electrolyser batch ids and local months ---
    data = rows(wb, "Data_15min")
    head = data[0]
    i_utc, i_month, i_batch = 1, head.index("Local month"), len(head) - 1
    body = [r for r in data[1:] if r[0] is not None]
    stamps = [r[i_utc] for r in body]
    e_runs = runs(stamps, [r[i_batch] for r in body])
    m_runs = runs(stamps, [r[i_month] for r in body])

    batches = [
        {"id": b, "unit": "electrolyser", "start": iso(s), "end": iso(e)} for b, s, e in e_runs
    ]
    batches += [
        {"id": f"N-{m}", "unit": "nh3_synthesis", "start": iso(s), "end": iso(e)}
        for m, s, e in m_runs
    ]

    # --- electrolyser batches ---
    h2: dict[str, dict[str, Any]] = OrderedDict()
    superseded: dict[str, Any] = {}
    for r in rows(wb, "H2_Batches")[1:]:
        if not (isinstance(r[0], str) and r[0].startswith("E-")):
            continue
        rec = dict(
            month=r[1],
            intervals=r[4],
            renewable_available_kWh=r[5],
            electricity_kWh=r[6],
            renewable_matched_kWh=r[7],
            rfnbo_share=r[8],
            product_kg=r[9],
            energy_MJ=r[10],
            water_emissions_g=r[11],
            grid_emissions_g=r[12],
            ghg=r[13],
            compliant=bool(r[14]),
            rfnbo_kg=r[15],
            rfnbo_MJ=r[16],
        )
        if rule_2_5 and rec["rfnbo_share"] > 0 and not rec["compliant"]:
            superseded.setdefault("h2_rfnbo_kg", {})[r[0]] = rec["rfnbo_kg"]
            rec["rfnbo_kg"] = rec["rfnbo_share"] * rec["product_kg"]
            rec["rfnbo_MJ"] = rec["rfnbo_kg"] * 120.0
        h2[r[0]] = rec

    # --- NH3 feed allocation ---
    feed_rows = rows(wb, "NH3_Feed")
    targets = {
        j: re.search(r"N-\d{4}-\d\d", str(h)).group(0)  # type: ignore[union-attr]
        for j, h in enumerate(feed_rows[0])
        if h and str(h).startswith("→ N-")
    }
    feed = []
    fed: dict[str, float] = {}
    fed_into: dict[str, dict[str, float]] = {}
    for r in feed_rows[1:]:
        if not (isinstance(r[0], str) and r[0].startswith("E-")):
            continue
        for j, target in targets.items():
            if r[j]:
                feed.append({"source": r[0], "target": target, "kg": r[j]})
                fed[r[0]] = fed.get(r[0], 0.0) + r[j]
                into = fed_into.setdefault(target, {})
                into[r[0]] = into.get(r[0], 0.0) + r[j]

    # --- NH3 batches ---
    nh3: dict[str, dict[str, Any]] = OrderedDict()
    for r in rows(wb, "NH3_Batches")[1:]:
        if not (isinstance(r[0], str) and r[0].startswith("N-")) or "opening" in r[0]:
            continue
        nh3[r[0]] = dict(
            month=r[1],
            intervals=r[2],
            product_kg=r[3],
            feed_consumed_kg=r[4],
            rfnbo_feed_kg=r[5],
            feed_ghg=r[7],
            rfnbo_share=r[8],
            feed_emissions_g=r[9],
            electricity_kWh=r[10],
            electricity_emissions_g=r[11],
            rfnbo_kg=r[12],
            rfnbo_MJ=r[13],
            ghg=r[14],
            compliant=bool(r[15]),
        )

    # --- H2 ledger ---
    t = labelled(wb, "H2_Ledger")
    h2_ledger = dict(
        opening_kg=pick(t, "Opening RFNBO ledger"),
        produced_kg=pick(t, "RFNBO H₂ produced"),
        out_feed_kg=pick(t, "RFNBO H₂ allocated to NH₃"),
        closing_before_kg=pick(t, "Closing RFNBO ledger before"),
        physical_kg=pick(t, "Measured physical inventory"),
        write_off_kg=pick(t, "Write-off", "Shortfall / write-off"),
        carried_kg=pick(t, "RFNBO H₂ carried"),
    )
    if "Energy-weighted GHG of carried H₂ (gCO₂e/MJ)" in t:
        h2_ledger["carried_ghg"] = t["Energy-weighted GHG of carried H₂ (gCO₂e/MJ)"]
    h2_write_offs: dict[str, float] = {}
    if rule_2_5:
        superseded["h2_ledger"] = dict(h2_ledger)
        lines = [
            (b, v["ghg"], v["month"], v["rfnbo_kg"] - fed.get(b, 0.0))
            for b, v in h2.items()
            if v["rfnbo_kg"] > 0
        ]
        closing = sum(x[3] for x in lines)
        bad = {b for b, v in h2.items() if not v["compliant"]}
        noncompliant = sum(kg for b, _g, _m, kg in lines if b in bad)
        excess = max(0.0, closing - h2_ledger["physical_kg"], noncompliant)
        left = excess
        for b, _g, _m, kg in sorted(
            lines, key=lambda x: (x[0] in bad, round(x[1], 9), x[2], x[0]), reverse=True
        ):
            if left <= 0:
                break
            take = min(kg, left)
            if take > 0:
                h2_write_offs[b] = take
                left -= take
        carried = {b: kg - h2_write_offs.get(b, 0.0) for b, _g, _m, kg in lines}
        mj = sum(carried.values()) * 120.0
        h2_ledger.update(
            produced_kg=sum(v["rfnbo_kg"] for v in h2.values()),
            closing_before_kg=closing,
            write_off_kg=excess,
            carried_kg=closing - excess,
            carried_ghg=(
                sum(kg * 120.0 * h2[b]["ghg"] for b, kg in carried.items()) / mj
                if closing - excess > 1e-6
                else None
            ),
        )
    h2_recon = dict(
        opening_physical_kg=pick(t, "Opening physical inventory"),
        produced_kg=pick(t, "H₂ produced (kg)"),
        consumed_kg=pick(t, "H₂ consumed by NH₃"),
        expected_kg=pick(t, "Expected closing"),
        measured_kg=pick(t, "Measured closing"),
        implied_loss_kg=pick(t, "Implied loss"),
    )

    # --- NH3 ledger ---
    lt = rows(wb, "NH3_Ledger")
    nh3_lines: dict[str, dict[str, Any]] = OrderedDict()
    for r in lt[1:]:
        if isinstance(r[0], str) and r[0].startswith("N-"):
            bid = r[0].split()[0]
            nh3_lines[bid] = dict(
                month=r[1],
                ghg=r[2],
                in_kg=r[3],
                out_consignment_kg=r[4],
                closing_before_kg=r[5],
                write_off_kg=r[6],
                carried_kg=r[7],
            )
    t2 = labelled(wb, "NH3_Ledger")
    nh3_ledger = dict(
        lines=nh3_lines,
        physical_kg=pick(t2, "Measured physical inventory"),
        write_off_kg=pick(t2, "Shortfall to write off"),
        carried_ghg=pick(t2, "Energy-weighted GHG of carried"),
    )
    nh3_recon = dict(
        opening_physical_kg=pick(t2, "Opening physical inventory"),
        produced_kg=pick(t2, "NH₃ produced"),
        shipped_c1_kg=pick(t2, "Shipped C1"),
        shipped_c2_kg=pick(t2, "Shipped C2"),
        shipped_spot_kg=pick(t2, "Spot sale"),
        expected_kg=pick(t2, "Expected closing"),
        measured_kg=pick(t2, "Measured closing"),
        implied_loss_kg=pick(t2, "Implied loss"),
    )

    # --- consignments ---
    ct = rows(wb, "Consignments")
    agg_cols = {}
    for j, h in enumerate(ct[0]):
        m = re.match(r"Aggregated batch \d+: (\w{3}) (\d{4})", str(h or ""))
        if m:
            agg_cols[j] = f"{m.group(2)}-{MONTHS[m.group(1)]:02d}"
    consignments, aggregated, cons_expected = [], [], {}
    for r in ct[1:]:
        if not (isinstance(r[0], str) and re.fullmatch(r"C\d+", r[0])):
            continue
        consignments.append(
            {
                "id": r[0],
                "output": CUSTOMER_OUTPUT[r[1]],
                "volume_kg": r[3],
                "ship_date": r[2].date().isoformat(),
                "target_gCO2e_per_MJ": r[4],
            }
        )
        parts = {}
        for j, month in agg_cols.items():
            if r[j]:
                aggregated.append(
                    {
                        "id": f"{r[0]}-{month}",
                        "consignment": r[0],
                        "parts": [{"batch": f"N-{month}", "kg": r[j]}],
                    }
                )
                parts[f"N-{month}"] = r[j]
        cons_expected[r[0]] = dict(
            volume_kg=r[3],
            target=r[4],
            distribution_adder=r[5],
            parts=parts,
            weighted_ghg=r[11],
            shipped_ghg=r[12],
        )

    if rule_12:
        consignments_by_id = {c["id"]: c for c in consignments}
        aggregated = _rule_12(
            h2,
            nh3,
            fed_into,
            feed,
            nh3_lines,
            nh3_ledger,
            consignments_by_id,
            cons_expected,
            superseded,
        )

    write_offs = [
        {"storage": "nh3_storage", "batch": b, "kg": v["write_off_kg"]}
        for b, v in nh3_lines.items()
        if v["write_off_kg"]
    ]
    write_offs += [
        {"storage": "h2_storage", "batch": b, "kg": kg} for b, kg in h2_write_offs.items()
    ]

    # --- minimise-mode variant (V1) ---
    vt = labelled(wb, "Variants")
    v1 = dict(
        volume_kg=pick(vt, "Volume (kg NH₃)"),
        distribution_adder=pick(vt, "Distribution adder"),
        sep_kg=pick(vt, "Sep NH₃ used"),
        q4_kg=pick(vt, "Q4 NH₃ used"),
        shipped_ghg=pick(vt, "Lowest achievable shipped GHG"),
    )

    if rule_12:
        v1 = _variant_minimise(v1, nh3_lines, superseded)

    decisions = {
        "batches": batches,
        "feed": feed,
        "consignments": consignments,
        "aggregated_batches": aggregated,
        "write_offs": write_offs,
    }
    expected = {
        "source": path.name,
        "rule_2_5_applied": rule_2_5,
        "rule_12_applied": rule_12,
        "intervals": len(body),
        "h2_batches": h2,
        "nh3_batches": nh3,
        "h2_ledger": h2_ledger,
        "h2_reconciliation": h2_recon,
        "nh3_ledger": nh3_ledger,
        "nh3_reconciliation": nh3_recon,
        "consignments": cons_expected,
        "variant_minimise": v1,
        "superseded": superseded,
    }
    return decisions, expected


def _parts(
    h2: dict[str, dict[str, Any]], bid: str, b: dict[str, Any], sources: dict[str, float]
) -> list[tuple[float, float, float, list[str], str | None]]:
    """(GHG, kg, share, sources, pool) of each part of NH₃ batch ``b``, in ascending GHG.
    Compliant H₂ of one month is one pool (§12.1); non-compliant H₂ is never averaged."""
    c_mj = b["feed_consumed_kg"] * H2_LHV
    e_mj = b["product_kg"] * NH3_LHV
    own = b["electricity_emissions_g"]
    groups: dict[tuple[str, ...], list[str]] = {}
    for src in sorted(sources):
        if h2[src]["rfnbo_kg"] > 0:
            g = h2[src]["ghg"]
            key = (
                ("pool", h2[src]["month"]) if g <= THRESHOLD + 1e-9 else ("line", repr(round(g, 9)))
            )
            groups.setdefault(key, []).append(src)
    out = []
    for key, srcs in groups.items():
        kg = sum(sources[s] for s in srcs)
        g = sum(sources[s] * h2[s]["ghg"] for s in srcs) / kg  # one LHV: energy-weighted
        share = kg * H2_LHV / c_mj
        ghg = g * c_mj / e_mj + own / e_mj + NH3_ADDER
        distinct = len({round(h2[s]["ghg"], 9) for s in srcs}) > 1
        pool = f"{bid}|{key[1]}|own" if key[0] == "pool" and distinct else None
        out.append((ghg, share * b["product_kg"], share, srcs, pool))
    return sorted(out, key=lambda x: (round(x[0], 9), x[3]))


def _write_off(lines: dict[str, dict[str, Any]], physical_kg: float) -> float:
    """Write off by the §2.7 rule: all non-compliant RFNBO, then the excess over the physical
    inventory, highest GHG first, then the latest month. Returns the total written off."""
    closing = sum(x["closing_before_kg"] for x in lines.values())
    noncompliant = sum(x["closing_before_kg"] for x in lines.values() if x["ghg"] > THRESHOLD)
    excess = max(0.0, closing - physical_kg, noncompliant)
    left = excess
    order = sorted(
        lines.items(),
        key=lambda kv: (kv[1]["ghg"] > THRESHOLD, round(kv[1]["ghg"], 9), kv[1]["month"], kv[0]),
        reverse=True,
    )
    for _b, x in order:
        take = min(x["closing_before_kg"], max(0.0, left))
        x["write_off_kg"] = take
        x["carried_kg"] = x["closing_before_kg"] - take
        left -= take
    return excess


def _rule_12(
    h2: dict[str, dict[str, Any]],
    nh3: dict[str, dict[str, Any]],
    fed_into: dict[str, dict[str, float]],
    feed: list[dict[str, Any]],
    nh3_lines: dict[str, dict[str, Any]],
    nh3_ledger: dict[str, Any],
    consignments: dict[str, dict[str, Any]],
    cons_expected: dict[str, dict[str, Any]],
    superseded: dict[str, Any],
) -> list[dict[str, Any]]:
    """Apply build plan §12 and §12.1 to the NH₃ batches (labelling pooled feed in ``feed``),
    the NH₃ ledger and the consignments; returns the re-derived aggregated batches."""
    superseded["nh3_ghg"] = {b: v["ghg"] for b, v in nh3.items()}
    superseded["nh3_ledger"] = {
        "lines": {b: dict(x) for b, x in nh3_lines.items()},
        "write_off_kg": nh3_ledger["write_off_kg"],
        "carried_ghg": nh3_ledger["carried_ghg"],
    }
    superseded["consignments"] = {
        c: {"parts": dict(e["parts"]), "weighted_ghg": e["weighted_ghg"]}
        for c, e in cons_expected.items()
    }
    lines: dict[str, dict[str, Any]] = OrderedDict(
        (b, x) for b, x in nh3_lines.items() if b not in nh3
    )
    pool_of: dict[tuple[str, str], str] = {}
    for bid, b in nh3.items():
        parts = _parts(h2, bid, b, fed_into.get(bid, {}))
        b["parts"] = {}
        for k, (ghg, kg, share, srcs, pool) in enumerate(parts, 1):
            pid = bid if len(parts) == 1 else f"{bid}.{k}"
            b["parts"][pid] = dict(
                ghg=ghg, kg=kg, share=share, compliant=ghg <= THRESHOLD + 1e-9, sources=srcs
            )
            if pool:
                b["parts"][pid]["pool"] = pool
                pool_of.update({(s, bid): pool for s in srcs})
            lines[pid] = dict(month=b["month"], ghg=ghg, in_kg=kg)
        b["ghg"] = parts[0][0] if len(parts) == 1 else None
        b["compliant"] = all(p["compliant"] for p in b["parts"].values())
    for a in feed:
        if (a["source"], a["target"]) in pool_of:
            a["pool"] = pool_of[a["source"], a["target"]]
    for x in lines.values():
        x["out_consignment_kg"] = 0.0
    aggregated = []
    for cid, c in consignments.items():
        e = cons_expected[cid]
        limit = min(e["target"] - e["distribution_adder"], THRESHOLD)
        ship_month = c["ship_date"][:7]
        avail = [
            (round(x["ghg"], 9), x["month"], pid)
            for pid, x in lines.items()
            if x["month"] <= ship_month and x["in_kg"] - x["out_consignment_kg"] > 1e-9
        ]
        order = sorted(a for a in avail if a[0] <= limit + 1e-9)
        order += sorted(a for a in avail if a[0] > limit + 1e-9)
        left, taken = float(c["volume_kg"]), {}
        for _g, _m, pid in order:
            if left <= 1e-9:
                break
            x = lines[pid]
            kg = min(left, x["in_kg"] - x["out_consignment_kg"])
            x["out_consignment_kg"] += kg
            taken[pid] = kg
            left -= kg
        e["parts"] = taken
        # One aggregated batch per production month, at its energy-weighted average (§12.1).
        e["aggregated_ghg"] = {
            month: sum(
                kg * lines[pid]["ghg"] for pid, kg in taken.items() if lines[pid]["month"] == month
            )
            / sum(kg for pid, kg in taken.items() if lines[pid]["month"] == month)
            for month in sorted({lines[pid]["month"] for pid in taken})
        }
        e["highest_ghg"] = max(e["aggregated_ghg"].values())
        e["shipped_ghg"] = e["highest_ghg"] + e["distribution_adder"]
        del e["weighted_ghg"]
        for month in sorted({lines[pid]["month"] for pid in taken}):
            aggregated.append(
                {
                    "id": f"{cid}-{month}",
                    "consignment": cid,
                    "parts": [
                        {"batch": pid, "kg": kg}
                        for pid, kg in taken.items()
                        if lines[pid]["month"] == month
                    ],
                }
            )
    for x in lines.values():
        x["closing_before_kg"] = x["in_kg"] - x["out_consignment_kg"]
    nh3_ledger["write_off_kg"] = _write_off(lines, nh3_ledger["physical_kg"])
    mj = sum(x["carried_kg"] for x in lines.values())
    nh3_ledger["carried_ghg"] = (
        sum(x["carried_kg"] * x["ghg"] for x in lines.values()) / mj if mj > 1e-6 else None
    )
    nh3_lines.clear()
    nh3_lines.update(lines)
    return aggregated


def _variant_minimise(
    v1: dict[str, Any], lines: dict[str, dict[str, Any]], superseded: dict[str, Any]
) -> dict[str, Any]:
    """V1 under §12: the lowest highest-part GHG for the volume, taking parts in ascending GHG
    (the September opening batch, then Q4 parts)."""
    superseded["variant_minimise"] = dict(v1)
    left, parts = float(v1["volume_kg"]), {}
    for pid, x in sorted(lines.items(), key=lambda kv: (round(kv[1]["ghg"], 9), kv[1]["month"])):
        if left <= 1e-9:
            break
        kg = min(left, x["in_kg"])
        parts[pid] = kg
        left -= kg
    out = dict(v1, parts=parts)
    out["sep_kg"] = sum(kg for pid, kg in parts.items() if lines[pid]["month"] == "2026-09")
    out["q4_kg"] = v1["volume_kg"] - out["sep_kg"]
    out["shipped_ghg"] = max(lines[pid]["ghg"] for pid in parts) + v1["distribution_adder"]
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("workbook", type=Path)
    p.add_argument("out_prefix")
    p.add_argument("--rule-2-5", action="store_true")
    p.add_argument("--rule-12", action="store_true")
    args = p.parse_args()
    decisions, expected = export(args.workbook, args.rule_2_5, args.rule_12)
    for suffix, obj in (("decisions", decisions), ("expected", expected)):
        Path(f"{args.out_prefix}.{suffix}.json").write_text(
            json.dumps(obj, indent=1, default=str) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
