"""Calculator on small synthetic datasets, hand-calculated.

Covers what the workbooks do not: renewable split between units (test 2), short RFNBO feed
(test 12), non-compliant RFNBO saved by blending (§2.5), H₂ write-off, hourly correlation.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any

import numpy as np
import pytest

from rfnbo.calc import CalculationError, RunResult, calculate
from rfnbo.calc.ledger import rule_write_offs
from rfnbo.config import load_config
from rfnbo.config.schema import PlantConfig
from rfnbo.data.quality import Frame, required_series
from rfnbo.model.entities import Decisions
from tests.conftest import EXAMPLE

UTC = dt.UTC
Q = dt.timedelta(minutes=15)
# 1 Oct 2026 00:00 Europe/Amsterdam
T0 = dt.datetime(2026, 9, 30, 22, 0, tzinfo=UTC)
BIG = 1e12  # physical inventory large enough that nothing is written off


def make_frame(
    cfg: PlantConfig,
    columns: dict[str, list[float]],
    start: dt.datetime = T0,
    quarter: str = "2026-Q4",
) -> Frame:
    n = len(next(iter(columns.values())))
    series = {name: np.zeros(n) for name in required_series(cfg)}
    series["h2_storage_inventory_kg"] = np.full(n, BIG)
    series["nh3_storage_inventory_kg"] = np.full(n, BIG)
    for name, values in columns.items():
        assert len(values) == n
        series[name] = np.asarray(values, dtype=np.float64)
    return Frame(tuple(start + i * Q for i in range(n)), series, cfg.timezone, quarter)


def at(i: int, start: dt.datetime = T0) -> str:
    return (start + i * Q).isoformat()


def batch(bid: str, unit: str, s: int, e: int, start: dt.datetime = T0) -> dict[str, Any]:
    return {"id": bid, "unit": unit, "start": at(s, start), "end": at(e, start)}


@pytest.fixture
def cfg() -> PlantConfig:
    return load_config(EXAMPLE)


# Electrolyser: 4 intervals of 2,500 kWh, 500 kg water, 50 kg H₂. Renewable covers the
# first three fully and 1,000 kWh of the fourth.
ELECTROLYSER = {
    "renewable_available_kWh": [2500, 2500, 2500, 1000],
    "electrolyser_electricity_kWh": [2500] * 4,
    "electrolyser_water_kg": [500] * 4,
    "electrolyser_H2_kg": [50] * 4,
}
# NH₃ synthesis runs only in the last interval: 170 kg H₂ → 1,000 kg NH₃, 500 kWh grid.
NH3 = {
    "nh3_synthesis_H2_kg": [0, 0, 0, 170],
    "nh3_synthesis_NH3_kg": [0, 0, 0, 1000],
    "nh3_synthesis_electricity_kWh": [0, 0, 0, 500],
}
E_BATCHES = [batch("E1", "electrolyser", 0, 3), batch("E2", "electrolyser", 3, 4)]
N_BATCH = [batch("N1", "nh3_synthesis", 0, 4)]


def run(cfg: PlantConfig, columns: dict[str, list[float]], **decisions: Any) -> RunResult:
    return calculate(cfg, make_frame(cfg, columns), Decisions.model_validate(decisions))


def test_electrolyser_within_batch_matching(cfg: PlantConfig) -> None:
    one = run(cfg, ELECTROLYSER, batches=[batch("E", "electrolyser", 0, 4), *N_BATCH])
    b = one.batches["E"]
    # matched = min(8,500, 10,000); grid 1,500 kWh x 400 + water 4,000 g over 24,000 MJ, + 1.0
    assert b.renewable_matched_kWh == 8500
    assert b.rfnbo_share == pytest.approx(0.85)
    assert b.ghg_gCO2e_per_MJ == pytest.approx((1500 * 400 + 4000) / 24000 + 1.0)
    # 26.17 gCO₂e/MJ: averaging over the batch keeps all 170 kg RFNBO H₂ compliant.
    assert b.compliant and b.rfnbo_kg == pytest.approx(170)

    two = run(cfg, ELECTROLYSER, batches=[*E_BATCHES, *N_BATCH])
    e1, e2 = two.batches["E1"], two.batches["E2"]
    assert e1.rfnbo_share == 1 and e1.ghg_gCO2e_per_MJ == pytest.approx(3000 / 18000 + 1)
    assert e2.rfnbo_share == pytest.approx(0.4)
    assert e2.ghg_gCO2e_per_MJ == pytest.approx((1500 * 400 + 500 * 2) / 6000 + 1)
    assert e2.is_rfnbo and not e2.compliant and e2.rfnbo_kg == pytest.approx(20)


def test_dark_interval_is_not_rfnbo(cfg: PlantConfig) -> None:
    cols = dict(ELECTROLYSER, renewable_available_kWh=[2500, 2500, 2500, 0])
    b = run(cfg, cols, batches=[*E_BATCHES, *N_BATCH]).batches["E2"]
    assert b.rfnbo_share == 0 and not b.is_rfnbo and b.rfnbo_kg == 0


def test_short_rfnbo_feed_prorates_own_emissions(cfg: PlantConfig) -> None:
    """Acceptance test 12: only 150 of 170 kg H₂ feed is RFNBO."""
    r = run(
        cfg,
        {**ELECTROLYSER, **NH3},
        batches=[*E_BATCHES, *N_BATCH],
        feed=[{"source": "E1", "target": "N1", "kg": 150}],
        # E2's 20 kg of non-compliant RFNBO H₂ is not fed, so it cannot be carried.
        write_offs=[{"storage": "h2_storage", "batch": "E2", "kg": 20}],
    )
    n = r.batches["N1"]
    share = 150 / 170
    feed_g = 150 * 120 * (3000 / 18000 + 1)
    own_g = 500 * 400
    assert n.rfnbo_share == pytest.approx(share)
    assert n.prorated_own_emissions_g == pytest.approx(share * own_g)
    assert n.ghg_gCO2e_per_MJ == pytest.approx(
        (feed_g + share * own_g) / (share * 1000 * 18.6) + 2.0
    )
    assert n.rfnbo_kg == pytest.approx(share * 1000)
    assert r.passed, r.failures


def test_noncompliant_rfnbo_saved_by_blending(cfg: PlantConfig) -> None:
    """§2.5: a 101 gCO₂e/MJ RFNBO H₂ batch blends into compliant NH₃."""
    r = run(
        cfg,
        {**ELECTROLYSER, **NH3},
        batches=[*E_BATCHES, *N_BATCH],
        feed=[
            {"source": "E1", "target": "N1", "kg": 150},
            {"source": "E2", "target": "N1", "kg": 20},
        ],
    )
    n = r.batches["N1"]
    feed_g = 150 * 120 * (3000 / 18000 + 1) + 20 * 120 * ((600000 + 1000) / 6000 + 1)
    assert n.rfnbo_share == pytest.approx(1.0)
    assert n.ghg_gCO2e_per_MJ == pytest.approx((feed_g + 200000) / 18600 + 2.0)
    assert n.compliant and n.rfnbo_kg == pytest.approx(1000)
    assert r.passed, r.failures


def test_h2_write_off_highest_ghg_first(cfg: PlantConfig) -> None:
    cols = {**ELECTROLYSER, **NH3, "h2_storage_inventory_kg": [BIG, BIG, BIG, 5.0]}
    base = dict(batches=[*E_BATCHES, *N_BATCH], feed=[{"source": "E1", "target": "N1", "kg": 140}])
    # Closing ledger: E1 10 kg + E2 20 kg = 30 kg against 5 kg measured: write off 25 kg.
    assert rule_write_offs([("E1", 1.17, "2026-10", 10), ("E2", 101.2, "2026-10", 20)], 25) == {
        "E2": 20,
        "E1": 5,
    }
    good = run(
        cfg,
        cols,
        **base,
        write_offs=[
            {"storage": "h2_storage", "batch": "E2", "kg": 20},
            {"storage": "h2_storage", "batch": "E1", "kg": 5},
        ],
    )
    assert good.passed, good.failures
    led = good.ledger("h2_storage")
    assert led.carried_kg == pytest.approx(5) and led.carried_ghg_gCO2e_per_MJ == pytest.approx(
        3000 / 18000 + 1
    )

    wrong_order = run(
        cfg,
        cols,
        **base,
        write_offs=[
            {"storage": "h2_storage", "batch": "E1", "kg": 10},
            {"storage": "h2_storage", "batch": "E2", "kg": 15},
        ],
    )
    assert {f.name for f in wrong_order.failures} == {"write_off_order", "noncompliant_not_carried"}

    none = run(cfg, cols, **base)
    assert {f.name for f in none.failures} == {
        "write_off_amount",
        "ledger_within_physical",
        "noncompliant_not_carried",
    }


def test_noncompliant_rfnbo_is_never_carried(cfg: PlantConfig) -> None:
    """Decided 2026-10-17: non-compliant RFNBO left at quarter end is written off in full, even
    when measured inventory has room for it."""
    cols = {**ELECTROLYSER, **NH3, "h2_storage_inventory_kg": [BIG] * 4}
    base = dict(batches=[*E_BATCHES, *N_BATCH], feed=[{"source": "E1", "target": "N1", "kg": 140}])
    lines = [("E1", 1.17, "2026-10", 10.0), ("E2", 101.2, "2026-10", 20.0)]
    assert rule_write_offs(lines, 20, noncompliant=["E2"]) == {"E2": 20}
    kept = run(cfg, cols, **base)
    assert {f.name for f in kept.failures} == {"write_off_amount", "noncompliant_not_carried"}
    led = kept.ledger("h2_storage")
    assert led.excess_kg == 0 and led.required_write_off_kg == pytest.approx(20)
    good = run(cfg, cols, **base, write_offs=[{"storage": "h2_storage", "batch": "E2", "kg": 20}])
    assert good.passed, good.failures
    assert good.ledger("h2_storage").carried_kg == pytest.approx(10)


def test_noncompliant_goes_first_even_on_a_ghg_tie() -> None:
    lines = [("A", 28.2, "2026-12", 10.0), ("B", 28.2, "2026-10", 10.0)]
    assert rule_write_offs(lines, 10, noncompliant=["B"]) == {"B": 10}


def test_write_off_ties_take_newest_month_first() -> None:
    lines = [
        ("A", 14.09, "2026-10", 100.0),
        ("B", 14.09, "2026-11", 100.0),
        ("C", 6.0, "2026-09", 100.0),
    ]
    assert rule_write_offs(lines, 150) == {"B": 100, "A": 50}


def test_renewable_split_between_units(cfg: PlantConfig) -> None:
    """Acceptance test 2: one renewable pool shared by two units."""
    raw = cfg.model_dump(mode="json")
    raw["flows"].append(
        {
            "id": "f_re_nh3",
            "source": "renewable_power",
            "target": "nh3_synthesis",
            "material": "electricity",
        }
    )
    shared = PlantConfig.model_validate(raw)
    cols = {**ELECTROLYSER, **NH3, "renewable_available_kWh": [3000, 3000, 2500, 1000]}
    batches = [*E_BATCHES, *N_BATCH]
    with pytest.raises(CalculationError, match="allocate"):
        run(shared, cols, batches=batches)

    alloc = [
        {"unit": "electrolyser", "kWh": [2500, 2500, 2500, 800]},
        {"unit": "nh3_synthesis", "kWh": [500, 500, 0, 200]},
    ]
    r = run(
        shared,
        cols,
        batches=batches,
        renewable_allocations=alloc,
        feed=[{"source": "E1", "target": "N1", "kg": 150}],
        write_offs=[{"storage": "h2_storage", "batch": "E2", "kg": 16}],
    )
    assert r.passed, r.failures
    assert r.batches["E2"].rfnbo_share == pytest.approx(800 / 2500)
    n = r.batches["N1"]
    # NH₃ matches 500 of its 500 kWh within the batch: zero electricity emissions, share
    # still set by RFNBO feed only.
    assert n.renewable_matched_kWh == 500 and n.nonrenewable_kWh == 0
    assert n.rfnbo_share == pytest.approx(150 / 170)

    over = [
        {"unit": "electrolyser", "kWh": [2500, 2500, 2500, 1000]},
        {"unit": "nh3_synthesis", "kWh": [500, 500, 0, 200]},
    ]
    bad = run(
        shared,
        cols,
        batches=batches,
        renewable_allocations=over,
        feed=[{"source": "E1", "target": "N1", "kg": 150}],
        write_offs=[{"storage": "h2_storage", "batch": "E2", "kg": 20}],
    )
    assert [f.name for f in bad.failures] == ["renewable_pool"]


def test_batches_must_cover_each_unit(cfg: PlantConfig) -> None:
    lit = dict(ELECTROLYSER, renewable_available_kWh=[2500] * 4)  # every batch compliant
    gap = run(
        cfg,
        lit,
        batches=[batch("E1", "electrolyser", 0, 2), batch("E2", "electrolyser", 3, 4), *N_BATCH],
    )
    overlap = run(
        cfg,
        lit,
        batches=[batch("E1", "electrolyser", 0, 3), batch("E2", "electrolyser", 2, 4), *N_BATCH],
    )
    for r in (gap, overlap):
        assert [f.name for f in r.failures] == ["batch_cover"]


def test_batch_may_not_cross_local_month(cfg: PlantConfig) -> None:
    # 31 Oct 2026 23:30 CET = 22:30 UTC; four intervals run into 1 Nov local.
    start = dt.datetime(2026, 10, 31, 22, 30, tzinfo=UTC)
    frame = make_frame(cfg, ELECTROLYSER, start=start)
    d = Decisions.model_validate(
        {
            "batches": [
                batch("E", "electrolyser", 0, 4, start),
                batch("N1", "nh3_synthesis", 0, 2, start),
                batch("N2", "nh3_synthesis", 2, 4, start),
            ]
        }
    )
    r = calculate(cfg, frame, d)
    assert [(f.name, f.subject) for f in r.failures] == [("batch_within_month", "E")]


def test_hourly_correlation_from_2030(cfg: PlantConfig) -> None:
    # 1 Jan 2030 00:00 CET = 31 Dec 2029 23:00 UTC; 8 intervals = two clock hours.
    start = dt.datetime(2029, 12, 31, 23, 0, tzinfo=UTC)
    cols = {k: v + v for k, v in ELECTROLYSER.items()}
    frame = make_frame(cfg, cols, start=start, quarter="2030-Q1")
    long = Decisions.model_validate(
        {
            "batches": [
                batch("E", "electrolyser", 0, 8, start),
                batch("N", "nh3_synthesis", 0, 8, start),
            ]
        }
    )
    r = calculate(cfg, frame, long)
    assert [(f.name, f.subject) for f in r.failures] == [("batch_max_length", "E")]
    hourly = Decisions.model_validate(
        {
            "batches": [
                batch("E1", "electrolyser", 0, 4, start),
                batch("E2", "electrolyser", 4, 8, start),
                batch("N", "nh3_synthesis", 0, 8, start),
            ]
        }
    )
    assert calculate(cfg, frame, hourly).passed


def test_consignment_rules(cfg: PlantConfig) -> None:
    raw = cfg.model_dump(mode="json")
    nh3s = next(b for b in raw["blocks"] if b["id"] == "nh3_storage")
    nh3s["opening_ledger"] = [
        {
            "batch_id": "OLD-A",
            "rfnbo_kg": 1000,
            "ghg_gCO2e_per_MJ": 6.0,
            "production_month": "2026-08",
        },
        {
            "batch_id": "OLD-B",
            "rfnbo_kg": 1000,
            "ghg_gCO2e_per_MJ": 30.0,
            "production_month": "2026-09",
        },
    ]
    c = PlantConfig.model_validate(raw)
    cons = [
        {
            "id": "C",
            "output": "consignment_a",
            "volume_kg": 500,
            "ship_date": "2026-11-15",
            "target_gCO2e_per_MJ": 10.0,
        }
    ]

    ok = run(
        c,
        ELECTROLYSER,
        batches=[*E_BATCHES, *N_BATCH],
        consignments=cons,
        aggregated_batches=[
            {"id": "A1", "consignment": "C", "parts": [{"batch": "OLD-A", "kg": 500}]}
        ],
        write_offs=[
            {"storage": "h2_storage", "batch": "E2", "kg": 20},
            {"storage": "nh3_storage", "batch": "OLD-B", "kg": 1000},
        ],
    )
    assert ok.passed, ok.failures
    assert ok.consignment("C").shipped_ghg_gCO2e_per_MJ == pytest.approx(9.0)

    mixed = run(
        c,
        ELECTROLYSER,
        batches=[*E_BATCHES, *N_BATCH],
        consignments=cons,
        aggregated_batches=[
            {
                "id": "A1",
                "consignment": "C",
                "parts": [{"batch": "OLD-A", "kg": 400}, {"batch": "OLD-B", "kg": 100}],
            }
        ],
        write_offs=[
            {"storage": "h2_storage", "batch": "E2", "kg": 20},
            {"storage": "nh3_storage", "batch": "OLD-B", "kg": 900},
        ],
    )
    assert {f.name for f in mixed.failures} == {
        "aggregated_single_month",
        "aggregated_compliant_only",
        "consignment_target",
    }
    shipped = mixed.consignment("C").shipped_ghg_gCO2e_per_MJ
    assert shipped is not None and math.isclose(shipped, (400 * 6 + 100 * 30) / 500 + 3)


def test_feed_from_unknown_batch_is_an_error(cfg: PlantConfig) -> None:
    with pytest.raises(CalculationError, match="unknown"):
        run(
            cfg,
            {**ELECTROLYSER, **NH3},
            batches=[*E_BATCHES, *N_BATCH],
            feed=[{"source": "E9", "target": "N1", "kg": 1}],
        )


def test_off_grid_boundary_is_an_error(cfg: PlantConfig) -> None:
    bad = {
        "id": "E",
        "unit": "electrolyser",
        "start": at(0),
        "end": (T0 + dt.timedelta(minutes=20)).isoformat(),
    }
    with pytest.raises(CalculationError, match="grid"):
        run(cfg, ELECTROLYSER, batches=[bad, *N_BATCH])


def test_missing_data_is_never_filled(cfg: PlantConfig) -> None:
    cols = dict(ELECTROLYSER, electrolyser_water_kg=[500, float("nan"), 500, 500])
    with pytest.raises(CalculationError, match="missing values"):
        run(cfg, cols, batches=[*E_BATCHES, *N_BATCH])
