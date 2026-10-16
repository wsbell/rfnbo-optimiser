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
| 2 | Diagram editor | not started |
| 3 | Data ingestion | done (CSV/Excel, plant time zone, DST, quality flags) |
| 4 | Calculator | done (golden tests reproduce both workbooks to 1e-6) |
| 5–9 | Traceability, optimiser, reports, results UI | not started |

Test case 2 predates the non-compliant RFNBO rule (build plan §2.5). Its golden fixtures were
regenerated with `tests/golden/export_workbook.py --rule-2-5`: 88 H₂ sub-batches above
28.2 gCO₂e/MJ (108,729.2 kg) now enter the H₂ ledger, and 94,451.4 kg of them are written off
at quarter end under the workbook's own decisions. The original values are kept under
`superseded` in `tests/golden/tc2.expected.json`.

## Quick start

```sh
uv sync
uv run pytest
uv run rfnbo validate examples/test_plant.json
```

See `CLAUDE.md` for conventions.
