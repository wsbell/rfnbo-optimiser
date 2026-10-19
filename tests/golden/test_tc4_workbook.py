"""Phase 16 specification: test case 4 (purchased RFNBO with proofs of sustainability).

The workbook's Checks sheet passes against an independent script (``tc4_reference``), the
reference reproduces the workbook from the workbook's own inputs, and the fixtures exported from
the workbook are current.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from functools import cache
from pathlib import Path
from typing import Any

import openpyxl

from rfnbo.config import load_config
from rfnbo.data import ingest, quality
from tests.golden import export_tc4
from tests.golden import tc3_reference as R
from tests.golden import tc4_reference as T
from tests.golden.test_tc3_workbook import SERIES

GOLDEN = Path(__file__).resolve().parent
WORKBOOK = export_tc4.WORKBOOK
SERIES4 = SERIES | {
    "supplier_to_liquefier_kg": "sup_liquefier_kg",
    "supplier_to_compressor_kg": "sup_compressor_kg",
}


def close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6)


@cache
def _fixtures() -> tuple[dict[str, Any], dict[str, Any]]:
    decisions = json.loads((GOLDEN / "tc4.decisions.json").read_text(encoding="utf-8"))
    expected = json.loads((GOLDEN / "tc4.expected.json").read_text(encoding="utf-8"))
    return decisions, expected


@cache
def _workbook_data() -> R.Data:
    raw = ingest.load(WORKBOOK, ingest.load_source_spec(GOLDEN / "tc4.source.json"))
    frame, flags = quality.align(raw, 2026, 4, "Europe/Amsterdam")
    assert not quality.has_errors(flags), flags
    utc = [t.astimezone(dt.UTC) for t in frame.index]
    local = [t.astimezone(R.TZ) for t in utc]
    series = {ref: [float(x) for x in frame.series[name]] for name, ref in SERIES4.items()}
    return R.Data(utc, local, [f"{t.year:04d}-{t.month:02d}" for t in local], series)


def test_workbook_checks_sheet_passes() -> None:
    wb = openpyxl.load_workbook(WORKBOOK, data_only=True, read_only=True)
    rows = [r for r in wb["Checks"].iter_rows(min_row=2, values_only=True) if r[1] is not None]
    assert len(rows) > 70
    assert all(r[4] is True for r in rows), [r[0] for r in rows if r[4] is not True]
    flags = {r[0]: r[4] for r in wb["Checks"].iter_rows(min_row=2, values_only=True) if r[0]}
    assert flags["All checks pass?"] is True


def test_workbook_inputs_are_the_reference_quarter() -> None:
    data, ref = _workbook_data(), T.build_data()
    assert data.utc == ref.utc
    for name, xs in ref.s.items():
        assert all(close(a, b) for a, b in zip(data.s[name], xs, strict=True)), name


def test_reference_reproduces_the_workbook_from_its_own_inputs() -> None:
    data = _workbook_data()
    _, x = _fixtures()
    got = T.run(data, T.base_decisions(data))
    for unit, key in (("liquefier", "liquefier_batches"), ("compressor", "compressor_batches")):
        for bid, want in x[key].items():
            b = got["units"][unit][bid]
            for k in ("feed_consumed_kg", "rfnbo_feed_kg", "pos_feed_kg", "rfnbo_kg", "ghg"):
                assert close(b[k], want[k]), (bid, k, b[k], want[k])
    for pid, want in x["pos"].items():
        assert close(got["pos"][pid]["allocated_kg"], want["allocated_kg"]), pid
        assert close(got["pos"][pid]["written_off_kg"], want["written_off_kg"]), pid
    o, w = got["objectives"], x["objectives"]
    for k in ("carried_value_MJ", "carried_emissions_g", "purchased_emissions_g"):
        assert close(o[k], w[k]), k
    assert T.checks_ok(got)


def test_fixtures_are_current() -> None:
    """Regenerate with ``uv run python tests/golden/export_tc4.py``."""
    assert export_tc4.export(WORKBOOK) == _fixtures()


def test_hand_calculated_values() -> None:
    """Spot values worked by hand (README of the workbook)."""
    _, x = _fixtures()
    e = 2.0 * 500 / (50 * 120) + 1.0  # 100% electrolyser H₂
    # December LH₂: 86,400 kg buffer H₂ + POS-B 9,000 kg @ 12 + POS-C 600 kg @ 27.9, 98% yield.
    dec = x["liquefier_batches"]["L-2026-12"]
    lh2 = 0.98 * 96000
    want = (86400 * e + 9000 * 12.0 + 600 * 27.9) / lh2 + 0.5
    assert close(dec["ghg"], want) and close(dec["rfnbo_kg"], lh2)
    # November CH₂: 57,600 kg pass-through H₂ + POS-A 8,000 @ 3 + POS-C 1,600 @ 27.9.
    nov = x["compressor_batches"]["C-2026-11"]
    assert close(nov["ghg"], (57600 * e + 8000 * 3.0 + 1600 * 27.9) / 67200 + 0.5)
    # November LH₂ leaves 2,000 kg of RFNBO in the buffer: its yield is below the weight.
    w = x["objectives"]["weights"]["h2_buffer"]
    assert close(w, 265500 / 271800) and 0.97 < w < 0.98
    assert close(x["liquefier_batches"]["L-2026-11"]["rfnbo_share"], 84400 / 86400)
    assert close(x["ledgers"]["h2_buffer"]["carried_kg"], 4880)
    assert x["supplier"] == {
        "metered_kg": 19200,
        "pos_kg": 23000,
        "allocated_kg": 19200,
        "non_rfnbo_kg": 0,
        "written_off_kg": 3800,
    }
    assert x["pos"]["POS-C"]["written_off_kg"] == 3800
    value = (4880 * w + 113560 + 66320) * 120
    assert close(x["objectives"]["carried_value_MJ"], value)
    v1 = x["variants"]["v1_spot"]
    assert v1["liquefier_credentials_kg"] == 0 and v1["pos_written_off_kg"] == 13400
    assert close(v1["carried_value_MJ"], (4880 * w + 56500 + 66320) * 120)


def test_plant_config_validates() -> None:
    cfg = load_config(GOLDEN / "tc4.plant.json")
    assert cfg.block("h2_supplier").kind == "rfnbo_supply"
