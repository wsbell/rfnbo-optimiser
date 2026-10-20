"""Batch lineage: where every kg of every ledger batch came from and went.

Edges run downstream: batch part → downstream batch (feed), batch → its parts (output, when a
batch has several), part → aggregated batch,
aggregated batch → consignment, batch → quarter-end write-off or carry-forward. A PoS is a node
too (§11.1 rule 6): PoS → unit batch (feed, credentials allocated to a supplier's delivery),
and PoS → write-off for credentials not allocated by quarter end. Credentials allocated into a
storage sit in its ledger under the PoS's id, so their edges are those of any ledger batch.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from rfnbo.calc.calculator import RunResult

EdgeKind = Literal["feed", "output", "aggregation", "consignment", "write_off", "carried"]


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    kind: EdgeKind
    kg: float


def write_off_node(storage: str, quarter: str) -> str:
    return f"write-off:{storage}:{quarter}"


def carried_node(storage: str, quarter: str) -> str:
    return f"carried:{storage}:{quarter}"


def pos_write_off_node(quarter: str) -> str:
    return f"write-off:pos:{quarter}"


def edges(run: RunResult) -> list[Edge]:
    out = [Edge(a.source, a.target, "feed", a.kg) for a in run.decisions.feed]
    for b in run.batches.values():  # a batch with several parts (build plan §12)
        out += [Edge(b.id, p.id, "output", p.kg) for p in b.parts if p.id != b.id]
    for p in run.purchases:
        to_unit: dict[str, float] = {}
        for a in p.allocations:
            if a.kind == "unit":
                to_unit[a.target] = to_unit.get(a.target, 0.0) + a.kg
        out += [Edge(p.id, b, "feed", kg) for b, kg in to_unit.items()]
        if p.unallocated_kg > 0:
            out.append(
                Edge(p.id, pos_write_off_node(run.frame.quarter), "write_off", p.unallocated_kg)
            )
    for c in run.consignments:
        for agg in c.aggregated:
            out += [Edge(p.batch, agg.id, "aggregation", p.kg) for p in agg.parts]
            out.append(Edge(agg.id, c.id, "consignment", agg.kg))
    for led in run.ledgers:
        for x in led.lines:
            if x.write_off_kg > 0:
                out.append(
                    Edge(
                        x.batch,
                        write_off_node(led.storage, led.quarter),
                        "write_off",
                        x.write_off_kg,
                    )
                )
            if x.carried_kg > 0:
                out.append(
                    Edge(x.batch, carried_node(led.storage, led.quarter), "carried", x.carried_kg)
                )
    return out


def graph(run: RunResult) -> dict[str, Any]:
    """Nodes and edges as plain JSON, in a deterministic order."""
    nodes: list[dict[str, Any]] = []
    for led in run.ledgers:
        for x in led.lines:
            if x.origin == "opening":
                nodes.append(
                    {"id": x.batch, "kind": "opening", "storage": led.storage, "month": x.month}
                )
    for p in run.purchases:
        nodes.append(
            {
                "id": p.id,
                "kind": "pos",
                "supplier": p.supplier,
                "product": p.product,
                "dispatch_date": p.dispatch_date.isoformat(),
                "ghg": p.ghg_gCO2e_per_MJ,
                "quantity_kg": p.quantity_kg,
            }
        )
    for b in run.batches.values():
        nodes.append(
            {
                "id": b.id,
                "kind": "batch",
                "unit": b.unit,
                "month": b.month,
                "rfnbo": b.is_rfnbo,
                "compliant": b.compliant,
            }
        )
        nodes += [
            {
                "id": p.id,
                "kind": "part",
                "batch": b.id,
                "ghg": p.ghg_gCO2e_per_MJ,
                "compliant": p.compliant,
                "purchased": p.purchased,
                "source": p.source,
            }
            for p in b.parts
            if p.id != b.id
        ]
    for c in run.consignments:
        nodes += [
            {"id": a.id, "kind": "aggregated_batch", "month": ",".join(a.months)}
            for a in c.aggregated
        ]
        nodes.append({"id": c.id, "kind": "consignment", "customer": c.customer})
    for led in run.ledgers:
        nodes.append({"id": write_off_node(led.storage, led.quarter), "kind": "write_off"})
        nodes.append({"id": carried_node(led.storage, led.quarter), "kind": "carried"})
    if run.purchases:
        nodes.append({"id": pos_write_off_node(run.frame.quarter), "kind": "write_off"})
    return {
        "nodes": nodes,
        "edges": [
            {"source": e.source, "target": e.target, "kind": e.kind, "kg": e.kg} for e in edges(run)
        ],
    }


def downstream_of(all_edges: list[Edge], node: str) -> list[Edge]:
    """Every edge reachable downstream of ``node`` (consignment drill-up)."""
    by_source: dict[str, list[Edge]] = defaultdict(list)
    for e in all_edges:
        by_source[e.source].append(e)
    return _walk(node, by_source, lambda e: e.target)


def upstream_of(all_edges: list[Edge], node: str) -> list[Edge]:
    """Every edge reachable upstream of ``node`` (consignment → batches drill-down)."""
    by_target: dict[str, list[Edge]] = defaultdict(list)
    for e in all_edges:
        by_target[e.target].append(e)
    return _walk(node, by_target, lambda e: e.source)


def _walk(start: str, index: dict[str, list[Edge]], step: Any) -> list[Edge]:
    seen: set[str] = {start}
    out: list[Edge] = []
    frontier = [start]
    while frontier:
        nxt: list[str] = []
        for n in frontier:
            for e in index.get(n, []):
                out.append(e)
                m = step(e)
                if m not in seen:
                    seen.add(m)
                    nxt.append(m)
        frontier = nxt
    return out
