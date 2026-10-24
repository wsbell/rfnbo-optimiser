"""The Data screen page (NiceGUI): set up a quarter, map and check the data, correct it with
reasons, enter consignments, PoS records and the opening ledger, and run (build plan §11.2).

All state and rules live in :class:`rfnbo.ui.data.workspace.Workspace`; this module lays it
out. The results browser's pages are served alongside, on the project's ``runs/`` folder, so a
sealed run opens there.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from nicegui import events, ui
from nicegui import run as background

from rfnbo.model.entities import ConsignmentSpec, ProofOfSustainability
from rfnbo.project.corrections import Correction
from rfnbo.ui.bfd_editor import page as editor
from rfnbo.ui.data.workspace import PlantSummary, Workspace, WorkspaceError
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

# A form's fields, built in the current layout context; returns a reader of their values.
Fields = Callable[[], Callable[[], None]]


def _slug(tab: str) -> str:
    return tab.split()[0].lower()


def _row_actions(
    table: ui.table,
    testid: str,
    remove: Callable[[str], None],
    edit: Callable[[str], None] | None = None,
) -> None:
    """Edit and delete buttons at the end of each row; they pass the row's key."""
    table.columns = [*table.columns, {"name": "actions", "label": "", "field": "actions"}]
    buttons = [("edit", "edit", "row_edit", "")] if edit else []
    buttons.append(("remove", "delete", "row_remove", ' color="negative"'))
    table.add_slot(
        "body-cell-actions",
        '<q-td :props="props" class="text-right">'
        + "".join(
            f'<q-btn flat dense round size="sm" icon="{icon}"{extra} '
            f":data-testid=\"'{testid}-{name}-' + props.key\" "
            f"@click=\"() => $parent.$emit('{event}', props.key)\" />"
            for name, icon, event, extra in buttons
        )
        + "</q-td>",
    )
    table.on("row_remove", lambda e: remove(str(e.args)))
    if edit:
        table.on("row_edit", lambda e: edit(str(e.args)))


class DataPage:
    """One browser tab's Data screen."""

    def __init__(self, root: Path) -> None:
        self.ws = Workspace(root)
        self.message: ui.label | None = None
        self.badges: dict[str, ui.badge] = {}
        self.dialogs: ui.element | None = None
        self.chosen: str | None = None  # plant config chosen on the Setup tab
        self.quarter: str | None = None
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
        if text:
            # Also as a toast: the message line sits above the tabs and is easy to miss, and
            # is hidden behind an open dialog.
            ui.notify(text, type="negative" if error else "positive", position="top")

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
        self.show_status()

    def show_status(self) -> None:
        """Badge each tab with where it stands (series mapped, errors, records entered)."""
        steps = {s.tab: s for s in self.ws.status()}
        blocking = self.ws.blocking(steps.values())
        for tab, badge in self.badges.items():
            if tab == "Run":
                text, color = ("ready", "positive") if not blocking else ("✗", "negative")
            elif (s := steps.get(tab.split()[0])) is None:
                text, color = "", "grey-7"
            else:
                text = s.badge or ("" if s.ok else "!")
                color = "grey-7" if not s.required else ("positive" if s.ok else "negative")
            badge.set_text(text)
            badge.props(f"color={color}")
            badge.set_visibility(bool(text))

    def confirm(
        self,
        question: str,
        fn: Callable[[], Any],
        ok: str,
        action: str = "Remove",
        cancelled: Callable[[], None] | None = None,
    ) -> None:
        """Ask before removing or replacing something; it cannot be undone."""
        assert self.dialogs is not None
        self.dialogs.clear()
        with self.dialogs, ui.dialog() as dialog, ui.card():
            ui.label(question)
            with ui.row():

                def yes() -> None:
                    dialog.close()
                    self.act(fn, ok)

                ui.button(action, on_click=yes).props(
                    f'color=negative data-testid="confirm-{action.lower()}"'
                )

                def no() -> None:
                    dialog.close()
                    if cancelled:
                        cancelled()

                ui.button("Cancel", on_click=no).props("flat")
        dialog.open()

    def edit(self, title: str, fields: Fields) -> None:
        """Edit a record in a dialog with the same fields as the form that added it."""
        assert self.dialogs is not None
        self.dialogs.clear()
        with self.dialogs, ui.dialog() as dialog, ui.card().classes("min-w-[48rem]"):
            ui.label(title).classes("text-base font-medium")
            with ui.row().classes("items-end gap-2"):
                apply = fields()
            with ui.row():
                # On a refusal the dialog stays open with the entries, and the toast says why.
                ui.button(
                    "Save", on_click=lambda: self.act(apply, "saved") and dialog.close()
                ).props('data-testid="edit-save"')
                ui.button("Cancel", on_click=dialog.close).props("flat")
        dialog.open()

    # ---- layout ----------------------------------------------------------------------------

    def build(self) -> None:
        with ui.row().classes("items-center gap-4"):
            ui.label(f"Data · {self.ws.root}").classes("text-lg font-medium")
            ui.link("Sealed runs →", "/").props('data-testid="runs-link"')
        self.message = ui.label("").props('data-testid="message"')
        self.dialogs = ui.element("div")
        with ui.tabs() as tabs:
            for name in TABS:
                with ui.tab(name).props(f'data-testid="tab-{_slug(name)}"'):
                    self.badges[name] = ui.badge("").props(
                        f'floating data-testid="badge-{_slug(name)}"'
                    )
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
        self.show_status()

    @ui.refreshable_method
    def setup(self) -> None:
        p = self.ws.project
        options = self.ws.plant_configs()
        if self.chosen is None:
            # A folder holding one plant config needs no choice, only the quarter.
            self.chosen = p.file.config if p else (options[0] if len(options) == 1 else None)
        ui.label("Plant config").classes("text-base font-medium")
        with ui.row().classes("items-center gap-2"):
            ui.select(
                options,
                label="Plant config in this folder",
                value=self.chosen if self.chosen in options else None,
                on_change=lambda e: self.choose_config(e.value),
            ).classes("w-96").props('data-testid="config-select"')
            picker = (
                ui.upload(auto_upload=True, on_upload=self.upload_config)
                .props('accept=".json" data-testid="upload-config"')
                .classes("hidden")
            )
            ui.button("Upload…", on_click=lambda: picker.run_method("pickFiles")).props(
                'flat icon=upload data-testid="upload-config-button"'
            ).tooltip("A plant config JSON file; it is copied into this folder")
            ui.button("New plant diagram", on_click=self.new_plant).props(
                'flat data-testid="new-plant"'
            )
        with (
            ui.expansion("Or use a plant config elsewhere").props(
                'dense data-testid="config-elsewhere"'
            ),
            ui.row().classes("items-end gap-2"),
        ):
            path = (
                ui.input("Path, absolute or relative to the project folder")
                .classes("w-96")
                .props('data-testid="config"')
            )
            ui.button("Use", on_click=lambda: self.choose_config(path.value.strip())).props(
                'data-testid="use-config"'
            )
        if not self.chosen:
            ui.label(
                "Choose the plant config, upload one (it is copied into this folder), or draw "
                "a new plant diagram."
            ).classes("text-sm text-grey")
            return
        summary = self.ws.plant_summary(self.chosen)
        self.plant_card(summary)
        if summary.plant is None:
            return
        quarters, suggested = self.ws.quarters(summary.plant, dt.date.today())
        if p and p.file.quarter not in quarters:
            quarters.insert(0, p.file.quarter)
        if self.quarter is None:
            self.quarter = p.file.quarter if p else suggested
        with ui.row().classes("items-center gap-2 mt-2"):
            ui.select(
                quarters,
                label="Quarter",
                value=self.quarter if self.quarter in quarters else None,
                on_change=lambda e: self.choose_quarter(e.value),
            ).classes("w-40").props('data-testid="quarter"')
            if not p:
                ui.button("Set up project", on_click=self.apply_setup).props(
                    'data-testid="apply-setup"'
                )
        if not p:
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
        table = _table(
            [
                ("i", "#", "n"),
                ("file", "File", ""),
                ("sheet", "Sheet", ""),
                ("time", "Time column (UTC)", ""),
                ("mapped", "Series mapped", "n"),
            ],
            rows,
            testid="sources",
            row_key="i",
        )
        files = {str(i): s.file for i, s in enumerate(p.file.sources)}
        _row_actions(
            table,
            "sources",
            remove=lambda k: self.confirm(
                f"Remove {files[k]} from the project? The file stays in data/; its column "
                "mapping is lost.",
                lambda: self.ws.remove_data_file(int(k)),
                f"{files[k]} removed",
            ),
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

    def plant_card(self, s: PlantSummary) -> None:
        with ui.card().classes("p-3 mt-2 w-full max-w-4xl").props('data-testid="plant-card"'):
            with ui.row().classes("items-center gap-2 w-full no-wrap"):
                if s.plant is not None:
                    c = s.plant
                    ui.icon("check_circle", color="positive")
                    ui.label(
                        f"{c.plant} · version {c.version} · effective {c.effective_from} · "
                        f"{c.timezone} · {len(c.blocks)} blocks, {len(c.flows)} flows"
                    ).props('data-testid="plant-summary"')
                else:
                    ui.icon("cancel", color="negative")
                    ui.label(f"{s.config} cannot be used yet: {len(s.issues)} issue(s)").props(
                        'data-testid="plant-summary"'
                    )
                ui.space()
                ui.button(
                    "Edit diagram",
                    on_click=lambda: ui.navigate.to(f"/plant?file={quote(s.config)}"),
                ).props('flat dense icon=edit data-testid="edit-plant"').tooltip(str(s.path))
            for issue in s.issues[:10]:
                ui.label(issue).classes("text-xs text-negative")
            if len(s.issues) > 10:
                ui.label(f"… and {len(s.issues) - 10} more: open the diagram to see them all")

    def choose_config(self, config: str | None) -> None:
        """A config chosen, uploaded or typed: used at once if it is valid."""
        self.chosen = config or None
        p = self.ws.project
        if not self.chosen or not self.ws.plant_summary(self.chosen).ok:
            self.setup.refresh()
            return
        if p and p.file.config != self.chosen:
            self.confirm(
                f"Switch this project's plant config from {p.file.config} to {self.chosen}?",
                lambda: self.ws.setup(str(self.chosen), p.file.quarter),
                f"plant config {self.chosen}",
                action="Switch",
                cancelled=self.reset_setup,
            )
        elif p or self.quarter:
            self.apply_setup()
        else:
            self.setup.refresh()

    def choose_quarter(self, quarter: str | None) -> None:
        self.quarter = quarter
        if quarter and self.chosen and self.ws.plant_summary(self.chosen).ok:
            self.apply_setup()

    def apply_setup(self) -> None:
        config, quarter = self.chosen, self.quarter
        if not config or not quarter:
            self.say("choose the plant config and the quarter", error=True)
            return
        self.act(
            lambda: self.ws.setup(config, quarter), f"plant config {config}, quarter {quarter}"
        )

    def reset_setup(self) -> None:
        p = self.ws.project
        self.chosen = p.file.config if p else None
        self.setup.refresh()

    async def upload_config(self, e: events.UploadEventArguments) -> None:
        content = await e.file.read()
        names: list[str] = []
        if self.act(lambda: names.append(self.ws.add_plant_config(e.file.name, content))):
            self.choose_config(names[0])

    def new_plant(self) -> None:
        name = self.ws.new_plant_name()
        self.ws.root.mkdir(parents=True, exist_ok=True)
        ui.navigate.to(f"/plant?file={quote(name)}")

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
                on_click=lambda: self.act(
                    lambda: self.ws.map_series(series.value, None, None), "unmapped"
                ),
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
            apply = self.correction_fields(names, None, "corr")()
            ui.button("Add correction", on_click=lambda: self.act(apply, "correction added")).props(
                'data-testid="add-correction"'
            )
        rows = [
            {
                "id": c.id,
                "series": c.series,
                "start": self.ws.local(c.start),
                "end": self.ws.local(c.end),
                "value": c.value,
                "reason": c.reason,
            }
            for c in p.corrections
        ]
        table = _table(
            [
                ("id", "Correction", ""),
                ("series", "Series", ""),
                ("start", "From (local)", ""),
                ("end", "To (local)", ""),
                ("value", "Value", "num"),
                ("reason", "Reason", ""),
            ],
            rows,
            testid="corrections",
        )
        by_id = {c.id: c for c in p.corrections}
        _row_actions(
            table,
            "corrections",
            remove=lambda k: self.confirm(
                f"Remove correction {k}? Its reason is lost.",
                lambda: self.ws.remove_correction(k),
                f"correction {k} removed",
            ),
            edit=lambda k: self.edit(
                f"Correction {k}", self.correction_fields(names, by_id[k], "edit-corr")
            ),
        )

    def correction_fields(self, names: list[str], c: Correction | None, prefix: str) -> Fields:
        def build() -> Callable[[], None]:
            series = (
                ui.select(names, label="Series", value=c.series if c else None)
                .classes("w-72")
                .props(f'data-testid="{prefix}-series"')
            )
            start = ui.input(
                "From",
                value=self.ws.local(c.start) if c else "",
                placeholder="2026-10-01 01:00",
            ).props(f'data-testid="{prefix}-from"')
            end = ui.input(
                "To (exclusive)",
                value=self.ws.local(c.end) if c else "",
                placeholder="2026-10-01 02:00",
            ).props(f'data-testid="{prefix}-to"')
            value = ui.number("Value per interval", value=c.value if c else None).props(
                f'data-testid="{prefix}-value"'
            )
            reason = (
                ui.input("Reason", value=c.reason if c else "")
                .classes("w-96")
                .props(f'data-testid="{prefix}-reason"')
            )

            def apply() -> None:
                if value.value is None:
                    raise WorkspaceError("enter the value per interval")
                self.ws.add_correction(
                    series.value,
                    start.value,
                    end.value,
                    float(value.value),
                    reason.value,
                    replace=c.id if c else None,
                )

            return apply

        return build

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
            apply = self.consignment_fields(outputs, None, "cons")()
            ui.button(
                "Add consignment", on_click=lambda: self.act(apply, "consignment added")
            ).props('data-testid="add-consignment"')
        table = _table(
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
        by_id = {c.id: c for c in p.consignments}
        _row_actions(
            table,
            "consignment-list",
            remove=lambda k: self.confirm(
                f"Remove consignment {k}?",
                lambda: self.ws.remove_consignment(k),
                f"consignment {k} removed",
            ),
            edit=lambda k: self.edit(
                f"Consignment {k}", self.consignment_fields(outputs, by_id[k], "edit-cons")
            ),
        )

    def consignment_fields(
        self, outputs: list[str], c: ConsignmentSpec | None, prefix: str
    ) -> Fields:
        def build() -> Callable[[], None]:
            cid = ui.input("Consignment id", value=c.id if c else "").props(
                f'data-testid="{prefix}-id"'
            )
            output = ui.select(outputs, label="Consignment output", value=c.output if c else None)
            output.classes("w-56").props(f'data-testid="{prefix}-output"')
            volume = ui.number("Volume (kg)", value=c.volume_kg if c else None).props(
                f'data-testid="{prefix}-volume"'
            )
            ship = ui.input(
                "Ship date", value=c.ship_date.isoformat() if c else "", placeholder="2026-12-15"
            ).props(f'data-testid="{prefix}-date"')
            target = ui.number(
                "Target (gCO2e/MJ), blank = minimise", value=c.target_gCO2e_per_MJ if c else None
            ).props(f'data-testid="{prefix}-target"')
            return lambda: self.ws.add_consignment(
                replace=c.id if c else None,
                id=cid.value,
                output=output.value,
                volume_kg=volume.value,
                ship_date=ship.value,
                target_gCO2e_per_MJ=target.value,
            )

        return build

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
            apply = self.pos_fields(None, "pos")()
            ui.button("Add PoS", on_click=lambda: self.act(apply, "PoS added")).props(
                'data-testid="add-pos"'
            )
        table = _table(
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
        by_id = {x.id: x for x in p.pos}
        _row_actions(
            table,
            "pos-list",
            remove=lambda k: self.confirm(
                f"Remove PoS {k}?", lambda: self.ws.remove_pos(k), f"PoS {k} removed"
            ),
            edit=lambda k: self.edit(f"PoS {k}", self.pos_fields(by_id[k], "edit-pos")),
        )

    def pos_fields(self, x: ProofOfSustainability | None, prefix: str) -> Fields:
        def build() -> Callable[[], None]:
            pid = ui.input("PoS id", value=x.id if x else "").props(f'data-testid="{prefix}-id"')
            supplier = ui.input("Supplier", value=x.supplier if x else "").props(
                f'data-testid="{prefix}-supplier"'
            )
            product = ui.input("Product", value=x.product if x else "H2").props(
                f'data-testid="{prefix}-product"'
            )
            dispatch = ui.input(
                "Dispatch date",
                value=x.dispatch_date.isoformat() if x else "",
                placeholder="2026-10-20",
            ).props(f'data-testid="{prefix}-date"')
            qty = ui.number("Quantity (kg)", value=x.quantity_kg if x else None).props(
                f'data-testid="{prefix}-qty"'
            )
            ghg = ui.number("GHG (gCO2e/MJ)", value=x.ghg_gCO2e_per_MJ if x else None).props(
                f'data-testid="{prefix}-ghg"'
            )

            def apply() -> None:
                self.ws.add_pos(
                    replace=x.id if x else None,
                    id=pid.value,
                    supplier=supplier.value,
                    product=product.value,
                    dispatch_date=dispatch.value,
                    quantity_kg=qty.value,
                    ghg_gCO2e_per_MJ=ghg.value,
                )

            return apply

        return build

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

            def use() -> None:
                run_dir = prior.value.strip() or None
                fn = lambda: self.ws.set_opening_from(run_dir)  # noqa: E731
                if run_dir and p.opening:
                    self.confirm(
                        f"Opening from {run_dir} replaces the {len(p.opening)} storage(s) "
                        "entered by hand. Continue?",
                        fn,
                        "saved",
                    )
                else:
                    self.act(fn, "saved")

            ui.button("Use", on_click=use).props('data-testid="use-opening-from"')
        ui.label("Or enter a storage's opening position by hand").classes(
            "text-base font-medium mt-4"
        )
        ui.label(
            "Saving sets the storage's opening inventory and, if a batch id is given, adds that "
            "ledger entry to the storage's opening ledger."
        ).classes("text-sm text-grey")
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
            rows += [
                {
                    "key": json.dumps([s, e.batch_id]),
                    "storage": s,
                    "inventory": o.opening_inventory_kg,
                    "batch": e.batch_id,
                    "kg": e.rfnbo_kg,
                    "ghg": e.ghg_gCO2e_per_MJ,
                    "month": e.production_month,
                }
                for e in o.opening_ledger
            ] or [
                {
                    "key": json.dumps([s, None]),
                    "storage": s,
                    "inventory": o.opening_inventory_kg,
                    "batch": "(none)",
                }
            ]
        table = _table(
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
            row_key="key",
        )

        def remove(key: str) -> None:
            storage, batch = json.loads(key)
            if batch is None:
                self.confirm(
                    f"Remove the opening position of {storage}?",
                    lambda: self.ws.clear_opening(storage),
                    f"{storage} removed",
                )
            else:
                self.confirm(
                    f"Remove opening entry {batch} from {storage}?",
                    lambda: self.ws.remove_opening_entry(storage, batch),
                    f"{batch} removed",
                )

        _row_actions(table, "opening", remove=remove)

    @ui.refreshable_method
    def run_tab(self) -> None:
        if not self.ws.ready:
            ui.label("Set up the project first.")
            return
        ui.label(
            "Optimise the quarter and seal it with its baseline under runs/. The same folder runs "
            "from the command line: rfnbo optimise --project <folder>."
        ).classes("text-sm text-grey")
        steps = self.ws.status()
        blocking = self.ws.blocking(steps)
        with ui.column().classes("gap-0 my-2").props('data-testid="checklist"'):
            for s in steps:
                icon, color = (
                    ("check_circle", "positive")
                    if s.ok
                    else ("cancel", "negative")
                    if s.required
                    else ("info", "grey")
                )
                with ui.row().classes("items-center gap-2 no-wrap"):
                    ui.icon(icon, color=color)
                    ui.label(f"{s.tab}: {s.detail}").props(f'data-testid="check-{_slug(s.tab)}"')
        with ui.row().classes("items-end gap-2"):
            label = ui.input("Run label (optional)").props('data-testid="run-label"')
            button = ui.button("Run").props('data-testid="run"')
        if blocking:
            button.disable()
            ui.label(
                "Not ready to run: " + "; ".join(s.tab for s in blocking) + " (marked ✗ above)"
            ).classes("text-negative text-sm").props('data-testid="run-blocked"')
        status = ui.label("").props('data-testid="run-status"')
        link = ui.column()

        async def go() -> None:
            button.disable()
            status.text = "Optimising… (this takes a minute or two)"
            status.classes(replace="")
            link.clear()
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
            met = o["consignments_met"] and not o["failed_checks"]
            status.classes(replace="text-positive" if met else "text-negative")
            with link:
                for s in out.summary["shortfalls"]:
                    ui.label(
                        f"{s['consignment']} not met: {s['reason']}; achievable "
                        f"{s['achievable_kg']:,.1f} kg"
                    ).classes("text-negative").props('data-testid="shortfall"')
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

    # The diagram editor, on a plant config of the project (opened from the Setup tab).
    editor.add_static()

    @ui.page("/plant")
    async def plant_page(file: str = "") -> None:
        if not file:
            ui.navigate.to("/data")
            return
        path = Path(file) if Path(file).is_absolute() else root / file
        await editor.open_editor(path, back="/data")

    # Open the Data screen itself; "/" is the results browser for the folder's runs.
    ui.run(
        port=port,
        show="/data" if show else False,
        reload=False,
        title="RFNBO data",
        show_welcome_message=False,
    )
