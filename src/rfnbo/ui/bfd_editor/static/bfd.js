// Glue between Drawflow (a view only) and the Python EditorDocument (the state).
// Every user edit is sent to Python with emitEvent; Python answers by calling bfd.* below.
// Programmatic changes run with `silent` set so they are not echoed back as user edits.
window.bfd = {
  editor: null,
  silent: false,

  load(container, nodes, edges) {
    if (this.editor) {
      this.silent = true;
      this.editor.clear();
      this.silent = false;
    } else {
      const ed = new Drawflow(container);
      ed.reroute = false;
      // Straight lines with right-angle bends (Drawflow's own are curves).
      ed.createCurvature = (sx, sy, ex, ey) => this.elbow(sx, sy, ex, ey);
      ed.start();
      const emit = (name, data) => { if (!this.silent) emitEvent(name, data); };
      ed.on("connectionCreated", (c) => emit("bfd_connection_created", c));
      ed.on("connectionRemoved", (c) => emit("bfd_connection_removed", c));
      ed.on("connectionSelected", (c) => emit("bfd_connection_selected", c));
      ed.on("nodeRemoved", (id) => emit("bfd_node_removed", Number(id)));
      ed.on("nodeSelected", (id) => emit("bfd_node_selected", Number(id)));
      ed.on("nodeUnselected", () => emit("bfd_unselected", null));
      ed.on("nodeMoved", (id) => {
        const n = ed.getNodeFromId(id);
        emit("bfd_node_moved", { id: Number(id), x: n.pos_x, y: n.pos_y });
      });
      this.editor = ed;
    }
    const ids = {};
    this.silent = true;
    try {
      for (const n of nodes) ids[n.block] = this.addNode(n);
      for (const e of edges) this.editor.addConnection(ids[e.source], ids[e.target], "output_1", "input_1");
    } finally {
      this.silent = false;
    }
    return ids;
  },

  // A connection from an output port (sx, sy) to an input port (ex, ey): along to the middle
  // of the gap, across, and in, ending in an arrowhead at the input port's edge. A target at
  // or behind the source is reached round the outside.
  elbow(sx, sy, ex, ey) {
    const tip = ex - 7;
    const head = ` M ${tip - 7} ${ey - 4.5} L ${tip} ${ey} L ${tip - 7} ${ey + 4.5}`;
    if (tip - sx >= 24) {
      const mx = Math.round(sx + (tip - sx) / 2);
      return `M ${sx} ${sy} L ${mx} ${sy} L ${mx} ${ey} L ${tip} ${ey}` + head;
    }
    const out = sx + 16, back = tip - 16, my = Math.round((sy + ey) / 2);
    return (
      `M ${sx} ${sy} L ${out} ${sy} L ${out} ${my} L ${back} ${my} L ${back} ${ey} L ${tip} ${ey}` +
      head
    );
  },

  // Inputs are drawn as pins on the units they feed unless shown: hidden, their nodes and
  // wires are not drawn (CSS on the canvas's bfd-show-inputs class).
  markInputs(ids) {
    const inputs = new Set(ids.map(String));
    document.querySelectorAll("#bfd-canvas .connection").forEach((el) => {
      const from = [...el.classList].find((c) => c.startsWith("node_out_node-"));
      el.classList.toggle("bfd-from-input", !!from && inputs.has(from.slice(14)));
    });
  },

  showInputs(show) {
    const ed = this.editor;
    document.getElementById("bfd-canvas").classList.toggle("bfd-show-inputs", show);
    // Wires drawn while their input was hidden start from nowhere: redraw them.
    for (const id of Object.keys(ed.drawflow.drawflow.Home.data)) {
      ed.updateConnectionNodes(`node-${id}`);
    }
    this.fit();
  },

  // Zoom and pan so every block drawn fits the canvas (never zooming in past 1:1).
  fit() {
    const ed = this.editor, box = document.getElementById("bfd-canvas");
    const show = box.classList.contains("bfd-show-inputs");
    const nodes = Object.values(ed.drawflow.drawflow.Home.data).filter(
      (n) => show || !n.class.split(" ").includes("bfd-input"),
    );
    const size = (n) => {
      const el = document.getElementById(`node-${n.id}`);
      return el ? [el.offsetWidth, el.offsetHeight] : [168, 80];
    };
    let zoom = 1, left = 0, top = 0;
    if (nodes.length) {
      left = Math.min(...nodes.map((n) => n.pos_x));
      top = Math.min(...nodes.map((n) => n.pos_y));
      const right = Math.max(...nodes.map((n) => n.pos_x + size(n)[0]));
      const bottom = Math.max(...nodes.map((n) => n.pos_y + size(n)[1]));
      zoom = Math.min(
        1, (box.clientWidth - 48) / (right - left), (box.clientHeight - 48) / (bottom - top),
      );
      zoom = Math.max(ed.zoom_min, zoom);
    }
    // Drawflow scales about the canvas centre, so the pan accounts for it.
    const cx = ed.precanvas.clientWidth / 2, cy = ed.precanvas.clientHeight / 2;
    ed.zoom = zoom;
    ed.zoom_last_value = zoom;
    ed.canvas_x = Math.round(24 - cx - (left - cx) * zoom);
    ed.canvas_y = Math.round(24 - cy - (top - cy) * zoom);
    ed.precanvas.style.transform =
      `translate(${ed.canvas_x}px, ${ed.canvas_y}px) scale(${ed.zoom})`;
  },

  addNode(n) {
    const wasSilent = this.silent;
    this.silent = true;
    try {
      return this.editor.addNode(n.block, n.inputs, n.outputs, n.x, n.y, "bfd-" + n.kind, {}, n.html);
    } finally {
      this.silent = wasSilent;
    }
  },

  addConnection(source, target) {
    this.silent = true;
    try {
      this.editor.addConnection(source, target, "output_1", "input_1");
    } finally {
      this.silent = false;
    }
  },

  // Flow colour by product and dashed pass-through links; pass-through nodes get a class.
  style(edges, passThrough) {
    for (const e of edges) {
      const sel = `.connection.node_out_node-${e.source}.node_in_node-${e.target}`;
      document.querySelectorAll(sel).forEach((el) => {
        el.classList.toggle("bfd-dashed", e.dashed);
        el.querySelectorAll(".main-path").forEach((p) => { p.style.stroke = e.colour; });
      });
    }
    const pt = new Set(passThrough.map(String));
    document.querySelectorAll(".drawflow-node").forEach((el) => {
      el.classList.toggle("bfd-pass_through", pt.has(el.id.replace("node-", "")));
    });
  },

  removeConnection(source, target) {
    this.silent = true;
    try {
      this.editor.removeSingleConnection(source, target, "output_1", "input_1");
    } finally {
      this.silent = false;
    }
  },

  setHtml(id, html) {
    this.editor.drawflow.drawflow.Home.data[id].html = html;
    const el = document.querySelector(`#node-${id} .drawflow_content_node`);
    if (el) el.innerHTML = html;
  },

  setBad(ids) {
    const bad = new Set(ids.map(String));
    document.querySelectorAll(".drawflow-node").forEach((el) => {
      el.classList.toggle("bfd-bad", bad.has(el.id.replace("node-", "")));
    });
  },

  // Python sets this after every edit and save: the diagram has changes its file lacks.
  dirty: false,
};

// Warn before leaving the page with unsaved changes.
window.addEventListener("beforeunload", (e) => {
  if (bfd.dirty) {
    e.preventDefault();
    e.returnValue = "";
  }
});

// Ctrl+S / Cmd+S saves. Blurring first applies a field edited on blur (a block's id).
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
    e.preventDefault();
    if (document.activeElement) document.activeElement.blur();
    emitEvent("bfd_save", null);
  }
});
