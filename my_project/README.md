# Simple test plant: electrolyser and compressor on solar

A project folder for `rfnbo data` and `rfnbo optimise --project`: one quarter of a plant, hour by hour, with
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
| `project.json` | Plant config, quarter (2026-Q4), the data file and its column mapping with units |
| `data/test_data.xlsx` | The hourly test data, used as it is |
| `consignments.json` | The consignments to meet |
| `pos.csv`, `corrections.json`, `opening.json` | Empty: no purchased RFNBO, no corrections, no opening ledger |
| `test_data.source.json` | The column mapping on its own, for `rfnbo optimise --source` or the Data screen's "Import a saved column mapping" |

## The data

`data/test_data.xlsx` has one row per hour, so the plant is calculated hour by hour (the data's
own time step; nothing is resampled). Its columns are in MWh and tonnes; they are converted on
reading to the kWh and kg the series are named in (x 1000), and every conversion is listed with
the run. Times are read as UTC interval starts (Excel's times are rounded to the second: a few
had drifted by a millisecond), and the last row, which holds only `#REF!` errors, is skipped and
reported. The file is not changed (SHA-256
`4a1a23e038adad06852c4ff2d3842e53ce966caa97e61dd64db5f566e1706a38`).

The data covers all of 2026, so any quarter can be run: change `quarter` in `project.json`
(rows outside it are ignored with a warning).

## How the spreadsheet's columns are used

| Series | Column | Unit in the file |
| --- | --- | --- |
| `solar_available_kWh` | Solar Power (MWh) | MWh: renewable power available, shared by both units |
| `electrolyser_electricity_kWh` | Electrolyser Power (MWh) | MWh |
| `electrolyser_H2_kg` | H2 Production (tonne) | tonne |
| `compressor_electricity_kWh` | Compressor Power (MWh) | MWh |
| `compressor_CH2_kg` | Compressor Flow (tonne) | tonne: the compressor's output (99.8% of its feed) |

The plant has three blocks and no electricity blocks: electricity is set once for the plant
(renewable power available and the grid factor, under Plant settings → Electricity), and each
unit meters its own consumption. The two direct links (electrolyser → compressor, compressor →
customer) need no column of their own: nothing is held between the units, so the compressor's
feed is the electrolyser's production, and what is filled into trailers for the customer is the
compressor's output. Each trailer load's delivered quantity is the consignment's `volume_kg`.

## Assumptions and placeholders: check these

| Item | Value used | Where |
| --- | --- | --- |
| Grid electricity emission factor | **200 gCO2e/kWh, PLACEHOLDER** | `plant.json`, `electricity.grid_gCO2e_per_kWh` |
| Distribution adder to the customer | **1.0 gCO2e/MJ, PLACEHOLDER** | `plant.json`, `dist_customer` |
| Consignment | **C1: 250,000 kg CH2, ships 2026-12-15, no target (minimise GHG), PLACEHOLDER** | `consignments.json` |
| Time stamps | UTC interval starts | `project.json` |
| Plant time zone | Europe/London (months and quarters follow it) | `plant.json` |
| Water, other inputs | Not modelled (no data) | |
| Unit GHG adders | 0 gCO2e/MJ for both units | `plant.json` |
| LHV | 120 MJ/kg for H2 and CH2 | `plant.json` |

The consignment is sized below the compliant CH2 the quarter can make with these (the 28.2
gCO2e/MJ threshold less the distribution adder). RFNBO the consignment does not
take is written off at quarter end (nothing is stored).
