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
| 3 | Data ingestion | calendar only (plant time zone, DST, quarter intervals) |
| 4–9 | Calculator, traceability, optimiser, reports, results UI | not started |

## Quick start

```sh
uv sync
uv run pytest
uv run rfnbo validate examples/test_plant.json
```

See `CLAUDE.md` for conventions.
