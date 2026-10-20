"""The Data screen page (NiceGUI): set up a quarter, map and check the data, correct it with
reasons, enter consignments, PoS records and the opening ledger, and run (build plan §11.2).

All state and rules live in :class:`rfnbo.ui.data.workspace.Workspace`; this module lays it
out. The results browser's pages are served alongside, on the project's ``runs/`` folder, so a
sealed run opens there.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nicegui import events, ui
from nicegui import run as background

from rfnbo.ui.data.workspace import Workspace, WorkspaceError
from rfnbo.ui.results import app as results
from rfnbo.ui.results.app import _table

TABS = (
    "Setup",
    "Mapping",
    "Preview",
    "Corrections",
    "Consignments",
    "PoS",
    "Opening ledger",
    "Run",
)


class DataPage:
    """One browser tab's Data screen."""

    def __init__(self, root: Path) -> None:
        self.ws = Workspace(root)
        self.message: ui.label | None = None
        self.start = 0

    # ---- helpers ---------------------------------------------------------------------------

    def act(self, fn: Callable[[], Any], ok: str = "") -> bool:
        """Apply an action; save and redraw on success, show why on failure."""
        try:
            fn()
            if self.ws.ready:
                self.ws.save()
        except (WorkspaceError, ValueError, OSError) as exc:
            self.say(str(exc), error=True)
            return False
        self.say(ok)
        self.redraw()
        return True

    def say(self, text: str, error: bool = False) -> None:
        if self.message is not None:
            self.message.text = text
            self.message.classes(replace="text-negative" if error else "text-positive")

    def redraw(self) -> None:
        for section in (
            self.setup,
            self.mapping,
            self.preview,
            self.corrections,
            self.consignments,
            self.pos,
            self.opening,
            self.run_tab,
        ):
            section.refresh()

    # ---- layout ----------------------------------------------------------------------------

    def build(self) -> None:
        with ui.row().classes("items-center gap-4"):
            ui.label(f"Data · {self.ws.root}").classes("text-lg font-medium")
            ui.link("Sealed runs →", "/").props('data-testid="runs-link"')
        self.message = ui.label("").props('data-testid="message"')
        with ui.tabs() as tabs:
            for name in TABS:
                ui.tab(name).props(f'data-testid="tab-{name.split()[0].lower()}"')
        with ui.tab_panels(tabs, value=TABS[0]).classes("w-full"):
            for name, section in zip(
                TABS,
                (
                    self.setup,
                    self.mapping,
                    self.preview,
                    self.corrections,
                    self.consignments,
                    self.pos,
                    self.opening,
                    self.run_tab,
                ),
                strict=True,
            ):
                with ui.tab_panel(name):
                    section()

    @ui.refreshable_method
    def setup(self) -> None:
        p = self.ws.project
        ui.label("Plant config and quarter").classes("text-base font-medium")
        with ui.row().classes("items-end gap-2"):
            config = (
                ui.input("Plant config (path)", value=p.file.config if p else "")
                .props('data-testid="config"')
                .classes("w-96")
            )
            quarter = ui.input("Quarter", value=p.file.quarter if p else "", placeholder="2026-Q4")
            quarter.props('data-testid="quarter"')
            ui.button(
                "Apply",
                on_click=lambda: self.act(
                    lambda: self.ws.setup(config.value.strip(), quarter.value.strip()), "saved"
                ),
            ).props('data-testid="apply-setup"')
        if not p:
            ui.label("Choose a validated plant config (made in rfnbo edit) and a quarter.")
            return
        ui.label("Data files").classes("text-base font-medium mt-4")
        ui.label(
            "Uploaded files are kept unchanged in data/. Columns whose header agrees with a "
            "series name are mapped automatically."
        ).classes("text-sm text-grey")
        ui.upload(label="Upload CSV or Excel", auto_upload=True, on_upload=self.upload).props(
            'accept=".csv,.xlsx,.xlsm" data-testid="upload-data"'
        )
        rows = [
            {
                "i": i,
                "file": s.file,
                "sheet": s.sheet or "",
                "time": s.time_column,
                "mapped": len(s.columns),
            }
            for i, s in enumerate(p.file.sources)
        ]
        _table(
            [
                ("i", "#", "n"),
                ("file", "File", ""),
                ("sheet", "Sheet", ""),
                ("time", "Time column (UTC)", ""),
                ("mapped", "Series mapped", "n"),
            ],
            rows,
            testid="sources",
        )
        if p.file.sources:
            with ui.row().classes("items-end gap-2"):
                which = ui.select(
                    {i: s.file for i, s in enumerate(p.file.sources)},
                    value=len(p.file.sources) - 1,
                    label="File",
                )
                ui.upload(
                    label="Import a saved column mapping (source spec JSON)",
                    auto_upload=True,
                    on_upload=lambda e, w=which: self.import_mapping(e, int(w.value)),
                ).props('accept=".json" data-testid="upload-mapping"')
                ui.button(
                    "Remove file",
                    on_click=lambda w=which: self.act(
                        lambda: self.ws.remove_data_file(int(w.value)), "removed"
                    ),
                ).props("flat")

    async def upload(self, e: events.UploadEventArguments) -> None:
        content = await e.file.read()
        self.act(lambda: self.ws.add_data_file(e.file.name, content), f"{e.file.name} added")

    async def import_mapping(self, e: events.UploadEventArguments, index: int) -> None:
        spec = json.loads((await e.file.read()).decode("utf-8"))
        n: list[int] = []
        self.act(lambda: n.append(self.ws.import_mapping(index, spec)), "")
        if n:
            self.say(f"{n[0]} series mapped from {e.file.name}")

    @ui.refreshable_method
    def mapping(self) -> None:
        if not self.ws.ready:
            ui.label("Set up the project first.")
            return
        try:
            rows = self.ws.mapping()
        except WorkspaceError as exc:
            ui.label(str(exc)).classes("text-negative")
            return
        done = sum(r.mapped for r in rows)
        ui.label(f"{done} of {len(rows)} series mapped").props('data-testid="mapped-count"')
        _table(
            [
                ("series", "Series the plant needs", ""),
                ("file", "File", ""),
                ("header", "Column", ""),
            ],
            [
                {"series": r.series, "file": r.file or "—", "header": r.header or "not mapped"}
                for r in rows
            ],
            testid="mapping",
            page=50,
        )
        p = self.ws.project
        if not p or not p.file.sources:
            return
        with ui.row().classes("items-end gap-2 mt-2"):
            series = ui.select([r.series for r in rows], label="Series").classes("w-72")
            src = ui.select(
                {i: s.file for i, s in enumerate(p.file.sources)}, value=0, label="File"
            )
            header = ui.select(self.ws.headers(0), label="Column").classes("w-96")
            src.on_value_change(lambda e: header.set_options(self.ws.headers(int(e.value))))
            ui.button(
                "Map",
                on_click=lambda: self.act(
                    lambda: self.ws.map_series(series.value, int(src.value), header.value), "mapped"
                ),
            )
            ui.button(
                "Unmap",
                on_click=lambda: self.act(lambda: self.ws.map_series(series.value, None, None), ""),
            ).props("flat")

    @ui.refreshable_method
    def preview(self) -> None:
        if not self.ws.ready:
            ui.label("Set up the project first.")
            return
        try:
            flags = self.ws.flags()
            view = self.ws.preview(self.start, 96)
        except WorkspaceError as exc:
            ui.label(str(exc)).classes("text-negative").props('data-testid="preview-error"')
            return
        errors = [f for f in flags if f.severity == "error"]
        verdict = (
            f"{len(errors)} data quality error(s): correct them (with a reason) before running"
            if errors
            else "No data quality errors"
        )
        ui.label(verdict).classes("text-negative" if errors else "text-positive").props(
            'data-testid="quality"'
        )
        _table(
            [
                ("severity", "Severity", ""),
                ("code", "Flag", ""),
                ("series", "Series", ""),
                ("detail", "Detail", ""),
            ],
            [
                {"severity": f.severity, "code": f.code, "series": f.series or "", "detail": str(f)}
                for f in flags
            ],
            testid="flags",
        )
        with ui.row().classes("items-end gap-2 mt-4"):
            start = ui.number("First interval", value=self.start, min=0, step=96, format="%d")
            ui.button("Show", on_click=lambda: self.goto(int(start.value or 0)))
            ui.button("First flagged interval", on_click=lambda: self.goto(self.ws.first_flagged()))
        ui.label(
            f"Intervals {self.start:,}–{min(self.start + 96, view['intervals']) - 1:,} of "
            f"{view['intervals']:,}, plant time zone; corrected values marked *"
        ).classes("text-sm text-grey")
        names = view["columns"]
        rows = [
            {"local": r["local"]}
            | {
                s: ("—" if r[s] is None else f"{r[s]:,.3f}") + ("*" if r[f"{s}__corrected"] else "")
                for s in names
            }
            for r in view["rows"]
        ]
        ui.table(
            columns=[{"name": "local", "label": "Start (local)", "field": "local", "align": "left"}]
            + [{"name": s, "label": s, "field": s, "align": "right"} for s in names],
            rows=rows,
            row_key="local",
        ).props('dense flat bordered data-testid="preview"').classes("w-full")

    def goto(self, start: int) -> None:
        self.start = max(0, start - start % 96)
        self.preview.refresh()

    @ui.refreshable_method
    def corrections(self) -> None:
        if not self.ws.ready:
            ui.label("Set up the project first.")
            return
        p = self.ws.project
        assert p is not None
        ui.label(
            "A correction sets one series to a value over [from, to), in the plant time zone. "
            "It needs a reason; the original file is never changed, and the sealed run records "
            "the file's hash and every correction."
        ).classes("text-sm text-grey")
        try:
            names = self.ws.required_series()
        except WorkspaceError as exc:
            ui.label(str(exc)).classes("text-negative")
            return
        with ui.row().classes("items-end gap-2"):
            series = (
                ui.select(names, label="Series").classes("w-72").props('data-testid="corr-series"')
            )
            start = ui.input("From", placeholder="2026-10-01 01:00").props(
                'data-testid="corr-from"'
            )
            end = ui.input("To (exclusive)", placeholder="2026-10-01 02:00").props(
                'data-testid="corr-to"'
            )
            value = ui.number("Value per interval").props('data-testid="corr-value"')
            reason = ui.input("Reason").classes("w-96").props('data-testid="corr-reason"')
            ui.button(
                "Add correction",
                on_click=lambda: self.act(
                    lambda: self.ws.add_correction(
                        series.value, start.value, end.value, float(value.value), reason.value
                    ),
                    "correction added",
                ),
            ).props('data-testid="add-correction"')
        rows = [
            {
                "id": c.id,
                "series": c.series,
                "start": c.start.isoformat(),
                "end": c.end.isoformat(),
                "value": c.value,
                "reason": c.reason,
            }
            for c in p.corrections
        ]
        _table(
            [
                ("id", "Correction", ""),
                ("series", "Series", ""),
                ("start", "From (UTC)", ""),
                ("end", "To (UTC)", ""),
                ("value", "Value", "num"),
                ("reason", "Reason", ""),
            ],
            rows,
            testid="corrections",
        )
        if rows:
            with ui.row().classes("items-end gap-2"):
                which = ui.select([r["id"] for r in rows], label="Correction")
                ui.button(
                    "Remove",
                    on_click=lambda: self.act(lambda: self.ws.remove_correction(which.value), ""),
                ).props("flat")

    @ui.refreshable_method
    def consignments(self) -> None:
        if not self.ws.ready:
            ui.label("Set up the project first.")
            return
        p = self.ws.project
        assert p is not None
        try:
            outputs = self.ws.outputs()
        except WorkspaceError as exc:
            ui.label(str(exc)).classes("text-negative")
            return
        with ui.row().classes("items-end gap-2"):
            cid = ui.input("Consignment id").props('data-testid="cons-id"')
            output = ui.select(outputs, label="Consignment output").classes("w-56")
            output.props('data-testid="cons-output"')
            volume = ui.number("Volume (kg)").props('data-testid="cons-volume"')
            ship = ui.input("Ship date", placeholder="2026-12-15").props('data-testid="cons-date"')
            target = ui.number("Target (gCO2e/MJ), blank = minimise").props(
                'data-testid="cons-target"'
            )
            ui.button(
                "Add consignment",
                on_click=lambda: self.act(
                    lambda: self.ws.add_consignment(
                        id=cid.value,
                        output=output.value,
                        volume_kg=volume.value,
                        ship_date=ship.value,
                        target_gCO2e_per_MJ=target.value,
                    ),
                    "consignment added",
                ),
            ).props('data-testid="add-consignment"')
        _table(
            [
                ("id", "Consignment", ""),
                ("output", "Output", ""),
                ("volume_kg", "Volume", "kg"),
                ("ship_date", "Ship date", ""),
                ("target_gCO2e_per_MJ", "Target", "gCO2e/MJ"),
            ],
            [c.model_dump(mode="json") for c in p.consignments],
            testid="consignment-list",
        )
        if p.consignments:
            with ui.row().classes("items-end gap-2"):
                which = ui.select([c.id for c in p.consignments], label="Consignment")
                ui.button(
                    "Remove",
                    on_click=lambda: self.act(lambda: self.ws.remove_consignment(which.value), ""),
                ).props("flat")

    @ui.refreshable_method
    def pos(self) -> None:
        if not self.ws.ready:
            ui.label("Set up the project first.")
            return
        p = self.ws.project
        assert p is not None
        ui.label(
            "A proof of sustainability for RFNBO bought from a supplier: compliant "
            "(≤ 28.2 gCO2e/MJ, distribution included) and dispatched in this quarter."
        ).classes("text-sm text-grey")
        with ui.row().classes("items-end gap-2"):
            pid = ui.input("PoS id").props('data-testid="pos-id"')
            supplier = ui.input("Supplier").props('data-testid="pos-supplier"')
            product = ui.input("Product", value="H2").props('data-testid="pos-product"')
            dispatch = ui.input("Dispatch date", placeholder="2026-10-20").props(
                'data-testid="pos-date"'
            )
            qty = ui.number("Quantity (kg)").props('data-testid="pos-qty"')
            ghg = ui.number("GHG (gCO2e/MJ)").props('data-testid="pos-ghg"')
            ui.button(
                "Add PoS",
                on_click=lambda: self.act(
                    lambda: self.ws.add_pos(
                        id=pid.value,
                        supplier=supplier.value,
                        product=product.value,
                        dispatch_date=dispatch.value,
                        quantity_kg=qty.value,
                        ghg_gCO2e_per_MJ=ghg.value,
                    ),
                    "PoS added",
                ),
            ).props('data-testid="add-pos"')
        _table(
            [
                ("id", "PoS", ""),
                ("supplier", "Supplier", ""),
                ("product", "Product", ""),
                ("dispatch_date", "Dispatched", ""),
                ("quantity_kg", "Quantity", "kg"),
                ("ghg_gCO2e_per_MJ", "GHG", "gCO2e/MJ"),
            ],
            [x.model_dump(mode="json") for x in p.pos],
            testid="pos-list",
        )
        if p.pos:
            with ui.row().classes("items-end gap-2"):
                which = ui.select([x.id for x in p.pos], label="PoS")
                ui.button(
                    "Remove", on_click=lambda: self.act(lambda: self.ws.remove_pos(which.value), "")
                ).props("flat")

    @ui.refreshable_method
    def opening(self) -> None:
        if not self.ws.ready:
            ui.label("Set up the project first.")
            return
        p = self.ws.project
        assert p is not None
        ui.label("Open from the previous quarter's sealed run").classes("text-base font-medium")
        with ui.row().classes("items-end gap-2"):
            prior = ui.input("Sealed run folder", value=p.file.opening_from or "").classes("w-96")
            ui.button(
                "Use",
                on_click=lambda: self.act(
                    lambda: self.ws.set_opening_from(prior.value.strip() or None), "saved"
                ),
            )
        ui.label("Or enter a storage's opening position by hand").classes(
            "text-base font-medium mt-4"
        )
        try:
            stores = self.ws.storages()
        except WorkspaceError as exc:
            ui.label(str(exc)).classes("text-negative")
            return
        with ui.row().classes("items-end gap-2"):
            store = ui.select(stores, label="Storage").classes("w-56")
            inv = ui.number("Opening physical inventory (kg)")
            bid = ui.input("Ledger batch id (optional)")
            kg = ui.number("RFNBO kg")
            ghg = ui.number("GHG (gCO2e/MJ)")
            month = ui.input("Production month", placeholder="2026-09")

            def save() -> None:
                old = p.opening.get(store.value)
                rows = [e.model_dump() for e in old.opening_ledger] if old else []
                if bid.value:
                    rows.append(
                        {
                            "batch_id": bid.value,
                            "rfnbo_kg": kg.value,
                            "ghg_gCO2e_per_MJ": ghg.value,
                            "production_month": month.value,
                        }
                    )
                self.ws.set_opening(store.value, float(inv.value or 0), rows)

            ui.button("Save storage", on_click=lambda: self.act(save, "saved"))
        rows: list[dict[str, Any]] = []
        for s, o in sorted(p.opening.items()):
            entries = o.opening_ledger or []
            rows += [
                {
                    "storage": s,
                    "inventory": o.opening_inventory_kg,
                    "batch": e.batch_id,
                    "kg": e.rfnbo_kg,
                    "ghg": e.ghg_gCO2e_per_MJ,
                    "month": e.production_month,
                }
                for e in entries
            ] or [{"storage": s, "inventory": o.opening_inventory_kg, "batch": "(none)"}]
        _table(
            [
                ("storage", "Storage", ""),
                ("inventory", "Opening inventory", "kg"),
                ("batch", "Batch", ""),
                ("kg", "RFNBO", "kg"),
                ("ghg", "GHG", "gCO2e/MJ"),
                ("month", "Month", ""),
            ],
            rows,
            testid="opening",
        )

    @ui.refreshable_method
    def run_tab(self) -> None:
        if not self.ws.ready:
            ui.label("Set up the project first.")
            return
        ui.label(
            "Optimise the quarter and seal it with its baseline under runs/. The same folder runs "
            "from the command line: rfnbo optimise --project <folder>."
        ).classes("text-sm text-grey")
        with ui.row().classes("items-end gap-2"):
            label = ui.input("Run label (optional)").props('data-testid="run-label"')
            button = ui.button("Run").props('data-testid="run"')
        status = ui.label("").props('data-testid="run-status"')
        link = ui.column()

        async def go() -> None:
            button.disable()
            status.text = "Optimising… (this takes a minute or two)"
            try:
                out = await background.io_bound(self.ws.run, label.value.strip() or None)
            except (WorkspaceError, ValueError) as exc:
                status.text = f"Not run: {exc}"
                status.classes(replace="text-negative")
                button.enable()
                return
            assert out is not None
            o = out.summary["objectives"]["optimised"]
            status.text = (
                f"Sealed to {out.out}: consignments met {o['consignments_met']}, carried value "
                f"{o['carried_value_MJ']:,.0f} MJ, {o['failed_checks']} failed checks"
            )
            status.classes(replace="text-positive")
            with link:
                ui.link(
                    "Open the sealed run in the results browser →",
                    f"/run/{out.run_ids['optimised']}",
                ).props('data-testid="open-run"')
            button.enable()

        button.on_click(go)


def run(root: Path, port: int = 8082, show: bool = True) -> None:
    results._ROOT = root / "runs"

    @ui.page("/data")
    def data_page() -> None:
        DataPage(root).build()

    ui.run(port=port, show=show, reload=False, title="RFNBO data", show_welcome_message=False)
