import { useCallback, useEffect, useState } from "react";
import { ReactFlow, Handle, Position, applyNodeChanges } from "@xyflow/react";
import "@xyflow/react/dist/style.css";

const KINDS = ["input", "processing_unit", "storage_unit", "distribution", "consignment_output"];

async function call(method, url, body) {
  const r = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json();
  if (!r.ok) throw new Error(data.detail);
  return data;
}

function BlockNode({ data }) {
  return (
    <div style={{ padding: 8, border: data.bad ? "2px solid red" : "1px solid #333",
                  background: "#e0f7fa", width: 150, fontSize: 12 }}>
      {data.kind !== "input" && <Handle type="target" position={Position.Left} />}
      <b>{data.name}</b><br /><small>{data.kind}</small>
      {data.kind !== "consignment_output" && <Handle type="source" position={Position.Right} />}
    </div>
  );
}
const nodeTypes = { block: BlockNode };

export default function App() {
  const [doc, setDoc] = useState(null);
  const [nodes, setNodes] = useState([]);
  const [selected, setSelected] = useState(null);
  const [error, setError] = useState("");

  const show = useCallback((d) => {
    setDoc(d);
    const bad = new Set(d.issues.map((i) => i.ref));
    setNodes(d.blocks.map((b) => ({
      id: b.id, type: "block",
      position: { x: d.positions[b.id][0], y: d.positions[b.id][1] },
      data: { name: b.name, kind: b.kind, bad: bad.has(b.id) },
    })));
  }, []);
  const run = (p) => p.then(show).catch((e) => setError(e.message));
  useEffect(() => { run(call("GET", "/api/doc")); }, []);

  if (!doc) return null;
  const edges = doc.flows.map((f) => ({ id: f.id, source: f.source, target: f.target }));
  const block = selected && doc.blocks.find((b) => b.id === selected);

  return (
    <div style={{ display: "flex", height: "95vh" }}>
      <div style={{ flexGrow: 1, border: "1px solid #ccc" }}>
        <ReactFlow
          nodes={nodes} edges={edges} nodeTypes={nodeTypes}
          onNodesChange={(c) => setNodes((n) => applyNodeChanges(c, n))}
          onNodeDragStop={(_, n) => run(call("POST", "/api/move", { id: n.id, ...n.position }))}
          onConnect={(c) => run(call("POST", "/api/flows", c))}
          onNodesDelete={(ns) => ns.forEach((n) => run(call("DELETE", `/api/blocks/${n.id}`)))}
          onEdgesDelete={(es) => es.forEach((e) => run(call("DELETE", `/api/flows/${e.id}`)))}
          onNodeClick={(_, n) => setSelected(n.id)}
          deleteKeyCode={["Backspace", "Delete"]}
        />
      </div>
      <div style={{ width: 320, padding: 8 }}>
        {KINDS.map((k) => (
          <div key={k}><button onClick={() => run(call("POST", "/api/blocks", { kind: k }))}>
            + {k}</button></div>
        ))}
        {block && (
          <label>Name <input key={block.id} defaultValue={block.name}
            onBlur={(e) => run(call("PATCH", `/api/blocks/${block.id}`, { name: e.target.value }))} />
          </label>
        )}
        <div><button onClick={() => call("POST", "/api/save")}>Save</button></div>
        {error && <div style={{ color: "red" }}>{error}</div>}
        {doc.issues.map((i, n) => (
          <div key={n} className="text-red" style={{ color: "red", fontSize: 12 }}>{i.message}</div>
        ))}
      </div>
    </div>
  );
}
