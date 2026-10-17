"""Build the test case 3 workbook (multi-step plant, build plan §10.4, phase 10).

    uv run python tests/golden/build_tc3_workbook.py [out.xlsx]

Writes the inputs, the expected decisions and live formulas for every result, and a Checks
sheet comparing each result with ``tc3_reference`` (an independent Python calculation). The
file has no cached values until it is recalculated, for example with LibreOffice:

    soffice --headless --convert-to xlsx --outdir <dir> <out.xlsx>

The committed workbook is recalculated; ``test_tc3_workbook.py`` reads its cached values.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.golden import tc3_reference as R

OUT = Path(__file__).parent / "workbooks" / "RFNBO_optimiser_test3_multistep_Q4_2026.xlsx"
N = 8836
LAST = N + 1
FONT = "Arial"
BLUE, GREEN, BLACK = "0000FF", "008000", "000000"
DECISION = PatternFill("solid", fgColor="C6EFCE")
KEY = PatternFill("solid", fgColor="FFFF00")

DATA_COLS = [
    ("A", "Interval", None),
    ("B", "Start (UTC)", None),
    ("C", "Start (local, Europe/Amsterdam)", None),
    ("D", "Local month", None),
    ("E", "Renewable power available (kWh)", "renewable_kWh"),
    ("F", "Renewable to electrolyser (kWh)", "alloc_electrolyser"),
    ("G", "Renewable to liquefier (kWh)", "alloc_liquefier"),
    ("H", "Renewable to compressor (kWh)", "alloc_compressor"),
    ("I", "Electrolyser electricity (kWh)", "electrolyser_kWh"),
    ("J", "Electrolyser water (kg)", "water_kg"),
    ("K", "Electrolyser H₂ produced (kg)", "h2_kg"),
    ("L", "Electrolyser H₂ to buffer storage (kg)", "to_buffer_kg"),
    ("M", "Electrolyser H₂ direct to compressor (kg)", "to_compressor_kg"),
    ("N", "Liquefier electricity (kWh)", "liquefier_kWh"),
    ("O", "Liquefier H₂ consumed (kg)", "liquefier_h2_kg"),
    ("P", "LH₂ produced (kg)", "lh2_kg"),
    ("Q", "Compressor electricity (kWh)", "compressor_kWh"),
    ("R", "Compressor H₂ consumed (kg)", "compressor_h2_kg"),
    ("S", "CH₂ produced (kg)", "ch2_kg"),
    ("T", "H₂ buffer inventory, end of interval (kg)", "buffer_inv_kg"),
    ("U", "LH₂ shipped CA (kg)", "ca_kg"),
    ("V", "LH₂ shipped spot, no claim (kg)", "spot_kg"),
    ("W", "LH₂ storage inventory, end of interval (kg)", "lh2_inv_kg"),
    ("X", "CH₂ shipped CB (kg)", "cb_kg"),
    ("Y", "CH₂ storage inventory, end of interval (kg)", "ch2_inv_kg"),
    ("Z", "Expected electrolyser batch ID", "batch_electrolyser"),
    ("AA", "Expected liquefier batch ID", "batch_liquefier"),
    ("AB", "Expected compressor batch ID", "batch_compressor"),
]


def rng(col: str) -> str:
    return f"Data_15min!${col}$2:${col}${LAST}"


def last(col: str) -> str:
    return f"Data_15min!{col}{LAST}"


def P(row: int) -> str:
    return f"Parameters!$B${row}"


class Sheet:
    def __init__(self, wb: Workbook, title: str, widths: dict[str, float] | None = None):
        self.ws = wb.create_sheet(title)
        for col, w in (widths or {}).items():
            self.ws.column_dimensions[col].width = w

    def put(
        self,
        ref: str,
        value: Any,
        *,
        kind: str = "auto",
        fill: PatternFill | None = None,
        fmt: str | None = None,
        bold: bool = False,
    ) -> None:
        c = self.ws[ref]
        c.value = value
        if kind == "auto":
            if isinstance(value, str) and value.startswith("="):
                kind = "link" if "!" in value else "formula"
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                kind = "input"
            else:
                kind = "text"
        color = {"input": BLUE, "link": GREEN}.get(kind, BLACK)
        c.font = Font(name=FONT, size=10, color=color, bold=bold)
        if fill:
            c.fill = fill
        if fmt:
            c.number_format = fmt
        elif kind in ("formula", "link", "input"):
            c.number_format = "#,##0.000000"

    def header(self, row: int, labels: list[str], start: int = 1) -> None:
        for i, label in enumerate(labels):
            ref = f"{get_column_letter(start + i)}{row}"
            self.put(ref, label, kind="text", bold=True)
            self.ws[ref].alignment = Alignment(wrap_text=True, vertical="top")

    def note(self, ref: str, text: str) -> None:
        self.put(ref, text, kind="text")


# ------------------------------------------------------------------ sheets


def readme(wb: Workbook) -> None:
    s = Sheet(wb, "README", {"A": 160})
    lines = [
        ("RFNBO optimiser — test case 3, multi-step plant, Q4 2026", True),
        (
            "Purpose: expected results for build plan phases 10–15 (several processing steps, "
            "some in parallel). Every result cell is a live formula you can trace back to the "
            "15-minute data. Column C of the Checks sheet comes from "
            "tests/golden/tc3_reference.py, a separate Python calculation that does not read "
            "these formulas.",
            False,
        ),
        ("", False),
        ("Colour key", True),
        (
            "Blue text = input value. Black text = formula. Green text = link to another sheet. "
            "Green fill = expected decision chosen by the optimiser. Yellow fill = key answer to "
            "compare against the app.",
            False,
        ),
        ("", False),
        ("Plant", True),
        (
            "Electrolyser → (split output, both flows metered) → H₂ buffer storage → liquefier → "
            "LH₂ storage → Distribution A → Consignment CA (Customer A, LH₂); and electrolyser → "
            "direct link (pass-through storage, no inventory) → compressor → CH₂ storage → "
            "Distribution B → Consignment CB (Customer B, CH₂). LH₂ storage also ships a spot "
            "sale with no RFNBO claim. One renewable pool feeds the electrolyser, liquefier and "
            "compressor; grid power covers the rest.",
            False,
        ),
        (
            "Per interval: electrolyser 2,500 kWh, 500 kg water, 50 kg H₂ (30 kg to the buffer, "
            "20 kg direct to the compressor). Liquefier 300 kWh, 30 kg H₂ in, 29.4 kg LH₂ out "
            "(2% boil-off; 29.1 kg, 3%, in November). Compressor 40 kWh, 20 kg H₂ in, 20 kg CH₂ "
            "out. LHV 120 MJ/kg for H₂, LH₂ and CH₂. Time zone Europe/Amsterdam; quarter = 1 Oct "
            "2026 00:00 to 1 Jan 2027 00:00 local, 8,836 intervals.",
            False,
        ),
        ("", False),
        ("What each month tests", True),
        (
            "October: 3,000 kWh renewable every interval (demand 2,840). Full cover for all three "
            "units; one batch per unit.",
            False,
        ),
        (
            "November: 3,000 kWh except 0 kWh on 18 Nov 06:00–18:00. The surplus elsewhere covers "
            "the dark spell for every unit within its month-long batch (shared pool, rule 6). "
            "Boil-off is 3%, so November LH₂ has the highest GHG of the quarter (losses raise "
            "GHG, rule 3).",
            False,
        ),
        (
            "December: 3,000 kWh; the liquefier is off all day on 8 December, so 2,880 kg of RFNBO "
            "H₂ builds up in the buffer. Shipments: CA 150,000 kg LH₂ on 15 Dec (target 5.0, "
            "distribution 2.0 gCO₂e/MJ); CB 120,000 kg CH₂ on 20 Dec (target 4.0, distribution "
            "1.0); a 60,000 kg LH₂ spot sale on 29 Dec. The spot sale forces an LH₂ write-off.",
            False,
        ),
        ("", False),
        ("Rules and decisions used (build plan §10.2 and §10.3, settled 2026-10-17)", True),
        (
            "1. Objectives, in order: meet every consignment; then maximise RFNBO held in storage "
            "at period end, in MJ, summed over every storage (H₂ buffer, LH₂, CH₂; the "
            "pass-through is always zero); then lowest carried emissions; then fewest batches "
            "(decision 1).",
            False,
        ),
        (
            "2. Split output: the electrolyser's RFNBO is assigned to each output flow, at most "
            "its metered quantity (decision 2). Expected choice: the compressor branch first (no "
            "loss), the rest to the buffer. With every electrolyser batch at 100% here, both "
            "flows simply get their metered quantity.",
            False,
        ),
        (
            "3. Pass-through: the direct link is a storage with no meter whose physical "
            "inventory is zero, so its RFNBO ledger must close at zero (decision 3).",
            False,
        ),
        (
            "4. Every unit matches renewable within its own batch: matched = min(Σ allocated, "
            "Σ electricity). Matched renewable raises RFNBO share only for the electrolyser; "
            "for the liquefier and compressor it removes grid emissions. Conditioning units keep "
            "monthly correlation from 2030 (decision 4).",
            False,
        ),
        (
            "5. Losses: emissions of the whole RFNBO feed are spread over the smaller RFNBO "
            "product, so boil-off raises GHG per MJ (decision 5).",
            False,
        ),
        (
            "6. Conditioning unit GHG = (RFNBO feed emissions + share × own emissions) ÷ RFNBO "
            "product energy + adder; share = RFNBO feed ÷ feed consumed. A batch above "
            "28.2 gCO₂e/MJ is still RFNBO (§2.5) but can never be consigned.",
            False,
        ),
        (
            "7. Storage ledgers close by the §2.7 rule: the excess over measured inventory is "
            "written off, highest GHG first, newest month first on ties.",
            False,
        ),
        (
            "8. Consignments take the highest-GHG compliant material their target allows, oldest "
            "month first on ties (as in test case 1), so the lowest-GHG material is what remains "
            "in storage. Aggregated batches hold one production month and one product.",
            False,
        ),
        ("", False),
        ("Why this decision set is optimal", True),
        (
            "Every electrolyser batch is 100% RFNBO and every downstream batch fully renewable-"
            "covered, so no decision can add RFNBO. Stored RFNBO is then capped by measured "
            "inventory: the buffer and LH₂ storage end full of RFNBO, and CH₂ storage holds all "
            "CH₂ RFNBO left after CB. Keeping 4,880 kg of RFNBO H₂ in the buffer, rather than "
            "liquefying it into LH₂ that would only be written off, is what reaches the cap: the "
            "liquefier's December batch therefore takes 2,000 kg less RFNBO feed than it "
            "consumed (97.7% share). Carried LH₂ is all at the lowest LH₂ GHG (Oct/Dec, "
            "1.6905). One batch per unit and month is the fewest possible.",
            False,
        ),
        ("", False),
        ("Variants (Variants sheet)", True),
        (
            "V1 — hourly correlation (2030): electrolyser batches within clock hours; the 12 dark "
            "hours become 0% batches and their H₂ reaches both branches as non-RFNBO.",
            False,
        ),
        (
            "V2 — renewable shortfall: December renewable cut to 2,500 kWh per interval and no "
            "LH₂ spot sale. Every kWh for the liquefier is a kWh the electrolyser loses. The "
            "answer depends on open follow-up (b) in build plan §10.3 (does non-compliant RFNBO "
            "held in storage count?), so both readings are worked out.",
            False,
        ),
        (
            "V3 — infeasible target on one product: CA's target cut to 3.5 gCO₂e/MJ, below the "
            "lowest LH₂ GHG plus distribution (3.69). CA cannot be met; CB is unaffected.",
            False,
        ),
        ("", False),
        ("Sheets", True),
        (
            "Parameters · Data_15min (inputs, renewable split decision, expected batch IDs) · "
            "E_Batches · E_Split (output assignment) · Buffer_Ledger (H₂ batch → liquefier batch) "
            "· L_Batches · PT_Ledger (pass-through, H₂ batch → compressor batch) · C_Batches · "
            "LH2_Ledger · CH2_Ledger · Consignments · Objectives · Variants · Checks.",
            False,
        ),
    ]
    for i, (text, bold) in enumerate(lines, 1):
        s.put(f"A{i}", text, kind="text", bold=bold)
        s.ws[f"A{i}"].alignment = Alignment(wrap_text=True, vertical="top")


PARAMS = [
    (2, "H₂, LH₂ and CH₂ lower heating value", 120, "MJ/kg", "Configurable"),
    (3, "Water emission factor", 2, "gCO₂e/kg", "Synthetic value"),
    (4, "Non-renewable electricity emission factor", 400, "gCO₂e/kWh", "Constant input"),
    (5, "Electrolyser fixed GHG adder", 1, "gCO₂e/MJ", "Synthetic value"),
    (6, "Liquefier fixed GHG adder", 0.5, "gCO₂e/MJ", "Synthetic value"),
    (7, "Compressor fixed GHG adder", 0.5, "gCO₂e/MJ", "Synthetic value"),
    (8, "RFNBO threshold (70% saving vs 94 gCO₂e/MJ)", 28.2, "gCO₂e/MJ", "2023/1185"),
    (9, "H₂ buffer opening physical inventory", 2000, "kg", ""),
    (10, "H₂ buffer opening RFNBO ledger", 0, "kg", "Manual entry"),
    (11, "LH₂ storage opening physical inventory", 1000, "kg", ""),
    (12, "LH₂ storage opening RFNBO ledger", 0, "kg", "Manual entry"),
    (13, "CH₂ storage opening physical inventory", 500, "kg", ""),
    (14, "CH₂ storage opening RFNBO ledger", 0, "kg", "Manual entry"),
    (15, "Distribution A adder (LH₂ trucks)", 2, "gCO₂e/MJ", "Synthetic value"),
    (16, "Distribution B adder (CH₂ tube trailers)", 1, "gCO₂e/MJ", "Synthetic value"),
]


def parameters(wb: Workbook) -> None:
    s = Sheet(wb, "Parameters", {"A": 48, "B": 14, "C": 12, "D": 20})
    s.header(1, ["Parameter", "Value", "Unit", "Note"])
    for row, label, value, unit, note in PARAMS:
        s.note(f"A{row}", label)
        s.put(f"B{row}", value, fmt="General")
        s.note(f"C{row}", unit)
        s.note(f"D{row}", note)


def data_sheet(wb: Workbook, data: R.Data, d: R.Decisions) -> None:
    s = Sheet(wb, "Data_15min", {c: 14 for c, _, _ in DATA_COLS})
    s.ws.column_dimensions["B"].width = 18
    s.ws.column_dimensions["C"].width = 18
    s.header(1, [label for _, label, _ in DATA_COLS])
    for c in ("F", "G", "H", "Z", "AA", "AB"):
        s.ws[f"{c}1"].fill = DECISION
    s.ws.freeze_panes = "E2"
    ids = {u: [""] * len(data) for u in R.UNITS}  # expected batch id per interval for each unit
    for u in R.UNITS:
        for bid, i0, i1 in d.batches[u]:
            for i in range(i0, i1):
                ids[u][i] = bid
    blue = Font(name=FONT, size=10, color=BLUE)
    plain = Font(name=FONT, size=10)
    ws = s.ws
    for i in range(len(data)):
        r = i + 2
        values: list[Any] = [
            i + 1,
            data.utc[i].replace(tzinfo=None),
            data.local[i].replace(tzinfo=None),
            data.month[i],
        ]
        for _, _, key in DATA_COLS[4:]:
            if key is None:
                continue
            if key.startswith("alloc_"):
                values.append(d.renewable[key[6:]][i])
            elif key.startswith("batch_"):
                values.append(ids[key[6:]][i])
            else:
                values.append(data.s[key][i])
        for j, v in enumerate(values):
            cell = ws.cell(row=r, column=j + 1, value=v)
            cell.font = blue if isinstance(v, (int, float)) and j > 0 else plain
        ws.cell(row=r, column=2).number_format = "yyyy-mm-dd hh:mm"
        ws.cell(row=r, column=3).number_format = "yyyy-mm-dd hh:mm"


def e_batches(wb: Workbook) -> None:
    s = Sheet(wb, "E_Batches", {"A": 14} | {get_column_letter(i): 15 for i in range(2, 18)})
    s.header(
        1,
        [
            "Batch ID",
            "Production month",
            "Start (local)",
            "End (local)",
            "Intervals",
            "Renewable allocated (kWh)",
            "Electricity used (kWh)",
            "Renewable matched (kWh)",
            "RFNBO share",
            "H₂ produced (kg)",
            "H₂ energy (MJ)",
            "Water emissions (gCO₂e)",
            "Non-renewable electricity emissions (gCO₂e)",
            "Batch GHG (gCO₂e/MJ)",
            "≤ 28.2 gCO₂e/MJ?",
            "RFNBO H₂ (kg)",
            "RFNBO H₂ (MJ)",
        ],
    )
    Z = rng("Z")
    for r, bid in zip((2, 3, 4), ("E-2026-10", "E-2026-11", "E-2026-12"), strict=True):
        s.put(f"A{r}", bid, kind="text", fill=DECISION)
        s.put(f"B{r}", f"=INDEX({rng('D')},MATCH(A{r},{Z},0))")
        s.put(f"C{r}", f"=INDEX({rng('C')},MATCH(A{r},{Z},0))", fmt="yyyy-mm-dd hh:mm")
        s.put(f"D{r}", f"=_xlfn.MAXIFS({rng('C')},{Z},A{r})+1/96", fmt="yyyy-mm-dd hh:mm")
        s.put(f"E{r}", f"=COUNTIF({Z},A{r})", fmt="#,##0")
        s.put(f"F{r}", f"=SUMIF({Z},A{r},{rng('F')})")
        s.put(f"G{r}", f"=SUMIF({Z},A{r},{rng('I')})")
        s.put(f"H{r}", f"=MIN(F{r},G{r})")
        s.put(f"I{r}", f"=H{r}/G{r}")
        s.put(f"J{r}", f"=SUMIF({Z},A{r},{rng('K')})")
        s.put(f"K{r}", f"=J{r}*{P(2)}")
        s.put(f"L{r}", f"=SUMIF({Z},A{r},{rng('J')})*{P(3)}")
        s.put(f"M{r}", f"=(G{r}-H{r})*{P(4)}")
        s.put(f"N{r}", f"=(L{r}+M{r})/K{r}+{P(5)}")
        s.put(f"O{r}", f"=N{r}<={P(8)}+0.000000001", fmt="General")
        s.put(f"P{r}", f"=I{r}*J{r}")
        s.put(f"Q{r}", f"=P{r}*{P(2)}")
    s.note("A5", "Total")
    for c in "EFGHJKLMPQ":
        s.put(f"{c}5", f"=SUM({c}2:{c}4)")
    s.note(
        "A7",
        "One batch per month: renewable covers every interval's demand within each month, so "
        "every batch is 100% RFNBO and splitting gains nothing. RFNBO H₂ = share × H₂ whatever "
        "the GHG (§2.5).",
    )


def e_split(wb: Workbook) -> None:
    s = Sheet(wb, "E_Split", {"A": 14} | {get_column_letter(i): 18 for i in range(2, 9)})
    s.header(
        1,
        [
            "Electrolyser batch",
            "RFNBO H₂ (kg)",
            "Metered to buffer (kg)",
            "Metered direct to compressor (kg)",
            "RFNBO assigned to compressor flow (kg)",
            "RFNBO assigned to buffer flow (kg)",
            "Parts add up to the batch's RFNBO?",
            "Each flow within its metered quantity?",
        ],
    )
    for r in (2, 3, 4):
        s.put(f"A{r}", f"=E_Batches!A{r}")
        s.put(f"B{r}", f"=E_Batches!P{r}")
        s.put(f"C{r}", f"=SUMIF({rng('Z')},A{r},{rng('L')})")
        s.put(f"D{r}", f"=SUMIF({rng('Z')},A{r},{rng('M')})")
        s.put(f"E{r}", f"=MIN(B{r},D{r})", fill=DECISION)
        s.put(f"F{r}", f"=B{r}-E{r}", fill=DECISION)
        s.put(f"G{r}", f"=ABS(E{r}+F{r}-B{r})<=0.000001*MAX(1,B{r})", fmt="General")
        s.put(f"H{r}", f"=AND(E{r}<=D{r}+0.000001,F{r}<=C{r}+0.000001)", fmt="General")
    s.note("A5", "Total")
    for c in "BCDEF":
        s.put(f"{c}5", f"=SUM({c}2:{c}4)")
    s.note(
        "A7",
        "Rule 4: the optimiser assigns each batch's RFNBO to its output flows. Expected choice: "
        "the compressor branch first (no loss), the remainder to the buffer. Each part enters "
        "the destination ledger with the batch's ID, GHG, RFNBO share and month.",
    )


def feed_ledger(
    wb: Workbook,
    title: str,
    *,
    flow_col: str,
    targets: list[str],
    target_sheet: str,
    pass_through: bool,
) -> None:
    s = Sheet(wb, title, {"A": 44} | {get_column_letter(i): 16 for i in range(2, 9)})
    s.header(
        1,
        [
            "Electrolyser batch",
            "Batch GHG (gCO₂e/MJ)",
            "RFNBO H₂ in (kg)",
            *[f"→ {t} (kg)" for t in targets],
            "Left in storage (kg)",
            "Allocation within available?",
        ],
    )
    for k, r in enumerate((2, 3, 4)):
        s.put(f"A{r}", f"=E_Batches!A{r}")
        s.put(f"B{r}", f"=E_Batches!N{r}")
        s.put(f"C{r}", f"=E_Split!{flow_col}{r}")
        for j, col in enumerate("DEF"):
            if j != k:
                s.put(f"{col}{r}", 0, fill=DECISION, fmt="#,##0.000000")
            elif pass_through or k < 2:
                s.put(f"{col}{r}", f"=MIN($C{r},{target_sheet}!$C${r})", fill=DECISION)
            else:
                # Keep the buffer full of RFNBO up to its measured inventory.
                s.put(
                    f"{col}{r}",
                    f"=MIN({target_sheet}!$C${r},$C{r}-MAX(0,$B$13-G2-G3))",
                    fill=DECISION,
                )
        s.put(f"G{r}", f"=C{r}-SUM(D{r}:F{r})")
        s.put(f"H{r}", f"=G{r}>=-0.000001", fmt="General")
    s.note("A5", "Total RFNBO H₂ (kg)")
    for c in "CDEFG":
        s.put(f"{c}5", f"=SUM({c}2:{c}4)")
    s.note("A6", "Feed GHG of each downstream batch (gCO₂e/MJ H₂)")
    for c in "DEF":
        s.put(f"{c}6", f"=SUMPRODUCT({c}2:{c}4,$B$2:$B$4)/{c}5")
    rows = [
        (9, "Opening RFNBO ledger (kg)", "=0" if pass_through else f"={P(10)}"),
        (10, "RFNBO H₂ in during the quarter (kg)", "=C5"),
        (11, "RFNBO H₂ fed downstream (kg)", "=SUM(D5:F5)"),
        (12, "Closing RFNBO ledger before write-off (kg)", "=B9+B10-B11"),
        (
            13,
            "Physical inventory at quarter end (kg)",
            "=0" if pass_through else f"={last('T')}",
        ),
        (14, "Write-off (kg)", "=MAX(0,B12-B13)"),
        (15, "RFNBO H₂ carried to Q1 2027 (kg)", "=B12-B14"),
        (
            16,
            "Energy-weighted GHG of carried H₂ (gCO₂e/MJ)",
            "=IF(B15>0,SUMPRODUCT(G2:G4,B2:B4)/B15,0)",
        ),
    ]
    for row, label, f in rows:
        s.note(f"A{row}", label)
        s.put(f"B{row}", f, fill=KEY if row == 15 else None)
    if pass_through:
        s.note("A17", "Pass-through ledger closes at zero?")
        s.put("B17", "=ABS(B15)<=0.000001", fmt="General")
        s.note("A19", "Physical check")
        s.note("A20", "Intervals where direct flow in ≠ compressor consumption")
        s.put("B20", f"=SUMPRODUCT(--(ABS({rng('M')}-{rng('R')})>0.000001))", fmt="#,##0")
        s.note(
            "A22",
            "Rule 5: a direct unit → unit link is a storage with no inventory meter whose "
            "physical inventory is zero, so anything not consumed downstream is written off.",
        )
    else:
        s.note("A18", "Physical reconciliation")
        recon = [
            (19, "Opening physical inventory (kg)", f"={P(9)}"),
            (20, "H₂ in from the electrolyser (kg)", f"=SUM({rng('L')})"),
            (21, "H₂ consumed by the liquefier (kg)", f"=SUM({rng('O')})"),
            (22, "Expected closing (kg)", "=B19+B20-B21"),
            (23, "Measured closing (kg)", "=B13"),
            (24, "Implied loss (kg)", "=B22-B23"),
        ]
        for row, label, f in recon:
            s.note(f"A{row}", label)
            s.put(f"B{row}", f)
        s.note(
            "A26",
            "The liquefier's last batch takes only enough RFNBO feed to leave the buffer full of "
            "RFNBO H₂ (4,880 kg): H₂ held here counts towards objective 2 at 120 MJ/kg, while "
            "the same H₂ liquefied loses 2% and the LH₂ would be written off anyway.",
        )


def unit_batches(
    wb: Workbook,
    title: str,
    *,
    ids: list[str],
    batch_col: str,
    consumed: str,
    product: str,
    alloc: str,
    elec: str,
    feed_sheet: str,
    adder: int,
    product_label: str,
) -> None:
    s = Sheet(wb, title, {"A": 14} | {get_column_letter(i): 15 for i in range(2, 20)})
    s.header(
        1,
        [
            "Batch ID",
            "Production month",
            "H₂ consumed (kg)",
            "RFNBO H₂ feed (kg)",
            "Feed GHG (gCO₂e/MJ H₂)",
            "RFNBO share",
            f"{product_label} produced (kg)",
            "Loss (kg)",
            "Loss (fraction of feed)",
            "Renewable allocated (kWh)",
            "Electricity used (kWh)",
            "Renewable matched (kWh)",
            "Own non-renewable electricity emissions (gCO₂e)",
            "Feed emissions (gCO₂e)",
            f"RFNBO {product_label} (kg)",
            f"RFNBO {product_label} (MJ)",
            "Batch GHG (gCO₂e/MJ)",
            "≤ 28.2 gCO₂e/MJ?",
            "Intervals",
        ],
    )
    B = rng(batch_col)
    for r, bid, fcol in zip((2, 3, 4), ids, "DEF", strict=True):
        s.put(f"A{r}", bid, kind="text", fill=DECISION)
        s.put(f"B{r}", f"=INDEX({rng('D')},MATCH(A{r},{B},0))")
        s.put(f"C{r}", f"=SUMIF({B},A{r},{rng(consumed)})")
        s.put(f"D{r}", f"={feed_sheet}!{fcol}5")
        s.put(f"E{r}", f"={feed_sheet}!{fcol}6")
        s.put(f"F{r}", f"=D{r}/C{r}")
        s.put(f"G{r}", f"=SUMIF({B},A{r},{rng(product)})")
        s.put(f"H{r}", f"=C{r}-G{r}")
        s.put(f"I{r}", f"=H{r}/C{r}")
        s.put(f"J{r}", f"=SUMIF({B},A{r},{rng(alloc)})")
        s.put(f"K{r}", f"=SUMIF({B},A{r},{rng(elec)})")
        s.put(f"L{r}", f"=MIN(J{r},K{r})")
        s.put(f"M{r}", f"=(K{r}-L{r})*{P(4)}")
        s.put(f"N{r}", f"=D{r}*{P(2)}*E{r}")
        s.put(f"O{r}", f"=F{r}*G{r}")
        s.put(f"P{r}", f"=O{r}*{P(2)}")
        s.put(f"Q{r}", f"=(N{r}+F{r}*M{r})/P{r}+{P(adder)}")
        s.put(f"R{r}", f"=Q{r}<={P(8)}+0.000000001", fmt="General")
        s.put(f"S{r}", f"=COUNTIF({B},A{r})", fmt="#,##0")
    s.note("A5", "Total")
    for c in "CDGHJKLMNOPS":
        s.put(f"{c}5", f"=SUM({c}2:{c}4)")
    s.note(
        "A7",
        "GHG = (feed emissions + share × own emissions) ÷ RFNBO product energy + adder. Losses "
        "(H − G) leave the feed emissions on less product, so they raise GHG (rule 3). "
        "Matched renewable only removes grid emissions here; the share comes from the feed "
        "(rule 2).",
    )


def product_ledger(
    wb: Workbook,
    title: str,
    *,
    order: list[str],
    batch_sheet: str,
    cons_row: int,
    inv_col: str,
    opening_physical: int,
    opening_ledger: int,
    produced_col: str,
    shipped: list[tuple[str, str]],
) -> None:
    s = Sheet(wb, title, {"A": 44} | {get_column_letter(i): 16 for i in range(2, 9)})
    s.header(
        1,
        [
            "Batch (write-off priority order)",
            "Production month",
            "GHG (gCO₂e/MJ)",
            "In: RFNBO (kg)",
            "Out: consignment (kg)",
            "Closing before write-off (kg)",
            "Write-off (kg)",
            "Carried to Q1 2027 (kg)",
        ],
    )
    look = f"{batch_sheet}!$A$2:$A$4"
    for k, (r, bid) in enumerate(zip((2, 3, 4), order, strict=True)):
        s.put(f"A{r}", bid, kind="text", fill=DECISION)
        s.put(f"B{r}", f"=INDEX({batch_sheet}!$B$2:$B$4,MATCH(A{r},{look},0))")
        s.put(f"C{r}", f"=INDEX({batch_sheet}!$Q$2:$Q$4,MATCH(A{r},{look},0))")
        s.put(f"D{r}", f"=INDEX({batch_sheet}!$O$2:$O$4,MATCH(A{r},{look},0))")
        s.put(
            f"E{r}",
            f'=IF(B{r}="2026-10",Consignments!$J${cons_row},'
            f'IF(B{r}="2026-11",Consignments!$K${cons_row},Consignments!$L${cons_row}))',
        )
        s.put(f"F{r}", f"=D{r}-E{r}")
        prev = "0" if k == 0 else f"SUM(G$2:G{r - 1})"
        s.put(f"G{r}", f"=MIN(F{r},MAX(0,$B$8-{prev}))")
        s.put(f"H{r}", f"=F{r}-G{r}")
    s.note("A5", "Total")
    for c in "DEFGH":
        s.put(f"{c}5", f"=SUM({c}2:{c}4)")
    s.note("A7", "Measured physical inventory at quarter end (kg)")
    s.put("B7", f"={last(inv_col)}")
    s.note("A8", "Shortfall to write off (kg)")
    s.put("B8", f"=MAX(0,{P(opening_ledger)}+F5-B7)")
    s.note("A9", "Energy-weighted GHG of carried inventory (gCO₂e/MJ)")
    s.put("B9", "=IF(H5>0,SUMPRODUCT(C2:C4,H2:H4)/H5,0)")
    s.note("A10", "Rows in priority order (highest GHG, then newest month)?")
    pair = "OR(ROUND(C{a},9)>ROUND(C{b},9),AND(ROUND(C{a},9)=ROUND(C{b},9),B{a}>B{b}))"
    s.put("B10", f"=AND({pair.format(a=2, b=3)},{pair.format(a=3, b=4)})", fmt="General")
    s.note("A12", "Physical reconciliation")
    rows = [
        (13, "Opening physical inventory (kg)", f"={P(opening_physical)}"),
        (14, "Produced (kg)", f"=SUM({rng(produced_col)})"),
    ]
    for j, (label, col) in enumerate(shipped):
        rows.append((15 + j, label, f"=SUM({rng(col)})"))
    n = 15 + len(shipped)
    out = "-".join(f"B{15 + j}" for j in range(len(shipped)))
    rows += [
        (n, "Expected closing (kg)", f"=B13+B14-{out}"),
        (n + 1, "Measured closing (kg)", "=B7"),
        (n + 2, "Implied loss (kg)", f"=B{n}-B{n + 1}"),
    ]
    for row, label, f in rows:
        s.note(f"A{row}", label)
        s.put(f"B{row}", f)


def consignments(wb: Workbook) -> None:
    s = Sheet(wb, "Consignments", {"A": 30} | {get_column_letter(i): 15 for i in range(2, 19)})
    s.header(
        1,
        [
            "Consignment",
            "Customer",
            "Product",
            "Source storage",
            "Ship date",
            "Volume (kg)",
            "Target (gCO₂e/MJ)",
            "Distribution adder (gCO₂e/MJ)",
            "Limit for aggregated batches (gCO₂e/MJ)",
            "Aggregated batch: Oct 2026 (kg)",
            "Aggregated batch: Nov 2026 (kg)",
            "Aggregated batch: Dec 2026 (kg)",
            "Weighted GHG before shipping (gCO₂e/MJ)",
            "Shipped GHG (gCO₂e/MJ)",
            "Meets target?",
            "≤ 28.2 gCO₂e/MJ?",
            "Only compliant batches?",
            "Volume met?",
        ],
    )
    spec = [
        (2, "CA", "Customer A", "LH₂", "lh2_storage", 15, 150000, 5.0, 15, "L_Batches"),
        (3, "CB", "Customer B", "CH₂", "ch2_storage", 20, 120000, 4.0, 16, "C_Batches"),
    ]
    for r, cid, cust, prod, sto, day, vol, target, adder, bs in spec:
        s.put(f"A{r}", cid, kind="text")
        s.put(f"B{r}", cust, kind="text")
        s.put(f"C{r}", prod, kind="text")
        s.put(f"D{r}", sto, kind="text")
        s.put(f"E{r}", dt.datetime(2026, 12, day), kind="input", fmt="yyyy-mm-dd")
        s.put(f"F{r}", vol, fmt="#,##0")
        s.put(f"G{r}", target, fmt="0.0")
        s.put(f"H{r}", f"={P(adder)}")
        s.put(f"I{r}", f"=G{r}-H{r}")
        if cid == "CA":
            # Highest-GHG material first: November LH₂ (3% boil-off), then the oldest month.
            s.put(f"K{r}", f"=MIN(F{r},{bs}!$O$3)", fill=DECISION)
            s.put(f"J{r}", f"=MIN(F{r}-K{r},{bs}!$O$2)", fill=DECISION)
        else:
            # All CH₂ has the same GHG: oldest month first.
            s.put(f"J{r}", f"=MIN(F{r},{bs}!$O$2)", fill=DECISION)
            s.put(f"K{r}", f"=MIN(F{r}-J{r},{bs}!$O$3)", fill=DECISION)
        s.put(f"L{r}", f"=F{r}-J{r}-K{r}", fill=DECISION)
        s.put(
            f"M{r}",
            f"=(J{r}*{bs}!$Q$2+K{r}*{bs}!$Q$3+L{r}*{bs}!$Q$4)/(J{r}+K{r}+L{r})",
        )
        s.put(f"N{r}", f"=M{r}+H{r}", fill=KEY)
        s.put(f"O{r}", f"=N{r}<=G{r}+0.000000001", fmt="General")
        s.put(f"P{r}", f"=N{r}<={P(8)}+0.000000001", fmt="General")
        s.put(
            f"Q{r}",
            f"=AND(OR(J{r}=0,{bs}!$R$2),OR(K{r}=0,{bs}!$R$3),OR(L{r}=0,{bs}!$R$4))",
            fmt="General",
        )
        s.put(f"R{r}", f"=ABS(J{r}+K{r}+L{r}-F{r})<=0.000001*F{r}", fmt="General")
    s.header(5, ["Aggregated batch", "Consignment", "Production month", "Source batch", "kg"])
    agg = [
        (6, "CA-2026-10", "CA", "2026-10", "L-2026-10", "=J2"),
        (7, "CA-2026-11", "CA", "2026-11", "L-2026-11", "=K2"),
        (8, "CB-2026-10", "CB", "2026-10", "C-2026-10", "=J3"),
        (9, "CB-2026-11", "CB", "2026-11", "C-2026-11", "=K3"),
        (10, "CB-2026-12", "CB", "2026-12", "C-2026-12", "=L3"),
    ]
    for r, aid, cid, month, src, f in agg:
        s.put(f"A{r}", aid, kind="text")
        s.put(f"B{r}", cid, kind="text")
        s.put(f"C{r}", month, kind="text")
        s.put(f"D{r}", src, kind="text")
        s.put(f"E{r}", f)
    notes = [
        "Each consignment is a specific amount of a specific product for a specific customer "
        "(decision 1). Aggregated batches hold one production month and never mix products.",
        "CA takes the highest-GHG LH₂ first (November, 3% boil-off), then October; the target "
        "does not bind. That leaves the lowest-GHG LH₂ (Oct/Dec) for storage. CB: all CH₂ has "
        "the same GHG, so the oldest month first.",
        "Distribution adders are added after shipping only.",
    ]
    for j, text in enumerate(notes):
        s.note(f"A{12 + j}", text)


def objectives(wb: Workbook) -> None:
    s = Sheet(wb, "Objectives", {"A": 70, "B": 18, "C": 60})
    s.header(1, ["Objective (in order)", "Value", "Note"])
    lhv = P(2)
    rows = [
        (
            2,
            "1. Consignments met",
            "=AND(Consignments!O2,Consignments!P2,Consignments!Q2,Consignments!R2)"
            "+AND(Consignments!O3,Consignments!P3,Consignments!Q3,Consignments!R3)",
            "Of 2",
        ),
        (3, "2. RFNBO held at period end: H₂ buffer (MJ)", f"=Buffer_Ledger!B15*{lhv}", ""),
        (4, "2. RFNBO held at period end: pass-through (MJ)", f"=PT_Ledger!B15*{lhv}", "Always 0"),
        (5, "2. RFNBO held at period end: LH₂ storage (MJ)", f"=LH2_Ledger!H5*{lhv}", ""),
        (6, "2. RFNBO held at period end: CH₂ storage (MJ)", f"=CH2_Ledger!H5*{lhv}", ""),
        (7, "2. RFNBO held at period end, all storages (MJ)", "=SUM(B3:B6)", "Key answer"),
        (
            8,
            "3. Carried emissions (gCO₂e)",
            f"=Buffer_Ledger!B15*Buffer_Ledger!B16*{lhv}"
            f"+SUMPRODUCT(LH2_Ledger!C2:C4,LH2_Ledger!H2:H4)*{lhv}"
            f"+SUMPRODUCT(CH2_Ledger!C2:C4,CH2_Ledger!H2:H4)*{lhv}",
            "",
        ),
        (
            9,
            "4. Batches",
            "=COUNTA(E_Batches!A2:A4)+COUNTA(L_Batches!A2:A4)+COUNTA(C_Batches!A2:A4)",
            "One per unit and month",
        ),
    ]
    for row, label, f, note in rows:
        s.note(f"A{row}", label)
        s.put(f"B{row}", f, fill=KEY if row in (2, 7) else None)
        s.note(f"C{row}", note)
    s.ws["B2"].number_format = "0"
    s.ws["B9"].number_format = "0"
    s.note("A11", "Upper-bound argument for objective 2")
    bound = [
        (12, "Every electrolyser batch 100% RFNBO?", "=MIN(E_Batches!I2:I4)>=1-0.000000001"),
        (
            13,
            "H₂ buffer holds RFNBO up to its measured inventory?",
            "=ABS(Buffer_Ledger!B15-Buffer_Ledger!B13)<=0.000001",
        ),
        (
            14,
            "LH₂ storage holds RFNBO up to its measured inventory?",
            "=ABS(LH2_Ledger!H5-LH2_Ledger!B7)<=0.000001",
        ),
        (
            15,
            "CH₂ storage holds all compressor RFNBO less CB?",
            "=ABS(CH2_Ledger!H5-(C_Batches!G5-Consignments!F3))<=0.000001",
        ),
        (16, "Objective 2 at its upper bound?", "=AND(B12:B15)"),
    ]
    for row, label, f in bound:
        s.note(f"A{row}", label)
        s.put(f"B{row}", f, fmt="General")
    s.note(
        "A18",
        "Objective 2 counts every storage, intermediate H₂ included, as §10.2 rule 7 reads "
        '("within each storage\'s measured inventory, summed as energy across products"). '
        "Rows 3–6 give each storage so a narrower reading can be compared.",
    )


def variants(wb: Workbook) -> None:
    s = Sheet(wb, "Variants", {"A": 74, "B": 18, "C": 90})
    s.header(1, ["Variant", "Value", "Note"])
    D, E, lhv = rng("D"), rng("E"), P(2)
    rows: list[tuple[int, str, Any, str]] = [
        (
            2,
            "V1 — Hourly correlation (2030)",
            None,
            "Same data re-dated to Q4 2030 (switch date 2030-01-01).",
        ),
        (
            3,
            "Clock hours in the quarter = electrolyser batches",
            f"=COUNT({rng('A')})/4",
            "Every electrolyser batch within one clock hour",
        ),
        (
            4,
            "Of which 0% RFNBO (dark hours)",
            f"=COUNTIF({E},0)/4",
            "18 Nov 06:00–18:00: no surplus can reach them",
        ),
        (5, "Liquefier and compressor batches", 6, "Still one per month each (monthly window)"),
        (
            6,
            "RFNBO H₂ (kg)",
            f'=SUMIF({E},">0",{rng("K")})',
            "2,550 kWh ≥ 2,500 kWh in every lit interval",
        ),
        (
            7,
            "RFNBO H₂ into the buffer (kg)",
            f'=SUMIF({E},">0",{rng("L")})',
            "Dark-hour H₂ is non-RFNBO in both flows",
        ),
        (8, "RFNBO H₂ into the pass-through (kg)", f'=SUMIF({E},">0",{rng("M")})', ""),
        (
            9,
            "Liquefier RFNBO feed (kg)",
            "=B7-Buffer_Ledger!B13",
            "Buffer kept full of RFNBO, as in the base case",
        ),
        (10, "Compressor RFNBO feed (kg)", "=B8", "Pass-through closes at zero"),
        (
            11,
            "RFNBO LH₂ (kg)",
            f'=L_Batches!O2+SUMIFS({rng("L")},{D},"2026-11",{E},">0")/L_Batches!C3*L_Batches!G3'
            "+L_Batches!O4",
            "November short by the dark hours' 1,440 kg; Oct and Dec as the base case",
        ),
        (
            12,
            "LH₂ carried (kg)",
            "=MIN(LH2_Ledger!B7,B11-Consignments!F2)",
            "Still capped by measured inventory",
        ),
        (13, "CH₂ carried (kg)", "=B10-Consignments!F3", "No CH₂ write-off"),
        (14, "RFNBO held at period end (MJ)", f"=(Buffer_Ledger!B13+B12+B13)*{lhv}", "Key answer"),
        (
            16,
            "V2 — Renewable shortfall in December",
            None,
            "December renewable 2,500 kWh per interval; no LH₂ spot sale, so LH₂ storage is "
            "never full.",
        ),
        (17, "December renewable per interval (kWh)", 2500, "Input for this variant"),
        (18, "December intervals", f'=COUNTIF({D},"2026-12")', ""),
        (19, "December renewable available (kWh)", "=B17*B18", "Exactly the electrolyser's demand"),
        (20, "December liquefier electricity (kWh)", f'=SUMIF({D},"2026-12",{rng("N")})', ""),
        (21, "December liquefier H₂ consumed (kg)", f'=SUMIF({D},"2026-12",{rng("O")})', ""),
        (22, "December LH₂ produced (kg)", f'=SUMIF({D},"2026-12",{rng("P")})', ""),
        (23, "GHG of 100% electrolyser H₂ (gCO₂e/MJ)", "=E_Batches!N4", "Water and adder only"),
        (
            24,
            "December LH₂ GHG with no renewable for the liquefier (gCO₂e/MJ)",
            f"=B23*B21/B22+B20*{P(4)}/(B22*{lhv})+{P(6)}",
            "Own emissions pro-rated by share, so this does not depend on the share",
        ),
        (
            25,
            "Least liquefier renewable for December LH₂ ≤ 28.2 (kWh)",
            f"=B20-({P(8)}-{P(6)}-B23*B21/B22)*B22*{lhv}/{P(4)}",
            "Each kWh comes off the electrolyser: 0.02 kg less RFNBO H₂",
        ),
        (26, "Reading A: non-compliant RFNBO in storage counts (follow-up (b) = yes)", None, ""),
        (27, "Renewable to the liquefier (kWh)", 0, "All renewable to the electrolyser"),
        (28, "December electrolyser RFNBO H₂ (kg)", "=(B19-B27)/2500*50", ""),
        (29, "December LH₂ GHG (gCO₂e/MJ)", "=B24", "Non-compliant, but stored"),
        (
            30,
            "LH₂ held at period end (kg)",
            "=L_Batches!O2+L_Batches!O3+(B28*30/50-Buffer_Ledger!B13)*B22/B21-Consignments!F2",
            "December LH₂ RFNBO = buffer RFNBO less the 4,880 kg kept in the buffer",
        ),
        (
            31,
            "CH₂ held at period end (kg)",
            "=C_Batches!O2+C_Batches!O3+B28*20/50-Consignments!F3",
            "",
        ),
        (
            32,
            "RFNBO held at period end (MJ)",
            f"=(Buffer_Ledger!B13+B30+B31)*{lhv}",
            "Key answer, reading A",
        ),
        (33, "Of which compliant (MJ)", f"=B32-(B28*30/50-Buffer_Ledger!B13)*B22/B21*{lhv}", ""),
        (34, "Reading B: only compliant RFNBO counts (follow-up (b) = no)", None, ""),
        (
            35,
            "0% electrolyser intervals at the end of December",
            "=IF(INT(B25/2500)*2500+1500>=B25,INT(B25/2500),INT(B25/2500)+1)",
            "Their renewable goes to the liquefier",
        ),
        (
            36,
            "Electrolyser renewable in the one part interval (kWh)",
            "=20/50*2500",
            "Makes exactly 20 kg RFNBO: that interval's compressor flow",
        ),
        (
            37,
            "Renewable to the liquefier (kWh)",
            "=B35*2500+(2500-B36)",
            "≥ the least needed (row 25)",
        ),
        (38, "December electrolyser RFNBO H₂ (kg)", "=(B19-B37)/2500*50", ""),
        (
            39,
            "December LH₂ GHG (gCO₂e/MJ)",
            f"=B23*B21/B22+(B20-B37)*{P(4)}/(B22*{lhv})+{P(6)}",
            "Compliant",
        ),
        (40, "100% electrolyser intervals in December", "=B18-B35-1", ""),
        (
            41,
            "December liquefier RFNBO feed (kg)",
            "=B40*30-Buffer_Ledger!B13",
            "Buffer kept full, as in the base case",
        ),
        (
            42,
            "December compressor RFNBO feed (kg)",
            "=B40*20+20",
            "Includes the part interval's 20 kg",
        ),
        (
            43,
            "GHG of the part-interval batch (gCO₂e/MJ)",
            f"=(500*{P(3)}+(2500-B36)*{P(4)})/(50*{lhv})+{P(5)}",
            "Non-compliant H₂ (§2.5), blended into CH₂",
        ),
        (
            44,
            "December CH₂ GHG (gCO₂e/MJ)",
            f'=(B40*20*B23+20*B43)/B42+SUMIF({D},"2026-12",{rng("Q")})*{P(4)}'
            f'/(SUMIF({D},"2026-12",{rng("S")})*{lhv})+{P(7)}',
            "No renewable for the compressor in December",
        ),
        (
            45,
            "LH₂ held at period end (kg)",
            "=L_Batches!O2+L_Batches!O3+B41*B22/B21-Consignments!F2",
            "",
        ),
        (46, "CH₂ held at period end (kg)", "=C_Batches!O2+C_Batches!O3+B42-Consignments!F3", ""),
        (
            47,
            "RFNBO held at period end (MJ)",
            f"=(Buffer_Ledger!B13+B45+B46)*{lhv}",
            "Key answer, reading B; all compliant",
        ),
        (
            48,
            "Which reading changes the answer?",
            "=B32>B47",
            "TRUE: under A the optimiser leaves December LH₂ non-compliant",
        ),
        (
            50,
            "V3 — Infeasible target on one product",
            None,
            "CA's target cut from 5.0 to 3.5 gCO₂e/MJ; CB unchanged.",
        ),
        (51, "CA target (gCO₂e/MJ)", 3.5, "Input for this variant"),
        (
            52,
            "Limit for aggregated batches (gCO₂e/MJ)",
            "=B51-Consignments!H2",
            "Target − Customer A adder",
        ),
        (53, "Lowest-GHG LH₂ available (gCO₂e/MJ)", "=MIN(L_Batches!Q2:Q4)", ""),
        (54, "CA feasible?", "=B53<=B52", "Expected: FALSE. Binding: no LH₂ below the limit"),
        (55, "Largest achievable CA volume (kg)", "=IF(B54,Consignments!F2,0)", "Expected: 0 kg"),
        (
            56,
            "CB still met?",
            "=AND(Consignments!O3,Consignments!P3,Consignments!Q3,Consignments!R3)",
            "One product's infeasibility does not touch the other",
        ),
        (
            57,
            "LH₂ write-off without CA (kg)",
            "=MAX(0,LH2_Ledger!D5-LH2_Ledger!B7)",
            "November, then December, then part of October",
        ),
        (58, "Consignments met", "=B56*1", "Of 2"),
        (59, "RFNBO held at period end (MJ)", "=Objectives!B7", "Unchanged: storage is still full"),
    ]
    for row, label, value, note in rows:
        bold = value is None
        s.put(f"A{row}", label, kind="text", bold=bold)
        if value is not None:
            key = row in (14, 32, 47, 54, 55)
            s.put(f"B{row}", value, fill=KEY if key else None)
        s.note(f"C{row}", note)
    for row in (3, 4, 5, 18, 35, 40, 58):
        s.ws[f"B{row}"].number_format = "#,##0"
    for row in (48, 54, 56):
        s.ws[f"B{row}"].number_format = "General"


def check_rows(cases: dict[str, Any]) -> list[tuple[str, str, float]]:
    b = cases["base"]
    u, led, cons, obj = b["units"], b["ledgers"], b["consignments"], b["objectives"]
    out: list[tuple[str, str, float]] = [
        ("Intervals in quarter", f"=COUNT({rng('A')})", b["intervals"])
    ]
    for r, bid in zip((2, 3, 4), ("E-2026-10", "E-2026-11", "E-2026-12"), strict=True):
        e = u["electrolyser"][bid]
        out += [
            (f"{bid} RFNBO share", f"=E_Batches!I{r}", e["rfnbo_share"]),
            (f"{bid} RFNBO H₂ (kg)", f"=E_Batches!P{r}", e["rfnbo_kg"]),
            (f"{bid} GHG (gCO₂e/MJ)", f"=E_Batches!N{r}", e["ghg"]),
            (
                f"{bid} RFNBO to compressor flow (kg)",
                f"=E_Split!E{r}",
                e["assigned_kg"]["to_compressor"],
            ),
            (f"{bid} RFNBO to buffer flow (kg)", f"=E_Split!F{r}", e["assigned_kg"]["to_buffer"]),
        ]
    for unit, sheet, ids in (
        ("liquefier", "L_Batches", ("L-2026-10", "L-2026-11", "L-2026-12")),
        ("compressor", "C_Batches", ("C-2026-10", "C-2026-11", "C-2026-12")),
    ):
        for r, bid in zip((2, 3, 4), ids, strict=True):
            x = u[unit][bid]
            out += [
                (f"{bid} RFNBO feed (kg)", f"={sheet}!D{r}", x["rfnbo_feed_kg"]),
                (f"{bid} RFNBO share", f"={sheet}!F{r}", x["rfnbo_share"]),
                (f"{bid} loss (kg)", f"={sheet}!H{r}", x["loss_kg"]),
                (f"{bid} RFNBO product (kg)", f"={sheet}!O{r}", x["rfnbo_kg"]),
                (f"{bid} GHG (gCO₂e/MJ)", f"={sheet}!Q{r}", x["ghg"]),
            ]
    out += [
        (
            "H₂ buffer closing ledger (kg)",
            "=Buffer_Ledger!B12",
            led["h2_buffer"]["closing_before_kg"],
        ),
        ("H₂ buffer write-off (kg)", "=Buffer_Ledger!B14", led["h2_buffer"]["write_off_kg"]),
        ("H₂ buffer carried (kg)", "=Buffer_Ledger!B15", led["h2_buffer"]["carried_kg"]),
        (
            "Pass-through closing ledger (kg)",
            "=PT_Ledger!B12",
            led["pt_compressor"]["closing_before_kg"],
        ),
        ("Pass-through write-off (kg)", "=PT_Ledger!B14", led["pt_compressor"]["write_off_kg"]),
    ]
    for sheet, storage, order in (
        ("LH2_Ledger", "lh2_storage", ("L-2026-11", "L-2026-12", "L-2026-10")),
        ("CH2_Ledger", "ch2_storage", ("C-2026-12", "C-2026-11", "C-2026-10")),
    ):
        lines = led[storage]["lines"]
        for r, bid in zip((2, 3, 4), order, strict=True):
            out += [
                (f"{storage} write-off {bid} (kg)", f"={sheet}!G{r}", lines[bid]["write_off_kg"]),
                (f"{storage} carried {bid} (kg)", f"={sheet}!H{r}", lines[bid]["carried_kg"]),
            ]
        out.append(
            (f"{storage} measured closing (kg)", f"={sheet}!B7", led[storage]["physical_kg"])
        )
    for r, cid in ((2, "CA"), (3, "CB")):
        c = cons[cid]
        for col, month in zip("JKL", ("2026-10", "2026-11", "2026-12"), strict=True):
            out.append(
                (f"{cid} {month} (kg)", f"=Consignments!{col}{r}", c["by_month"].get(month, 0.0))
            )
        out.append((f"{cid} shipped GHG (gCO₂e/MJ)", f"=Consignments!N{r}", c["shipped_ghg"]))
    out += [
        ("Consignments met", "=Objectives!B2", obj["consignments_met"]),
        ("RFNBO held at period end (MJ)", "=Objectives!B7", obj["stored_MJ"]),
        ("Carried emissions (gCO₂e)", "=Objectives!B8", obj["carried_emissions_g"]),
        ("Batches", "=Objectives!B9", obj["batches"]),
    ]
    v1, v2a, v2b, v3 = (
        cases[k] for k in ("v1_hourly", "v2_including", "v2_compliant", "v3_infeasible")
    )
    zero = sum(1 for e in v1["units"]["electrolyser"].values() if e["rfnbo_share"] == 0)
    out += [
        ("V1 electrolyser batches", "=Variants!B3", len(v1["units"]["electrolyser"])),
        ("V1 0% electrolyser batches", "=Variants!B4", zero),
        (
            "V1 RFNBO H₂ (kg)",
            "=Variants!B6",
            sum(e["rfnbo_kg"] for e in v1["units"]["electrolyser"].values()),
        ),
        ("V1 liquefier RFNBO feed (kg)", "=Variants!B9", v1["ledgers"]["h2_buffer"]["out_kg"]),
        (
            "V1 compressor RFNBO feed (kg)",
            "=Variants!B10",
            v1["ledgers"]["pt_compressor"]["out_kg"],
        ),
        ("V1 RFNBO LH₂ (kg)", "=Variants!B11", v1["ledgers"]["lh2_storage"]["in_kg"]),
        ("V1 RFNBO held at period end (MJ)", "=Variants!B14", v1["objectives"]["stored_MJ"]),
        (
            "V2 least liquefier renewable (kWh)",
            "=Variants!B25",
            R.v2_min_liquefier_kWh(R.build_data(R.V2_DEC_RENEWABLE_KWH, spot=False)),
        ),
        (
            "V2 reading A: December LH₂ GHG (gCO₂e/MJ)",
            "=Variants!B29",
            v2a["units"]["liquefier"]["L-2026-12"]["ghg"],
        ),
        ("V2 reading A: RFNBO held (MJ)", "=Variants!B32", v2a["objectives"]["stored_MJ"]),
        (
            "V2 reading A: compliant RFNBO held (MJ)",
            "=Variants!B33",
            v2a["objectives"]["stored_compliant_MJ"],
        ),
        (
            "V2 reading B: December electrolyser RFNBO H₂ (kg)",
            "=Variants!B38",
            sum(
                e["rfnbo_kg"]
                for k, e in v2b["units"]["electrolyser"].items()
                if k.startswith("E-2026-12")
            ),
        ),
        (
            "V2 reading B: December LH₂ GHG (gCO₂e/MJ)",
            "=Variants!B39",
            v2b["units"]["liquefier"]["L-2026-12"]["ghg"],
        ),
        (
            "V2 reading B: December CH₂ GHG (gCO₂e/MJ)",
            "=Variants!B44",
            v2b["units"]["compressor"]["C-2026-12"]["ghg"],
        ),
        (
            "V2 reading B: RFNBO held (MJ)",
            "=Variants!B47",
            v2b["objectives"]["stored_compliant_MJ"],
        ),
        ("V3 largest CA volume (kg)", "=Variants!B55", v3["consignments"]["CA"]["allocated_kg"]),
        ("V3 LH₂ write-off (kg)", "=Variants!B57", v3["ledgers"]["lh2_storage"]["write_off_kg"]),
        ("V3 consignments met", "=Variants!B58", v3["objectives"]["consignments_met"]),
        ("V3 RFNBO held at period end (MJ)", "=Variants!B59", v3["objectives"]["stored_MJ"]),
    ]
    return out


def checks(wb: Workbook, cases: dict[str, Any]) -> int:
    s = Sheet(wb, "Checks", {"A": 52, "B": 20, "C": 26, "D": 14, "E": 8})
    s.header(1, ["Check", "Workbook", "Independent Python result", "Difference", "Pass?"])
    rows = check_rows(cases)
    for i, (label, f, value) in enumerate(rows, 2):
        s.note(f"A{i}", label)
        s.put(f"B{i}", f)
        s.put(f"C{i}", float(value))
        s.put(f"D{i}", f"=B{i}-C{i}")
        s.put(f"E{i}", f"=ABS(D{i})<=0.000001*MAX(1,ABS(C{i}))", fmt="General")
    end = len(rows) + 1
    flags = [
        "E_Split!G2:G4",
        "E_Split!H2:H4",
        "Buffer_Ledger!H2:H4",
        "PT_Ledger!H2:H4",
        "LH2_Ledger!B10",
        "CH2_Ledger!B10",
        "PT_Ledger!B17",
        "Objectives!B16",
    ]
    s.note(
        f"A{end + 2}",
        "Rule flags all TRUE (split, allocations, write-off order, pass-through, bound)?",
    )
    s.put(f"E{end + 2}", f"=AND({','.join(flags)})", fmt="General")
    s.note(f"A{end + 3}", "Renewable pool never over-allocated in any interval?")
    s.put(
        f"E{end + 3}",
        f"=SUMPRODUCT(--({rng('F')}+{rng('G')}+{rng('H')}>{rng('E')}+0.000000001))=0",
        fmt="General",
    )
    s.note(f"A{end + 5}", "All checks pass?")
    s.put(
        f"E{end + 5}",
        f"=AND(COUNTIF(E2:E{end},FALSE())=0,E{end + 2},E{end + 3})",
        fill=KEY,
        fmt="General",
    )
    s.note(
        f"A{end + 6}",
        "Column C was computed by tests/golden/tc3_reference.py from the same inputs, not from "
        "these formulas.",
    )
    return end + 5


def build(out: Path) -> Path:
    data = R.build_data()
    d = R.base_decisions(data)
    cases = R.all_cases()
    wb = Workbook()
    wb.remove(wb.active)
    readme(wb)
    parameters(wb)
    data_sheet(wb, data, d)
    e_batches(wb)
    e_split(wb)
    feed_ledger(
        wb,
        "Buffer_Ledger",
        flow_col="F",
        targets=["L-2026-10", "L-2026-11", "L-2026-12"],
        target_sheet="L_Batches",
        pass_through=False,
    )
    unit_batches(
        wb,
        "L_Batches",
        ids=["L-2026-10", "L-2026-11", "L-2026-12"],
        batch_col="AA",
        consumed="O",
        product="P",
        alloc="G",
        elec="N",
        feed_sheet="Buffer_Ledger",
        adder=6,
        product_label="LH₂",
    )
    feed_ledger(
        wb,
        "PT_Ledger",
        flow_col="E",
        targets=["C-2026-10", "C-2026-11", "C-2026-12"],
        target_sheet="C_Batches",
        pass_through=True,
    )
    unit_batches(
        wb,
        "C_Batches",
        ids=["C-2026-10", "C-2026-11", "C-2026-12"],
        batch_col="AB",
        consumed="R",
        product="S",
        alloc="H",
        elec="Q",
        feed_sheet="PT_Ledger",
        adder=7,
        product_label="CH₂",
    )
    product_ledger(
        wb,
        "LH2_Ledger",
        order=["L-2026-11", "L-2026-12", "L-2026-10"],
        batch_sheet="L_Batches",
        cons_row=2,
        inv_col="W",
        opening_physical=11,
        opening_ledger=12,
        produced_col="P",
        shipped=[("Shipped CA (kg)", "U"), ("Spot sale, no claim (kg)", "V")],
    )
    product_ledger(
        wb,
        "CH2_Ledger",
        order=["C-2026-12", "C-2026-11", "C-2026-10"],
        batch_sheet="C_Batches",
        cons_row=3,
        inv_col="Y",
        opening_physical=13,
        opening_ledger=14,
        produced_col="S",
        shipped=[("Shipped CB (kg)", "X")],
    )
    consignments(wb)
    objectives(wb)
    variants(wb)
    checks(wb, cases)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out", nargs="?", type=Path, default=OUT)
    print(build(ap.parse_args().out))


if __name__ == "__main__":
    main()
