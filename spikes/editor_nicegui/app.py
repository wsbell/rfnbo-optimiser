"""Spike A: NiceGUI page hosting Drawflow, exchanging edits with EditorDocument.

uv run --with nicegui python spikes/editor_nicegui/app.py <config.json> [port]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from nicegui import app, ui

from rfnbo.ui.bfd_editor.document import KINDS, EditorDocument, EditorError

PATH = Path(sys.argv[1])
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8080
app.add_static_files("/static", Path(__file__).parent / "static")
PORTS = {"input": (0, 1), "consignment_output": (1, 0)}


@ui.page("/")
def page() -> None:
    doc = EditorDocument.load(PATH)
    ids: dict[str, str] = {}  # drawflow node id -> block id

    ui.add_head_html(
        '<link rel="stylesheet" href="/static/drawflow.min.css">'
        '<script src="/static/drawflow.min.js"></script>'
        "<style>#df{height:80vh;border:1px solid #ccc}.drawflow-node.bad{border:2px solid red}</style>"
    )
    with ui.row().classes("w-full no-wrap"):
        canvas = ui.element("div").props('id="df"').classes("grow")
        with ui.column().classes("w-80"):
            for kind in KINDS:
                ui.button(f"+ {kind}", on_click=lambda k=kind: add(k)).props("dense")
            name = ui.input("Name")
            ui.button("Save", on_click=lambda: save())
            issues = ui.column()
    selected: dict[str, str] = {}

    def refresh() -> None:
        issues.clear()
        with issues:
            for i in doc.issues():
                ui.label(str(i)).classes("text-red text-xs")

    def node_js(block_id: str) -> str:
        b = doc.block(block_id)
        x, y = doc.position(block_id)
        n_in, n_out = PORTS.get(b["kind"], (1, 1))
        html = f"<b>{b.get('name', '')}</b><br><small>{b['kind']}</small>"
        return (
            f"window.ids[{json.dumps(block_id)}]=editor.addNode({json.dumps(block_id)},"
            f"{n_in},{n_out},{x},{y},{json.dumps(b['kind'])},{{}},{json.dumps(html)});"
        )

    def add(kind: str) -> None:
        block_id = doc.add_block(kind, 60, 60)
        ui.run_javascript(node_js(block_id) + "emitEvent('ids', window.ids);")
        refresh()

    def save() -> None:
        n = len(doc.save(PATH))
        ui.notify(f"saved with {n} issue(s)")

    def on_created(e: object) -> None:
        a = e.args  # type: ignore[attr-defined]
        src, tgt = ids[str(a["output_id"])], ids[str(a["input_id"])]
        try:
            doc.connect(src, tgt)
        except EditorError as exc:
            ui.notify(str(exc), type="negative")
            ui.run_javascript(
                f"editor.removeSingleConnection({a['output_id']},{a['input_id']},"
                "'output_1','input_1')"
            )
        refresh()

    def on_removed(e: object) -> None:
        a = e.args  # type: ignore[attr-defined]
        src, tgt = ids.get(str(a["output_id"])), ids.get(str(a["input_id"]))
        for f in list(doc.flows):
            if f["source"] == src and f["target"] == tgt:
                doc.disconnect(f["id"])
        refresh()

    def on_moved(e: object) -> None:
        a = e.args  # type: ignore[attr-defined]
        doc.move(ids[str(a["id"])], a["x"], a["y"])

    def on_node_removed(e: object) -> None:
        block_id = ids.pop(str(e.args))  # type: ignore[attr-defined]
        doc.remove_block(block_id)
        refresh()

    def on_selected(e: object) -> None:
        selected["id"] = ids[str(e.args)]  # type: ignore[attr-defined]
        name.value = doc.block(selected["id"]).get("name", "")

    def on_ids(e: object) -> None:
        ids.clear()
        ids.update({str(v): k for k, v in e.args.items()})  # type: ignore[attr-defined]

    def rename(e: object) -> None:
        if "id" in selected:
            doc.update_block(selected["id"], name=name.value)

    name.on("blur", rename)
    for event, fn in (
        ("created", on_created),
        ("removed", on_removed),
        ("moved", on_moved),
        ("node_removed", on_node_removed),
        ("selected", on_selected),
        ("ids", on_ids),
    ):
        ui.on(event, fn)

    nodes = "".join(node_js(b["id"]) for b in doc.blocks)
    edges = "".join(
        f"editor.addConnection(window.ids[{json.dumps(f['source'])}],"
        f"window.ids[{json.dumps(f['target'])}],'output_1','input_1');"
        for f in doc.flows
    )
    ui.timer(
        0.1,
        once=True,
        callback=lambda: ui.run_javascript(f"""
        window.editor = new Drawflow(document.getElementById('df')); editor.start();
        window.ids = {{}}; {nodes} {edges}
        emitEvent('ids', window.ids);
        editor.on('connectionCreated', c => emitEvent('created', c));
        editor.on('connectionRemoved', c => emitEvent('removed', c));
        editor.on('nodeMoved', id => {{ const n = editor.getNodeFromId(id);
            emitEvent('moved', {{id, x: n.pos_x, y: n.pos_y}}); }});
        editor.on('nodeRemoved', id => emitEvent('node_removed', id));
        editor.on('nodeSelected', id => emitEvent('selected', id));
    """),
    )
    refresh()


ui.run(port=PORT, reload=False, show=False)
