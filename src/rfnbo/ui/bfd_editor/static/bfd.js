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
};
