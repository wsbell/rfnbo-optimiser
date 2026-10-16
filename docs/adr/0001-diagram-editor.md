# ADR 0001: Block flow diagram editor technology

Status: accepted (phase 2)

## Context

The build plan (§3.4) asks for a diagram editor whose only job is to produce valid
plant-config JSON, decoupled from the engine. Two options were to be spiked, keeping the one
that round-trips the test plant cleanly with less code:

1. A NiceGUI page hosting a JavaScript diagram library (Drawflow) as a component.
2. A React Flow front end talking to a FastAPI back end.

## What was built

Both spikes share one pure-Python editing model, `rfnbo.ui.bfd_editor.document.EditorDocument`
(add, connect, move, rename, delete, validate, save), so the comparison is only the UI layer.
The spike code is in the git history: `spikes/` in the first phase 2 commit, removed in the next.

Each spike was driven in headless Chromium through the same script: load the test plant,
drag a block, add a distribution block, connect NH₃ storage to it by dragging between ports,
save, reload the page, delete the new block, save again and validate the file with
`load_config`. A second script dragged a disallowed connection (water → H₂ storage).

| | A: NiceGUI + Drawflow | B: React Flow + FastAPI |
| --- | --- | --- |
| Round trip (load, drag, add, connect, save, reload, delete, validate) | passes | passes |
| Disallowed connection rejected with a message | passes | passes |
| UI code (non-blank lines) | 104, one Python file | 151: 57 Python, 77 JSX, 17 config |
| Languages | Python | Python, JavaScript |
| Build step | none | Node, npm install (49 MB), Vite build |
| Shipped JS | Drawflow 0.0.60, 46 KB, vendored, MIT | 323 KB bundle |
| Property forms | Python (NiceGUI), same process as the model | JSX, via an HTTP API |

## Decision

Option 1, NiceGUI + Drawflow. Both round-trip the test plant cleanly; option 1 does it with
less code, in one language, with no front-end build in CI or deployment, and keeps the
property forms in Python next to the pydantic schema they edit.

## Consequences

- Drawflow is small and lightly maintained. It is vendored (`src/rfnbo/ui/bfd_editor/static`),
  so it cannot change under us, and it is only a view: all state and validation live in
  `EditorDocument`, which is fully unit-tested. Replacing the canvas later touches only the
  thin page module.
- Canvas ↔ Python messages go through NiceGUI events; block ids are passed as JSON, never
  interpolated as raw strings.
- NiceGUI is an optional dependency (`ui` extra). The engine never imports `rfnbo.ui`.
- Block positions are stored in the config's optional `layout`, which the engine ignores and
  which is left out of a sealed run's config hash.
