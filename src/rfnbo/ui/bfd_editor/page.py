"""The block flow diagram editor page (NiceGUI + vendored Drawflow).

All state lives in :class:`EditorDocument`; this module renders it and forwards user actions.
See docs/adr/0001-diagram-editor.md for why NiceGUI + Drawflow.
"""

from __future__ import annotations

import html
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from nicegui import app, events, ui

from rfnbo.ui import theme
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
# Block colours follow the app's look (rfnbo.ui.theme): a quiet tint and a kind label per kind.
CSS = """
#bfd-canvas { height: calc(100vh - 230px); min-height: 420px; width: 100%; position: relative;
              background-color: #FBFCFD; border-radius: 0 0 8px 8px;
              background-image: radial-gradient(#DCE1E7 1px, transparent 1px);
              background-size: 16px 16px; }
#bfd-canvas .drawflow-node { width: 168px; min-height: 64px; padding: 8px 10px;
                             box-sizing: border-box; background: #FFFFFF; color: #0A2540;
                             border: 1px solid #C9D1DA; border-radius: 8px;
                             box-shadow: 0 1px 2px rgba(10,37,64,.06); }
#bfd-canvas .drawflow-node.selected { border: 2px solid #5146E5; background: #FFFFFF;
                                      box-shadow: 0 0 0 4px rgba(81,70,229,.15); }
#bfd-canvas .drawflow-node.bfd-bad { border: 2px solid #A3123A; }
#bfd-canvas .bfd-input { background: #FFF8EB; border-color: #EED9B4; }
#bfd-canvas .bfd-rfnbo_supply { background: #FFF8EB; border-color: #E2C48C; }
#bfd-canvas .bfd-storage_unit { background: #F2F7FF; border-color: #C9D9EE; }
#bfd-canvas .bfd-distribution { background: #F7F4FF; border-color: #DAD2F2; }
#bfd-canvas .bfd-consignment_output { background: #FDF3F7; border-color: #F0D2DF; }
#bfd-canvas .drawflow-node.bfd-pass_through { background: #FFFFFF; border: 1px dashed #8792A2;
                                              min-height: 48px; }
#bfd-canvas .drawflow-node.bfd-processing_unit { background: #F1FAF4; border-color: #BFE3CB; }
#bfd-canvas .drawflow-node .input, #bfd-canvas .drawflow-node .output {
  box-sizing: border-box; width: 12px; height: 12px; margin: 0; top: 0; background: #FFFFFF;
  border: 2px solid #8792A2; }
#bfd-canvas .drawflow-node .input { left: -17px; }
#bfd-canvas .drawflow-node .output { right: -5px; }
#bfd-canvas .drawflow-node .input:hover, #bfd-canvas .drawflow-node .output:hover {
  border-color: #5146E5; transform: scale(1.4); }
#bfd-canvas .connection .main-path { stroke: #8792A2; stroke-width: 2px; fill: none;
  stroke-linejoin: round; stroke-linecap: round; }
#bfd-canvas:not(.bfd-show-inputs) .drawflow-node.bfd-input,
#bfd-canvas:not(.bfd-show-inputs) .connection.bfd-from-input { display: none; }
#bfd-canvas.bfd-show-inputs .bfd-pins { display: none; }
.bfd-pins { margin-top: 2px; font-size: 11px; line-height: 14px; color: #7A4A06; }
#bfd-canvas .connection .main-path.selected { stroke: #5146E5; }
#bfd-canvas .connection.bfd-dashed .main-path { stroke-dasharray: 8 5; }
.bfd-kind { font-size: 10px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase;
            color: #425466; }
.bfd-input .bfd-kind, .bfd-rfnbo_supply .bfd-kind { color: #7A4A06; }
.bfd-processing_unit .bfd-kind { color: #05603A; }
.bfd-storage_unit .bfd-kind { color: #1D4F91; }
.bfd-distribution .bfd-kind { color: #5B3BA8; }
.bfd-consignment_output .bfd-kind { color: #9B1F5A; }
.bfd-title { font-weight: 600; font-size: 13px; line-height: 16px; }
.bfd-sub { font-size: 11px; line-height: 14px; color: #697386; }
.rf-props { max-height: calc(100vh - 230px); min-height: 420px; overflow-y: auto; }
"""


def pin_label(block: dict[str, Any]) -> str:
    """An input as a pin on the unit it feeds: what it is, in a word or two."""
    if block.get("material") == "electricity":
        return "renewable" if block.get("renewable") else "non-renewable"
    return str(block.get("material") or block.get("name") or block.get("id"))


def node_html(block: dict[str, Any], pins: Sequence[str] = ()) -> str:
    name = html.escape(str(block.get("name", "")))
    label = KIND_LABELS.get(block.get("kind", ""), "")
    if block.get("kind") == "storage_unit" and block.get("pass_through"):
        label = "Pass-through"
    bid = html.escape(str(block.get("id", "")))
    return (
        f'<div class="bfd-kind">{label}</div><div class="bfd-title">{name}</div>'
        f'<div class="bfd-sub">{bid}</div>'
        + (f'<div class="bfd-pins">In: {html.escape(", ".join(pins))}</div>' if pins else "")
    )


class Editor:
    """One browser tab's editor."""

    def __init__(self, path: Path, back: str | None = None) -> None:
        self.path = path
        self.back = back  # link back to the page that opened the editor
        self.doc = EditorDocument.load(path) if path.exists() else EditorDocument.new()
        self.node_of: dict[str, int] = {}
        self.selected: tuple[str, str] | None = None  # ("block" | "flow", id)
        # Inputs are pins on the units they feed until shown; a new diagram shows them.
        self.show_inputs = not any(b.get("kind") == "input" for b in self.doc.blocks)

    def html(self, block: dict[str, Any]) -> str:
        return node_html(block, self.pins(block["id"]))

    def pins(self, block_id: str) -> list[str]:
        """The inputs flowing into a block, as pin labels."""
        kinds = {b.get("id"): b for b in self.doc.blocks}
        return [
            pin_label(kinds[f["source"]])
            for f in self.doc.flows
            if f.get("target") == block_id and kinds.get(f.get("source"), {}).get("kind") == "input"
        ]

    # ---- layout ----------------------------------------------------------------------

    def build(self) -> None:
        ui.add_head_html(
            '<link rel="stylesheet" href="/bfd-static/drawflow.min.css">'
            '<script src="/bfd-static/drawflow.min.js"></script>'
            '<script src="/bfd-static/bfd.js"></script>'
            f"<style>{CSS}</style>"
        )
        theme.apply()
        right = theme.top_bar([("Plant config", None)], back=self.back)
        with right:
            self.saved_state = ui.label().props('data-testid="saved-state"')
            theme.secondary(ui.button("Download JSON", on_click=self.download)).props(
                'data-testid="download"'
            )
            opener = (
                ui.upload(auto_upload=True, on_upload=self.upload)
                .props('accept=.json data-testid="upload"')
                .classes("hidden")
            )
            theme.secondary(
                ui.button("Open JSON…", on_click=lambda: opener.run_method("pickFiles"))
            ).tooltip(f"Load a plant config; Save writes it to {self.path.name}")
            ui.button("Save", on_click=self.save).props('data-testid="save"').tooltip("Ctrl+S")
        with theme.page(wide=True):
            with ui.row().classes("w-full items-end gap-3").style("flex-wrap: wrap"):
                with ui.column().classes("gap-1"):
                    ui.label("Plant config").classes("rf-h1").props('role="heading" aria-level="1"')
                    self.title = ui.label().classes("rf-sub").style("font-size: 14px")
                ui.space()
                theme.secondary(ui.button("Plant settings", on_click=self.show_plant)).props(
                    'data-testid="plant-settings"'
                )
                with ui.dropdown_button("Add block", icon="add", auto_close=True).props(
                    'outline color=dark no-caps data-testid="add-block"'
                ):
                    for kind in KINDS:
                        ui.item(KIND_LABELS[kind], on_click=lambda k=kind: self.add(k)).props(
                            f'data-testid="add-{kind}"'
                        )
            with ui.row().classes("w-full items-start gap-5").style("flex-wrap: wrap"):
                with (
                    ui.element("section")
                    .classes("rf-card")
                    .style("flex: 999 1 640px; min-width: 0; width: auto")
                ):
                    with ui.row().classes("rf-card-head w-full"):
                        ui.label("Diagram").classes("rf-h2")
                        ui.label(
                            "Drag from a block's right port to another block's left port to "
                            "connect them"
                        ).classes("rf-sub")
                        ui.space()
                        with ui.row().classes("gap-1"):
                            for label, call in (
                                ("−", "zoom_out"),
                                ("1:1", "zoom_reset"),
                                ("+", "zoom_in"),
                            ):
                                theme.secondary(
                                    ui.button(
                                        label,
                                        on_click=lambda c=call: ui.run_javascript(
                                            f"bfd.editor.{c}()"
                                        ),
                                    )
                                ).props(f'dense data-testid="zoom-{call}"').style("min-width: 36px")
                    self.inputs_strip = (
                        ui.row()
                        .classes("w-full items-center gap-2")
                        .style(f"padding: 8px 20px; border-bottom: 1px solid {theme.DIVIDER}")
                    )
                    with ui.element("div").classes("relative w-full"):
                        ui.element("div").props('id="bfd-canvas"')
                with (
                    ui.element("aside")
                    .classes("rf-card")
                    .style("flex: 1 1 320px; max-width: 400px; min-width: 0; width: auto"),
                    ui.column().classes("rf-card-body rf-props w-full gap-2"),
                ):
                    self.props_box = ui.column().classes("w-full gap-2")
                    ui.separator().style(f"background: {theme.DIVIDER}")
                    self.summary = ui.label().props('data-testid="issue-summary"')
                    self.issues_box = ui.column().classes("w-full gap-1")
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
                    "html": self.html(b),
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
        ui.run_javascript(f"bfd.showInputs({json.dumps(self.show_inputs)})")

    # ---- canvas events -----------------------------------------------------------------

    def block_of(self, node_id: Any) -> str | None:
        for block_id, n in self.node_of.items():
            if n == int(node_id):
                return block_id
        return None

    async def add(self, kind: str) -> None:
        if kind == "input" and not self.show_inputs:
            self.set_inputs(True)
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
            "html": self.html(b),
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
            "html": self.html(b),
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
        self.saved_state.classes(replace="text-warning" if dirty else "rf-note")
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
        ui.label("Plant").classes("rf-h2")
        for spec in PLANT_FIELDS:
            self.field(
                spec,
                self.doc.data.get(spec.key),
                lambda v, k=spec.key: self.doc.update_plant(**{k: v}),
            )
        ui.label("Settings").classes("rf-eyebrow mt-3")
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
        ui.label(KIND_LABELS[b["kind"]]).classes("rf-h2")
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
        ui.label(f"Flow {f['source']} → {f['target']}").classes("rf-h2")
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
        ui.label(spec.label).classes("rf-eyebrow mt-3")
        for i, row in enumerate(rows):
            with ui.card().props("flat bordered").classes("w-full p-3 gap-1"):
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
                f"{json.dumps(self.html(self.doc.block(block_id)))})"
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
        inputs = [b for b in self.doc.blocks if b.get("kind") == "input"]
        units = [b for b in self.doc.blocks if b.get("kind") == "processing_unit"]
        marked = [self.node_of[b["id"]] for b in inputs if b["id"] in self.node_of]
        ui.run_javascript(
            "".join(
                f"bfd.setHtml({self.node_of[b['id']]}, {json.dumps(self.html(b))});"
                for b in units
                if b["id"] in self.node_of
            )
            + f"bfd.markInputs({json.dumps(marked)});"
        )
        self.input_chips(inputs)

    def input_chips(self, inputs: list[dict[str, Any]]) -> None:
        """The strip over the canvas: show or hide inputs, and each input as a chip that
        selects it (how a hidden input is edited)."""
        self.inputs_strip.clear()
        with self.inputs_strip:
            ui.switch(
                "Show inputs", value=self.show_inputs, on_change=lambda e: self.set_inputs(e.value)
            ).props('dense data-testid="show-inputs"')
            if not inputs:
                return
            ui.label("Inputs:" if self.show_inputs else "Inputs, drawn as pins:").classes("rf-note")
            names = {b.get("id"): b.get("name") or b.get("id") for b in self.doc.blocks}
            for b in inputs:
                feeds = [f["target"] for f in self.doc.flows if f.get("source") == b["id"]]
                into = (
                    names.get(feeds[0], feeds[0])
                    if len(feeds) == 1
                    else f"{len(feeds)} units"
                    if feeds
                    else "not connected"
                )
                ui.button(
                    f"{b.get('name') or b['id']} → {into}",
                    on_click=lambda bid=b["id"]: self.select(("block", bid)),
                ).props(f'flat dense data-testid="input-{b["id"]}"').style(
                    "background: #FFF8EB; color: #7A4A06; font-size: 12px; padding: 0 8px"
                )

    def set_inputs(self, show: bool) -> None:
        self.show_inputs = show
        ui.run_javascript(f"bfd.showInputs({json.dumps(show)})")
        self.refresh()

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


async def open_editor(path: Path, back: str | None = None) -> None:
    """Build the editor on the current page, editing ``path``."""
    editor = Editor(path, back)
    editor.build()
    await ui.context.client.connected()
    await editor.load_canvas()


def add_static() -> None:
    app.add_static_files("/bfd-static", STATIC)


def run(path: Path, port: int = 8080, show: bool = True) -> None:
    add_static()
    theme.setup()

    @ui.page("/")
    async def index() -> None:
        await open_editor(path)

    ui.run(port=port, show=show, reload=False, title="RFNBO plant editor")
