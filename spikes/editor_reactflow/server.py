"""Spike B back end: FastAPI wrapping EditorDocument, serving the built React Flow app.

uv run --with fastapi --with uvicorn python spikes/editor_reactflow/server.py <config> [port]
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from rfnbo.ui.bfd_editor.document import EditorDocument, EditorError

PATH = Path(sys.argv[1])
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
doc = EditorDocument.load(PATH)
api = FastAPI()


def state() -> dict[str, Any]:
    return {
        "blocks": doc.blocks,
        "flows": doc.flows,
        "positions": {b["id"]: doc.position(b["id"]) for b in doc.blocks},
        "issues": [{"rule": i.rule, "ref": i.ref, "message": str(i)} for i in doc.issues()],
    }


def apply(fn: Any, *args: Any, **kw: Any) -> dict[str, Any]:
    try:
        fn(*args, **kw)
    except EditorError as exc:
        raise HTTPException(400, str(exc)) from exc
    return state()


@api.get("/api/doc")
def get_doc() -> dict[str, Any]:
    global doc
    doc = EditorDocument.load(PATH)
    return state()


@api.post("/api/blocks")
def add_block(body: dict[str, Any]) -> dict[str, Any]:
    return apply(doc.add_block, body["kind"], body.get("x", 60), body.get("y", 60))


@api.delete("/api/blocks/{block_id}")
def remove_block(block_id: str) -> dict[str, Any]:
    return apply(doc.remove_block, block_id)


@api.patch("/api/blocks/{block_id}")
def update_block(block_id: str, body: dict[str, Any]) -> dict[str, Any]:
    return apply(doc.update_block, block_id, **body)


@api.post("/api/move")
def move(body: dict[str, Any]) -> dict[str, Any]:
    return apply(doc.move, body["id"], body["x"], body["y"])


@api.post("/api/flows")
def connect(body: dict[str, Any]) -> dict[str, Any]:
    return apply(doc.connect, body["source"], body["target"])


@api.delete("/api/flows/{flow_id}")
def disconnect(flow_id: str) -> dict[str, Any]:
    return apply(doc.disconnect, flow_id)


@api.post("/api/save")
def save() -> dict[str, Any]:
    return {"issues": len(doc.save(PATH))}


api.mount("/", StaticFiles(directory=Path(__file__).parent / "web" / "dist", html=True))

if __name__ == "__main__":
    uvicorn.run(api, port=PORT)
