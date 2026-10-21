"""The block flow diagram editor page (NiceGUI + vendored Drawflow).

All state lives in :class:`EditorDocument`; this module renders it and forwards user actions.
See docs/adr/0001-diagram-editor.md for why NiceGUI + Drawflow.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from nicegui import app, events, ui

from rfnbo.ui.bfd_editor.document import KIND_LABELS, KINDS, EditorDocument, EditorError
from rfnbo.ui.bfd_editor.fields import (
    BLOCK_FIELDS,
    FLOW_FIELDS,
    NULLABLE_NUMBERS,
    PLANT_FIELDS,
    SETTINGS_FIELDS,
    FieldSpec,
    TableSpec,
)

STATIC = Path(__file__).parent / "static"
PORTS = {"input": (0, 1), "rfnbo_supply": (0, 1), "consignment_output": (1, 0)}
CSS = """
#bfd-canvas { height: calc(100vh - 140px); width: 100%; border: 1px solid #ccc;
              position: relative; background: #fafafa; }
#bfd-canvas .drawflow-node { width: 170px; min-height: 56px; padding: 6px 10px;
                             border: 1px solid #555; border-radius: 6px; }
#bfd-canvas .drawflow-node.selected { border: 2px solid #1565c0; background: #e3f2fd; }
#bfd-canvas .drawflow-node.bfd-bad { border: 2px solid #c62828; }
#bfd-canvas .bfd-input { background: #fff8e1; }
#bfd-canvas .bfd-rfnbo_supply { background: #fffde7; border-style: double; }
#bfd-canvas .bfd-processing_unit { background: #e8f5e9; }
#bfd-canvas .bfd-storage_unit { background: #e3f2fd; }
#bfd-canvas .bfd-distribution { background: #f3e5f5; }
#bfd-canvas .bfd-consignment_output { background: #fce4ec; }
#bfd-canvas .drawflow-node.bfd-pass_through { border-style: dashed; min-height: 40px; }
#bfd-canvas .connection .main-path { stroke-width: 3px; }
#bfd-canvas .connection.bfd-dashed .main-path { stroke-dasharray: 8 5; }
.bfd-title { font-weight: 600; font-size: 13px; }
.bfd-sub { font-size: 11px; color: #555; }
"""


def node_html(block: dict[str, Any]) -> str:
    name = html.escape(str(block.get("name", "")))
    label = KIND_LABELS.get(block.get("kind", ""), "")
    if block.get("kind") == "storage_unit" and block.get("pass_through"):
        label = "Pass-through"
    bid = html.escape(str(block.get("id", "")))
    return f'<div class="bfd-title">{name}</div><div class="bfd-sub">{label} · {bid}</div>'


class Editor:
    """One browser tab's editor."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.doc = EditorDocument.load(path) if path.exists() else EditorDocument.new()
        self.node_of: dict[str, int] = {}
        self.selected: tuple[str, str] | None = None  # ("block" | "flow", id)

    # ---- layout ----------------------------------------------------------------------

    def build(self) -> None:
        ui.add_head_html(
            '<link rel="stylesheet" href="/bfd-static/drawflow.min.css">'
            '<script src="/bfd-static/drawflow.min.js"></script>'
            '<script src="/bfd-static/bfd.js"></script>'
            f"<style>{CSS}</style>"
        )
        with ui.row().classes("w-full items-center gap-2"):
            self.title = ui.label().classes("text-lg font-medium")
            self.saved_state = ui.label().props('data-testid="saved-state"')
            ui.space()
            for kind in KINDS:
                ui.button(f"+ {KIND_LABELS[kind]}", on_click=lambda k=kind: self.add(k)).props(
                    f'dense outline data-testid="add-{kind}"'
                )
            ui.button("Plant settings", on_click=self.show_plant).props(
                'dense flat data-testid="plant-settings"'
            )
            ui.button("Save", on_click=self.save).props('data-testid="save"').tooltip("Ctrl+S")
            ui.button("Download", on_click=self.download).props('flat data-testid="download"')
            ui.upload(label="Open JSON", auto_upload=True, on_upload=self.upload).props(
                'accept=.json flat dense data-testid="upload"'
            ).classes("w-40")
        with ui.row().classes("w-full no-wrap"):
            with ui.element("div").classes("grow relative"):
                ui.element("div").props('id="bfd-canvas"')
                with ui.row().classes("absolute gap-1").style("top: 8px; right: 8px; z-index: 5"):
                    for label, call in (("+", "zoom_in"), ("−", "zoom_out"), ("1:1", "zoom_reset")):
                        ui.button(
                            label, on_click=lambda c=call: ui.run_javascript(f"bfd.editor.{c}()")
                        ).props(f'dense outline size=sm data-testid="zoom-{call}"')
            with (
                ui.column()
                .classes("w-96 gap-1")
                .style("height: calc(100vh - 140px); overflow-y: auto")
            ):
                self.props_box = ui.column().classes("w-full")
                ui.separator()
                self.summary = ui.label().props('data-testid="issue-summary"')
                self.issues_box = ui.column().classes("w-full gap-0")
        for event, handler in (
            ("bfd_connection_created", self.on_connection_created),
            ("bfd_connection_removed", self.on_connection_removed),
            ("bfd_connection_selected", self.on_connection_selected),
            ("bfd_node_removed", self.on_node_removed),
            ("bfd_node_selected", self.on_node_selected),
            ("bfd_unselected", lambda _: self.select(None)),
            ("bfd_node_moved", self.on_node_moved),
            ("bfd_save", lambda _: self.save()),
        ):
            ui.on(event, handler)

    async def load_canvas(self) -> None:
        nodes = []
        for b in self.doc.blocks:
            x, y = self.doc.position(b["id"])
            n_in, n_out = PORTS.get(b["kind"], (1, 1))
            nodes.append(
                {
                    "block": b["id"],
                    "kind": b["kind"],
                    "inputs": n_in,
                    "outputs": n_out,
                    "x": x,
                    "y": y,
                    "html": node_html(b),
                }
            )
        edges = [{"source": f["source"], "target": f["target"]} for f in self.doc.flows]
        ids = await ui.run_javascript(
            f"return bfd.load(document.getElementById('bfd-canvas'), "
            f"{json.dumps(nodes)}, {json.dumps(edges)})",
            timeout=10,
        )
        self.node_of = {k: int(v) for k, v in ids.items()}
        self.select(None)
        self.refresh()

    # ---- canvas events -----------------------------------------------------------------

    def block_of(self, node_id: Any) -> str | None:
        for block_id, n in self.node_of.items():
            if n == int(node_id):
                return block_id
        return None

    async def add(self, kind: str) -> None:
        block_id = self.doc.add_block(kind)
        b = self.doc.block(block_id)
        x, y = self.doc.position(block_id)
        n_in, n_out = PORTS.get(kind, (1, 1))
        node = {
            "block": block_id,
            "kind": kind,
            "inputs": n_in,
            "outputs": n_out,
            "x": x,
            "y": y,
            "html": node_html(b),
        }
        self.node_of[block_id] = int(
            await ui.run_javascript(f"return bfd.addNode({json.dumps(node)})")
        )
        self.select(("block", block_id))
        self.refresh()

    async def on_connection_created(self, e: events.GenericEventArguments) -> None:
        src, tgt = self.block_of(e.args["output_id"]), self.block_of(e.args["input_id"])
        if src is None or tgt is None:
            return
        kinds = (self.doc.block(src)["kind"], self.doc.block(tgt)["kind"])
        if kinds == ("processing_unit", "processing_unit"):
            await self.link_units(src, tgt, int(e.args["output_id"]), int(e.args["input_id"]))
            return
        try:
            flow_id = self.doc.connect(src, tgt)
        except EditorError as exc:
            ui.notify(str(exc), type="negative")
            ui.run_javascript(
                f"bfd.removeConnection({int(e.args['output_id'])}, {int(e.args['input_id'])})"
            )
            return
        self.select(("flow", flow_id))
        self.refresh()

    async def link_units(self, src: str, tgt: str, src_node: int, tgt_node: int) -> None:
        """A unit dragged onto a unit: replace the drawn link with a pass-through storage."""
        ui.run_javascript(f"bfd.removeConnection({src_node}, {tgt_node})")
        try:
            pt = self.doc.link_units(src, tgt)
        except EditorError as exc:
            ui.notify(str(exc), type="negative")
            return
        b = self.doc.block(pt)
        x, y = self.doc.position(pt)
        node = {
            "block": pt,
            "kind": "storage_unit",
            "inputs": 1,
            "outputs": 1,
            "x": x,
            "y": y,
            "html": node_html(b),
        }
        n = int(await ui.run_javascript(f"return bfd.addNode({json.dumps(node)})"))
        self.node_of[pt] = n
        ui.run_javascript(f"bfd.addConnection({src_node}, {n}); bfd.addConnection({n}, {tgt_node})")
        ui.notify(f"Linked {src} → {tgt} through pass-through storage {pt}")
        self.select(("block", pt))
        self.refresh()

    def on_connection_removed(self, e: events.GenericEventArguments) -> None:
        src, tgt = self.block_of(e.args["output_id"]), self.block_of(e.args["input_id"])
        for f in list(self.doc.flows):
            if f.get("source") == src and f.get("target") == tgt:
                self.doc.disconnect(f["id"])
                if self.selected == ("flow", f["id"]):
                    self.select(None)
        self.refresh()

    def on_connection_selected(self, e: events.GenericEventArguments) -> None:
        src, tgt = self.block_of(e.args["output_id"]), self.block_of(e.args["input_id"])
        flow = next(
            (f for f in self.doc.flows if f.get("source") == src and f.get("target") == tgt), None
        )
        if flow is not None:
            self.select(("flow", flow["id"]))

    def on_node_removed(self, e: events.GenericEventArguments) -> None:
        block_id = self.block_of(e.args)
        if block_id is None:
            return
        self.doc.remove_block(block_id)
        del self.node_of[block_id]
        self.select(None)
        self.refresh()

    def on_node_selected(self, e: events.GenericEventArguments) -> None:
        block_id = self.block_of(e.args)
        if block_id is not None:
            self.select(("block", block_id))

    def on_node_moved(self, e: events.GenericEventArguments) -> None:
        block_id = self.block_of(e.args["id"])
        if block_id is not None:
            self.doc.move(block_id, e.args["x"], e.args["y"])
            self.show_saved_state()

    # ---- files ---------------------------------------------------------------------------

    def save(self) -> None:
        issues = self.doc.save(self.path)
        self.show_saved_state()
        ui.notify(
            f"Saved {self.path.name}: " + (f"{len(issues)} issue(s) to fix" if issues else "valid"),
            type="warning" if issues else "positive",
        )

    def show_saved_state(self) -> None:
        dirty = self.doc.dirty
        self.saved_state.text = "● unsaved changes" if dirty else "saved"
        self.saved_state.classes(replace="text-warning" if dirty else "text-grey")
        ui.run_javascript(f"bfd.dirty = {json.dumps(dirty)}")

    def download(self) -> None:
        ui.download.content(self.doc.to_json(), self.path.name, "application/json")

    async def upload(self, e: events.UploadEventArguments) -> None:
        try:
            self.doc = EditorDocument.from_json(await e.file.text())
        except (EditorError, json.JSONDecodeError) as exc:
            ui.notify(f"Not a plant config: {exc}", type="negative")
            return
        await self.load_canvas()
        ui.notify(f"Opened {e.file.name}; Save writes to {self.path.name}")

    # ---- properties panel ----------------------------------------------------------------

    def show_plant(self) -> None:
        self.select(None)

    def select(self, what: tuple[str, str] | None) -> None:
        self.selected = what
        self.props_box.clear()
        with self.props_box:
            if what is None:
                self.plant_form()
            elif what[0] == "block":
                self.block_form(what[1])
            else:
                self.flow_form(what[1])

    def plant_form(self) -> None:
        ui.label("Plant").classes("text-base font-medium")
        for spec in PLANT_FIELDS:
            self.field(
                spec,
                self.doc.data.get(spec.key),
                lambda v, k=spec.key: self.doc.update_plant(**{k: v}),
            )
        ui.label("Settings").classes("text-sm font-medium mt-2")
        settings = self.doc.data.get("settings", {})
        for spec in SETTINGS_FIELDS:
            self.field(
                spec,
                settings.get(spec.key),
                lambda v, k=spec.key: self.doc.update_settings(**{k: v}),
            )

    def block_form(self, block_id: str) -> None:
        # The form follows renames in place (``cur``) rather than being rebuilt, so typing in
        # the next field right after changing the id is never lost.
        cur = [block_id]
        b = self.doc.block(block_id)
        ui.label(KIND_LABELS[b["kind"]]).classes("text-base font-medium")
        id_input = (
            ui.input("Id", value=block_id).props('dense data-testid="field-id"').classes("w-full")
        )
        id_input.on("blur", lambda: self.rename(cur, id_input.value))
        self.field(
            FieldSpec("name", "Name", "text"),
            b.get("name"),
            lambda v: self.edit_block(cur[0], name=v),
        )
        for spec in BLOCK_FIELDS[b["kind"]]:
            if isinstance(spec, TableSpec):
                self.table(cur, spec)
            else:
                self.field(
                    spec, b.get(spec.key), lambda v, k=spec.key: self.edit_block(cur[0], **{k: v})
                )
        ui.button("Delete block", on_click=lambda: self.delete_block(cur[0])).props(
            'flat color=negative dense data-testid="delete-block"'
        )
        self.issue_list(block_id)

    def flow_form(self, flow_id: str) -> None:
        cur = [flow_id]
        f = self.doc.flow(flow_id)
        ui.label(f"Flow {f['source']} → {f['target']}").classes("text-base font-medium")
        id_input = (
            ui.input("Id", value=flow_id).props('dense data-testid="field-id"').classes("w-full")
        )
        id_input.on("blur", lambda: self.rename_flow(cur, id_input.value))
        for spec in FLOW_FIELDS:
            self.field(
                spec, f.get(spec.key), lambda v, k=spec.key: self.edit_flow(cur[0], **{k: v})
            )
        ui.button("Delete flow", on_click=lambda: self.delete_flow(cur[0])).props(
            'flat color=negative dense data-testid="delete-flow"'
        )
        self.issue_list(flow_id)

    def field(self, spec: FieldSpec, value: Any, apply: Any, testid: str | None = None) -> None:
        tid = f'data-testid="{testid or "field-" + spec.key}"'

        def changed(v: Any) -> None:
            try:
                apply(v)
            except EditorError as exc:
                ui.notify(str(exc), type="negative")
            self.refresh()

        if spec.type == "bool":
            ui.checkbox(spec.label, value=bool(value), on_change=lambda e: changed(e.value)).props(
                tid
            ).classes("w-full")
        elif spec.type == "number":
            ui.number(spec.label, value=value, on_change=lambda e: changed(e.value)).props(
                f"dense {tid}"
            ).classes("w-full")
        elif spec.type in ("select", "block_ref"):
            options = (
                list(spec.options)
                if spec.type == "select"
                else [b["id"] for b in self.doc.blocks if b.get("kind") in spec.options]
            )
            ui.select(
                options,
                label=spec.label,
                value=value if value in options else None,
                on_change=lambda e: changed(e.value),
            ).props(f"dense {tid}").classes("w-full")
        else:
            inp = ui.input(
                spec.label,
                value="" if value is None else str(value),
                on_change=lambda e: changed(
                    (e.value or None) if spec.type == "optional_text" else e.value
                ),
            )
            inp.props(f"dense {tid}" + (" type=date" if spec.type == "date" else "")).classes(
                "w-full"
            )
            if spec.hint:
                inp.tooltip(spec.hint)

    def table(self, cur: list[str], spec: TableSpec) -> None:
        rows = self.doc.block(cur[0]).get(spec.key, [])
        ui.label(spec.label).classes("text-sm font-medium mt-2")
        for i, row in enumerate(rows):
            with ui.card().classes("w-full p-2 gap-0"):
                for col in spec.columns:
                    nullable = (spec.key, col.key) in NULLABLE_NUMBERS
                    self.field(
                        col,
                        row.get(col.key),
                        lambda v, k=col.key, n=i, nl=nullable: self.doc.update_row(
                            cur[0], spec.key, n, **{k: None if nl and v is None else v}
                        ),
                        testid=f"{spec.key}-{i}-{col.key}",
                    )
                ui.button(
                    "Remove", on_click=lambda n=i: self.remove_row(cur[0], spec.key, n)
                ).props(f'flat dense size=sm data-testid="{spec.key}-{i}-remove"')
        ui.button(
            f"Add {spec.label.lower()[:-1] if spec.label.endswith('s') else 'row'}",
            on_click=lambda: self.add_row(cur[0], spec),
        ).props(f'flat dense data-testid="{spec.key}-add"')

    # ---- edits -----------------------------------------------------------------------------

    def edit_block(self, block_id: str, **fields: Any) -> None:
        self.doc.update_block(block_id, **fields)
        if block_id in self.node_of:
            ui.run_javascript(
                f"bfd.setHtml({self.node_of[block_id]}, "
                f"{json.dumps(node_html(self.doc.block(block_id)))})"
            )

    def rename(self, cur: list[str], new: str) -> None:
        old = cur[0]
        if new == old:
            return
        try:
            self.doc.rename_block(old, new)
        except EditorError as exc:
            ui.notify(str(exc), type="negative")
            return
        cur[0] = new
        self.node_of[new] = self.node_of.pop(old)
        self.selected = ("block", new)
        self.edit_block(new)
        self.refresh()

    def edit_flow(self, flow_id: str, **fields: Any) -> None:
        self.doc.update_flow(flow_id, **fields)

    def rename_flow(self, cur: list[str], new: str) -> None:
        old = cur[0]
        if new == old:
            return
        try:
            self.doc.update_flow(old, id=new)
        except EditorError as exc:
            ui.notify(str(exc), type="negative")
            return
        cur[0] = new
        self.selected = ("flow", new)
        self.refresh()

    def delete_block(self, block_id: str) -> None:
        # Drawflow removes the node and its connections; its events update the document.
        ui.run_javascript(f"bfd.editor.removeNodeId('node-{self.node_of[block_id]}')")

    def delete_flow(self, flow_id: str) -> None:
        f = self.doc.flow(flow_id)
        self.doc.disconnect(flow_id)
        ui.run_javascript(
            f"bfd.removeConnection({self.node_of[f['source']]}, {self.node_of[f['target']]})"
        )
        self.select(None)
        self.refresh()

    def add_row(self, block_id: str, spec: TableSpec) -> None:
        row: dict[str, Any] = {c.key: None for c in spec.columns}
        if spec.key == "emission_factors":
            unit = self.doc.block(block_id).get("quantity_unit", "kg")
            row.update(
                value=0.0,
                unit=f"gCO2e/{unit}",
                valid_from=self.doc.data.get("effective_from"),
                source="",
            )
        self.doc.add_row(block_id, spec.key, row)
        self.select(("block", block_id))
        self.refresh()

    def remove_row(self, block_id: str, table: str, index: int) -> None:
        self.doc.remove_row(block_id, table, index)
        self.select(("block", block_id))
        self.refresh()

    # ---- validation display -------------------------------------------------------------

    def refresh(self) -> None:
        issues = self.doc.issues()
        self.title.text = f"{self.doc.data.get('plant', '')} · {self.path.name}"
        self.show_saved_state()
        self.summary.text = (
            f"{len(issues)} issue(s)" if issues else "No issues: the diagram is valid"
        )
        self.summary.classes(replace="text-negative" if issues else "text-positive")
        self.issues_box.clear()
        with self.issues_box:
            for i in issues:
                ui.label(str(i)).classes("text-xs text-negative cursor-pointer").on(
                    "click", lambda _, ref=i.ref: self.goto(ref)
                )
        bad = [self.node_of[i.ref] for i in issues if i.ref in self.node_of]
        ui.run_javascript(f"bfd.setBad({json.dumps(bad)})")
        edges = [
            dict(s, source=self.node_of[s["source"]], target=self.node_of[s["target"]])
            for s in self.doc.edge_styles()
            if s["source"] in self.node_of and s["target"] in self.node_of
        ]
        passing = [n for b, n in self.node_of.items() if self.doc.is_pass_through(b)]
        ui.run_javascript(f"bfd.style({json.dumps(edges)}, {json.dumps(passing)})")

    def issue_list(self, ref: str) -> None:
        for i in self.doc.issues_by_ref().get(ref, []):
            ui.label(f"{i.rule}: {i.message}").classes("text-xs text-negative")

    def goto(self, ref: str | None) -> None:
        if ref is None:
            self.select(None)
        elif any(b.get("id") == ref for b in self.doc.blocks):
            self.select(("block", ref))
        elif any(f.get("id") == ref for f in self.doc.flows):
            self.select(("flow", ref))


def run(path: Path, port: int = 8080, show: bool = True) -> None:
    app.add_static_files("/bfd-static", STATIC)

    @ui.page("/")
    async def index() -> None:
        editor = Editor(path)
        editor.build()
        await ui.context.client.connected()
        await editor.load_canvas()

    ui.run(port=port, show=show, reload=False, title="RFNBO plant editor")
