"""Editing state for a block flow diagram, independent of any UI toolkit.

The editor's only job is to produce valid plant-config JSON. This module holds the plant
config as plain JSON-shaped data while it is being edited (it may be incomplete or invalid at
any moment), applies edits, and reports every schema and rule issue against the block or flow
it concerns. The UI layer only renders this state and forwards user actions to it.
"""

from __future__ import annotations

import contextlib
import copy
import datetime as dt
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from rfnbo.config.schema import Electricity, PlantConfig, compact, synthetic_owner
from rfnbo.config.validate import ALLOWED_CONNECTIONS, Issue, validate

KINDS = (
    "input",
    "rfnbo_supply",
    "processing_unit",
    "storage_unit",
    "distribution",
    "consignment_output",
)
KIND_LABELS = {
    "input": "Input",
    "rfnbo_supply": "RFNBO supplier",
    "processing_unit": "Processing unit",
    "storage_unit": "Storage unit",
    "distribution": "Distribution",
    "consignment_output": "Consignment output",
}
# Flow colours by material, so each product's path through the plant reads at a glance.
# Electricity is grey; other materials take the palette in alphabetical order.
ELECTRICITY_COLOUR = "#9e9e9e"
PALETTE = ("#1565c0", "#2e7d32", "#ef6c00", "#6a1b9a", "#00838f", "#ad1457", "#5d4037", "#827717")
# Key order used when saving, so files diff cleanly.
_TOP_ORDER = (
    "schema_version",
    "plant",
    "version",
    "effective_from",
    "timezone",
    "settings",
    "electricity",
    "blocks",
    "flows",
    "layout",
)


def _a(label: str) -> str:
    """'an input', 'a storage unit'."""
    word = label.lower()
    return ("an " if word[0] in "aeiou" else "a ") + word


class EditorError(ValueError):
    """An edit that cannot be applied (as opposed to a validation issue in the result)."""


def _defaults(kind: str, effective_from: str) -> dict[str, Any]:
    """Starting fields for a new block. Required facts are left blank so validation asks."""
    if kind == "input":
        return {
            "material": "",
            "quantity_unit": "kg",
            "renewable": False,
            "emission_factors": [
                {"value": 0.0, "unit": "gCO2e/kg", "valid_from": effective_from, "source": ""}
            ],
        }
    if kind == "processing_unit":
        return {
            "product": "",
            "lhv_MJ_per_kg": 0.0,
            "electricity_adds_to_lhv": False,
            "fixed_adder_gCO2e_per_MJ": 0.0,
            "production_series": "",
            "electricity_series": None,
        }
    if kind == "storage_unit":
        return {
            "product": "",
            "inventory_series": "",
            "opening_inventory_kg": 0.0,
            "opening_ledger": [],
        }
    if kind == "rfnbo_supply":
        return {"supplier": "", "product": "", "lhv_MJ_per_kg": 0.0}
    if kind == "distribution":
        return {"customer": "", "fixed_adder_gCO2e_per_MJ": 0.0}
    if kind == "consignment_output":
        return {"customer": "", "distribution_block": ""}
    raise EditorError(f"unknown block kind {kind!r}")


def _ordered_rows(targets: list[float], rows: int) -> list[int]:
    """Increasing rows in ``range(rows)``, one per target, as near the targets as they can be
    (least total distance; the lower row on a tie)."""
    n = len(targets)
    inf = float("inf")
    # cost[k][r]: best total for the first k+1 targets with target k on row r.
    cost = [[inf] * rows for _ in range(n)]
    back = [[-1] * rows for _ in range(n)]
    for r in range(rows):
        cost[0][r] = abs(r - targets[0])
    for k in range(1, n):
        best, arg = inf, -1
        for r in range(rows):
            if r > 0 and cost[k - 1][r - 1] < best:
                best, arg = cost[k - 1][r - 1], r - 1
            if arg >= 0:
                cost[k][r] = best + abs(r - targets[k])
                back[k][r] = arg
    r = min(range(rows), key=lambda x: (cost[n - 1][x], x))
    out = [r]
    for k in range(n - 1, 0, -1):
        r = back[k][r]
        out.append(r)
    return out[::-1]


# What a processing unit may be linked to directly (build plan §13): the loader puts a
# pass-through storage in the link, which the diagram never shows.
DIRECT_TARGETS = ("processing_unit", "distribution")
CONNECTIONS = ALLOWED_CONNECTIONS | {("processing_unit", k) for k in DIRECT_TARGETS}


class EditorDocument:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = copy.deepcopy(data)
        self.data.setdefault("blocks", [])
        self.data.setdefault("flows", [])
        self.data.setdefault("layout", {})
        # The diagram as last loaded from or saved to its file; None: not from its file (an
        # uploaded diagram, until saved).
        self._saved: dict[str, Any] | None = None

    @property
    def dirty(self) -> bool:
        """True if the diagram (layout included) has changes its file does not hold."""
        return self._saved != self.data

    def mark_saved(self) -> None:
        self._saved = copy.deepcopy(self.data)

    # ---- creation and persistence -------------------------------------------------

    @classmethod
    def new(
        cls,
        plant: str = "New plant",
        timezone: str = "Europe/Amsterdam",
        effective_from: dt.date | None = None,
    ) -> EditorDocument:
        """An empty diagram; unchanged until edited, though nothing is written yet."""
        doc = cls(
            {
                "schema_version": 1,
                "plant": plant,
                "version": "1",
                "effective_from": (effective_from or dt.date.today()).isoformat(),
                "timezone": timezone,
                "settings": {},
                "blocks": [],
                "flows": [],
                "layout": {},
            }
        )
        doc.mark_saved()
        return doc

    @classmethod
    def from_json(cls, text: str) -> EditorDocument:
        """A diagram from its JSON, in the compact form (build plan §13): electricity blocks
        become the plant's electricity settings, pass-through storages between units direct
        links, wherever that loses nothing. The engine reads both forms the same."""
        data = json.loads(text)
        if not isinstance(data, dict):
            raise EditorError("a plant config must be a JSON object")
        # Too malformed to compact: shown as it is, with its issues.
        with contextlib.suppress(AttributeError, TypeError, KeyError, IndexError):
            data = compact(data)
        return cls(data)

    @classmethod
    def load(cls, path: str | Path) -> EditorDocument:
        doc = cls.from_json(Path(path).read_text(encoding="utf-8"))
        doc.mark_saved()
        return doc

    def to_json(self) -> str:
        """JSON for saving. A valid diagram is normalised through the schema."""
        data: dict[str, Any] = self.data
        if not self.issues():
            data = PlantConfig.model_validate(self.data).model_dump(mode="json", exclude_none=True)
            # The file keeps the compact form: what the loader added is left out again, and
            # direct links keep their ends.
            given = {f.get("id"): f for f in self.flows}
            ids = {b.get("id") for b in self.blocks}
            data["blocks"] = [b for b in data["blocks"] if b["id"] in ids]
            data["flows"] = [
                dict(f, source=given[f["id"]]["source"], target=given[f["id"]]["target"])
                for f in data["flows"]
                if f["id"] in given
            ]
            if self.data.get("electricity") is not None:
                data["electricity"] = Electricity.model_validate(
                    self.data["electricity"]
                ).model_dump(mode="json", exclude_none=True)
            for b in data["blocks"]:  # only pass-through storages say so, keeping files short
                if b.get("pass_through") is False:
                    del b["pass_through"]
                for e in b.get("opening_ledger", []):  # and only purchased entries say so
                    if e.get("origin") == "produced":
                        del e["origin"]
            # A pass-through's outflow series is derived on loading; the file keeps it unset.
            unset = {f.get("id") for f in self.flows if not f.get("series")}
            for f in data["flows"]:
                if f["id"] in unset:
                    f.pop("series", None)
        ordered = {k: data[k] for k in _TOP_ORDER if k in data}
        ordered.update({k: v for k, v in data.items() if k not in ordered})
        return json.dumps(ordered, indent=2, ensure_ascii=False) + "\n"

    def save(self, path: str | Path) -> list[Issue]:
        """Write the diagram (drafts too, so work is never lost); return outstanding issues."""
        Path(path).write_text(self.to_json(), encoding="utf-8")
        self.mark_saved()
        return self.issues()

    # ---- lookup -------------------------------------------------------------------

    @property
    def blocks(self) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = self.data["blocks"]
        return blocks

    @property
    def flows(self) -> list[dict[str, Any]]:
        flows: list[dict[str, Any]] = self.data["flows"]
        return flows

    def block(self, block_id: str) -> dict[str, Any]:
        for b in self.blocks:
            if b.get("id") == block_id:
                return b
        raise EditorError(f"no block {block_id!r}")

    def flow(self, flow_id: str) -> dict[str, Any]:
        for f in self.flows:
            if f.get("id") == flow_id:
                return f
        raise EditorError(f"no flow {flow_id!r}")

    def position(self, block_id: str) -> tuple[float, float]:
        pos = self.data["layout"].get(block_id)
        if pos:
            return float(pos["x"]), float(pos["y"])
        return self.auto_position(block_id)

    def auto_position(self, block_id: str) -> tuple[float, float]:
        """Where a block sits in the automatic layout (configs saved without one)."""
        return self.auto_layout()[block_id]

    def auto_layout(self) -> dict[str, tuple[float, float]]:
        """Lanes, left to right by flow: each block a column after the blocks feeding it, an
        RFNBO supplier just before the units it supplies, and inputs in a column of their own
        to the left (x < 0, beside the units they feed). Within a column, blocks are ordered
        and spaced so lines run as straight as they can."""
        ids = [b["id"] for b in self.blocks]
        kind = {b["id"]: b.get("kind") for b in self.blocks}
        edges = [
            (f["source"], f["target"])
            for f in self.flows
            if f.get("source") in kind and f.get("target") in kind
        ]
        parents = {i: [s for s, t in edges if t == i and kind[s] != "input"] for i in ids}
        children = {i: [t for s, t in edges if s == i] for i in ids}

        depth: dict[str, int] = {}

        def d(node: str, seen: frozenset[str] = frozenset()) -> int:
            if node not in depth:
                up = [p for p in parents[node] if p not in seen]
                depth[node] = 1 + max((d(p, seen | {node}) for p in up), default=-1)
            return depth[node]

        col = {i: d(i) for i in ids if kind[i] != "input"}
        for i in col:  # a source without feed sits one column before what it supplies
            if not parents[i] and children[i]:
                col[i] = max(0, min(col[c] for c in children[i] if c in col) - 1)
        for i in ids:
            if kind[i] == "input":
                col[i] = -1
        columns: dict[int, list[str]] = {}
        for i in ids:
            columns.setdefault(col[i], []).append(i)
        order = sorted(columns)
        # A link pulls its ends into line; one-to-one links (a chain) pull hardest, a fan-out
        # or fan-in less, so chains run straight.
        weight: dict[str, dict[str, float]] = {i: {} for i in ids}
        for s, t in edges:
            if kind[s] == "input" and kind[t] != "input":
                weight[s][t] = 1 / len(children[s])  # inputs follow the units they feed
                continue
            w = 1 / (max(len(children[s]), 1) * max(len(parents[t]), 1))
            weight[s][t] = weight[t][s] = w

        def pull(b: str, at: dict[str, float], side: int = 0) -> float | None:
            """Weighted mean place of a block's neighbours (on one side if ``side``)."""
            near = [
                (at[n], w)
                for n, w in weight[b].items()
                if n in at and col[n] != col[b] and (side == 0 or (col[n] - col[b]) * side > 0)
            ]
            total = sum(w for _, w in near)
            return sum(p * w for p, w in near) / total if total else None

        # Order each column by its neighbours' places, sweeping in from the left, then the
        # right (each sweep looks back at the columns it has passed).
        def place(c: int) -> dict[str, float]:
            n = len(columns[c])
            return {b: k - (n - 1) / 2 for k, b in enumerate(columns[c])}

        for sweep, side in ((order, -1), (order[::-1], 1)) * 2:
            where: dict[str, float] = {}
            for c in order:
                where.update(place(c))
            for c in sweep:
                current = place(c)

                def key(
                    b: str,
                    current: dict[str, float] = current,
                    where: dict[str, float] = where,
                    side: int = side,
                ) -> tuple[float, float]:
                    p = pull(b, where, side)
                    return (current[b] if p is None else p, current[b])

                columns[c].sort(key=key)
                where.update(place(c))

        # Rows: a grid as tall as the fullest column; each other column takes the rows nearest
        # its neighbours' (in order, one block a row), refined over a few passes.
        rows_n = max(len(v) for v in columns.values()) if columns else 0
        row: dict[str, float] = {}
        for c in order:
            if len(columns[c]) == rows_n:
                row.update({b: k for k, b in enumerate(columns[c])})
        for _ in range(4):
            for c in order:
                if len(columns[c]) == rows_n:
                    continue
                targets = [pull(b, row) for b in columns[c]]
                mid = (rows_n - 1) / 2
                chosen = _ordered_rows([mid if t is None else t for t in targets], rows_n)
                row.update(zip(columns[c], chosen, strict=True))
        return {i: (40.0 + 240.0 * col[i], 40.0 + 140.0 * row[i]) for i in ids}

    # ---- edits ----------------------------------------------------------------------

    def _new_id(self, stem: str, taken: set[str], bare: bool = False) -> str:
        if bare and stem not in taken:
            return stem
        n = 1
        while f"{stem}_{n}" in taken:
            n += 1
        return f"{stem}_{n}"

    def _ids(self) -> set[str]:
        return {b.get("id", "") for b in self.blocks} | {f.get("id", "") for f in self.flows}

    def free_position(self) -> tuple[float, float]:
        """First grid slot, six blocks wide, that no block occupies."""
        taken = [self.position(b["id"]) for b in self.blocks]
        for n in range(10_000):
            x, y = 40.0 + 220.0 * (n % 6), 40.0 + 140.0 * (n // 6)
            if all(abs(x - tx) > 150 or abs(y - ty) > 90 for tx, ty in taken):
                return x, y
        raise EditorError("no free position")  # pragma: no cover

    def add_block(
        self,
        kind: str,
        x: float | None = None,
        y: float | None = None,
        block_id: str | None = None,
        name: str | None = None,
    ) -> str:
        if kind not in KINDS:
            raise EditorError(f"unknown block kind {kind!r}")
        block_id = block_id or self._new_id(kind, self._ids())
        if block_id in self._ids():
            raise EditorError(f"id {block_id!r} is already used")
        block = {
            "kind": kind,
            "id": block_id,
            "name": name or f"{KIND_LABELS[kind]} {block_id.rsplit('_', 1)[-1]}",
        }
        block.update(_defaults(kind, str(self.data.get("effective_from", ""))))
        if x is None or y is None:
            x, y = self.free_position()
        self.blocks.append(block)
        self.move(block_id, x, y)
        return block_id

    def remove_block(self, block_id: str) -> None:
        self.block(block_id)
        self.data["blocks"] = [b for b in self.blocks if b.get("id") != block_id]
        self.data["flows"] = [
            f for f in self.flows if block_id not in (f.get("source"), f.get("target"))
        ]
        self.data["layout"].pop(block_id, None)
        for b in self.blocks:
            if b.get("kind") == "consignment_output" and b.get("distribution_block") == block_id:
                b["distribution_block"] = ""

    def rename_block(self, old: str, new: str) -> None:
        """Change a block id and every reference to it."""
        if old == new:
            return
        block = self.block(old)
        if not new or new in self._ids():
            raise EditorError(f"id {new!r} is empty or already used")
        block["id"] = new
        for f in self.flows:
            for end in ("source", "target"):
                if f.get(end) == old:
                    f[end] = new
        for b in self.blocks:
            if b.get("distribution_block") == old:
                b["distribution_block"] = new
        if old in self.data["layout"]:
            self.data["layout"][new] = self.data["layout"].pop(old)

    def update_block(self, block_id: str, **fields: Any) -> None:
        block = self.block(block_id)
        if "kind" in fields and fields["kind"] != block["kind"]:
            raise EditorError("a block's kind cannot change; delete it and add a new one")
        if "id" in fields:
            self.rename_block(block_id, fields.pop("id"))
        block.update(fields)

    def move(self, block_id: str, x: float, y: float) -> None:
        self.block(block_id)
        self.data["layout"][block_id] = {"x": round(float(x), 1), "y": round(float(y), 1)}

    def connect(self, source: str, target: str, flow_id: str | None = None) -> str:
        """Add a flow, inferring the material it carries from its source."""
        src, tgt = self.block(source), self.block(target)
        pair = (src["kind"], tgt["kind"])
        if pair not in CONNECTIONS:
            raise EditorError(
                f"{_a(KIND_LABELS[pair[0]])} cannot flow into {_a(KIND_LABELS[pair[1]])}"
            )
        if any(f.get("source") == source and f.get("target") == target for f in self.flows):
            raise EditorError(f"{source} is already connected to {target}")
        material = self._provided(src)
        flow: dict[str, Any] = {
            "id": flow_id or self._new_id(f"f_{source}_{target}", self._ids(), bare=True),
            "source": source,
            "target": target,
            "material": material,
        }
        if flow["id"] in self._ids():
            raise EditorError(f"id {flow['id']!r} is already used")
        self.flows.append(flow)
        # Fill obvious blanks downstream so the user does not type the same thing twice.
        if tgt["kind"] == "storage_unit" and not tgt.get("product"):
            tgt["product"] = material
        if tgt["kind"] == "consignment_output":
            if not tgt.get("distribution_block"):
                tgt["distribution_block"] = source
            if not tgt.get("customer"):
                tgt["customer"] = src.get("customer", "")
        return str(flow["id"])

    def is_direct(self, flow: dict[str, Any]) -> bool:
        kind = {b.get("id"): b.get("kind") for b in self.blocks}
        return (kind.get(flow.get("source")), kind.get(flow.get("target"))) in {
            ("processing_unit", k) for k in DIRECT_TARGETS
        }

    def is_pass_through(self, block_id: str) -> bool:
        return any(
            b.get("id") == block_id and b.get("kind") == "storage_unit" and b.get("pass_through")
            for b in self.blocks
        )

    def edge_styles(self) -> list[dict[str, Any]]:
        """Colour of each flow (by material) and whether it is drawn dashed (a direct link
        between units, or through a pass-through storage)."""
        materials = sorted({str(f.get("material", "")) for f in self.flows} - {"electricity", ""})
        colour = {m: PALETTE[i % len(PALETTE)] for i, m in enumerate(materials)}
        colour["electricity"] = ELECTRICITY_COLOUR
        return [
            {
                "source": f.get("source"),
                "target": f.get("target"),
                "colour": colour.get(str(f.get("material", "")), "#555555"),
                "dashed": self.is_direct(f)
                or self.is_pass_through(str(f.get("source", "")))
                or self.is_pass_through(str(f.get("target", ""))),
            }
            for f in self.flows
        ]

    def _provided(self, block: dict[str, Any]) -> str:
        if block["kind"] == "input":
            return str(block.get("material", ""))
        if block["kind"] == "distribution":
            feeds = {f.get("material") for f in self.flows if f.get("target") == block["id"]}
            return str(feeds.pop()) if len(feeds) == 1 else ""
        return str(block.get("product", ""))

    def disconnect(self, flow_id: str) -> None:
        self.flow(flow_id)
        self.data["flows"] = [f for f in self.flows if f.get("id") != flow_id]

    def update_flow(self, flow_id: str, **fields: Any) -> None:
        flow = self.flow(flow_id)
        if "id" in fields and fields["id"] != flow_id and fields["id"] in self._ids():
            raise EditorError(f"id {fields['id']!r} is already used")
        flow.update({k: (None if v == "" and k == "series" else v) for k, v in fields.items()})

    def add_row(self, block_id: str, table: str, row: dict[str, Any]) -> int:
        rows = self.block(block_id).setdefault(table, [])
        rows.append(dict(row))
        return len(rows) - 1

    def update_row(self, block_id: str, table: str, index: int, **fields: Any) -> None:
        rows = self.block(block_id).get(table, [])
        if not 0 <= index < len(rows):
            raise EditorError(f"{block_id}.{table} has no row {index}")
        rows[index].update(fields)

    def remove_row(self, block_id: str, table: str, index: int) -> None:
        rows = self.block(block_id).get(table, [])
        if not 0 <= index < len(rows):
            raise EditorError(f"{block_id}.{table} has no row {index}")
        del rows[index]

    def update_settings(self, **fields: Any) -> None:
        self.data.setdefault("settings", {}).update(fields)

    def update_electricity(self, **fields: Any) -> None:
        """The plant's electricity settings (build plan §13); a blank series means grid only."""
        section = self.data.setdefault("electricity", {})
        for k, v in fields.items():
            if v is None or v == "":
                section.pop(k, None)
            else:
                section[k] = v

    def update_plant(self, **fields: Any) -> None:
        unknown = set(fields) - {"plant", "version", "effective_from", "timezone", "settings"}
        if unknown:
            raise EditorError(f"not plant-level fields: {sorted(unknown)}")
        self.data.update(fields)

    # ---- validation -------------------------------------------------------------------

    def issues(self) -> list[Issue]:
        """Schema issues (per field), then cross-block rule issues once the schema holds."""
        try:
            cfg = PlantConfig.model_validate(self.data)
        except ValidationError as exc:
            return [self._schema_issue(e) for e in exc.errors()]
        except ValueError as exc:  # the compact form could not be expanded
            return [Issue("schema", str(exc), None)]
        # Issues on what the loader added belong to what it was added for.
        return [Issue(i.rule, i.message, synthetic_owner(i.ref, self.data)) for i in validate(cfg)]

    def _schema_issue(self, err: Any) -> Issue:
        loc = list(err["loc"])
        ref = None
        if len(loc) >= 2 and loc[0] in ("blocks", "flows") and isinstance(loc[1], int):
            items = self.blocks if loc[0] == "blocks" else self.flows
            if loc[1] < len(items):
                ref = items[loc[1]].get("id")
            loc = loc[2:]
            if loc and loc[0] in KINDS:
                loc = loc[1:]
        field = ".".join(str(p) for p in loc) or "(block)"
        return Issue("schema", f"{field}: {err['msg']}", ref)

    def issues_by_ref(self) -> dict[str | None, list[Issue]]:
        out: dict[str | None, list[Issue]] = {}
        for i in self.issues():
            out.setdefault(i.ref, []).append(i)
        return out
