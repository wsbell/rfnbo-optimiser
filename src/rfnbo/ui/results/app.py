"""Results browser pages (NiceGUI): runs → run → consignment → batch → intervals.

All content comes from :mod:`rfnbo.ui.results.views`, which re-calculates sealed runs with the
calculator; this module only lays it out, in the shared look (:mod:`rfnbo.ui.theme`).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from nicegui import ui

from rfnbo.data import calendar as cal
from rfnbo.report.excel import write_xlsx
from rfnbo.report.tables import all_tables
from rfnbo.ui import theme
from rfnbo.ui.results import views as V

_ROOT: Path = Path(".")
# Unit markers that right-align a numeric column without printing a unit in its header.
NO_SUFFIX = {"", "share", "n", "num"}
_LOADED: dict[str, V.LoadedRun] = {}
RUNS = ("Sealed runs", "/runs")


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


def _table(
    columns: list[tuple[str, str, str]],
    rows: list[dict[str, Any]],
    links: dict[str, str] | None = None,
    testid: str = "",
    page: int = 0,
    row_key: str | None = None,
) -> ui.table:
    """columns: (key, label, unit); links: column key → row key holding the href; row_key:
    a row field that identifies the row, kept unformatted (default: the first column)."""
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
        if row_key is not None:
            row[row_key] = r.get(row_key)
        shown.append(row)
    table = (
        ui.table(
            columns=cols, rows=shown, row_key=row_key or columns[0][0], pagination=page or None
        )
        .props(f'flat bordered data-testid="{testid}"')
        .classes("w-full")
    )
    for k in links or {}:
        table.add_slot(
            f"body-cell-{k}",
            f"""
            <q-td :props="props">
              <a v-if="props.row['{k}__href']" :href="props.row['{k}__href']"
                 :data-testid="'link-' + props.value" style="font-weight: 600"
                 >{{{{ props.value }}}}</a>
              <span v-else>{{{{ props.value }}}}</span>
            </q-td>""",
        )
    return table


def _not_found(exc: Exception) -> None:
    theme.top_bar([RUNS])
    with theme.page():
        ui.label(str(exc)).classes("text-negative")
        ui.link("All runs", "/runs")


@ui.page("/runs")
def runs_page() -> None:
    theme.apply()
    theme.top_bar([("Sealed runs", None)])
    with theme.page():
        theme.title("Sealed runs", str(_ROOT))
        runs = list(_runs().values())
        if not runs:
            ui.label(f"No sealed runs under {_ROOT}").classes("rf-note")
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
    theme.apply()
    try:
        loaded = _get(run_id)
    except V.NotFound as exc:
        _not_found(exc)
        return
    run, info = loaded.run, loaded.info
    view = V.overview(run)
    right = theme.top_bar([RUNS, (run_id, None)])
    with right:
        theme.secondary(
            ui.button("Excel report", icon="download", on_click=lambda: _download(loaded))
        ).props('data-testid="download-report"')
    with theme.page():
        with (
            theme.title(
                f"{info.plant} · {info.quarter}", f"{info.label} · config v{info.config_version}"
            ),
            ui.row().classes("gap-2 mt-1"),
        ):
            if loaded.problems:
                theme.pill("Does not reproduce", "danger", testid="reproduce")
            else:
                theme.pill("Reproduces byte for byte", "success", testid="reproduce")
            if run.frame.provisional:
                until = cal.to_local(run.frame.end, run.frame.timezone)
                theme.pill(
                    f"Provisional: quarter to date until {until:%Y-%m-%d %H:%M}",
                    "caution",
                    testid="provisional",
                ).tooltip("Closed as if the quarter ended at the data's end; not a closing")
            failed = len(run.failures)
            theme.pill(
                f"{len(run.checks)} checks, {failed} failed",
                "danger" if failed else "neutral",
            )
        for p in loaded.problems:
            ui.label(p).classes("text-negative text-xs")

        o = view["objectives"]
        theme.kpis(
            [
                ("Consignments met", fmt(o.consignments_met), ""),
                ("RFNBO carried forward, final product", fmt(o.carried_value_MJ), "MJ"),
                ("Compliant RFNBO, final products", fmt(o.final_rfnbo_kg), "kg"),
                ("Emissions carried forward", fmt(o.carried_emissions_g), "gCO₂e"),
                ("Failed checks", fmt(len(run.failures)), ""),
            ]
        )

        with theme.card(
            "Consignments", "Shipped GHG: highest aggregated batch GHG plus the distribution adder"
        ):
            rows = [
                dict(r, href=f"/run/{run_id}/consignment/{r['consignment']}")
                for r in view["consignments"]
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

        with theme.card("Units"):
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

        with theme.card("Products"):
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
            with theme.card("Purchased RFNBO: PoS register"):
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

        with theme.card("Storage ledgers"):
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
            with theme.card("Failed checks"):
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
    theme.apply()
    try:
        run = _get(run_id).run
        view = V.consignment_view(run, consignment_id)
    except V.NotFound as exc:
        _not_found(exc)
        return
    s = view["summary"]
    theme.top_bar([RUNS, (run_id, f"/run/{run_id}"), (consignment_id, None)])
    with theme.page():
        theme.title(
            f"Consignment {consignment_id} · {s['customer']} · {s['product']}",
            f"Ships {s['ship_date']}",
        )
        with ui.element("section").classes("rf-card rf-kpis"):
            for label, value, unit in (
                ("Volume", fmt(s["volume_kg"]), "kg"),
                ("Highest aggregated batch GHG", fmt(s["highest_ghg"], "gCO2e/MJ"), "gCO₂e/MJ"),
                ("Distribution adder", fmt(s["adder"], "gCO2e/MJ"), "gCO₂e/MJ"),
                ("Shipped GHG", fmt(s["shipped_ghg"], "gCO2e/MJ"), "gCO₂e/MJ"),
                ("Target", fmt(s["target"], "gCO2e/MJ"), "gCO₂e/MJ"),
            ):
                with ui.element("div").classes("rf-kpi"):
                    ui.label(label).classes("rf-kpi-label")
                    with ui.element("div").classes("rf-kpi-figure"):
                        ui.label(value).classes("rf-kpi-value").props(
                            f'data-testid="cons-{label.split()[0].lower()}"'
                        )
                        ui.label(unit).style(f"color: {theme.MUTED}; font-weight: 500")
        for agg in view["aggregated"]:
            with theme.card(
                f"Aggregated batch {agg['id']}",
                f"Production month {agg['months']} · {fmt(agg['kg'])} kg at "
                f"{fmt(agg['ghg'], 'gCO2e/MJ')} gCO₂e/MJ",
            ):
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
        with theme.card("Rule checks"):
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
    theme.apply()
    try:
        run = _get(run_id).run
        view = V.batch_view(run, batch_id)
    except V.NotFound as exc:
        _not_found(exc)
        return
    theme.top_bar([RUNS, (run_id, f"/run/{run_id}"), (batch_id, None)])
    down = [dict(d, href=_href(run_id, d)) for d in view["downstream"]]
    with theme.page():
        if view["kind"] == "pos":
            theme.title(
                f"{batch_id}: proof of sustainability from {view['supplier']}",
                f"{view['product']} · dispatched {view['dispatch_date']} ({view['quarter']})",
            )
            theme.kpis(
                [
                    ("Received", fmt(view["quantity_kg"]), "kg"),
                    ("GHG incl. distribution", fmt(view["ghg"], "gCO2e/MJ"), "gCO₂e/MJ"),
                    ("Allocated", fmt(view["allocated_kg"]), "kg"),
                    ("Written off, not allocated", fmt(view["written_off_kg"]), "kg"),
                ]
            )
            with theme.card("Credentials allocated to the supplier's metered deliveries"):
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
            theme.title(f"{batch_id}: opening ledger entry", f"In {view['storage']}")
            with theme.card():
                ui.label(
                    f"Produced in {view['month']}, carried into this quarter with GHG "
                    f"{fmt(view['ghg'], 'gCO2e/MJ')} gCO2e/MJ and {fmt(view['rfnbo_kg'])} kg "
                    "RFNBO. It has no interval data in this run; trace it in the prior "
                    "quarter's sealed run."
                ).props('data-testid="opening-note"')
        else:
            rec = view["record"]
            with (
                theme.title(
                    f"{batch_id} · {rec['unit_name']}",
                    f"{rec['start']} to {rec['end']} ({rec['intervals']} intervals)",
                ),
                ui.row().classes("gap-2 mt-1 items-center"),
            ):
                theme.pill(
                    "Compliant" if rec["compliant"] else "Not compliant",
                    "success" if rec["compliant"] else "danger",
                )
                ui.link(
                    "Interval data behind this batch →",
                    f"/run/{run_id}/batch/{batch_id}/intervals",
                ).props('data-testid="intervals-link"')
            theme.kpis(
                [
                    ("RFNBO share", fmt(rec["rfnbo_share"], "share"), ""),
                    ("GHG", fmt(rec["ghg"], "gCO2e/MJ"), "gCO₂e/MJ"),
                    ("Compliant", fmt(rec["compliant"]), ""),
                    ("RFNBO", fmt(rec["rfnbo_qty"]), "kg"),
                    ("Product", fmt(rec["output"]["qty"]), "kg"),
                ]
            )
            with theme.card("Inputs"):
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
                ok = "✓" if view["recompute_ok"] else "✗"
                ui.label(f"Formula: {rec['formula']}").classes("rf-note rf-mono")
                ui.label(
                    f"{ok} GHG recomputed from this record: "
                    f"{fmt(view['recomputed_ghg'], 'gCO2e/MJ')}"
                ).props('data-testid="recompute"').classes(
                    "text-positive" if view["recompute_ok"] else "text-negative"
                )
            if len(view["outputs"]) > 1:
                with theme.card("Split output", "RFNBO to each storage"):
                    _table(
                        [("storage", "Storage", ""), ("rfnbo_kg", "RFNBO", "kg")],
                        view["outputs"],
                        testid="outputs",
                    )
            if view["upstream"]:
                with theme.card("Upstream batches and PoS", "RFNBO feed"):
                    rows = [
                        dict(u, href=f"/run/{run_id}/batch/{u['batch']}") for u in view["upstream"]
                    ]
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
        with theme.card("Where this batch went"):
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
    theme.apply()
    try:
        run = _get(run_id).run
        view = V.interval_view(run, batch_id)
    except V.NotFound as exc:
        _not_found(exc)
        return
    theme.top_bar(
        [
            RUNS,
            (run_id, f"/run/{run_id}"),
            (batch_id, f"/run/{run_id}/batch/{batch_id}"),
            ("Interval data", None),
        ]
    )
    with theme.page():
        with theme.title(f"{batch_id}: interval data"):
            ui.label(f"{batch_id}: {len(view.table.rows):,} intervals").classes("rf-sub").props(
                'data-testid="interval-count"'
            )
        with theme.card("Interval totals against the batch record") as (head, _):
            with head:
                verdict = (
                    "✓ Interval totals equal the batch record"
                    if view.consistent
                    else "✗ Interval totals differ from the batch record"
                )
                ui.label(verdict).classes(
                    "text-positive" if view.consistent else "text-negative"
                ).props('data-testid="totals-check"')
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
        with theme.card("Intervals", view.matched_note):
            _table(
                [(c.key, c.header, c.unit) for c in view.table.columns],
                view.table.rows,
                testid="intervals",
                page=96,
            )


def run(root: Path, port: int = 8081, show: bool = True) -> None:
    global _ROOT
    _ROOT = root
    theme.setup()

    @ui.page("/")
    def index() -> None:
        ui.navigate.to("/runs")

    ui.run(port=port, show=show, reload=False, title="RFNBO results")
