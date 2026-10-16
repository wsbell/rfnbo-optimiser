# CLAUDE.md

RFNBO GHG and mass balance optimiser: retrospective accounting of one quarter of 15-minute
plant data under Delegated Regulations (EU) 2023/1184 and 2023/1185 and certification-scheme
mass balance rules. The build plan and scope document define the domain rules; follow them.

## Commands

```sh
uv sync                      # install (Python 3.12)
uv run pytest                # tests
uv run ruff check . && uv run ruff format --check .
uv run mypy                  # strict type check
uv run rfnbo validate examples/test_plant.json
```

## Conventions

- Metric units only: kg, kWh, MJ, gCO₂e/MJ. Every quantity field carries its unit in its name
  (`rfnbo_kg`, `lhv_MJ_per_kg`, `fixed_adder_gCO2e_per_MJ`).
- Store timestamps in UTC (interval start); derive months, hours and quarters only via
  `rfnbo.data.calendar` using the plant time zone.
- The calculator is pure and deterministic. No randomness, no wall-clock reads.
- Every reported number comes from the calculator, never directly from the solver.
- Every rule is a named check in `calc/check.py` with a regulation reference and a unit test.
  Config rules are named functions in `config/validate.py`, each with a failing-case test.
- Never fill or alter input data silently.
- Floating-point comparisons use explicit tolerances (1e-6 relative); threshold checks use
  `≤ threshold + 1e-9`.
- Pin solver and versions; record them in every run.
- If a domain rule is unclear, stop and ask; do not invent regulatory behaviour.
- Golden tests from the workbooks must pass before merging any change to `calc/` or `optimise/`.
- The engine never imports UI code.

## Layout

- `src/rfnbo/config/`: pydantic block flow diagram schema (`schema.py`) and named
  validation rules (`validate.py`).
- `src/rfnbo/data/calendar.py`: plant-time-zone months, clock hours, quarters, intervals.
- `examples/test_plant.json`: the scope document's test plant. Values whose `source` says
  PLACEHOLDER must be replaced from the test workbooks.
- `tests/unit`, `tests/property`, `tests/integration`, `tests/golden` (workbook fixtures).
