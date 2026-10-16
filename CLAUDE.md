# CLAUDE.md

RFNBO GHG and mass balance optimiser: retrospective accounting of one quarter of 15-minute
plant data under Delegated Regulations (EU) 2023/1184 and 2023/1185 and certification-scheme
mass balance rules. The build plan (`docs/build-plan.md`) and scope document define the domain
rules; follow them. Phases 10–15 (multi-step networks) need the §10.3 decisions first.

## Commands

```sh
uv sync --extra ui           # install (Python 3.12); the ui extra is the diagram editor
uv run playwright install chromium   # once, for the browser tests in tests/e2e
uv run pytest                # tests (browser tests skip without NiceGUI or Chromium)
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
- `src/rfnbo/data/`: `calendar.py` (plant-time-zone months, clock hours, quarters),
  `ingest.py` (CSV/Excel → raw series), `quality.py` (grid alignment, quality flags, `Frame`).
- `src/rfnbo/model/entities.py`: decision sets (batches, feed, renewable split, consignments,
  aggregated batches, write-offs) as JSON-serialisable pydantic models.
- `src/rfnbo/calc/`: `batch.py`, `ledger.py`, `consign.py`, `check.py` (named rule checks),
  `calculator.py` (`calculate(config, frame, decisions) -> RunResult`).
- `src/rfnbo/trace/`: `json_log.py` (per-batch records, `recompute_ghg`), `lineage.py`
  (edges and drill-down), `run.py` (seal, verify, open next quarter from a sealed run).
  Sealed outputs must stay byte-identical on re-run: no wall-clock, no unordered iteration.
- `src/rfnbo/ui/bfd_editor/`: the diagram editor (`rfnbo edit`). `document.py` holds all
  editing state and validation with no UI imports; `fields.py` defines the property forms
  (a test checks every schema field has one); `page.py` is the NiceGUI page; `static/` holds
  vendored Drawflow (MIT) and `bfd.js` glue. Decision record: `docs/adr/0001-diagram-editor.md`.
- `src/rfnbo/optimise/batching.py`: stage A. Exact DP per unit and month over 15-minute
  intervals (hourly windows from the switch date for electrolytic units), Lagrangian value
  against the downstream blend limit, μ search on feedable RFNBO. Proposals only: re-run
  everything through the calculator.
- `src/rfnbo/optimise/allocate.py`: stage B (feed LP per downstream unit; compliance is linear
  in the feed) and stage C (consignment LP, then write-offs by the §2.7 rule).
  `pipeline.py` runs A → B → C with B↔C feedback and relaxation when a consignment cannot be
  met; `baseline.py` is one batch per month, FIFO; `lp.py` wraps HiGHS with fixed, recorded
  options (`solver_info()` goes into every sealed run).
- `examples/test_plant.json`: the test plant from the workbooks;
  `examples/test_workbook_source.json` maps workbook columns to series;
  `examples/test_consignments_*.json` are the workbooks' consignment requirements.
- `tests/golden/`: the two test workbooks, `export_workbook.py`, and the exported
  `*.decisions.json` / `*.expected.json` fixtures. Regenerate fixtures with the script, never
  by hand (test case 2 uses `--rule-2-5`).
