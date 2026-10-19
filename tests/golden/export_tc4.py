"""Export test case 4's decision set and expected values to JSON fixtures.

    uv run python tests/golden/export_tc4.py [workbook.xlsx] [out-prefix]

Writes ``<out-prefix>.decisions.json`` and ``<out-prefix>.expected.json`` (default
``tests/golden/tc4``). Values are read from the workbook's cached results; nothing here uses
the calculator. The decision set is test case 3's, plus the PoS records (inputs) and the
credentials allocated to the supplier's deliveries.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import openpyxl

from tests.golden.export_tc3 import decision_set, labelled, num, rows, table, write

HERE = Path(__file__).parent
WORKBOOK = HERE / "workbooks" / "RFNBO_optimiser_test4_purchased_Q4_2026.xlsx"


def export(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    decisions = decision_set(wb)
    register = rows(wb, "PoS")[1:4]
    decisions["pos"] = [
        {
            "id": r[0],
            "supplier": r[1],
            "product": r[2],
            "dispatch_date": r[3].date().isoformat(),
            "quantity_kg": num(r[4]),
            "ghg_gCO2e_per_MJ": num(r[5]),
        }
        for r in register
    ]
    decisions["pos_allocations"] = [
        {"pos": r[0], "flow": r[1], "batch": r[2], "kg": num(r[4])}
        for r in rows(wb, "PoS_Alloc")[1:5]
        if r[4] and r[4] > 0
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
        "storage_feed_kg",
        "supply_kg",
        "storage_rfnbo_feed_kg",
        "pos_feed_kg",
        "storage_feed_emissions_g",
        "pos_emissions_g",
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
    for storage, sheet in (("h2_buffer", "Buffer_Ledger"), ("pt_compressor", "PT_Ledger")):
        lab = labelled(wb, sheet)
        ledgers[storage] = {
            "in_kg": num(lab["RFNBO H₂ in during the quarter (kg)"]),
            "out_feed_kg": num(lab["RFNBO H₂ fed downstream (kg)"]),
            "physical_kg": num(lab["Physical inventory at quarter end (kg)"]),
            "write_off_kg": num(lab["Write-off (kg)"]),
            "carried_kg": num(lab["RFNBO H₂ carried to Q1 2027 (kg)"]),
        }
    for storage, sheet in (("lh2_storage", "LH2_Ledger"), ("ch2_storage", "CH2_Ledger")):
        lab = labelled(wb, sheet)
        ledgers[storage] = {
            "lines": table(wb, sheet, ledger_keys),
            "measured_kg": num(lab["Measured physical inventory at quarter end (kg)"]),
        }
    pos_keys = [
        "supplier",
        "product",
        None,
        "quantity_kg",
        "ghg",
        "quarter",
        "in_quarter",
        "compliant",
        "allocated_kg",
        "written_off_kg",
        "within_quantity",
        "dispatch_month",
    ]
    pos = table(wb, "PoS", pos_keys)
    lab = labelled(wb, "PoS")
    supplier = {
        "metered_kg": num(lab["Metered delivery in the quarter (kg)"]),
        "pos_kg": num(lab["PoS quantity dispatched in the quarter (kg)"]),
        "allocated_kg": num(lab["Credentials allocated (kg)"]),
        "non_rfnbo_kg": num(lab["Delivered without credentials: non-RFNBO (kg)"]),
        "written_off_kg": num(lab["PoS quantity written off, not allocated (kg)"]),
    }
    cons = {
        r[0]: {"weighted_ghg": num(r[12]), "shipped_ghg": num(r[13])}
        for r in rows(wb, "Consignments")[1:3]
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
        "carried_value_MJ": num(obj["2. Compliant RFNBO carried forward (MJ of final product)"]),
        "carried_emissions_g": num(obj["3. Carried emissions (gCO₂e)"]),
        "batches": num(obj["4. Batches"]),
        "purchased_emissions_g": num(obj["Tie-break: purchased emissions allocated (gCO₂e)"]),
        "pos_written_off_kg": num(obj["PoS quantity written off, not allocated (kg)"]),
        "at_upper_bound": obj["Objective 2 at its upper bound?"],
    }
    v = rows(wb, "Variants")

    def at(row: int) -> Any:
        return num(v[row - 1][1])

    variants = {
        "v1_spot": {
            "note": "Test case 3's 60,000 kg LH2 spot sale is back: LH2 storage is over its "
            "inventory, so no credentials go to the liquefier.",
            "lh2_measured_kg": at(4),
            "december_lh2_rfnbo_kg": at(6),
            "lh2_write_off_kg": at(8),
            "lh2_carried_kg": at(9),
            "liquefier_credentials_kg": at(11),
            "compressor_credentials_kg": at(12),
            "pos_written_off_kg": at(13),
            "h2_buffer_carried_kg": at(14),
            "ch2_carried_kg": at(15),
            "carried_value_MJ": at(16),
        }
    }
    checks = rows(wb, "Checks")
    all_pass = next(r[4] for r in checks if r[0] == "All checks pass?")
    expected = {
        "source": path.name,
        "liquefier_batches": table(wb, "L_Batches", u_keys),
        "compressor_batches": table(wb, "C_Batches", u_keys),
        "ledgers": ledgers,
        "pos": pos,
        "supplier": supplier,
        "consignments": cons,
        "objectives": objectives,
        "variants": variants,
        "workbook_checks_pass": all_pass,
    }
    return decisions, expected


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("workbook", nargs="?", type=Path, default=WORKBOOK)
    ap.add_argument("prefix", nargs="?", type=Path, default=HERE / "tc4")
    args = ap.parse_args()
    decisions, expected = export(args.workbook)
    write(args.prefix.with_name(args.prefix.name + ".decisions.json"), decisions)
    write(args.prefix.with_name(args.prefix.name + ".expected.json"), expected)


if __name__ == "__main__":
    main()
