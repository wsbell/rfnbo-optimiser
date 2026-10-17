"""Export a test workbook's decision set and expected values to JSON fixtures.

    uv run python tests/golden/export_workbook.py <workbook.xlsx> <out-prefix> [--rule-2-5]

Writes ``<out-prefix>.decisions.json`` and ``<out-prefix>.expected.json``. Values are read from
the workbook's cached results; nothing here uses the calculator.

``--rule-2-5`` regenerates what the non-compliant RFNBO rule (build plan §2.5) changes: an
electrolyser sub-batch above 28.2 gCO2e/MJ is still RFNBO and enters the H2 ledger, so its RFNBO
kg and the H2 ledger and write-off are recomputed here, independently of the app. Non-compliant
RFNBO cannot be carried into the next quarter (decided 2026-10-17), so all of it left at quarter
end is written off, ahead of compliant batches. Workbook values that change are kept under
``superseded``.
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


def export(path: Path, rule_2_5: bool) -> tuple[dict[str, Any], dict[str, Any]]:
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
    for r in feed_rows[1:]:
        if not (isinstance(r[0], str) and r[0].startswith("E-")):
            continue
        for j, target in targets.items():
            if r[j]:
                feed.append({"source": r[0], "target": target, "kg": r[j]})
                fed[r[0]] = fed.get(r[0], 0.0) + r[j]

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


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("workbook", type=Path)
    p.add_argument("out_prefix")
    p.add_argument("--rule-2-5", action="store_true")
    args = p.parse_args()
    decisions, expected = export(args.workbook, args.rule_2_5)
    for suffix, obj in (("decisions", decisions), ("expected", expected)):
        Path(f"{args.out_prefix}.{suffix}.json").write_text(
            json.dumps(obj, indent=1, default=str) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
