# RFNBO GHG & Mass Balance Optimiser — Build Plan for Claude Code

This plan is the hand-off to Claude Code. Give it this file, the scope document, and the two
test workbooks. It covers what to build (the domain rules), how to structure it, and the order
to build it in, with acceptance criteria for each phase.

- Scope document: https://claude.ai/code/artifact/2c870a95-2c8b-4552-ba41-adce97d6351c
- Test case 1: `RFNBO_optimiser_test_Q4_2026.xlsx` (simple, hand-checkable)
- Test case 2: `RFNBO_optimiser_test_Q4_2026_undersized.xlsx` (474 electrolyser batches, JSON traceability)
- Both workbooks predate the non-compliant RFNBO rule (§2.5) and must be regenerated before
  they are used as golden tests. Treat their GHG arithmetic as correct; treat their compliance
  handling as superseded.

---

## 1. What the app does

A Python application that takes one quarter of 15-minute plant data and finds the compliant
batching, feed allocation, consignment allocation and write-off that best meets RFNBO
consignment requirements. It is **retrospective accounting**, not plant dispatch. It solves the
whole plant in one optimisation and reports per unit. Every number is traceable to the
15-minute input data.

Regulatory basis: Delegated Regulation (EU) 2023/1184 (temporal correlation), 2023/1185 (GHG
methodology), and certification scheme mass balance rules (ISCC EU, CertifHy).

---

## 2. Domain rules (the specification)

### 2.1 Plant configuration
The plant is a block flow diagram saved as JSON, versioned with an effective-from date.

| Block | Key properties |
| --- | --- |
| Input | Material/utility, emission factor + unit + valid-from + source, linked time series |
| Processing unit | Product, LHV (MJ/kg), `electricity_adds_to_lhv` flag, fixed GHG adder (gCO₂e/MJ), linked series |
| Storage unit | Product, linked 15-minute inventory series (kg), opening physical inventory, opening RFNBO ledger |
| Distribution block | Customer, fixed GHG adder (gCO₂e/MJ), applied only after shipping |
| Consignment output | Customer, linked distribution block |

Flows connect blocks and carry one material each. A flow from an input maps that input's
emission factor to the unit. Validation: no orphans, unit/product consistency, every unit has a
production series.

### 2.2 Inputs
- 15-minute series, timestamped UTC: renewable power available (kWh), electricity per unit
  (kWh), other inputs (kg), production per unit (kg), transfers (kg), storage inventory (kg).
- All renewable power supplied is assumed compliant on additionality and geography.
- Non-renewable electricity emission factor: one constant input.
- Consignments: customer, product, kg, ship date, target GHG (gCO₂e/MJ, applies to the shipped
  consignment). Or "minimise mode": one volume, find the lowest GHG.
- Never fill data gaps silently. Flag gaps, negatives and meter-balance breaks; any substitution
  rule is explicit and logged.
- Purchased RFNBO from third-party suppliers, each delivery backed by a proof of
  sustainability (PoS): phase 16, §11.1. Entering plant data in the UI: phase 17, §11.2.

### 2.3 Batches and temporal correlation
- A batch is a contiguous run of 15-minute intervals on one unit, with one RFNBO share and one
  GHG intensity. The optimiser picks boundaries freely: from one interval up to the maximum.
- Maximum length: one calendar month to 31 Dec 2029; from 1 Jan 2030, one clock hour for units
  where electricity adds to product LHV (switch date configurable). Other units: one month.
- Batches never cross calendar months. Month/hour boundaries use the plant time zone (DST aware).
- **Renewable matching is within the batch**: matched kWh = min(Σ renewable, Σ electricity)
  over the batch's own intervals.

### 2.4 RFNBO share and GHG
- Electrolyser-type unit: RFNBO share = matched kWh ÷ electricity kWh.
- Downstream unit: only RFNBO feed is allocated to it. RFNBO share = RFNBO feed ÷ total feed. If
  short (to be avoided), the unit's own emissions are pro-rated to the RFNBO portion by energy.
- Only the RFNBO sub-batch enters the ledger. Non-RFNBO product is not tracked after it is made.
- GHG intensity is constant across a batch; RFNBO and non-RFNBO sub-batches share it.

```
GHG_batch = (Σ q_i · EF_i  +  Σ_feed MJ_b · GHG_b) / MJ_out  +  A_unit      [gCO2e/MJ]
```
Matched renewable electricity has EF = 0. Feed batches carry their own GHG.

### 2.5 Compliant and non-compliant RFNBO
- An RFNBO sub-batch is RFNBO whatever its GHG. It is **compliant** at ≤ 28.2 gCO₂e/MJ (70%
  saving vs 94 gCO₂e/MJ) and **non-compliant** above.
- Non-compliant RFNBO enters the ledger and can be fed downstream, but it stays non-compliant:
  ~~blended with low-GHG RFNBO in the same downstream batch it can be "saved"~~ (superseded
  2026-10-20, §12: batches are never averaged, so nothing is saved by blending).
- Non-compliant sub-batches can never be aggregated into a consignment.
- 0% RFNBO share → no RFNBO.

### 2.6 Consignments
- A consignment is one or more aggregated batches. An aggregated batch holds compliant
  sub-batches from one calendar month. A consignment can hold aggregated batches from different
  months. Splits and merges are allowed with full lineage.
- ~~Aggregated batch GHG = energy-weighted average of its sub-batches. Shipped consignment GHG
  = energy-weighted average of its aggregated batches + customer distribution adder.~~
  Superseded 2026-10-20 (§12): every part of a consignment keeps its own GHG; each part's GHG
  plus the distribution adder must be ≤ the consignment target and ≤ 28.2.

### 2.7 Mass balance
- Each storage unit keeps an RFNBO ledger per calendar quarter, checked at quarter end only.
  Data that ends early gives a provisional, quarter-to-date run (§10.3 decision 13).
- Quarter end: closing ledger ≥ 0 and ≤ measured physical inventory. Non-compliant RFNBO
  (§2.5) cannot be stored and carried into the next quarter: all of it left at quarter end is
  written off (decided 2026-10-17). Beyond that, any excess over physical inventory is written
  off, highest-GHG batches first (non-compliant batches always first). Carried batches keep
  ID, GHG, RFNBO share and production month.
- Losses are not modelled; they appear in a physical reconciliation.
- Opening ledger: manual entry or a prior sealed run (run setting).

### 2.8 Objectives (lexicographic, never a weighted sum; revised 2026-10-17)
1. Meet every consignment (fixed targets) or minimise consignment GHG (minimise mode).
2. Maximise compliant RFNBO carried forward to the next quarter, in MJ of final product, summed
   over every storage. Each storage's carried MJ is multiplied by its weight: 1 for a storage
   that feeds a distribution block; otherwise Σ over its outflows of (share of its metered
   outflow) × (the receiving unit's energy yield, product MJ ÷ feed MJ, over the quarter) ×
   (weight of the storage that unit outputs to). So 100 MJ of H₂ in front of a liquefier that
   loses 10% counts as 90 MJ. Weights come from the quarter's own metered data; if a unit did
   not run, from the previous sealed run (`carried_weight_fallback`, set by `--opening-from`),
   otherwise the run stops and asks.
3. Minimise GHG of inventory carried forward.
4. Fewest batches, with a materiality threshold: stage A splits a batch only if that gains at
   least 120 MJ of final product (1 kg H₂; `--batch-materiality-MJ`). This replaces the 0.01 MJ
   per-batch penalty.

Minimising write-off is **not** an objective: it would reward making less RFNBO. Objective 2
replaces "compliant RFNBO volume of the final product": once consignments are fixed it differs
only by counting what is held upstream, at what it would become downstream. It also stops the
optimiser feeding product that would only be written off (test case 1: H₂ stays in storage
instead of making December NH₃ that the spot sale writes off).
Tie-breaks: consignments draw equal-GHG material oldest month first; equal-GHG write-offs take
newest month first.

### 2.9 Out of scope (for now)
Additionality/geography checks, grid-electricity routes, electricity storage, co-products,
plant dispatch/forecasting, Union Database submissions, integration with the system-of-record
prototype.

---

## 3. Architecture

### 3.1 The one rule that matters most
**Separate the calculator from the optimiser.**

- The **calculator** takes a plant config, the data, and a complete set of decisions (batch
  boundaries, renewable/feed allocations, consignment allocations, write-offs) and computes every
  number. It is pure, deterministic and the only source of truth for reported figures.
- The **optimiser** only proposes decisions. Every optimiser result is re-run through the
  calculator and every constraint is re-checked before it is reported.

This keeps the audit trail independent of solver internals, lets auditors re-run any decision
set, and lets the test workbooks be checked without the optimiser.

### 3.2 Package layout
```
rfnbo/
  pyproject.toml                # uv, Python 3.12
  CLAUDE.md                     # conventions (see §7)
  src/rfnbo/
    config/        schema.py    # pydantic models for the block flow diagram
                   validate.py
    data/          ingest.py    # CSV/Excel → validated 15-min frames (UTC)
                   quality.py   # gap/negative/balance flags
                   calendar.py  # plant tz, month/hour/quarter boundaries
    model/         entities.py  # Batch, SubBatch, Allocation, AggregatedBatch,
                                # Consignment, LedgerEntry, WriteOff, Run
    calc/          batch.py     # RFNBO share, GHG per batch
                   ledger.py    # storage ledgers, quarter end, write-off
                   consign.py   # aggregation and shipped GHG
                   check.py     # every constraint as a named, testable check
    optimise/      batching.py  # stage A: segmentation (DP)
                   allocate.py  # stage B/C: LP/MILP via HiGHS
                   pipeline.py  # orchestration, iteration, bounds
                   baseline.py  # one batch per unit per month, FIFO
    trace/         json_log.py  # per-batch traceability JSON
                   run.py       # sealed run record, hashes
    report/        excel.py, csv.py
    ui/            app.py       # NiceGUI (see §3.4)
                   bfd_editor/  # diagram editor component
  tests/
    golden/        # fixtures exported from the test workbooks
    unit/, property/, integration/
  examples/test_plant.json
```

### 3.3 Stack
- Python 3.12, `uv`, `pydantic` v2, `polars` (or pandas) for time series.
- HiGHS via `highspy` or `linopy` for LP/MILP; numpy for the batching dynamic programme.
- `openpyxl` for Excel export, `hypothesis` for property tests, `pytest`.
- UI: NiceGUI (pure Python). Diagram editor: spike two options before committing (§4 phase 2).

### 3.4 Block flow diagram editor
The editor's only job is to produce valid plant-config JSON. Keep it decoupled: the engine never
imports UI code. Spike options:
1. NiceGUI page hosting a JavaScript diagram library (React Flow or Drawflow) as a custom
   component, exchanging config JSON.
2. A small React Flow front end talking to a FastAPI back end.

Pick the one that round-trips the test plant config cleanly with less code.

---

## 4. Build phases

Each phase ends with passing tests and a short demo. Do them in order.

| # | Phase | Deliverables | Acceptance |
| --- | --- | --- | --- |
| 0 | Repo skeleton | uv project, lint (ruff), type check (mypy/pyright), pytest, CI, CLAUDE.md | CI green on empty package |
| 1 | Config schema | Pydantic models for blocks/flows/EFs with validity dates; validator; `examples/test_plant.json` (the scope doc's test plant) | Valid config loads; each validation rule has a failing-case test |
| 2 | Diagram editor | Spike both options, choose one, build editor: add/connect blocks, edit properties, save/load JSON, show validation errors | Test plant can be drawn, saved, reloaded and passes validation |
| 3 | Data ingestion | CSV/Excel import, UTC handling, plant tz calendar (DST), quality flags | Test case 1 data loads: 8,836 intervals, 25-hour day on 25 Oct 2026 |
| 4 | Calculator | Batch GHG/share, downstream pro-rating, compliant/non-compliant, aggregation, ledgers, write-off, physical reconciliation, all checks in `calc/check.py` | Given the workbook's decisions, reproduces every value on the workbook Checks sheets to 1e-6 relative |
| 5 | Traceability | Per-batch JSON (schema §5), lineage graph, sealed run record with input/config hashes | Re-running a sealed run gives byte-identical outputs |
| 6 | Optimiser stage A | Batching dynamic programme per unit and month, hourly mode from 2030 | Isolates dark intervals with no surplus; averages when surplus exists; hourly batches ≤ 4 intervals |
| 7 | Optimiser stages B/C | Feed allocation, consignment aggregation, write-off as LP/MILP; pipeline; baseline | Matches or beats regenerated test case 2 on objectives; every constraint check passes |
| 8 | Reports | Batch register, lineage, consignment report, renewable matching, ledgers, reconciliation, calc log, baseline comparison; Excel/CSV export | Reports regenerate the workbook tables |
| 9 | Results UI | Run screen, drill-down consignment → batches → intervals | A user can trace any consignment to 15-minute data in the UI |
| 10 | Multi-step specification and test case 3 | Domain decisions §10.3 settled and logged; test case 3 workbook (electrolyser → H₂ buffer → liquefier and compressor in parallel; shared renewable pool) with hand-calculated expected values; JSON fixtures | Workbook Checks sheet passes against an independent script; open questions in §10.3 all answered |
| 11 | Config, validation and editor for networks | Storage feeding several units; unit outputs split to several storages (metered); pass-through storage for direct unit → unit links; units with several product feeds; conditioning units; new named rules; editor support | Test plant 3 can be drawn, saved, reloaded and validates; each new rule has a failing-case test; test plants 1 and 2 unchanged |
| 12 | Calculator and traceability on networks | Any processing network (directed, acyclic) in topological order; split outputs; pass-through ledgers; renewable split across units; lineage through N steps; new checks | Given test case 3's decisions, reproduces every value to 1e-6; golden tests 1 and 2 still pass; GHG recomputes from every record |
| 13 | Optimiser stage A for several electricity users | Renewable pool split between the electrolyser and conditioning units (per interval, within correlation periods); batching of conditioning units where their GHG depends on boundaries | Never over-allocates a pool; on test case 3 splits renewable as the hand calculation does; stage A results unchanged on test cases 1 and 2 |
| 14 | Optimiser stage B on networks | Joint feed LP for parallel units sharing a storage; successive LP with back-propagated GHG limits for units in series; objective across several final products; bound per product | Matches or beats test case 3; series test (H₂ → compressor → NH₃) meets every check; gap to bound reported |
| 15 | Stage C, reports and UI for several final products | Consignments per product and storage; ledgers, reports and drill-down across N steps; baseline for networks; performance budget for larger plants | Every check passes on test case 3; a consignment traces to 15-minute data through every step; one quarter of test plant 3 in under 10 minutes |
| 16 | Purchased RFNBO (proof of sustainability) | RFNBO supply block with metered flows into chosen units; PoS records (dispatch date, kg, GHG); optimiser allocates each PoS to supply flows within the mass balance period; checks, lineage, reports; test case 4 workbook first | Given test case 4's decisions, reproduces every value to 1e-6; optimiser matches or beats it; every PoS kg traces to the unit batches it fed; §11.1 questions answered |
| 17 | Plant data entry in the UI | Data screen: upload files, map columns to the series the plant needs, preview with quality flags, logged corrections with reasons, forms for consignments, PoS and opening ledger; run the optimiser from the UI | Test case 1 set up entirely in the UI seals the same run id as the CLI; a correction shows in the run record and report; browser test |

---

## 5. Traceability JSON (per batch)

Follow the shape used in test case 2. Every input, quantity, unit, emission factor, factor unit
and emissions figure, so the GHG can be recomputed from the record alone.

```json
{"batch":"E-2026-10-002","unit":"Electrolyser","start":"2026-10-01T09:30+02:00",
 "end":"2026-10-01T15:30+02:00","intervals":24,"renewable_available_kWh":50360.0,
 "inputs":[
  {"input":"Renewable electricity (matched in batch)","qty":50360.0,"uom":"kWh","ef":0.0,"ef_uom":"gCO2e/kWh","emissions_g":0.0},
  {"input":"Non-renewable electricity","qty":9640.0,"uom":"kWh","ef":400.0,"ef_uom":"gCO2e/kWh","emissions_g":3856000.0},
  {"input":"Water","qty":12000.0,"uom":"kg","ef":2.0,"ef_uom":"gCO2e/kg","emissions_g":24000.0}],
 "adders":[{"name":"Electrolyser fixed adder","value":1.0,"uom":"gCO2e/MJ","emissions_g":144000.0}],
 "output":{"product":"H2","qty":1200.0,"uom":"kg","lhv":120.0,"lhv_uom":"MJ/kg","energy_MJ":144000.0},
 "rfnbo_share":0.839333,"ghg":27.944444,"ghg_uom":"gCO2e/MJ","threshold":28.2,
 "compliant":true,"rfnbo_qty":1007.2,"rfnbo_uom":"kg",
 "formula":"ghg = (sum(inputs.emissions_g) + adder_g) / output.energy_MJ"}
```

Downstream batch records list each upstream batch as an input (kg, MJ, GHG, emissions), plus
`rfnbo_share` and which emissions were pro-rated. Add `config_version`, `run_id` and the
regulation clause for each rule check.

---

## 6. Optimisation approach

The full problem is non-linear (batch GHG is a ratio that depends on boundaries) and too large
to enumerate (≈4.4 million possible batches per unit per month). Use a staged pipeline and
report the gap to a bound.

**Stage A — batching (per unit with renewable input, per month).** Exact dynamic programme over
15-minute intervals using prefix sums: for each end interval, evaluate every start within the
allowed window. Batch value = RFNBO volume, with a Lagrangian penalty μ·(GHG − downstream limit)
so batches are shaped for what downstream units can absorb. Search μ to maximise final-product
compliant RFNBO. Downstream limit example (test plant): H₂ feed must blend to ≤ 13.45 gCO₂e/MJ
for NH₃ to stay ≤ 28.2. A per-batch penalty (the §2.8 materiality threshold) breaks ties
towards fewer batches. O(n²) per month is fine (n ≈ 2,976).

**Stage B — feed allocation (LP).** Variables x[b, m] = kg of upstream batch b to downstream
batch m. Each downstream batch's compliance is linear in x:
`Σ_b x[b,m]·(k·GHG_b + e_m + A − 28.2) ≤ 0`. Include non-compliant upstream batches (§2.5). The
same pattern applies to every downstream unit. Small secondary weight prefers same-month feed
for readability.

**Stage C — consignments, ledgers, write-off (LP/MILP).** With downstream GHGs fixed by stage B,
consignment GHG constraints are linear. Enforce: compliant sub-batches only, one month per
aggregated batch, quarter-end ledger ≤ physical, write-off highest GHG first.

**Iteration and bounds.** If stage C cannot meet a consignment, feed that back (e.g. reserve
low-GHG feed for a downstream batch) and re-run B. Always report: objective values, which stage
bound, and the gap to an upper bound (e.g. LP relaxation). Later, consider column generation
(the DP as the pricing problem) for a provable optimum.

**Baseline.** Always also compute one batch per unit per month with first-in-first-out
allocation, and show the optimised result next to it.

**Performance budget.** One quarter, test plant: target under 5 minutes on a laptop. Profile
before optimising code.

---

## 7. CLAUDE.md conventions (paste into the repo)

- Metric units only: kg, kWh, MJ, gCO₂e/MJ. Every quantity field carries its unit.
- Store timestamps in UTC; derive months, hours and quarters only via `data/calendar.py` using
  the plant time zone.
- The calculator is pure and deterministic. No randomness, no wall-clock reads.
- Every reported number comes from the calculator, never directly from the solver.
- Every rule is a named check in `calc/check.py` with a regulation reference and a unit test.
- Never fill or alter input data silently.
- Floating-point comparisons use explicit tolerances (1e-6 relative); threshold checks use
  `≤ threshold + 1e-9`.
- Pin solver and versions; record them in every run.
- If a domain rule is unclear, stop and ask; do not invent regulatory behaviour.
- Golden tests from the workbooks must pass before merging any change to `calc/` or `optimise/`.

---

## 8. Testing strategy

- **Golden tests:** export the workbook decision sets and expected values to JSON fixtures
  (`tests/golden/`). Phase 4 must reproduce them exactly; phase 7 must match or beat them.
- **Property tests (hypothesis):** batches never cross months; hourly batches ≤ 1 h after the
  switch date; mass conserved per unit; ledger non-negative and ≤ physical at quarter end;
  consignments contain only compliant sub-batches from one month per aggregated batch; GHG
  recomputed from JSON equals reported GHG.
- **Scenario tests (scope doc acceptance tests 1–12):** full cover, renewable shortfall, month
  boundary with DST, hourly correlation, fixed targets, minimise mode, quarter-end shortfall,
  infeasible target, reproducibility, free batch sizing, consignment aggregation, short RFNBO feed.
- **Still missing from the test data:** renewable split between units (test 2), H₂ write-off,
  short RFNBO feed (test 12), and non-compliant RFNBO saved by blending. Build these as small
  synthetic datasets in phase 4.

---

## 9. Risks and open points

- The reference results in test case 2 are not a proven optimum; the app should match or beat
  them, not reproduce every boundary.
- Routing high-GHG feed into product that will be written off is optimal under §2.8 but may
  draw auditor questions. Make it visible in reports.
- Within-batch renewable matching under monthly correlation is a deliberate interpretation;
  keep it configurable.
- Solver performance at 15-minute resolution for larger plants is unproven; measure early
  (phase 6).

---

## 10. Extension: multiple and parallel processing steps (phases 10–15)

Phases 0–9 assume one chain: one electrolyser → one storage → one downstream unit → one final
product. Real plants condition hydrogen in several steps, some of them in parallel, for
example an electrolyser feeding a liquefier (liquid H₂) and a compressor (compressed H₂) at
the same time, or a compressor feeding NH₃ synthesis. Phases 10–15 generalise the app to any
directed, acyclic processing network. The regulatory basis does not change.

### 10.1 What the code assumes today

These are the single-chain limits the new phases remove. Everything else (topological unit
order, energy-based RFNBO share over several feeds, per-interval renewable allocation
decisions, the lineage graph) already works on networks.

| Where | Assumption |
| --- | --- |
| ~~`config/validate.py` `unit_outputs`~~ | ~~A unit sends its whole output to exactly one flow~~ (removed in phase 11) |
| ~~`config/validate.py` `ALLOWED_CONNECTIONS`~~ | ~~No unit → unit link~~ (phase 11: a pass-through storage) |
| ~~`calc/batch.py` `output_storage`~~ | ~~One output storage per unit~~ (phase 12: `output_storages`; the single-chain optimiser still uses `output_storage`) |
| `calc/consign.py` `consignment_route` | One storage feeds each distribution block (kept: validated by `distribution_single_product`) |
| `optimise/batching.py` | ~~One unit per renewable pool~~ (phase 13: `optimise/pool.py`); one electrolytic unit per pool |
| ~~`optimise/allocate.py` `feed_storage`~~ | ~~One downstream stage~~ (phase 14: `stages`, `stage_b_network`) |
| ~~`optimise/pipeline.py` `bound`~~ | ~~Single downstream stage only~~ (phase 14: `value_bound` for any plant) |

### 10.2 Domain rules to add (decided 2026-10-17; see §10.3)

1. **Networks.** Units, storages and flows form a directed acyclic graph. A storage may feed
   several units; a unit may take product from several storages (RFNBO share by energy over
   all product feeds, as today); a unit may send its output to several destinations, each
   output flow metered.
2. **Conditioning units.** A unit whose product is the same molecule as its feed (liquefier,
   compressor, purifier) is a processing unit like any other: its RFNBO share follows its
   RFNBO feed, and its GHG adds its own inputs (mostly electricity) and fixed adder.
   `electricity_adds_to_lhv` is false, so its RFNBO share is never set by its electricity, and
   from 2030 it keeps a monthly correlation window (scope doc assumption).
3. **Losses in a unit** (boil-off in a liquefier, leakage in a compressor) are the difference
   between metered feed and metered output. Emissions of the feed are spread over the output
   that remains, so losses raise GHG per MJ; they are not written off separately.
4. **Split outputs.** When one batch's output is metered to several destinations, the
   optimiser assigns the batch's RFNBO to each output flow individually (a decision, not a
   pro-rata split): each flow receives at most its metered quantity of RFNBO, the parts add up
   to the batch's RFNBO, and each part enters the destination's ledger with the batch's ID,
   GHG, RFNBO share and month.
5. **Pass-through storage.** A direct unit → unit link is modelled as a storage with no
   inventory meter whose physical inventory is zero. Its RFNBO ledger must close at zero each
   quarter: anything not consumed downstream is written off by the §2.7 rule.
6. **Shared renewable pool.** Renewable power is split between all units that draw on it, per
   interval, never above what was available (rule exists; the optimiser now decides it).
   Matched renewable electricity counts as zero emissions for any unit; it raises RFNBO share
   only for units where electricity adds to product LHV. Every unit matches renewable within
   its own batch. Batches of conditioning units are variable in size, from one interval up to
   one calendar month, and the optimiser chooses their boundaries; the electrolyser keeps its
   hourly limit from 2030.
7. **Several final products.** Each final product has its own storages, ledgers, distribution
   blocks and consignments. Aggregated batches never mix products. Objectives as §2.8: first
   meet every consignment (a specific quantity of a specific output for a specific customer);
   then maximise compliant RFNBO carried forward at the end of the period (within each
   storage's measured inventory), in MJ of final product with the storage weights of §2.8.
8. **Intermediate compliance.** An intermediate RFNBO batch above 28.2 gCO₂e/MJ is non-compliant
   RFNBO (§2.5): it may be fed on and saved by blending, never consigned. Only the step whose
   output is consigned must be compliant.

### 10.3 Decisions (settled 2026-10-17)

| # | Question | Decision |
| --- | --- | --- |
| 1 | What does the optimiser pursue with several products? | Meet each consignment first (a specific amount of a specific output for a specific customer); then maximise the RFNBO that can be stored at the end of the period, in MJ across products |
| 2 | How is RFNBO assigned when a batch's output is split? | The optimiser assigns RFNBO to each output flow individually (rule 4) |
| 3 | Direct unit → unit links? | Pass-through storage whose RFNBO ledger must be zero at quarter end (rule 5); the editor creates it |
| 4 | Renewable matching for conditioning units? | Within the batch; batches of variable size, up to one month, boundaries chosen by the optimiser (rule 6) |
| 5 | Losses inside a unit? | They raise the GHG of the batch produced: emissions of the whole feed over the smaller output (rule 3) |
| 6 | Recycle flows (boil-off gas back to a compressor)? | Out of scope; flows stay one way |
| 7 | Hydrogen consigned at more than one point? | Allowed: every storage that feeds a distribution block is a final product point |
| 8 | A unit's output straight to customers, with no storage (a tube trailer fill)? (decided 2026-10-27) | Allowed: the unit feeds the distribution block through a pass-through storage, as in rule 5 (the editor inserts it when a unit is dragged onto a distribution block). Consignments are entered as usual and drawn from its ledger, which closes at zero each quarter: RFNBO no consignment takes is written off (§2.7); consignments cannot exceed the RFNBO the unit produced (ledger ≥ 0). A pipeline is the distribution block itself; a compressor before it stays a processing unit, its emissions in the batch GHG. No new emissions rule |
| 9 | Does a pass-through storage's outflow need its own meter? (decided 2026-10-28) | No: it holds nothing, so what leaves it each interval is what came in. An outflow given no series takes the inflow's: the metered flow into it when the unit splits its output, else the unit's production series. A series given on the outflow is kept (and checked against the inflow by the storage balance) |
| 10 | At what time step is the plant calculated? (decided 2026-10-30) | At the data's own step: hourly data is calculated hour by hour, 15-minute data in 15-minute intervals; nothing is resampled. The step is read from the timestamps and must divide an hour (1 to 60 minutes), so every clock hour, month and quarter starts on an interval; all data files of a quarter have the same step. Batches are runs of whole intervals as before |
| 11 | Data in other units? (decided 2026-10-30) | Converted on reading to the unit a series is named in (kWh, kg): energy (Wh, kWh, MWh, GWh), mass (g, kg, t), and rates (W to GW, kg/h, t/h) multiplied by the interval's length. A column's unit is read from its header ("Solar Power (MWh)") or set in the mapping; a column of the wrong kind is refused. Every conversion is reported with the run (an info flag, sealed) |
| 12 | Comparison with a baseline? (decided 2026-10-30) | Not by default: a run calculates and seals the result for the given data and rules. The baseline (one batch per unit and month, FIFO) is calculated only on request (`rfnbo optimise --compare`) |
| 13 | Data for less than the quarter (e.g. one month)? (decided 2026-10-30) | Run provisionally, quarter to date: from the quarter's start to the data's last interval, closed as if the quarter ended there (write-offs and carried RFNBO at the data's end, by §2.7). Gaps within that period are still errors. Labelled PROVISIONAL in the quality flags, the sealed record, the report summary, the results browser and the CLI; a provisional run cannot open the next quarter. The mass balance period stays the calendar quarter |

**Follow-up (settled 2026-10-17).** (a) The single-chain optimiser uses the new objective 2
now (§2.8). (b) Non-compliant RFNBO cannot be stored and carried between quarters: it is
written off at quarter end (§2.7), so only compliant RFNBO counts. Objective 2 weights each
storage by its downstream energy yield, measured from the quarter's data (weighted over
several downstream units by metered outflow). Stage A's tie-break is a 120 MJ materiality
threshold.

### 10.4 Phase details

**Phase 10 — Specification and test case 3.** Before code, as for phases 0–9: build test
case 3 as a workbook with live formulas and hand-calculated expected values, and an
independent Python check. Plant: one electrolyser → H₂ buffer storage → liquefier → LH₂ storage
→ customer A, and in parallel → compressor → CH₂ storage → customer B; one renewable pool
shared by electrolyser, liquefier and compressor; a boil-off loss in the liquefier; one split
output; a pass-through link; a consignment per product. Variants: hourly correlation (2030),
renewable shortfall that forces a choice between electrolyser share and liquefier GHG,
infeasible target on one product only. Export fixtures to `tests/golden/`. Record every §10.3
answer in the scope doc's decisions log.

*Done 2026-10-17.* Workbook `tests/golden/workbooks/RFNBO_optimiser_test3_multistep_Q4_2026.xlsx`
(built by `tests/golden/build_tc3_workbook.py`, checked by `tc3_reference.py`, fixtures from
`export_tc3.py`); draft network config `tests/golden/tc3.plant.json`. The expected optimum keeps
the H₂ buffer full of RFNBO rather than liquefying H₂ that would be written off as LH₂; this
holds because liquefying it would only add LH₂ that is written off. Variant V2 shows the
liquefier given just enough renewable to comply, since non-compliant LH₂ cannot be carried.

**Phase 11 — Config, validation and editor.** *Done 2026-10-17:* `pass_through` storages,
rules `split_outputs_metered`, `pass_through_links`, `unit_feed_products`, `storages_produced`
(and `unit_outputs` relaxed to at least one output); a unit dragged onto a unit in the editor
inserts a pass-through storage; flows coloured by product, pass-through links dashed; data
quality balances split outputs on their meters and pass-throughs to zero. Plan: Schema: output flows of a unit carry a metered
series when there are several; storage `pass_through` flag (no inventory series, physical
zero); unit → unit links allowed and saved as an implicit pass-through storage. Rules (each
with a failing-case test): every split output metered; product consistent along every path;
no cycles (existing); every final product reachable from a unit; a distribution block fed by
storages of one product. Editor: ports accept several connections; pass-through links drawn
as dashed lines; per-product colouring. Test plants 1 and 2 load unchanged.

**Phase 12 — Calculator and traceability.** *Done 2026-10-19:* decision sets carry
`output_allocations`; ledgers are keyed by storage and batch, so a split batch sits in every
storage it went to; feed draws on the storage that feeds the target unit; new checks
`output_split` and `pass_through_closes`; records show where each batch's RFNBO went and
which storage each feed came through. Test case 3 and the reference's hourly and shortfall
cases reproduce to 1e-6. Plan: Remove the single-output and single-storage
assumptions listed in §10.1. Split outputs create one ledger entry per destination. Pass-through
ledgers close at zero. New checks: split output adds up to production; pass-through ledger
closes at zero; renewable pool across all units (existing check, now exercised). Traceability
records list every upstream batch at every step; lineage drill-down crosses N steps. Golden
tests 1 and 2 must still pass unchanged.

**Phase 13 — Stage A for several electricity users.** *Done 2026-10-19* (`optimise/pool.py`):
stage A alternates with a pool split until stable. The electrolyser is batched on its pool;
each batch keeps what it can match from its own intervals; the surplus covers the
conditioning units within their month-long batches; a conditioning batch that would miss the
threshold takes the shortfall from the electrolyser, from the end of the month backwards, when
its compliant product is worth more (objective 2) than the electrolyser RFNBO given up; then
the electrolyser is re-batched. The baseline gives the electrolyser first call with no repair.
Test case 3: full cover in the base case; the hourly variant's 2,209 batches; in the shortfall
the liquefier takes exactly the least it needs (190,615.7 kWh). Conditioning units keep one
batch per month. Plan: Extend the dynamic programme with a
renewable split: for each correlation window, renewable kWh is shared between the electrolyser
(raises RFNBO share) and conditioning units (lowers their GHG only). Approach: a per-window LP
inside the DP value for the electrolyser, with the conditioning units' monthly demand as a
second pool claim; or alternate stage A (electrolyser boundaries) with a split LP until stable.
Conditioning units keep one batch per month unless splitting lowers final-product GHG.
Acceptance includes: the electrolyser's results are unchanged when no other unit draws on the
pool.

**Phase 14 — Stage B on networks.** *Done 2026-10-19:* downstream units are grouped into
stages by the units that feed them. A stage of parallel units (or one fed through a split
output) is one LP with joint availability rows and the split output's assignment as
variables; units in series are solved stage by stage, each intermediate held to the blended-
feed limit its downstream unit needs (back-propagated). Objectives as §2.8, with the value
objective from the second pipeline round. A bound on objective 2 (each storage at most its
measured inventory and its largest possible RFNBO inflow, less consignments) is reported for
any plant. Test case 3: carried value equals the hand calculation (gap to the bound 0); the
shortfall variant is beaten (19,427,373 vs 19,421,847 MJ); the infeasible target is reported on
CA only. A series plant (H₂ → compressor → NH₃) meets every check. The baseline splits outputs
pro rata to the metered flows. Plan: Parallel units sharing one storage: one LP with joint
availability rows (linear, exact). Units in series: downstream GHG depends on the GHG of an
intermediate batch, which depends on its own feed, so the joint problem is bilinear (a pooling
problem). Approach: successive linear programmes in topological order, with GHG limits
back-propagated from each final product to every intermediate unit (as the 13.45 gCO₂e/MJ limit
is derived today), iterated until stable; report the gap to an upper bound from the LP with
intermediate GHGs fixed at their best values. Objective 2 as settled in §10.3 question 1.

**Phase 15 — Stage C, reports and UI.** *Done 2026-10-19:* stage C runs per final-product
storage (each distribution block is fed by storages of one product, so every consignment
carries one product, shown in reports and the UI). New reports: Products (each storage's RFNBO
in, fed on, consigned, written off and carried, with its weight and carried value), Split
outputs, and Renewable by batch for a shared pool; the per-period matching table shows only
hourly-batched units under hourly correlation. The results browser shows products, split
outputs and the storage each feed came through; a browser test follows CA (LH₂) through the
liquefier and the split electrolyser batch to 15-minute data. Baseline on networks: phase 14.
Performance (`tests/golden/benchmark_networks.py`): test plant 3 about 12 s per quarter, a
five-unit plant about 13 s, every check passing; stage A's batching search is about 90% of
the time, so it is where to start if larger plants need speed. Plan: Stage C per final-product storage; consignments carry a
product; the baseline (one batch per unit per month, FIFO) on networks; reports grouped by unit
and product; drill-down from a consignment through every step to 15-minute data. Measure the
performance budget on test plant 3 and a five-unit plant; profile before optimising.

### 10.5 Order

Phases 10 and 11 can start now (the §10.3 decisions are settled). Do phase 12 before phase 8 (reports), so that reports are
written once for any network rather than reworked; then 13–15, with phase 9 (results UI)
built on the network drill-down from phase 15.

---

## 11. Extension: purchased RFNBO and plant data entry (phases 16–17)

Requested 2026-10-17. Phase 16 done 2026-10-19, phase 17 2026-10-20. §11.1's questions are all answered (2026-10-17) and logged in the scope
doc.

### 11.1 Purchased RFNBO with a proof of sustainability (phase 16)

RFNBO bought from a third-party supplier (for example H₂ delivered by tube trailer) enters the
plant as an input to units the user chooses, such as the liquefier or the compressor.

Draft rules:

1. **Supply block.** A new block kind, *RFNBO supply*: supplier, product (e.g. H₂) and LHV. It
   connects by flows to the units or storages it may feed; each flow carries a metered 15-minute
   series of the physical quantity delivered (kg). Not all of it is RFNBO: product without PoS
   credentials allocated to it is non-RFNBO.
2. **Proof of sustainability.** Each PoS has an ID, supplier, product, dispatch date, quantity
   (kg) and GHG intensity (gCO₂e/MJ), which includes distribution to this plant. It must be
   compliant (≤ 28.2 gCO₂e/MJ). It belongs to the mass balance period (calendar quarter, plant
   time zone) of its dispatch date. PoS records are entered in the UI (phase 17) or
   imported from CSV, and are part of the sealed run's inputs.
3. **Allocation is a decision.** The optimiser allocates each PoS's quantity to the supply
   flows in the same quarter: into a unit, per batch, allocated kg ≤ metered kg delivered in
   that batch's intervals; into a storage, the allocated kg enters the storage's ledger as a
   batch with the PoS's ID and GHG. For each PoS, allocated kg ≤ its quantity; product must
   match. One PoS may be split across flows, units, storages and batches. PoS quantity not
   allocated by quarter end is written off.
4. **Effect on the unit.** Allocated PoS kg is RFNBO feed with the PoS's GHG intensity, treated
   exactly like RFNBO feed from a storage (§2.4): it counts in the unit batch's RFNBO share by
   energy over all RFNBO feeds and adds its emissions to the feed emissions. Purchased RFNBO
   carries its own certification, so the temporal-correlation rules for electrolysers do not
   apply to it.
5. **Objectives unchanged (§2.8).** Allocation is chosen with feed allocation (stage B), which
   stays linear: a low-GHG PoS goes where it rescues compliance or a consignment target, and PoS
   quantity is not pushed into product that would be written off.
6. **Traceability and checks.** Each PoS is a lineage node, so a consignment drills down to the
   PoS that fed it as well as to 15-minute data. New named checks: PoS dispatched in the
   quarter; allocation within metered delivery per batch and flow; allocation within the PoS
   quantity; product match. A reconciliation flags metered supply against PoS totals.
7. **Reports.** A PoS register (received, allocated, unallocated, where it went) and PoS
   columns in the feed allocation and lineage reports.

Decisions (2026-10-17):

| # | Question | Decision |
| --- | --- | --- |
| 1 | Which period does a PoS fall in? | The quarter it lands in (its dispatch date). Its RFNBO credentials are allocated to the supplier's metered flow in that quarter; the metered flow is not all RFNBO, and the rest is non-RFNBO |
| 2 | PoS quantity not allocated by quarter end? | Written off. Credentials allocated to a flow into a storage are held in that storage's ledger and follow §2.7 like any other batch |
| 3 | Transport from the supplier? | Included in the PoS GHG intensity: no adder on receipt |
| 4 | PoS above 28.2 gCO₂e/MJ? | Not allowed: a PoS must be compliant. A named validation rule rejects it |
| 5 | Into a storage as well as a unit? | Yes: supply flows may go into units or storages |
| 6 | Allocation against metered flow | Per batch (into a unit) or per interval range (into a storage), allocated kg ≤ metered kg, within the quarter |
| 7 | May credentials be allocated to flow metered before the PoS's dispatch date? | Yes, as long as both are in the same mass balance period (calendar quarter) |
| 8 | How purchased RFNBO is aggregated | The PoS needs only its dispatch date; the supplier's production month plays no part. Processed further in the plant, it becomes part of that unit batch (e.g. LH₂, CH₂) and is aggregated as that batch. Passed through without processing (held in a storage that feeds distribution), it is aggregated only with other passed-through RFNBO dispatched in the same calendar month (plant time zone), and its dispatch month sets its place in the write-off order |

*Done 2026-10-19.* Test case 4 (`tests/golden/workbooks/RFNBO_optimiser_test4_purchased_Q4_2026.xlsx`,
built by `build_tc4_workbook.py`, checked by `tc4_reference.py`, fixtures from `export_tc4.py`):
test plant 3 plus a supplier delivering 9,600 kg of H₂ into the compressor (1–10 November) and
9,600 kg into the liquefier (11–20 December), no LH₂ spot sale; POS-A 8,000 kg at 3.0, POS-B
9,000 kg at 12.0 (dispatched after some of the delivery it covers), POS-C 6,000 kg at 27.9 (only
2,200 kg covered; 3,800 kg written off). The expected optimum covers every metered kg and keeps
the H₂ buffer full: November's liquefier yield (0.97) is below the buffer's weight (0.9768), so
that batch takes 2,000 kg less RFNBO (the optimiser found this before the hand calculation did).
Variant V1 restores the spot sale: LH₂ storage is over its inventory, so no credentials go to
the liquefier. What was built:

- Schema: block kind `rfnbo_supply` (supplier, product, LHV); rule `supply_flows` (every supply
  flow metered, never into an electrolytic unit); supply flows may go into units or storages;
  storage balance and reconciliation count deliveries; storage weights and stage A's blend limit
  count them as feed. The editor has the block and its form.
- Decision sets: `pos` (records, inputs like consignments) and `pos_allocations` (PoS, flow, kg,
  and a batch for a flow into a unit or a start and end for a flow into a storage).
  `rfnbo optimise --pos file.csv|json` imports them (`rfnbo.model.pos`, rules `pos_compliant`,
  `pos_in_quarter`, `pos_supplier`, `pos_unique` before the run).
- Calculator: credentials into a unit are RFNBO feed lines (origin `pos`, via the supply flow);
  into a storage they are a ledger line under the PoS id (origin `purchased`, dispatch month),
  which can be fed on, consigned or carried. PoS register and supplier reconciliation in
  `calc/purchase.py`. Named checks `pos_compliant`, `pos_in_quarter`, `pos_within_quantity`,
  `pos_product`, `pos_within_delivery` (per flow and batch or range; ranges may not overlap) and
  `aggregated_same_source` (passed-through RFNBO is aggregated only with other passed-through
  RFNBO, by dispatch month).
- Lineage: each PoS is a node; PoS → batch (feed) and PoS → `write-off:pos:<quarter>` for what was
  not allocated. Records show each feed line's origin; the results browser has a PoS register and
  a PoS page, and a browser test follows CA through December's liquefier batch to POS-B.
- Optimiser: stage B allocates credentials in the network LP with the feed (any stage with a
  supply flow uses it). From the second round a credential is valued at its marginal kept share:
  nothing into a batch whose storage is over its inventory. Ties: least purchased emissions
  (lowest-GHG credentials), then credentials nearest their dispatch month; the objective key ends
  with purchased emissions. Deliveries into storages get credentials after stage B, lowest GHG
  first, up to the storage's room. Baseline: credentials in dispatch order, FIFO.
- Reports: PoS register, PoS allocations, supply reconciliation; PoS rows in each feed table and
  PoS sources in the lineage table.

Phase 16 details. Before code, as for phase 10: build test case 4 as a workbook with live
formulas, hand-calculated expected values and an independent Python check. Plant: test plant 3
plus a supplier delivering H₂ into the compressor and the liquefier; three PoS (one low-GHG,
one near the threshold, one only part covered by metered delivery). Then: schema and editor
(supply block, metered flows), `PoS` and `PoSAllocation` entities in decision sets, calculator
and checks, lineage and traceability records, stage B allocation, reports. Depends on phase 12
(units with several RFNBO feeds).

### 11.2 Plant data entry in the UI (phase 17)

Today plant data comes in as CSV or Excel files with a JSON column mapping, through the CLI. A
*Data* screen in the app does the same without hand-written files:

- **Run setup.** Choose the plant config and quarter; upload the data file(s); map columns to
  the series the plant needs (the list comes from the config; headers are matched
  automatically where they agree); preview in the plant time zone with every quality flag
  (gaps, negatives, meter-balance breaks). Nothing is filled silently.
- **Corrections.** A value or range can be corrected only with a reason. Corrections are kept
  as a log applied on top of the original file; the sealed run records the original file's
  hash and the corrections, and reports list them.
- **Forms** for consignments (customer, output, kg, ship date, target or minimise mode), PoS
  records (phase 16) and the opening ledger (manual entry or a previous sealed run). Constants
  (emission factors, LHVs, adders) stay in the diagram editor.
- **Run.** Optimise from the UI; the sealed run opens in the results browser.
- **Project folder.** The data files, mapping, corrections, consignments and PoS records are
  saved as plain files, so the CLI and the UI work on the same inputs.

*Done 2026-10-20.* `rfnbo data <folder>` serves the Data screen at `/data`, with the results
browser on the folder's `runs/`. A project folder holds `project.json` (plant config, quarter,
data files and their column mapping, opening from a sealed run), `consignments.json`,
`pos.csv`, `corrections.json`, `opening.json` and `data/` (uploads, never changed);
`rfnbo optimise --project <folder>` runs the same files and gives the same run id. Columns
whose header agrees with a series name are mapped automatically; a saved source spec can be
imported. Corrections set one series to a value over [from, to) in plant time, each with a
reason, never overlapping; a wall time the clock change makes ambiguous or skips is refused
until its UTC offset is given. A missing interval stays an error unless every required series
then has a value. Sealed runs record the corrections next to the original files' SHA-256, and
reports list them (Data corrections). The PoS form applies the §11.1 rules on entry. Tests:
`tests/integration/test_data_project.py` and the browser test `tests/e2e/test_data_e2e.py`
(upload, map, correct, consignment, PoS, run, open in the results browser).

Typing whole 15-minute series by hand (8,836 intervals a quarter) is not offered; series come
from files and are corrected in the UI. Phase 17 does not depend on the network phases and
can be done at any time; the PoS form arrives with phase 16.

---

## 12. No averaging across batches (phase 18)

Decided 2026-10-20, from the certification-scheme rules (ISCC EU 203 v4.1 §4.2, EU 205-1 v1.4;
other recognised schemes word the same rules differently):

> "if batches with different GHG emissions figures are physically mixed, the batches have to be
> kept separate in the quantity bookkeeping. Creating an average of the GHG emissions of
> different batches is not allowed." (ISCC EU 203 §4.2) Processing adjusts a batch's size by a
> conversion factor; its characteristics stay assigned to it.

> "all individual values considered in the average must meet the 70% GHG savings threshold."
> (ISCC EU 205-1, calculation period: at most one calendar month; monthly until 2029, hourly
> from 2030 for electrolysis)

| # | Question | Decision |
| --- | --- | --- |
| 1 | A unit batch fed by RFNBO from several sources (own batches, PoS; revised in §12.1) | The batch's RFNBO output is split into **parts**, one per source GHG: each part keeps its source's GHG, adjusted by the unit's yield, own emissions and adder; parts are never averaged. Purchased RFNBO is never a part of the same aggregated batch as the plant's own (replaces §11.1 decision 8's "becomes part of that unit batch") |
| 2 | Saving non-compliant RFNBO by blending (§2.5) | Not allowed, for the plant's own RFNBO as well as purchased: a part from a non-compliant source stays non-compliant |
| 3 | Averaging interval values into a monthly batch (revised in §12.1) | Only values that each meet the threshold. Until 2029 the electrolyser's GHG is calculated per batch of at most a month, with renewable matched over the batch (temporal correlation is monthly), so a batch is one value; from 2030 electrolyser batches are single clock hours and are not averaged further. Averaging happened only downstream and in consignments, which decisions 1 and 4 remove |
| 4 | Consignment GHG (revised in §12.1: averaged within an aggregated batch) | No average: each part of a consignment, plus the distribution adder, must meet the target and the threshold. Reports show each part's shipped GHG and the highest |
| 5 | Purchased RFNBO carried into the next quarter | Keeps its purchased origin in the opening ledger, so it is still aggregated only with purchased RFNBO |
| 6 | Credentials to rescue a conditioning unit short of renewable | Not done: sources are not mixed (decision 2) |

Consequences:

- **Calculator.** A downstream batch's RFNBO output is a set of parts. For part j fed MJ_j of
  RFNBO at g_j, with C the feed the batch consumed (MJ), E its product energy and own its own
  emissions: share_j = MJ_j ÷ C, kg_j = share_j × product kg and
  GHG_j = g_j × C ÷ E + own ÷ E + adder. Parts with the same origin and GHG (to 9 dp) are one
  part (identical characteristics may be summarised). A single part keeps the batch's id; several
  are `<batch>.1`, `<batch>.2`, … in ascending GHG. Ledgers, feed, consignments and write-offs
  work on parts. A batch is compliant when every part is.
- **Optimiser.** A source can feed a batch compliantly only if its own part would comply:
  g_j ≤ (T − A − own ÷ E) × E ÷ C, the limit stage A already computes, now per batch rather than
  on a blend, and back-propagated through units in series (T is the downstream product's own
  limit). Stage A scores a batching by the RFNBO in batches at or below that limit; μ only
  shapes candidate batchings (lower GHG), it no longer values a blend. Stage B has no blend rows
  (each feed variable is allowed or not); stage C allocates only parts at or below each
  consignment's limit. When a consignment cannot be met, stage A runs again with the
  consignment's limit (target − adder) as a cap on the products filling its storage, and the
  pool split repairs conditioning units to that cap; the result is kept if it meets more
  consignments, or the same number with better objectives. The pool split takes renewable from
  the electrolyser in whole intervals (a part-dark interval would raise the GHG of the batch the
  conditioning unit's part is set by). The baseline fills consignments first in, first out among
  the parts within each consignment's limit.
- **Test cases.** Test cases 1 and 2 (the original workbooks) blend hydrogen sub-batches of
  different GHG into NH₃ batches: their fixtures are regenerated under these rules
  (`export_workbook.py --rule-12`, which also applies §12.1) with the workbook values kept under `superseded`. Test
  case 1's consignments were met only by averaging September NH₃ (6) with October's (14.09):
  without it only the 300,000 kg of September NH₃ is at or below their 12 gCO₂e/MJ limit, so
  neither C1 nor C2 can be met. Test case 2's are met from September NH₃. Test case 3's base
  case is unchanged (every electrolyser batch has the same GHG); in its shortfall variant the
  hand calculation gives the liquefier whole intervals and cannot meet CB (December CH₂, 8.33,
  is above CB's limit), while the optimiser meets CB by also giving the compressor renewable.
  In test case 4 POS-C's part would be above 28.2 in either unit, so all 6,000 kg of it is
  written off; the workbook has a Parts sheet and per-part ledgers.

### 12.1 Monthly averaging of compliant batches (phase 19; revises decisions 1, 3 and 4)

Decided 2026-10-20, from ISCC EU 205-1: "The GHG values may be calculated as an average for the
entire production of fuels occurring during a period of at most one calendar month … all
individual values considered in the average must meet the 70% GHG savings threshold. Individual
time interval values with emissions savings of less than 70% are not allowed to be considered
for the monthly average."

| # | Question | Decision |
| --- | --- | --- |
| 7 | Which batches may be averaged | Batches (or parts) of the **same source** and the **same production month**, each meeting the threshold on its own. The source is the plant's own production, or a single supplier: RFNBO from different suppliers is never averaged or aggregated together, at any stage |
| 8 | Production month | Own RFNBO: the month of its final processing (the unit batch that made it). Purchased RFNBO: always the PoS's dispatch month, whether passed through or processed by the plant (for example credentials into the compressor or liquefier) |
| 9 | Where averaging may happen | Anywhere, while decision 7 holds: H₂ (or any intermediate) may be averaged before it feeds a downstream unit, and the parts of a consignment's aggregated batch are averaged by energy. Non-compliant RFNBO never enters an average (decision 2 stands) |
| 10 | Who decides | The optimiser: averaging is a decision (a pool label on feed, and the content of each aggregated batch), taken only where it is beneficial |
| 11 | Which threshold each value in an average must meet | Each lot as the product it is at that point, per MJ of that product's LHV: an H₂ lot entering a pool is ≤ 28.2 gCO₂e/MJ of H₂ (120 MJ/kg); the NH₃ part made from the pool is ≤ 28.2 gCO₂e/MJ of NH₃ (18.6 MJ/kg); an aggregated batch is checked per MJ of the product shipped. Example: December H₂ lots at 1.17 and 17.83 gCO₂e/MJ (both compliant as H₂) may be pooled into one NH₃ batch, whose part is 14.66 gCO₂e/MJ |

Consequences:

- **Decision set.** A feed allocation (and a PoS allocation into a unit) may carry a `pool`
  label. Feed lines into one downstream batch with the same label make one part, at the
  energy-weighted GHG of the lines; without a label each source GHG makes its own part, as in
  §12. Named checks: every line in a pool has the same source and production month and meets the
  threshold.
- **Calculator.** Parts, ledger lines and opening ledger entries record their source (`own` or
  the supplier); purchased opening entries name their supplier. An aggregated batch's GHG is the
  energy-weighted average of its parts; it must hold one production month and one source and
  only compliant parts. A consignment's shipped GHG is its highest aggregated batch's GHG plus
  the distribution adder, so every aggregated batch meets the target and the threshold.
- **Optimiser.** Stage A values a month's compliant batches as a blend against the downstream
  limit (per month and source, never across months). Stage B may pool same-month, same-source
  compliant lines into one downstream batch (one linear row per pool) where that feeds more than
  separate parts could; stage C keeps one aggregated batch per consignment, month and source
  with a linear row holding its average to the consignment's limit.
- **Test cases.** The workbooks' NH₃ batches average all their H₂; under §12.1 the compliant H₂
  of one month is averaged (a pool), and non-compliant H₂ stays apart. Test case 1's December
  NH₃ is therefore one compliant part at the workbook's 14.66 gCO₂e/MJ again (§12 had split off
  a non-compliant part from the 17.83 H₂); its consignments still cannot be met, as they average
  September with October NH₃. Test case 2's NH₃ write-off is the workbook's again. Test cases 3
  and 4 are unchanged: each month holds one part per source.

## 13. Compact plant config: electricity settings and direct links (decided 2026-10-30)

The block flow diagram showed modelling devices rather than the plant: an electricity input
block with a flow into every unit for the renewable pool and another for the grid, and a
pass-through storage block in every direct unit → unit or unit → customer link. The plant
config gains a compact form; the loader expands it into the full form the engine has always
read, so calculation, optimisation and checks are unchanged and both forms load.

| # | Question | Decision |
| --- | --- | --- |
| 1 | Electricity | An `electricity` section of the plant config, not blocks: the renewable power available series (kWh per interval; blank for grid power only) and the grid emission factor (gCO₂e/kWh) with its source. Every processing unit meters its total electricity (`electricity_series`), agnostic of where it came from; within a batch, renewable power available is matched first and the rest is grid power. Batch boundaries and the per-interval split of the pool between units stay optimiser decisions. Every unit that meters electricity draws on the pool (a plant where a unit must take grid power only keeps the full form) |
| 2 | Grid emission factor | One constant for a run; it may change from run to run (set it before the run; the sealed run records it with the config) |
| 3 | Direct links | A unit is linked to a unit or a distribution block by a plain flow. The loader inserts the pass-through storage (§10.2 rule 5, §10.3 decision 8) with id `<flow id>__link`; the diagram never shows it, and its checks are reported on the flow |
| 4 | Distribution meters | A distribution block has no metered series. A trailer load's delivered quantity is the consignment's `volume_kg` (already an input), so no time series is needed. A storage feeding a distribution block keeps its outflow meter for now (it is the storage's balance; open, §13.1) |
| 5 | Pipeline losses | Not tracked: a difference between a unit's output and what the customer's meter records is ignored (§2.7: losses are not modelled) |
| 6 | Pipeline delivery | One pipeline. The optimiser assigns RFNBO batches to the pipeline's time intervals and decides which output each goes to. **Open:** what an output of the pipeline is and how its consignments are defined (§13.1) |

Named rule added: `unit_electricity_source` (a unit that meters electricity draws it from the
plant's electricity settings or an electricity input). Ids the loader adds are reserved:
`renewable_power`, `grid_power`, `<unit>__renewable_power`, `<unit>__grid_power`,
`<flow>__link`, `<flow>__out`. The editor opens a full-form file in the compact form where
nothing is lost (`schema.compact`) and saves the compact form. The golden plants stay in the
full form, so their fixtures (which name the pass-through storages) are unchanged; they also
validate when compacted.

### 13.1 Open: pipeline outputs

Decision 6 needs, before it is built: what the pipeline's outputs are (customers or offtake
points along it, each a distribution block with its own fixed adder?), whether each output's
customer meter is a series (kg per interval) that bounds what RFNBO can be assigned to it in
each interval, whether RFNBO may be assigned to an interval before the batch that made it ends,
and how a pipeline consignment is defined (a period's metered delivery, with its target).
Also open: whether a real storage (one with an inventory meter) feeding a distribution block
still needs a meter on that outflow, or whether the outflow is taken as the storage's balance
(inflows less the change in inventory), losses included.

