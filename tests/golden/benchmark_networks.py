"""Performance budget on networks (build plan §10.4, phase 15).

    uv run python tests/golden/benchmark_networks.py [--profile]

Times one quarter of test plant 3 (three units) and a five-unit plant through the whole
optimiser (stage A, B and C, baseline included), and with ``--profile`` lists the functions
that take the time. ``test_performance.py`` holds test plant 3 to the 10-minute budget.

The five-unit plant is test plant 3 with a purifier between the H₂ buffer and the liquefier
(linked by a second pass-through) and NH₃ synthesis drawing on the CH₂ storage, all on the one
renewable pool.
"""

from __future__ import annotations

import argparse
import cProfile
import datetime as dt
import io
import json
import pstats
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from rfnbo.config import validate
from rfnbo.config.schema import PlantConfig
from rfnbo.data.quality import Frame
from rfnbo.model.entities import ConsignmentSpec
from rfnbo.optimise.pipeline import OptimisationResult, optimise
from tests.golden import tc3_reference as R
from tests.golden.test_tc3_workbook import SERIES

GOLDEN = Path(__file__).resolve().parent


def plant_3() -> PlantConfig:
    return PlantConfig.model_validate_json((GOLDEN / "tc3.plant.json").read_text("utf-8"))


def five_unit_plant() -> PlantConfig:
    raw: dict[str, Any] = json.loads((GOLDEN / "tc3.plant.json").read_text("utf-8"))
    raw["plant"] = "Five-unit benchmark plant"
    raw["blocks"] += [
        {
            "id": "purifier",
            "name": "Purifier",
            "kind": "processing_unit",
            "product": "H2",
            "lhv_MJ_per_kg": 120.0,
            "electricity_adds_to_lhv": False,
            "fixed_adder_gCO2e_per_MJ": 0.2,
            "production_series": "purifier_H2_kg",
            "electricity_series": "purifier_electricity_kWh",
        },
        {
            "id": "pt_liquefier",
            "name": "Direct link purifier to liquefier",
            "kind": "storage_unit",
            "product": "H2",
            "pass_through": True,
            "opening_inventory_kg": 0.0,
        },
        {
            "id": "nh3_synthesis",
            "name": "NH3 synthesis",
            "kind": "processing_unit",
            "product": "NH3",
            "lhv_MJ_per_kg": 18.6,
            "electricity_adds_to_lhv": False,
            "fixed_adder_gCO2e_per_MJ": 2.0,
            "production_series": "nh3_NH3_kg",
            "electricity_series": "nh3_electricity_kWh",
        },
        {
            "id": "nh3_storage",
            "name": "NH3 storage",
            "kind": "storage_unit",
            "product": "NH3",
            "inventory_series": "nh3_storage_inventory_kg",
            "opening_inventory_kg": 0.0,
        },
        {
            "id": "dist_customer_c",
            "name": "Distribution to Customer C",
            "kind": "distribution",
            "customer": "Customer C",
            "fixed_adder_gCO2e_per_MJ": 3.0,
        },
        {
            "id": "consignment_c",
            "name": "Consignments to Customer C",
            "kind": "consignment_output",
            "customer": "Customer C",
            "distribution_block": "dist_customer_c",
        },
    ]
    flows = [f for f in raw["flows"] if f["id"] != "f_buf_liq"]
    flows += [
        {
            "id": "f_buf_pur",
            "source": "h2_buffer",
            "target": "purifier",
            "material": "H2",
            "series": "purifier_feed_kg",
        },
        {
            "id": "f_re_pur",
            "source": "renewable_power",
            "target": "purifier",
            "material": "electricity",
        },
        {
            "id": "f_grid_pur",
            "source": "grid_power",
            "target": "purifier",
            "material": "electricity",
        },
        {"id": "f_pur_pt", "source": "purifier", "target": "pt_liquefier", "material": "H2"},
        {
            "id": "f_pt_liq",
            "source": "pt_liquefier",
            "target": "liquefier",
            "material": "H2",
            "series": "liquefier_H2_kg",
        },
        {
            "id": "f_ch2s_nh3",
            "source": "ch2_storage",
            "target": "nh3_synthesis",
            "material": "CH2",
            "series": "nh3_CH2_kg",
        },
        {
            "id": "f_re_nh3",
            "source": "renewable_power",
            "target": "nh3_synthesis",
            "material": "electricity",
        },
        {
            "id": "f_grid_nh3",
            "source": "grid_power",
            "target": "nh3_synthesis",
            "material": "electricity",
        },
        {"id": "f_nh3_nh3s", "source": "nh3_synthesis", "target": "nh3_storage", "material": "NH3"},
        {
            "id": "f_nh3s_c",
            "source": "nh3_storage",
            "target": "dist_customer_c",
            "material": "NH3",
            "series": "nh3_shipped_cc_kg",
        },
        {
            "id": "f_c_cons",
            "source": "dist_customer_c",
            "target": "consignment_c",
            "material": "NH3",
        },
    ]
    raw["flows"] = flows
    cfg = PlantConfig.model_validate(raw)
    issues = validate(cfg)
    if issues:
        raise ValueError(issues)
    return cfg


def frame_3() -> Frame:
    data = R.build_data()
    series = {n: np.asarray(data.s[r], dtype=np.float64) for n, r in SERIES.items()}
    return Frame(tuple(data.utc), series, "Europe/Amsterdam", "2026-Q4")


def frame_5() -> Frame:
    """Test case 3's data, plus the purifier (30 kg in, 30 kg out, 15 kWh) and NH₃ synthesis
    (9.79 kg CH₂ → 55 kg NH₃, 27.5 kWh per interval); CH₂ storage now also feeds NH₃."""
    data = R.build_data()
    s = {n: np.asarray(data.s[r], dtype=np.float64) for n, r in SERIES.items()}
    n = len(data)
    feed = s["liquefier_H2_kg"]
    s["purifier_feed_kg"] = feed.copy()
    s["purifier_H2_kg"] = feed.copy()
    s["purifier_electricity_kWh"] = np.where(feed > 0, 15.0, 0.0)
    s["nh3_CH2_kg"] = np.full(n, 9.79)
    s["nh3_NH3_kg"] = np.full(n, 55.0)
    s["nh3_electricity_kWh"] = np.full(n, 27.5)
    ship = np.array([60000.0 / 96 if (t.month, t.day) == (12, 22) else 0.0 for t in data.local])
    s["nh3_shipped_cc_kg"] = ship
    s["nh3_storage_inventory_kg"] = np.cumsum(s["nh3_NH3_kg"] - ship)
    # CB shrinks to what CH₂ storage can still ship: rebuild its inventory meter.
    cb = np.where(s["ch2_shipped_cb_kg"] > 0, 60000.0 / 96, 0.0)
    s["ch2_shipped_cb_kg"] = cb
    s["ch2_storage_inventory_kg"] = 500.0 + np.cumsum(s["compressor_CH2_kg"] - cb - 9.79)
    return Frame(tuple(data.utc), s, "Europe/Amsterdam", "2026-Q4")


def consignments(five: bool) -> list[ConsignmentSpec]:
    out = [
        ConsignmentSpec(
            id="CA",
            output="consignment_a",
            volume_kg=150000,
            ship_date=dt.date(2026, 12, 15),
            target_gCO2e_per_MJ=5.0,
        ),
        ConsignmentSpec(
            id="CB",
            output="consignment_b",
            volume_kg=60000 if five else 120000,
            ship_date=dt.date(2026, 12, 20),
            target_gCO2e_per_MJ=4.0,
        ),
    ]
    if five:
        out.append(
            ConsignmentSpec(
                id="CC",
                output="consignment_c",
                volume_kg=60000,
                ship_date=dt.date(2026, 12, 22),
                target_gCO2e_per_MJ=20.0,
            )
        )
    return out


def run(name: str) -> tuple[OptimisationResult, float]:
    five = name == "five_unit"
    cfg, frame = (five_unit_plant(), frame_5()) if five else (plant_3(), frame_3())
    start = time.perf_counter()
    res = optimise(cfg, frame, consignments(five))
    return res, time.perf_counter() - start


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile", action="store_true")
    args = ap.parse_args()
    for name in ("plant_3", "five_unit"):
        prof = cProfile.Profile() if args.profile else None
        if prof:
            prof.enable()
        res, secs = run(name)
        if prof:
            prof.disable()
        o = res.optimised
        units = len({b.unit for b in o.run.batches.values()})
        print(
            f"{name}: {secs:,.1f} s, {units} units, {len(o.run.batches)} batches, "
            f"{len(o.run.failures)} failed checks, "
            f"consignments met {o.objectives.consignments_met}, "
            f"carried value {o.objectives.carried_value_MJ:,.0f} MJ "
            f"(bound gap {res.value_bound().gap:.4%})"
        )
        if prof:
            out = io.StringIO()
            pstats.Stats(prof, stream=out).sort_stats("cumulative").print_stats(15)
            print(out.getvalue())


if __name__ == "__main__":
    main()
