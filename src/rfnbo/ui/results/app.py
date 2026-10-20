"""Results browser pages (NiceGUI): runs → run → consignment → batch → 15-minute intervals.

All content comes from :mod:`rfnbo.ui.results.views`, which re-calculates sealed runs with the
calculator; this module only lays it out.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from nicegui import ui

from rfnbo.report.excel import write_xlsx
from rfnbo.report.tables import all_tables
from rfnbo.ui.results import views as V

_ROOT: Path = Path(".")
# Unit markers that right-align a numeric column without printing a unit in its header.
NO_SUFFIX = {"", "share", "n", "num"}
_LOADED: dict[str, V.LoadedRun] = {}


def _runs() -> dict[str, V.RunInfo]:
    return {r.run_id: r for r in V.find_runs(_ROOT)}


def _get(run_id: str) -> V.LoadedRun:
    if run_id not in _LOADED:
        info = _runs().get(run_id)
        if info is None:
            raise V.NotFound(f"no sealed run {run_id!r} under {_ROOT}")
        _LOADED[run_id] = V.load(info)
    return _LOADED[run_id]


def fmt(v: Any, unit: str = "") -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        if unit in ("gCO2e/MJ", "share"):
            return f"{v:.6f}"
        return f"{v:,.3f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def _crumbs(*parts: tuple[str, str | None]) -> None:
    with ui.row().classes("items-center gap-1 text-sm"):
        for i, (label, href) in enumerate(parts):
            if i:
                ui.label("›").classes("text-grey")
            if href:
                ui.link(label, href).props(f'data-testid="crumb-{i}"')
            else:
                ui.label(label).classes("font-medium")


def _table(
    columns: list[tuple[str, str, str]],
    rows: list[dict[str, Any]],
    links: dict[str, str] | None = None,
    testid: str = "",
    page: int = 0,
) -> None:
    """columns: (key, label, unit); links: column key → row key holding the href."""
    cols = [
        {
            "name": k,
            "label": label if unit in NO_SUFFIX else f"{label} ({unit})",
            "field": k,
            "align": "left" if not unit else "right",
        }
        for k, label, unit in columns
    ]
    shown = []
    for r in rows:
        row: dict[str, Any] = {k: fmt(r.get(k), unit) for k, _, unit in columns}
        for k, href_key in (links or {}).items():
            row[f"{k}__href"] = r.get(href_key)
        shown.append(row)
    table = ui.table(
        columns=cols, rows=shown, row_key=columns[0][0], pagination=page or None
    ).props(f'dense flat bordered data-testid="{testid}"')
    for k in links or {}:
        table.add_slot(
            f"body-cell-{k}",
            f"""
            <q-td :props="props">
              <a v-if="props.row['{k}__href']" :href="props.row['{k}__href']"
                 :data-testid="'link-' + props.value" class="text-primary">{{{{ props.value }}}}</a>
              <span v-else>{{{{ props.value }}}}</span>
            </q-td>""",
        )


def _not_found(exc: Exception) -> None:
    ui.label(str(exc)).classes("text-negative")
    ui.link("All runs", "/")


@ui.page("/")
def runs_page() -> None:
    _crumbs(("Sealed runs", None))
    runs = list(_runs().values())
    if not runs:
        ui.label(f"No sealed runs under {_ROOT}").classes("text-grey")
        return
    rows = [
        {
            "run_id": r.run_id,
            "href": f"/run/{r.run_id}",
            "label": r.label,
            "plant": r.plant,
            "quarter": r.quarter,
            "version": r.config_version,
            "checks": r.checks,
            "failed": r.failed,
        }
        for r in runs
    ]
    _table(
        [
            ("run_id", "Run", ""),
            ("label", "Folder", ""),
            ("plant", "Plant", ""),
            ("quarter", "Quarter", ""),
            ("version", "Config version", ""),
            ("checks", "Checks", "n"),
            ("failed", "Failed", "n"),
        ],
        rows,
        links={"run_id": "href"},
        testid="runs",
    )


@ui.page("/run/{run_id}")
def run_page(run_id: str) -> None:
    try:
        loaded = _get(run_id)
    except V.NotFound as exc:
        _not_found(exc)
        return
    run, info = loaded.run, loaded.info
    view = V.overview(run)
    _crumbs(("Sealed runs", "/"), (run_id, None))
    with ui.row().classes("items-center gap-4"):
        ui.label(f"{info.plant} · {info.quarter} · config v{info.config_version}").classes(
            "text-lg font-medium"
        )
        if loaded.problems:
            ui.badge("does not reproduce", color="negative").props('data-testid="reproduce"')
        else:
            ui.badge("reproduces byte for byte", color="positive").props('data-testid="reproduce"')
        ui.button("Excel report", on_click=lambda: _download(loaded)).props(
            'flat dense data-testid="download-report"'
        )
    for p in loaded.problems:
        ui.label(p).classes("text-negative text-xs")

    o = view["objectives"]
    with ui.row().classes("gap-4"):
        for label, value in (
            ("Consignments met", fmt(o.consignments_met)),
            ("RFNBO carried forward, final-product MJ", fmt(o.carried_value_MJ)),
            ("Compliant RFNBO, final product (kg)", fmt(o.final_rfnbo_kg)),
            ("Emissions carried forward (gCO2e)", fmt(o.carried_emissions_g)),
            ("Failed checks", fmt(len(run.failures))),
        ):
            with ui.card().classes("p-3"):
                ui.label(label).classes("text-xs text-grey")
                ui.label(value).classes("text-base font-medium")

    ui.label("Consignments").classes("text-base font-medium mt-4")
    rows = [
        dict(r, href=f"/run/{run_id}/consignment/{r['consignment']}") for r in view["consignments"]
    ]
    _table(
        [
            ("consignment", "Consignment", ""),
            ("customer", "Customer", ""),
            ("product", "Product", ""),
            ("ship_date", "Ship date", ""),
            ("volume_kg", "Volume", "kg"),
            ("target", "Target", "gCO2e/MJ"),
            ("shipped_ghg", "Shipped GHG", "gCO2e/MJ"),
            ("margin_target", "Margin to target", "gCO2e/MJ"),
        ],
        rows,
        links={"consignment": "href"},
        testid="consignments",
    )

    ui.label("Units").classes("text-base font-medium mt-4")
    _table(
        [
            ("name", "Unit", ""),
            ("batches", "Batches", "n"),
            ("product_kg", "Product", "kg"),
            ("rfnbo_kg", "RFNBO", "kg"),
            ("compliant_rfnbo_kg", "Compliant RFNBO", "kg"),
            ("non_compliant_batches", "Non-compliant RFNBO batches", "n"),
        ],
        view["units"],
        testid="units",
    )

    ui.label("Products").classes("text-base font-medium mt-4")
    _table(
        [
            ("storage", "Storage", ""),
            ("product", "Product", ""),
            ("kind", "Role", ""),
            ("in_kg", "RFNBO in", "kg"),
            ("out_feed_kg", "Fed on", "kg"),
            ("out_consignment_kg", "Consigned", "kg"),
            ("write_off_kg", "Written off", "kg"),
            ("carried_kg", "Carried", "kg"),
            ("weight", "Weight", "num"),
            ("value_MJ", "Carried value", "MJ"),
        ],
        view["products"],
        testid="products",
    )

    if view["pos"]:
        ui.label("Purchased RFNBO: PoS register").classes("text-base font-medium mt-4")
        _table(
            [
                ("pos", "PoS", ""),
                ("supplier", "Supplier", ""),
                ("dispatch_date", "Dispatched", ""),
                ("quantity_kg", "Received", "kg"),
                ("ghg", "GHG", "gCO2e/MJ"),
                ("allocated_kg", "Allocated", "kg"),
                ("written_off_kg", "Written off", "kg"),
                ("to", "Where it went", ""),
            ],
            [dict(r, href=f"/run/{run_id}/batch/{r['pos']}") for r in view["pos"]],
            links={"pos": "href"},
            testid="pos",
        )

    ui.label("Storage ledgers").classes("text-base font-medium mt-4")
    _table(
        [
            ("storage", "Storage", ""),
            ("opening_kg", "Opening", "kg"),
            ("produced_kg", "Produced", "kg"),
            ("out_feed_kg", "Out: feed", "kg"),
            ("out_consignment_kg", "Out: consignments", "kg"),
            ("physical_kg", "Physical", "kg"),
            ("write_off_kg", "Write-off", "kg"),
            ("carried_kg", "Carried", "kg"),
            ("carried_ghg", "Carried GHG", "gCO2e/MJ"),
        ],
        view["ledgers"],
        testid="ledgers",
    )

    if view["failures"]:
        ui.label("Failed checks").classes("text-base font-medium mt-4 text-negative")
        _table(
            [
                ("check", "Check", ""),
                ("subject", "Subject", ""),
                ("detail", "Detail", ""),
                ("reference", "Reference", ""),
            ],
            view["failures"],
            testid="failures",
        )


def _download(loaded: V.LoadedRun) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = write_xlsx(
            all_tables(loaded.run, loaded.info.run_id),
            Path(tmp) / f"report_{loaded.info.run_id}.xlsx",
        )
        ui.download.content(path.read_bytes(), path.name)


@ui.page("/run/{run_id}/consignment/{consignment_id}")
def consignment_page(run_id: str, consignment_id: str) -> None:
    try:
        run = _get(run_id).run
        view = V.consignment_view(run, consignment_id)
    except V.NotFound as exc:
        _not_found(exc)
        return
    s = view["summary"]
    _crumbs(("Sealed runs", "/"), (run_id, f"/run/{run_id}"), (consignment_id, None))
    ui.label(
        f"Consignment {consignment_id} · {s['customer']} · {s['product']} · ships {s['ship_date']}"
    ).classes("text-lg font-medium")
    with ui.row().classes("gap-4"):
        for label, value in (
            ("Volume (kg)", fmt(s["volume_kg"])),
            ("Highest aggregated batch GHG (gCO2e/MJ)", fmt(s["highest_ghg"], "gCO2e/MJ")),
            ("Distribution adder", fmt(s["adder"], "gCO2e/MJ")),
            ("Shipped GHG", fmt(s["shipped_ghg"], "gCO2e/MJ")),
            ("Target", fmt(s["target"], "gCO2e/MJ")),
        ):
            with ui.card().classes("p-3"):
                ui.label(label).classes("text-xs text-grey")
                ui.label(value).classes("text-base font-medium").props(
                    f'data-testid="cons-{label.split()[0].lower()}"'
                )
    for agg in view["aggregated"]:
        ui.label(
            f"Aggregated batch {agg['id']} · production month {agg['months']} · "
            f"{fmt(agg['kg'])} kg at {fmt(agg['ghg'], 'gCO2e/MJ')} gCO2e/MJ"
        ).classes("text-base font-medium mt-4")
        rows = [dict(p, href=f"/run/{run_id}/batch/{p['batch']}") for p in agg["parts"]]
        _table(
            [
                ("batch", "Batch", ""),
                ("month", "Month", ""),
                ("kg", "Quantity", "kg"),
                ("MJ", "Energy", "MJ"),
                ("ghg", "GHG", "gCO2e/MJ"),
                ("compliant", "≤ threshold", ""),
            ],
            rows,
            links={"batch": "href"},
            testid=f"parts-{agg['id']}",
        )
    ui.label("Rule checks").classes("text-base font-medium mt-4")
    _table(
        [
            ("check", "Check", ""),
            ("passed", "Passed", ""),
            ("detail", "Detail", ""),
            ("reference", "Reference", ""),
        ],
        view["checks"],
        testid="checks",
    )


@ui.page("/run/{run_id}/batch/{batch_id}")
def batch_page(run_id: str, batch_id: str) -> None:
    try:
        run = _get(run_id).run
        view = V.batch_view(run, batch_id)
    except V.NotFound as exc:
        _not_found(exc)
        return
    _crumbs(("Sealed runs", "/"), (run_id, f"/run/{run_id}"), (batch_id, None))
    down = [dict(d, href=_href(run_id, d)) for d in view["downstream"]]
    if view["kind"] == "pos":
        ui.label(
            f"{batch_id}: proof of sustainability from {view['supplier']} · {view['product']} · "
            f"dispatched {view['dispatch_date']} ({view['quarter']})"
        ).classes("text-lg font-medium")
        with ui.row().classes("gap-4"):
            for label, value in (
                ("Received (kg)", fmt(view["quantity_kg"])),
                ("GHG incl. distribution", fmt(view["ghg"], "gCO2e/MJ")),
                ("Allocated (kg)", fmt(view["allocated_kg"])),
                ("Written off, not allocated (kg)", fmt(view["written_off_kg"])),
            ):
                with ui.card().classes("p-3"):
                    ui.label(label).classes("text-xs text-grey")
                    ui.label(value).classes("text-base font-medium")
        ui.label("Credentials allocated to the supplier's metered deliveries").classes(
            "text-base font-medium mt-4"
        )
        rows = [
            dict(a, href=f"/run/{run_id}/batch/{a['to']}" if a["drillable"] else None)
            for a in view["allocations"]
        ]
        _table(
            [
                ("flow", "Supply flow", ""),
                ("to", "Batch or storage", ""),
                ("start", "From (local)", ""),
                ("intervals", "Intervals", ""),
                ("kg", "Credentials", "kg"),
                ("metered_kg", "Metered delivery", "kg"),
            ],
            rows,
            links={"to": "href"},
            testid="pos-allocations",
        )
    elif view["kind"] == "opening":
        ui.label(f"{batch_id}: opening ledger entry in {view['storage']}").classes(
            "text-lg font-medium"
        )
        ui.label(
            f"Produced in {view['month']}, carried into this quarter with GHG "
            f"{fmt(view['ghg'], 'gCO2e/MJ')} gCO2e/MJ and {fmt(view['rfnbo_kg'])} kg RFNBO. "
            "It has no 15-minute data in this run; trace it in the prior quarter's sealed "
            "run."
        ).props('data-testid="opening-note"')
    else:
        rec = view["record"]
        ui.label(
            f"{batch_id} · {rec['unit_name']} · {rec['start']} to {rec['end']} "
            f"({rec['intervals']} intervals)"
        ).classes("text-lg font-medium")
        with ui.row().classes("gap-4"):
            for label, value in (
                ("RFNBO share", fmt(rec["rfnbo_share"], "share")),
                ("GHG (gCO2e/MJ)", fmt(rec["ghg"], "gCO2e/MJ")),
                ("Compliant", fmt(rec["compliant"])),
                ("RFNBO (kg)", fmt(rec["rfnbo_qty"])),
                ("Product (kg)", fmt(rec["output"]["qty"])),
            ):
                with ui.card().classes("p-3"):
                    ui.label(label).classes("text-xs text-grey")
                    ui.label(value).classes("text-base font-medium")
        ui.link(
            "15-minute data behind this batch →", f"/run/{run_id}/batch/{batch_id}/intervals"
        ).props('data-testid="intervals-link"').classes("mt-2")
        ui.label("Inputs").classes("text-base font-medium mt-4")
        _table(
            [
                ("input", "Input", ""),
                ("qty", "Quantity", "num"),
                ("uom", "Unit", ""),
                ("ef", "Emission factor", "num"),
                ("ef_uom", "EF unit", ""),
                ("emissions_g", "Emissions", "gCO2e"),
            ],
            rec["inputs"],
            testid="inputs",
        )
        if len(view["outputs"]) > 1:
            ui.label("Split output (RFNBO to each storage)").classes("text-base font-medium mt-4")
            _table(
                [("storage", "Storage", ""), ("rfnbo_kg", "RFNBO", "kg")],
                view["outputs"],
                testid="outputs",
            )
        if view["upstream"]:
            ui.label("Upstream batches and PoS (RFNBO feed)").classes("text-base font-medium mt-4")
            rows = [dict(u, href=f"/run/{run_id}/batch/{u['batch']}") for u in view["upstream"]]
            _table(
                [
                    ("batch", "Batch or PoS", ""),
                    ("via", "Via storage or supply flow", ""),
                    ("kg", "Feed", "kg"),
                    ("MJ", "Energy", "MJ"),
                    ("ghg", "GHG", "gCO2e/MJ"),
                    ("compliant", "≤ threshold", ""),
                ],
                rows,
                links={"batch": "href"},
                testid="upstream",
                page=25,
            )
        ok = "✓" if view["recompute_ok"] else "✗"
        ui.label(f"Formula: {rec['formula']}").classes("text-xs text-grey mt-2")
        ui.label(
            f"{ok} GHG recomputed from this record: {fmt(view['recomputed_ghg'], 'gCO2e/MJ')}"
        ).props('data-testid="recompute"')
    ui.label("Where this batch went").classes("text-base font-medium mt-4")
    _table(
        [("to", "To", ""), ("kind", "Kind", ""), ("kg", "Quantity", "kg")],
        down,
        links={"to": "href"},
        testid="downstream",
        page=25,
    )


def _href(run_id: str, edge: dict[str, Any]) -> str | None:
    if edge["kind"] == "feed":
        return f"/run/{run_id}/batch/{edge['to']}"
    if edge["kind"] == "aggregation" and edge.get("consignment"):
        return f"/run/{run_id}/consignment/{edge['consignment']}"
    return None


@ui.page("/run/{run_id}/batch/{batch_id}/intervals")
def intervals_page(run_id: str, batch_id: str) -> None:
    try:
        run = _get(run_id).run
        view = V.interval_view(run, batch_id)
    except V.NotFound as exc:
        _not_found(exc)
        return
    _crumbs(
        ("Sealed runs", "/"),
        (run_id, f"/run/{run_id}"),
        (batch_id, f"/run/{run_id}/batch/{batch_id}"),
        ("15-minute data", None),
    )
    ui.label(f"{batch_id}: {len(view.table.rows):,} intervals").classes(
        "text-lg font-medium"
    ).props('data-testid="interval-count"')
    verdict = (
        "✓ Interval totals equal the batch record"
        if view.consistent
        else "✗ Interval totals differ from the batch record"
    )
    ui.label(verdict).classes("text-positive" if view.consistent else "text-negative").props(
        'data-testid="totals-check"'
    )
    _table(
        [
            ("label", "Quantity", ""),
            ("intervals_total", "Sum of intervals", "num"),
            ("batch_value", "Batch record", "num"),
            ("ok", "Equal", ""),
        ],
        [
            {
                "label": f"{c.label} ({c.unit})",
                "intervals_total": c.intervals_total,
                "batch_value": c.batch_value,
                "ok": c.ok,
            }
            for c in view.checks
        ],
        testid="totals",
    )
    ui.label(view.matched_note).classes("text-sm mt-2")
    _table(
        [(c.key, c.header, c.unit) for c in view.table.columns],
        view.table.rows,
        testid="intervals",
        page=96,
    )


def run(root: Path, port: int = 8081, show: bool = True) -> None:
    global _ROOT
    _ROOT = root
    ui.run(port=port, show=show, reload=False, title="RFNBO results")
