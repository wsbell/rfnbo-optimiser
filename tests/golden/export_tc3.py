"""Export test case 3's decision set and expected values to JSON fixtures.

    uv run python tests/golden/export_tc3.py [workbook.xlsx] [out-prefix]

Writes ``<out-prefix>.decisions.json`` and ``<out-prefix>.expected.json`` (default
``tests/golden/tc3``). Values are read from the workbook's cached results; nothing here uses
the calculator. The decision set extends today's format with ``output_allocations`` (rule 4:
RFNBO of a batch assigned to each of its metered output flows); phase 12 adds it to the model.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

import openpyxl

HERE = Path(__file__).parent
WORKBOOK = HERE / "workbooks" / "RFNBO_optimiser_test3_multistep_Q4_2026.xlsx"
STEP = dt.timedelta(minutes=15)
UNIT_OF_COLUMN = {
    "Expected electrolyser batch ID": "electrolyser",
    "Expected liquefier batch ID": "liquefier",
    "Expected compressor batch ID": "compressor",
}
ALLOC_OF_COLUMN = {
    "Renewable to electrolyser (kWh)": "electrolyser",
    "Renewable to liquefier (kWh)": "liquefier",
    "Renewable to compressor (kWh)": "compressor",
}
FLOW = {"to_compressor": "f_el_pt", "to_buffer": "f_el_buf"}
OUTPUT = {"CA": "consignment_a", "CB": "consignment_b"}
STORAGE_SHEET = {
    "h2_buffer": "Buffer_Ledger",
    "pt_compressor": "PT_Ledger",
    "lh2_storage": "LH2_Ledger",
    "ch2_storage": "CH2_Ledger",
}


def rows(wb: Any, sheet: str) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in wb[sheet].iter_rows(values_only=True)]


def labelled(wb: Any, sheet: str) -> dict[str, Any]:
    return {str(r[0]).strip(): r[1] for r in rows(wb, sheet) if r and r[0] is not None}


def num(x: Any) -> Any:
    """Cached numbers as written; whole floats as ints, like the other fixtures."""
    if isinstance(x, float) and x.is_integer():
        return int(x)
    return x


def iso(ts: dt.datetime) -> str:
    return ts.replace(tzinfo=dt.UTC).isoformat()


def table(
    wb: Any, sheet: str, keys: list[str | None], first: int = 2, n: int = 3
) -> dict[str, Any]:
    """Rows ``first`` .. ``first + n - 1`` keyed by column A; columns named by ``keys``."""
    out: dict[str, Any] = {}
    for r in rows(wb, sheet)[first - 1 : first - 1 + n]:
        out[str(r[0])] = {k: num(v) for k, v in zip(keys, r[1:], strict=False) if k}
    return out


def decision_set(wb: Any) -> dict[str, Any]:
    """The decision set on the workbook's green cells (test case 3's sheet layout)."""
    data = rows(wb, "Data_15min")
    head = list(data[0])
    body = [r for r in data[1:] if r[0] is not None]
    stamps = [r[1] for r in body]

    batches: list[dict[str, Any]] = []
    for label, unit in UNIT_OF_COLUMN.items():
        j = head.index(label)
        current: dict[str, Any] | None = None
        for ts, r in zip(stamps, body, strict=True):
            bid = r[j]
            if current and current["id"] == bid and current["_end"] == ts:
                current["_end"] = ts + STEP
                continue
            if any(b["id"] == bid for b in batches):
                raise ValueError(f"batch {bid} is not contiguous")
            current = {"id": bid, "unit": unit, "_start": ts, "_end": ts + STEP}
            batches.append(current)
    for b in batches:
        b["start"], b["end"] = iso(b.pop("_start")), iso(b.pop("_end"))

    renewable = [
        {"unit": unit, "kWh": [num(r[head.index(label)]) for r in body]}
        for label, unit in ALLOC_OF_COLUMN.items()
    ]
    outputs = []
    for r in rows(wb, "E_Split")[1:4]:
        for flow, v in (("to_compressor", r[4]), ("to_buffer", r[5])):
            if v and v > 0:
                outputs.append({"batch": r[0], "flow": FLOW[flow], "kg": num(v)})
    feed = []
    for sheet in ("Buffer_Ledger", "PT_Ledger"):
        sh = rows(wb, sheet)
        targets = [str(h).removeprefix("→ ").removesuffix(" (kg)") for h in sh[0][3:6]]
        for r in sh[1:4]:
            for t, v in zip(targets, r[3:6], strict=True):
                if v and v > 0:
                    feed.append({"source": r[0], "target": t, "kg": num(v)})
    cons_rows = rows(wb, "Consignments")
    consignments = [
        {
            "id": r[0],
            "output": OUTPUT[r[0]],
            "volume_kg": num(r[5]),
            "ship_date": r[4].date().isoformat(),
            "target_gCO2e_per_MJ": num(r[6]),
        }
        for r in cons_rows[1:3]
    ]
    aggregated = [
        {"id": r[0], "consignment": r[1], "parts": [{"batch": r[3], "kg": num(r[4])}]}
        for r in cons_rows[5:10]
        if r[4] and r[4] > 0
    ]
    write_offs = []
    for storage, sheet in STORAGE_SHEET.items():
        if storage in ("h2_buffer", "pt_compressor"):
            if labelled(wb, sheet)["Write-off (kg)"] > 1e-9:
                raise ValueError(f"{sheet}: write-off by batch is not tabulated")
            continue
        for r in rows(wb, sheet)[1:4]:
            if r[6] and r[6] > 1e-9:
                write_offs.append({"storage": storage, "batch": r[0], "kg": num(r[6])})
    return {
        "batches": batches,
        "renewable_allocations": renewable,
        "output_allocations": outputs,
        "feed": feed,
        "consignments": consignments,
        "aggregated_batches": aggregated,
        "write_offs": write_offs,
    }


def export(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    decisions = decision_set(wb)
    body = [r for r in rows(wb, "Data_15min")[1:] if r[0] is not None]

    e_keys = [
        "month",
        None,
        None,
        "intervals",
        "renewable_allocated_kWh",
        "electricity_kWh",
        "renewable_matched_kWh",
        "rfnbo_share",
        "product_kg",
        "energy_MJ",
        "water_emissions_g",
        "grid_emissions_g",
        "ghg",
        "compliant",
        "rfnbo_kg",
        "rfnbo_MJ",
    ]
    split_keys = [
        "rfnbo_kg",
        "metered_to_buffer_kg",
        "metered_to_compressor_kg",
        "assigned_to_compressor_kg",
        "assigned_to_buffer_kg",
    ]
    u_keys = [
        "month",
        "feed_consumed_kg",
        "rfnbo_feed_kg",
        "feed_ghg",
        "rfnbo_share",
        "product_kg",
        "loss_kg",
        "loss_fraction",
        "renewable_allocated_kWh",
        "electricity_kWh",
        "renewable_matched_kWh",
        "own_emissions_g",
        "feed_emissions_g",
        "rfnbo_kg",
        "rfnbo_MJ",
        "ghg",
        "compliant",
        "intervals",
    ]
    ledger_keys = [
        "month",
        "ghg",
        "in_kg",
        "out_consignment_kg",
        "closing_before_kg",
        "write_off_kg",
        "carried_kg",
    ]
    ledgers: dict[str, Any] = {}
    for storage, sheet in STORAGE_SHEET.items():
        lab = labelled(wb, sheet)
        if storage in ("h2_buffer", "pt_compressor"):
            sh = rows(wb, sheet)
            targets = [str(h).removeprefix("→ ").removesuffix(" (kg)") for h in sh[0][3:6]]
            ledgers[storage] = {
                "opening_kg": num(lab["Opening RFNBO ledger (kg)"]),
                "in_kg": num(lab["RFNBO H₂ in during the quarter (kg)"]),
                "out_feed_kg": num(lab["RFNBO H₂ fed downstream (kg)"]),
                "closing_before_kg": num(lab["Closing RFNBO ledger before write-off (kg)"]),
                "physical_kg": num(lab["Physical inventory at quarter end (kg)"]),
                "write_off_kg": num(lab["Write-off (kg)"]),
                "carried_kg": num(lab["RFNBO H₂ carried to Q1 2027 (kg)"]),
                "carried_ghg": num(lab["Energy-weighted GHG of carried H₂ (gCO₂e/MJ)"]),
                "left_by_batch_kg": {r[0]: num(r[6]) for r in sh[1:4]},
                "feed_ghg": {t: num(v) for t, v in zip(targets, sh[5][3:6], strict=True)},
            }
        else:
            ledgers[storage] = {
                "lines": table(wb, sheet, ledger_keys),
                "measured_kg": num(lab["Measured physical inventory at quarter end (kg)"]),
                "shortfall_kg": num(
                    lab["Shortfall to write off (kg): over inventory, and all non-compliant"]
                ),
                "carried_ghg": num(lab["Energy-weighted GHG of carried inventory (gCO₂e/MJ)"]),
            }
    recon: dict[str, Any] = {}
    for storage in ("h2_buffer", "lh2_storage", "ch2_storage"):
        lab = labelled(wb, STORAGE_SHEET[storage])
        recon[storage] = {
            "expected_kg": num(lab["Expected closing (kg)"]),
            "measured_kg": num(lab["Measured closing (kg)"]),
            "implied_loss_kg": num(lab["Implied loss (kg)"]),
        }
    cons_keys = [
        "customer",
        "product",
        "storage",
        None,
        "volume_kg",
        "target",
        "distribution_adder",
        "limit",
        "kg_2026_10",
        "kg_2026_11",
        "kg_2026_12",
        "highest_ghg",
        "shipped_ghg",
        "meets_target",
        "within_threshold",
        "compliant_parts_only",
        "volume_met",
    ]
    cons = table(wb, "Consignments", cons_keys, n=2)
    for c in cons.values():
        c["parts_by_month"] = {
            m: c.pop(f"kg_{m.replace('-', '_')}") for m in ("2026-10", "2026-11", "2026-12")
        }
    obj = labelled(wb, "Objectives")
    objectives = {
        "consignments_met": num(obj["1. Consignments met"]),
        "weights": {
            "h2_buffer": num(obj["Storage weight: H₂ buffer (MJ final product per MJ)"]),
            "pt_compressor": num(obj["Storage weight: pass-through"]),
            "lh2_storage": num(obj["Storage weight: LH₂ storage"]),
            "ch2_storage": num(obj["Storage weight: CH₂ storage"]),
        },
        "stored_MJ_by_storage": {
            "h2_buffer": num(obj["Compliant RFNBO carried: H₂ buffer (MJ)"]),
            "pt_compressor": num(obj["Compliant RFNBO carried: pass-through (MJ)"]),
            "lh2_storage": num(obj["Compliant RFNBO carried: LH₂ storage (MJ)"]),
            "ch2_storage": num(obj["Compliant RFNBO carried: CH₂ storage (MJ)"]),
        },
        "carried_value_MJ": num(obj["2. Compliant RFNBO carried forward (MJ of final product)"]),
        "carried_emissions_g": num(obj["3. Carried emissions (gCO₂e)"]),
        "batches": num(obj["4. Batches"]),
        "at_upper_bound": obj["Objective 2 at its upper bound?"],
    }
    v = {r[0]: r[1] for r in rows(wb, "Variants")[1:] if r[0]}
    vr = rows(wb, "Variants")

    def at(row: int) -> Any:
        return num(vr[row - 1][1])

    variants = {
        "v1_hourly": {
            "note": "Same data re-dated to Q4 2030; electrolyser batches within clock hours.",
            "electrolyser_batches": at(3),
            "zero_share_batches": at(4),
            "conditioning_batches": at(5),
            "rfnbo_h2_kg": at(6),
            "rfnbo_to_buffer_kg": at(7),
            "rfnbo_to_pass_through_kg": at(8),
            "liquefier_rfnbo_feed_kg": at(9),
            "compressor_rfnbo_feed_kg": at(10),
            "rfnbo_lh2_kg": at(11),
            "lh2_carried_kg": at(12),
            "ch2_carried_kg": at(13),
            "carried_value_MJ": at(14),
        },
        "v2_shortfall": {
            "note": "December renewable 2,500 kWh per interval and no LH2 spot sale. "
            "Non-compliant RFNBO cannot be carried, so the liquefier gets enough renewable "
            "(whole intervals) for December LH2 to comply. December CH2 is above CB's limit and "
            "is not averaged down (§12), so CB is not met.",
            "december_renewable_per_interval_kWh": at(17),
            "lh2_ghg_without_renewable": at(24),
            "least_liquefier_renewable_kWh": at(25),
            "rejected_all_to_electrolyser": {
                "liquefier_renewable_kWh": at(27),
                "december_rfnbo_h2_kg": at(28),
                "december_lh2_ghg": at(29),
                "lh2_carried_kg": at(30),
                "ch2_carried_kg": at(31),
                "carried_value_MJ": at(32),
                "december_lh2_written_off_kg": at(33),
            },
            "zero_share_intervals": at(35),
            "part_interval_electrolyser_kWh": at(36),
            "liquefier_renewable_kWh": at(37),
            "december_rfnbo_h2_kg": at(38),
            "december_lh2_ghg": at(39),
            "liquefier_rfnbo_feed_kg": at(41),
            "compressor_rfnbo_feed_kg": at(42),
            "cb_limit": at(43),
            "december_ch2_ghg": at(44),
            "lh2_carried_kg": at(45),
            "ch2_carried_kg": at(46),
            "h2_buffer_carried_kg": at(47),
            "carried_value_MJ": at(48),
            "beats_rejected": v["Better than the rejected alternative?"],
            "cb_met": v["CB met?"],
        },
        "v3_infeasible": {
            "ca_target": at(52),
            "limit": at(53),
            "lowest_lh2_ghg": at(54),
            "ca_feasible": v["CA feasible?"],
            "ca_max_volume_kg": at(56),
            "cb_met": v["CB still met?"],
            "lh2_write_off_kg": at(58),
            "consignments_met": at(59),
            "carried_value_MJ": at(60),
        },
    }
    checks = rows(wb, "Checks")
    all_pass = next(r[4] for r in checks if r[0] == "All checks pass?")
    expected = {
        "source": path.name,
        "intervals": sum(1 for r in body if isinstance(r[0], (int, float))),
        "electrolyser_batches": table(wb, "E_Batches", e_keys),
        "output_split": table(wb, "E_Split", split_keys),
        "liquefier_batches": table(wb, "L_Batches", u_keys),
        "compressor_batches": table(wb, "C_Batches", u_keys),
        "ledgers": ledgers,
        "reconciliation": recon,
        "consignments": cons,
        "objectives": objectives,
        "variants": variants,
        "workbook_checks_pass": all_pass,
    }
    return decisions, expected


def write(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("workbook", nargs="?", type=Path, default=WORKBOOK)
    ap.add_argument("prefix", nargs="?", type=Path, default=HERE / "tc3")
    args = ap.parse_args()
    decisions, expected = export(args.workbook)
    write(args.prefix.with_name(args.prefix.name + ".decisions.json"), decisions)
    write(args.prefix.with_name(args.prefix.name + ".expected.json"), expected)


if __name__ == "__main__":
    main()
