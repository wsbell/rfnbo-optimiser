"""Phase 14 acceptance: the optimiser on networks.

Parallel units on one producer (test plant 3: electrolyser split between the liquefier and the
compressor) are allocated in one LP with the split output; units in series (H₂ → compressor →
NH₃) stage by stage with the GHG limit back-propagated from the final product. The optimiser
matches or beats test case 3 and its variants, every check passes, and the gap to an upper
bound on objective 2 is reported.
"""

from __future__ import annotations

import datetime as dt
import json
from functools import cache
from pathlib import Path
from typing import Any

import numpy as np

from rfnbo.config import validate
from rfnbo.config.schema import PlantConfig
from rfnbo.data.quality import Frame
from rfnbo.model.entities import ConsignmentSpec
from rfnbo.optimise import allocate
from rfnbo.optimise.pipeline import OptimisationResult, optimise
from tests.golden import tc3_reference as R
from tests.golden.test_pool_golden import _frame
from tests.golden.test_tc3_calc import _decisions, _inputs, approx

GOLDEN = Path(__file__).resolve().parent


@cache
def _expected() -> dict[str, Any]:
    out: dict[str, Any] = json.loads((GOLDEN / "tc3.expected.json").read_text("utf-8"))
    return out


def _consignments(ca_target: float = 5.0) -> list[ConsignmentSpec]:
    return [
        ConsignmentSpec(
            id="CA",
            output="consignment_a",
            volume_kg=150000,
            ship_date=dt.date(2026, 12, 15),
            target_gCO2e_per_MJ=ca_target,
        ),
        ConsignmentSpec(
            id="CB",
            output="consignment_b",
            volume_kg=120000,
            ship_date=dt.date(2026, 12, 20),
            target_gCO2e_per_MJ=4.0,
        ),
    ]


@cache
def _optimised(case: str) -> OptimisationResult:
    cfg, frame, _ = _inputs()
    if case == "base":
        return optimise(cfg, frame, _decisions().consignments)
    if case == "v2_shortfall":
        data = R.build_data(dec_renewable_kWh=R.V2_DEC_RENEWABLE_KWH, spot=False)
        return optimise(cfg, _frame(data), _consignments())
    return optimise(cfg, frame, _consignments(ca_target=R.V3_CA_TARGET))


def test_parallel_units_are_one_stage() -> None:
    cfg, _, _ = _inputs()
    from rfnbo.calc.batch import unit_order

    (stage,) = allocate.stages(cfg, unit_order(cfg))
    assert {u.id for u in stage} == {"liquefier", "compressor"}
    assert allocate.needs_network(cfg, stage)


def test_test_case_3_is_matched() -> None:
    """Every check passes; the carried value equals the hand calculation (the optimiser finds
    that the buffer should stay full rather than liquefy H₂ that would be written off)."""
    res = _optimised("base")
    o = res.optimised
    assert res.feasible and o.run.failures == []
    assert o.objectives.consignments_met
    want = _expected()["objectives"]
    approx(o.objectives.carried_value_MJ, want["carried_value_MJ"], "carried value")
    approx(o.run.ledger("h2_buffer").carried_kg, 4880.0, "buffer full of RFNBO")
    approx(o.run.ledger("lh2_storage").write_off_kg, 57040.0, "LH2 write-off")
    assert o.run.ledger("pt_compressor").carried_kg == 0
    assert o.decisions.output_allocations  # the split output was decided
    vb = res.value_bound()
    assert vb.gap < 1e-6, (vb.upper_MJ, vb.achieved_MJ)
    assert res.baseline.run.failures == []
    assert o.objectives.key() >= res.baseline.objectives.key()


def test_shortfall_variant_is_beaten() -> None:
    res = _optimised("v2_shortfall")
    assert res.feasible and res.optimised.run.failures == []
    want = _expected()["variants"]["v2_shortfall"]["carried_value_MJ"]
    assert res.optimised.objectives.carried_value_MJ >= want * (1 - 1e-9)


def test_infeasible_target_on_one_product() -> None:
    res = _optimised("v3_infeasible")
    (short,) = res.shortfalls
    assert short.consignment == "CA" and short.achievable_kg == 0
    assert "1.5" in short.reason
    run = res.optimised.run
    assert run.failures == [] and [c.id for c in run.consignments] == ["CB"]
    want = _expected()["variants"]["v3_infeasible"]["carried_value_MJ"]
    approx(res.optimised.objectives.carried_value_MJ, want, "carried value")


# ---- units in series: electrolyser → H₂ → compressor → CH₂ → NH₃ synthesis ------------------


def _series_plant() -> PlantConfig:
    ef = [{"value": 0.0, "unit": "gCO2e/kWh", "valid_from": "2026-01-01", "source": "test"}]
    grid = [{"value": 400.0, "unit": "gCO2e/kWh", "valid_from": "2026-01-01", "source": "test"}]
    water = [{"value": 2.0, "unit": "gCO2e/kg", "valid_from": "2026-01-01", "source": "test"}]

    def unit(i: str, product: str, lhv: float, adder: float, lhv_e: bool) -> dict[str, Any]:
        return {
            "id": i,
            "name": i,
            "kind": "processing_unit",
            "product": product,
            "lhv_MJ_per_kg": lhv,
            "electricity_adds_to_lhv": lhv_e,
            "fixed_adder_gCO2e_per_MJ": adder,
            "production_series": f"{i}_kg",
            "electricity_series": f"{i}_kWh",
        }

    def store(i: str, product: str, opening: float) -> dict[str, Any]:
        return {
            "id": i,
            "name": i,
            "kind": "storage_unit",
            "product": product,
            "inventory_series": f"{i}_kg",
            "opening_inventory_kg": opening,
        }

    def flow(s: str, t: str, m: str, series: str | None = None) -> dict[str, Any]:
        f = {"id": f"f_{s}_{t}", "source": s, "target": t, "material": m}
        return f | ({"series": series} if series else {})

    raw = {
        "schema_version": 1,
        "plant": "Series test plant",
        "version": "1",
        "effective_from": "2026-01-01",
        "timezone": "Europe/Amsterdam",
        "settings": {},
        "blocks": [
            {
                "id": "re",
                "name": "re",
                "kind": "input",
                "material": "electricity",
                "quantity_unit": "kWh",
                "renewable": True,
                "emission_factors": ef,
                "series": "re_kWh",
            },
            {
                "id": "grid",
                "name": "grid",
                "kind": "input",
                "material": "electricity",
                "quantity_unit": "kWh",
                "emission_factors": grid,
            },
            {
                "id": "water",
                "name": "water",
                "kind": "input",
                "material": "water",
                "quantity_unit": "kg",
                "emission_factors": water,
            },
            unit("el", "H2", 120.0, 1.0, True),
            store("h2s", "H2", 1000.0),
            unit("comp", "CH2", 120.0, 0.5, False),
            store("ch2s", "CH2", 500.0),
            unit("nh3", "NH3", 18.6, 2.0, False),
            store("nh3s", "NH3", 0.0),
            {
                "id": "dist",
                "name": "dist",
                "kind": "distribution",
                "customer": "Customer A",
                "fixed_adder_gCO2e_per_MJ": 3.0,
            },
            {
                "id": "cons",
                "name": "cons",
                "kind": "consignment_output",
                "customer": "Customer A",
                "distribution_block": "dist",
            },
        ],
        "flows": [
            flow("re", "el", "electricity"),
            flow("grid", "el", "electricity"),
            flow("water", "el", "water", "water_kg"),
            flow("el", "h2s", "H2"),
            flow("h2s", "comp", "H2", "comp_H2_kg"),
            flow("grid", "comp", "electricity"),
            flow("comp", "ch2s", "CH2"),
            flow("ch2s", "nh3", "CH2", "nh3_CH2_kg"),
            flow("grid", "nh3", "electricity"),
            flow("nh3", "nh3s", "NH3"),
            flow("nh3s", "dist", "NH3", "shipped_kg"),
            flow("dist", "cons", "NH3"),
        ],
    }
    cfg = PlantConfig.model_validate(raw)
    assert validate(cfg) == []
    return cfg


def _series_frame() -> Frame:
    """Three days in October 2026. On the second day, 06:00–12:00 local, renewable covers only
    40% of the electrolyser: non-compliant RFNBO H₂ that the compressor can partly blend."""
    start = dt.datetime(2026, 9, 30, 22, 0, tzinfo=dt.UTC)
    n = 288
    idx = tuple(start + i * dt.timedelta(minutes=15) for i in range(n))
    local = [t.astimezone(R.TZ) for t in idx]
    re = np.array([1000.0 if (t.day == 2 and 6 <= t.hour < 12) else 2500.0 for t in local])
    shipped = np.array([50000.0 / 96 if t.day == 3 else 0.0 for t in local])
    nh3_out = np.full(n, 275.0)
    s = {
        "re_kWh": re,
        "el_kWh": np.full(n, 2500.0),
        "water_kg": np.full(n, 500.0),
        "el_kg": np.full(n, 50.0),
        "h2s_kg": np.full(n, 1000.0),
        "comp_H2_kg": np.full(n, 50.0),
        "comp_kWh": np.full(n, 40.0),
        "comp_kg": np.full(n, 50.0),
        "ch2s_kg": 500.0 + np.cumsum(np.full(n, 50.0 - 48.95)),
        "nh3_CH2_kg": np.full(n, 48.95),
        "nh3_kWh": np.full(n, 137.5),
        "nh3_kg": nh3_out,
        "nh3s_kg": np.cumsum(nh3_out - shipped),
        "shipped_kg": shipped,
    }
    return Frame(idx, s, "Europe/Amsterdam", "2026-Q4")


def test_units_in_series_meet_every_check() -> None:
    cfg, frame = _series_plant(), _series_frame()
    cons = [
        ConsignmentSpec(
            id="C1",
            output="cons",
            volume_kg=50000,
            ship_date=dt.date(2026, 10, 3),
            target_gCO2e_per_MJ=26.0,
        )
    ]
    res = optimise(cfg, frame, cons)
    run = res.optimised.run
    assert res.feasible and run.failures == [], [str(f) for f in run.failures][:5]
    from rfnbo.calc.batch import unit_order

    stages = allocate.stages(cfg, unit_order(cfg))
    assert [[u.id for u in s] for s in stages] == [["comp"], ["nh3"]]
    # The compressor's product is held to the limit NH₃ synthesis needs to comply.
    limit = allocate.ghg_limit(cfg, frame, cfg.block("comp"))  # type: ignore[arg-type]
    assert limit < 28.2
    comp = [b for b in run.batches.values() if b.unit == "comp"]
    assert comp and all((b.ghg_gCO2e_per_MJ or 0) <= limit + 1e-6 for b in comp)
    nh3 = [b for b in run.batches.values() if b.unit == "nh3"]
    assert all(b.compliant for b in nh3 if b.rfnbo_kg > 0)
    # Some non-compliant H₂ is saved by blending into the compressor (§2.5).
    fed = {a.source for a in run.decisions.feed if run.batches[a.target].unit == "comp"}
    assert any(not run.batches[b].compliant for b in fed)
    assert run.consignment("C1").shipped_ghg_gCO2e_per_MJ is not None
    assert res.value_bound().upper_MJ >= res.optimised.objectives.carried_value_MJ - 1e-6
