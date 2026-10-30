"""The same data at a finer time step is never worse: every hourly batching is available at
15 minutes. Stage A once assumed a compressor on the shared solar pool fully covered by it, so
it blended the electrolyser's batches to the threshold rather than to the compressor's real
feed limit; at 15 minutes it then cut many batches just under the threshold that no blend could
take, and lost a quarter of the RFNBO."""

from __future__ import annotations

import datetime as dt
import math
from pathlib import Path

import openpyxl

from rfnbo.config.schema import ProcessingUnit
from rfnbo.model.entities import ConsignmentSpec
from rfnbo.optimise import batching, pool
from rfnbo.optimise.pipeline import optimise
from rfnbo.project.folder import Prepared, prepare
from rfnbo.ui.data.workspace import Workspace

ROOT = Path(__file__).resolve().parents[2]
PLANT = ROOT / "tests" / "fixtures" / "simple_plant.json"
DATA = ROOT / "my_project" / "data" / "test_data.xlsx"
COLUMNS = {  # series -> spreadsheet column (MWh or tonne per hour)
    "solar_available_kWh": "Solar Power (MWh)",
    "electrolyser_electricity_kWh": "Electrolyser Power (MWh)",
    "electrolyser_H2_kg": "H2 Production (tonne)",
    "compressor_electricity_kWh": "Compressor Power (MWh)",
    "compressor_CH2_kg": "Compressor Flow (tonne)",
}
START, UNTIL = dt.datetime(2026, 9, 30, 23), dt.datetime(2026, 12, 1)  # Oct and Nov, London


def _hours() -> list[tuple[dt.datetime, list[float]]]:
    """The spreadsheet's hours from START to UNTIL (times rounded to the minute)."""
    wb = openpyxl.load_workbook(DATA, read_only=True, data_only=True)
    rows = wb.worksheets[0].iter_rows(values_only=True)
    header = [str(h).strip() if h is not None else "" for h in next(rows)]
    t_col, cols = header.index("Time"), [header.index(c) for c in COLUMNS.values()]
    out = []
    for row in rows:
        t = row[t_col]
        if not isinstance(t, dt.datetime):
            continue
        t = (t + dt.timedelta(seconds=30)).replace(second=0, microsecond=0)
        if START <= t < UNTIL:
            out.append((t, [float(row[c]) for c in cols]))  # type: ignore[arg-type]
    return out


def _csv(step: int) -> tuple[bytes, list[str]]:
    """Hourly as given (``step`` 1), or each hour split evenly into ``step`` intervals in kWh
    and kg."""
    if step == 1:
        heads = list(COLUMNS.values())
    else:
        heads = [s.replace("_kWh", " (kWh)").replace("_kg", " (kg)") for s in COLUMNS]
    lines = ["Time," + ",".join(heads)]
    for t, values in _hours():
        for q in range(step):
            when = t + q * dt.timedelta(hours=1) / step
            cells = values if step == 1 else [v * 1000 / step for v in values]
            lines.append(f"{when.isoformat()}Z," + ",".join(repr(v) for v in cells))
    return ("\n".join(lines) + "\n").encode(), heads


def _prepared(tmp: Path, step: int) -> tuple[Prepared, list[ConsignmentSpec]]:
    ws = Workspace(tmp)
    (tmp / "plant.json").write_bytes(PLANT.read_bytes())
    ws.setup("plant.json", "2026-Q4")
    data, heads = _csv(step)
    i = ws.add_data_file("data.csv", data)
    for series, head in zip(COLUMNS, heads, strict=True):
        ws.map_series(series, i, head)
    ws.add_consignment(
        id="C1", output="consignment_customer", volume_kg=100_000, ship_date="2026-11-15"
    )
    ws.save()
    assert ws.project is not None
    prep = prepare(ws.project)
    assert prep.ok and prep.frame.interval == dt.timedelta(hours=1) / step
    return prep, ws.project.consignments


def _final_rfnbo_kg(tmp: Path, step: int) -> float:
    prep, consignments = _prepared(tmp, step)
    result = optimise(prep.config, prep.frame, consignments)
    assert result.feasible and result.optimised.run.failures == []
    return result.optimised.objectives.final_rfnbo_kg


def test_15_minute_split_of_hourly_data_is_not_worse(tmp_path: Path) -> None:
    (tmp_path / "h").mkdir()
    (tmp_path / "q").mkdir()
    hourly = _final_rfnbo_kg(tmp_path / "h", 1)
    quarter_hourly = _final_rfnbo_kg(tmp_path / "q", 4)
    assert hourly > 0
    assert quarter_hourly >= hourly * (1 - 1e-6)


def test_stage_a_blends_to_the_feed_limit_the_pool_split_leaves(tmp_path: Path) -> None:
    """The compressor's grid electricity counts against the electrolyser's blend limit unless
    the pool split can cover it: here the electrolyser takes all the solar every month."""
    prep, _ = _prepared(tmp_path, 1)
    cfg, frame = prep.config, prep.frame
    el = cfg.block("electrolyser")
    assert isinstance(el, ProcessingUnit)
    split = pool.plan(
        cfg, frame, el, lambda e_pool, *, cover=None: batching.batch_unit(cfg, frame, el)
    )
    assert split.reserved_kWh == {"compressor": 0.0}
    s = frame.series
    out_MJ = float(s["compressor_CH2_kg"].sum()) * 120.0
    feed_MJ = float(s["electrolyser_H2_kg"].sum()) * 120.0
    own = float(s["compressor_electricity_kWh"].sum()) * 200.0  # grid, gCO2e
    covered = batching.downstream(cfg, frame, el)
    uncovered = batching.downstream(cfg, frame, el, cover={"compressor": 0.0})
    assert covered is not None and uncovered is not None
    assert math.isclose(covered.limit_gCO2e_per_MJ, 28.2 * out_MJ / feed_MJ, rel_tol=1e-9)
    assert math.isclose(
        uncovered.limit_gCO2e_per_MJ, (28.2 - own / out_MJ) * out_MJ / feed_MJ, rel_tol=1e-9
    )
    assert uncovered.limit_gCO2e_per_MJ < covered.limit_gCO2e_per_MJ - 3.0
