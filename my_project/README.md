# Simple test plant: electrolyser and compressor on solar

A project folder for `rfnbo data` and `rfnbo optimise --project`: one quarter of a plant with
an electrolyser feeding a compressor directly, the compressor filling tube trailers straight for
one customer, solar power shared by both units and grid power for the rest.

## Run it

```sh
uv run rfnbo optimise --project my_project            # optimise 2026-Q4 and seal under runs/
uv run rfnbo data my_project                          # or open it on the Data screen
uv run rfnbo edit my_project/plant.json               # view or edit the plant diagram
```

## Files

| File | What it is |
| --- | --- |
| `plant.json` | The plant config (block flow diagram); `rfnbo validate my_project/plant.json` |
| `project.json` | Plant config, quarter (2026-Q4) and the data file with its column mapping |
| `data/test_data_15min.csv` | The 15-minute data, converted from `test_data.xlsx` (below) |
| `consignments.json` | The consignments to meet |
| `pos.csv`, `corrections.json`, `opening.json` | Empty: no purchased RFNBO, no corrections, no opening ledger |
| `convert_hourly.py` | Rebuilds the data file from the spreadsheet |
| `test_data.source.json` | The column mapping on its own, for `rfnbo optimise --source` or the Data screen's "Import a saved column mapping" |

## The data conversion

`test_data.xlsx` has one row per hour in MWh and tonnes; the optimiser reads 15-minute intervals
in kWh and kg. `uv run python my_project/convert_hourly.py my_project/test_data.xlsx` splits each
hour evenly into four intervals (MWh x 1000 / 4 = kWh; tonne x 1000 / 4 = kg), reads times as
UTC interval starts rounded to the minute (Excel had drifted a few by a millisecond), and skips
the last row, which holds only `#REF!` errors. The spreadsheet is not changed. Converted from the
file with SHA-256 `4a1a23e038adad06852c4ff2d3842e53ce966caa97e61dd64db5f566e1706a38`.

The data covers all of 2026, so any quarter can be run: change `quarter` in `project.json`
(rows outside it are ignored with a warning).

## How the spreadsheet's columns are used

| Series | Column | Note |
| --- | --- | --- |
| `solar_available_kWh` | Solar Power (MWh) | Renewable power available, shared by both units |
| `electrolyser_electricity_kWh` | Electrolyser Power (MWh) | |
| `electrolyser_H2_kg` | H2 Production (tonne) | |
| `compressor_electricity_kWh` | Compressor Power (MWh) | |
| `compressor_CH2_kg` | Compressor Flow (tonne) | Read as the compressor's output (99.8% of its feed) |

The two direct links need no column of their own: a pass-through storage holds nothing, so
the compressor's feed is the electrolyser's production, and what is filled into trailers for the
customer is the compressor's output.

## Assumptions and placeholders: check these

| Item | Value used | Where |
| --- | --- | --- |
| Grid electricity emission factor | **200 gCO2e/kWh, PLACEHOLDER** | `plant.json`, `grid_power` |
| Distribution adder to the customer | **1.0 gCO2e/MJ, PLACEHOLDER** | `plant.json`, `dist_customer` |
| Consignment | **C1: 250,000 kg CH2, ships 2026-12-15, no target (minimise GHG), PLACEHOLDER** | `consignments.json` |
| Time stamps | UTC interval starts | `convert_hourly.py` |
| Plant time zone | Europe/London (months and quarters follow it) | `plant.json` |
| 15-minute profile | Each hour split evenly | `convert_hourly.py` |
| Water, other inputs | Not modelled (no data) | |
| Unit GHG adders | 0 gCO2e/MJ for both units | `plant.json` |
| LHV | 120 MJ/kg for H2 and CH2 | `plant.json` |

With these, 2026-Q4 can make about 319 t of CH2 at or below 27.2 gCO2e/MJ (the 28.2 threshold
less the distribution adder); the consignment is sized below that. RFNBO the consignment does not
take is written off at quarter end (nothing is stored).
