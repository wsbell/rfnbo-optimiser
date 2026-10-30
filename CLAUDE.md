# CLAUDE.md

RFNBO GHG and mass balance optimiser: retrospective accounting of one quarter of 15-minute
plant data under Delegated Regulations (EU) 2023/1184 and 2023/1185 and certification-scheme
mass balance rules. The build plan (`docs/build-plan.md`) and scope document define the domain
rules; follow them. Phases 10–15 (multi-step networks) follow the decisions in §10.3.

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
- `src/rfnbo/calc/`: `batch.py` (a downstream batch's RFNBO is split into parts, one per source
  GHG and origin, build plan §12; feed carrying one `pool` label is averaged into one part,
  §12.1: same source and production month, each compliant), `ledger.py` (lines per part), `consign.py`, `purchase.py` (PoS register and
  supplier reconciliation), `check.py` (named rule checks), `calculator.py`
  (`calculate(config, frame, decisions) -> RunResult`).
- `src/rfnbo/model/pos.py`: PoS records from CSV or JSON, and the rules a record must meet
  before a run uses it (`rfnbo optimise --pos`). PoS records sit in the decision set like
  consignments; their allocation to suppliers' metered deliveries is a decision.
- `src/rfnbo/project/`: project folders (`folder.py`: every input of a quarter as plain files,
  `prepare()` aligns, corrects and checks the data), `corrections.py` (reasoned replacements,
  recorded in sealed provenance), `run.py` (optimise and seal; shared by `rfnbo optimise
  --project` and the Data screen).
- `src/rfnbo/trace/`: `json_log.py` (per-batch records, `recompute_ghg`), `lineage.py`
  (edges and drill-down), `run.py` (seal, verify, open next quarter from a sealed run).
  Sealed outputs must stay byte-identical on re-run: no wall-clock, no unordered iteration.
- `src/rfnbo/ui/theme.py`: the shared look (colour tokens, type, card/table/button/input/pill
  styles, top bar). Every screen calls `theme.setup()` before `ui.run` and `theme.apply()` on
  each page; build new UI from its helpers rather than ad-hoc colours.
- `src/rfnbo/ui/bfd_editor/`: the diagram editor (`rfnbo edit`). `document.py` holds all
  editing state and validation with no UI imports; `fields.py` defines the property forms
  (a test checks every schema field has one); `page.py` is the NiceGUI page; `static/` holds
  vendored Drawflow (MIT) and `bfd.js` glue. Decision record: `docs/adr/0001-diagram-editor.md`.
- `src/rfnbo/ui/results/`: the results browser (`rfnbo results`). `views.py` builds every page
  from a sealed run re-calculated by the calculator (no UI imports, fully unit-tested);
  `app.py` lays it out: runs → run → consignment → batch → 15-minute intervals.
- `src/rfnbo/ui/data/`: the Data screen (`rfnbo data <folder>`). `workspace.py` holds all its
  state and actions with no UI imports (tested in `tests/integration/test_data_project.py`);
  `app.py` lays it out and serves the results browser on the folder's `runs/`. Its Plant tab
  embeds the diagram editor (`Editor.build_embedded`), where each meter picks its data column
  (`Columns`); a draft plant can be set up and mapped, and is run only once valid.
- `src/rfnbo/optimise/batching.py`: stage A. Exact DP per unit and month over 15-minute
  intervals (hourly windows from the switch date for electrolytic units), scored by the RFNBO
  in compliant batches each month can blend at or below the downstream limit (§12.1; limits
  back-propagated through units in series; `caps` tighten them to a consignment's limit), μ
  shaping the candidates and each month taking its best. Proposals
  only: re-run everything through the calculator.
- `src/rfnbo/optimise/pool.py`: stage A with a renewable pool shared by the electrolyser and
  conditioning units: alternates the DP with a per-interval split until stable.
- `src/rfnbo/optimise/allocate.py`: stage B (feed LP per downstream unit; compliance is linear
  in the feed) and stage C (consignment LP, then write-offs by the §2.7 rule).
  On networks `stage_b_network` allocates a stage of parallel units in one LP, including
  a split output's assignment and PoS credentials for suppliers' deliveries into units
  (`storage_credits` fills deliveries into storages afterwards); `stages` orders units in series.
  Stage B pools compliant feed of one month and source where that feeds more (§12.1); stage C
  holds each aggregated batch's (month, source) average to the consignment's limit.
  `pipeline.py` runs A → B → C with B↔C feedback and relaxation when a consignment cannot be
  met, then stage A again with the unmet consignments' limits as caps; `baseline.py` is one
  batch per month, FIFO; `lp.py` wraps HiGHS with fixed, recorded
  options (`solver_info()` goes into every sealed run).
- `src/rfnbo/report/`: `tables.py` builds every report table from a calculated run (never
  from solver output); `excel.py` and `csv_export.py` write them; `build.py` reports a sealed
  run by re-running the calculator on its files.
- `examples/test_plant.json`: the test plant from the workbooks;
  `examples/test_workbook_source.json` maps workbook columns to series;
  `examples/test_consignments_*.json` are the workbooks' consignment requirements.
- `tests/golden/`: the two test workbooks, `export_workbook.py`, and the exported
  `*.decisions.json` / `*.expected.json` fixtures. Regenerate fixtures with the script, never
  by hand (test case 2 uses `--rule-2-5`). Test case 3 (multi-step plant): `build_tc3_workbook.py`
  writes the workbook (recalculate it with LibreOffice before committing), `tc3_reference.py` is
  the independent check, `export_tc3.py` writes `tc3.*.json`; `tc3.plant.json` is test plant 3
  (the calculator reproduces it in `test_tc3_calc.py`). Test case 4 (purchased RFNBO):
  `build_tc4_workbook.py`, `tc4_reference.py`, `export_tc4.py` and `tc4.*` the same way, tested in
  `test_tc4_workbook.py` and `test_tc4_calc.py`. Test cases 1 and 2 are exported with
  `--rule-12` (build plan §12 and §12.1; test case 2 also with `--rule-2-5`). `benchmark_networks.py` times the
  optimiser on test plant 3 and a five-unit plant (`--profile` for where the time goes).
