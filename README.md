# RFNBO GHG & Mass Balance Optimiser

Takes one quarter of 15-minute plant data and finds the compliant batching, feed allocation,
consignment allocation and write-off that best meets RFNBO consignment requirements
(Delegated Regulations (EU) 2023/1184 and 2023/1185). It does retrospective accounting, not
plant dispatch.

## Status

| Phase | Scope | State |
| --- | --- | --- |
| 0 | Repo skeleton, lint, type check, tests, CI | done |
| 1 | Plant config schema and validator | done |
| 2 | Diagram editor | done (NiceGUI + Drawflow; see `docs/adr/0001-diagram-editor.md`) |
| 3 | Data ingestion | done (CSV/Excel, plant time zone, DST, quality flags) |
| 4 | Calculator | done (golden tests reproduce both workbooks to 1e-6) |
| 5 | Traceability | done (per-batch JSON, lineage, sealed runs reproduce byte for byte) |
| 6 | Optimiser stage A (batching) | done (`rfnbo batch`; reproduces test case 1 and its 2030 hourly variant exactly) |
| 7 | Optimiser stages B/C, pipeline, baseline | done (`rfnbo optimise`; matches test case 1, beats regenerated test case 2) |
| 8 | Reports | done (`rfnbo report`; Excel and CSV; regenerates the workbook tables) |
| 9 | Results UI | done (`rfnbo results`; consignment → batches → 15-minute data) |
| 10–15 | Multiple and parallel processing steps (e.g. electrolyser → liquefier and compressor) | planned; decisions settled (`docs/build-plan.md` §10.3) |

The full build plan, including the multi-step extension and the decisions it needs, is in
`docs/build-plan.md`.

Test case 2 predates the non-compliant RFNBO rule (build plan §2.5). Its golden fixtures were
regenerated with `tests/golden/export_workbook.py --rule-2-5`: 88 H₂ sub-batches above
28.2 gCO₂e/MJ (108,729.2 kg) now enter the H₂ ledger, and 94,451.4 kg of them are written off
at quarter end under the workbook's own decisions. The original values are kept under
`superseded` in `tests/golden/tc2.expected.json`.

## Quick start

```sh
uv sync --extra ui            # the ui extra is only needed for the diagram editor
uv run rfnbo edit examples/test_plant.json   # opens the block flow diagram editor
uv run rfnbo results runs/                    # browse sealed runs down to 15-minute data
uv run pytest
uv run rfnbo validate examples/test_plant.json

# calculate the test case 1 decision set and seal the run, then re-check it
uv run rfnbo run --config examples/test_plant.json \
  --source examples/test_workbook_source.json \
  --data tests/golden/workbooks/RFNBO_optimiser_test_Q4_2026.xlsx \
  --decisions tests/golden/tc1.decisions.json --quarter 2026-Q4 --out runs/tc1
uv run rfnbo verify runs/tc1

# optimise a quarter against consignment requirements; seals the result and the baseline
uv run rfnbo optimise --config examples/test_plant.json \
  --source examples/test_workbook_source.json \
  --data tests/golden/workbooks/RFNBO_optimiser_test_Q4_2026_undersized.xlsx \
  --consignments examples/test_consignments_Q4_2026_undersized.json \
  --quarter 2026-Q4 --out runs/tc2-opt
```

A sealed run directory holds `frame.csv` (the exact 15-minute data used), `batches.jsonl`
(one traceability record per batch) and `run.json` (config, decisions, hashes, environment,
ledgers, consignments, lineage and every rule check). The run id is a hash of the inputs.
`--opening-from <dir>` opens the next quarter from a sealed run's closing ledger.

`rfnbo report <run-dir> --out DIR [--baseline <run-dir>] [--csv]` writes the reports for a
sealed run (it first checks the run still reproduces): batch register per unit, feed
allocation, lineage, consignment report, renewable matching, storage ledgers, physical
reconciliation, calculation log and, with a baseline, the baseline comparison. `rfnbo
optimise` writes the same report automatically.

See `CLAUDE.md` for conventions.
