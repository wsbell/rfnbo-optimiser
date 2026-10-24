"""The editor's document model, without any UI toolkit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rfnbo.config import load_config
from rfnbo.config.schema import (
    ConsignmentOutput,
    DistributionBlock,
    EmissionFactor,
    Flow,
    InputBlock,
    OpeningLedgerEntry,
    PlantConfig,
    ProcessingUnit,
    RfnboSupply,
    Settings,
    StorageUnit,
)
from rfnbo.ui.bfd_editor.document import EditorDocument, EditorError
from rfnbo.ui.bfd_editor.fields import (
    BLOCK_FIELDS,
    FLOW_FIELDS,
    PLANT_FIELDS,
    SETTINGS_FIELDS,
    TableSpec,
)
from tests.conftest import EXAMPLE


def test_example_round_trips_byte_for_byte() -> None:
    text = EXAMPLE.read_text(encoding="utf-8")
    doc = EditorDocument.load(EXAMPLE)
    assert doc.issues() == []
    assert doc.to_json() == text


def draw(spec: PlantConfig) -> EditorDocument:
    """Build a plant through editor operations only, as a user drawing it would."""
    doc = EditorDocument.new(plant="draft")
    doc.update_plant(
        plant=spec.plant,
        version=spec.version,
        effective_from=spec.effective_from.isoformat(),
        timezone=spec.timezone,
    )
    doc.update_settings(**spec.settings.model_dump(mode="json"))
    for b in spec.blocks:
        raw = b.model_dump(mode="json")
        block_id = doc.add_block(raw.pop("kind"))
        doc.rename_block(block_id, raw.pop("id"))
        tables = {k: raw.pop(k) for k in ("emission_factors", "opening_ledger") if k in raw}
        doc.update_block(b.id, **raw)
        for table, rows in tables.items():
            for i in reversed(range(len(doc.block(b.id).get(table, [])))):
                doc.remove_row(b.id, table, i)
            for row in rows:
                doc.add_row(b.id, table, row)
    for f in spec.flows:
        flow_id = doc.connect(f.source, f.target)
        doc.update_flow(flow_id, id=f.id, material=f.material, series=f.series)
    for block_id, pos in spec.layout.items():
        doc.move(block_id, pos.x, pos.y)
    return doc


def test_test_plant_can_be_drawn_saved_and_reloaded(tmp_path: Path) -> None:
    spec = load_config(EXAMPLE)
    doc = draw(spec)
    assert doc.issues() == []
    out = tmp_path / "drawn.json"
    assert doc.save(out) == []
    assert load_config(out) == spec
    assert out.read_text(encoding="utf-8") == EXAMPLE.read_text(encoding="utf-8")


PLANT_3 = Path(__file__).resolve().parents[1] / "golden" / "tc3.plant.json"


def test_multi_step_plant_can_be_drawn_saved_and_reloaded(tmp_path: Path) -> None:
    """Phase 11: test plant 3 (split output, pass-through link, parallel units)."""
    spec = load_config(PLANT_3)
    doc = draw(spec)
    assert doc.issues() == []
    out = tmp_path / "drawn.json"
    assert doc.save(out) == []
    again = load_config(out)
    assert again.model_dump(exclude={"layout"}) == spec.model_dump(exclude={"layout"})
    assert EditorDocument.load(out).issues() == []


def test_plant_with_a_supplier_can_be_drawn_saved_and_reloaded(tmp_path: Path) -> None:
    """Phase 16: test plant 4 (an RFNBO supplier delivering into two units)."""
    spec = load_config(PLANT_3.with_name("tc4.plant.json"))
    doc = draw(spec)
    assert doc.issues() == []
    out = tmp_path / "drawn.json"
    assert doc.save(out) == []
    assert load_config(out).model_dump(exclude={"layout"}) == spec.model_dump(exclude={"layout"})
    sup = doc.add_block("rfnbo_supply", block_id="h2_supplier_2")
    doc.update_block(sup, supplier="Other", product="H2", lhv_MJ_per_kg=120.0)
    flow = doc.connect(sup, "h2_buffer")
    assert doc.flow(flow)["material"] == "H2"
    assert {i.rule for i in doc.issues()} == {"supply_flows"}  # the delivery needs a meter
    doc.update_flow(flow, series="supplier_2_to_buffer_kg")
    assert doc.issues() == []
    with pytest.raises(EditorError, match="cannot flow into"):
        doc.connect(sup, "dist_customer_a")


def test_unit_to_unit_link_inserts_a_pass_through_storage() -> None:
    doc = EditorDocument.new()
    el = doc.add_block("processing_unit", block_id="electrolyser")
    doc.update_block(el, product="H2")
    comp = doc.add_block("processing_unit", block_id="compressor")
    doc.update_block(comp, product="CH2")
    pt = doc.link_units(el, comp)
    assert pt == "pt_electrolyser_compressor" and doc.is_pass_through(pt)
    b = doc.block(pt)
    assert b["product"] == "H2" and b["inventory_series"] is None
    assert b["opening_inventory_kg"] == 0 and b["opening_ledger"] == []
    assert [(f["source"], f["target"], f["material"]) for f in doc.flows] == [
        (el, pt, "H2"),
        (pt, comp, "H2"),
    ]
    assert all(s["dashed"] for s in doc.edge_styles())
    with pytest.raises(EditorError, match="already linked"):
        doc.link_units(el, comp)
    with pytest.raises(EditorError, match="only two processing units"):
        doc.link_units(el, pt)


def test_flows_are_coloured_by_product() -> None:
    doc = EditorDocument.load(PLANT_3)
    styles = {(s["source"], s["target"]): s for s in doc.edge_styles()}
    assert styles["renewable_power", "electrolyser"]["colour"] == "#9e9e9e"
    h2 = styles["electrolyser", "h2_buffer"]["colour"]
    assert styles["h2_buffer", "liquefier"]["colour"] == h2
    assert styles["liquefier", "lh2_storage"]["colour"] != h2
    assert styles["electrolyser", "pt_compressor"]["dashed"]
    assert not styles["electrolyser", "h2_buffer"]["dashed"]


def test_connect_infers_material_and_fills_blanks() -> None:
    doc = EditorDocument.new()
    unit = doc.add_block("processing_unit")
    doc.update_block(unit, product="H2")
    store = doc.add_block("storage_unit")
    flow = doc.connect(unit, store)
    assert doc.flow(flow)["material"] == "H2" and doc.block(store)["product"] == "H2"

    dist = doc.add_block("distribution")
    doc.update_block(dist, customer="Customer A")
    doc.connect(store, dist)
    out = doc.add_block("consignment_output")
    doc.connect(dist, out)
    assert doc.block(out)["distribution_block"] == dist
    assert doc.block(out)["customer"] == "Customer A"
    assert doc.flows[-1]["material"] == "H2"


@pytest.mark.parametrize(
    ("src", "tgt"),
    [
        ("input", "storage_unit"),
        ("processing_unit", "processing_unit"),
        ("consignment_output", "distribution"),
    ],
)
def test_disallowed_connections_are_refused(src: str, tgt: str) -> None:
    doc = EditorDocument.new()
    a, b = doc.add_block(src), doc.add_block(tgt)
    with pytest.raises(EditorError, match="cannot flow into"):
        doc.connect(a, b)
    assert doc.flows == []


def test_duplicate_connection_is_refused() -> None:
    doc = EditorDocument.load(EXAMPLE)
    with pytest.raises(EditorError, match="already connected"):
        doc.connect("water", "electrolyser")


def test_rename_updates_every_reference() -> None:
    doc = EditorDocument.load(EXAMPLE)
    doc.rename_block("dist_customer_a", "dist_a")
    assert doc.block("consignment_a")["distribution_block"] == "dist_a"
    assert {f["source"] for f in doc.flows if f["target"] == "consignment_a"} == {"dist_a"}
    assert "dist_a" in doc.data["layout"] and "dist_customer_a" not in doc.data["layout"]
    assert doc.issues() == []
    with pytest.raises(EditorError, match="already used"):
        doc.rename_block("dist_a", "water")


def test_remove_block_removes_its_flows_and_links() -> None:
    doc = EditorDocument.load(EXAMPLE)
    doc.remove_block("dist_customer_a")
    assert all("dist_customer_a" not in (f["source"], f["target"]) for f in doc.flows)
    assert doc.block("consignment_a")["distribution_block"] == ""
    rules = {i.rule for i in doc.issues()}
    assert rules == {"schema"}  # the consignment output now names no distribution block


def test_schema_issues_point_at_their_block() -> None:
    doc = EditorDocument.load(EXAMPLE)
    doc.update_block("electrolyser", lhv_MJ_per_kg=0)
    doc.block("water")["emission_factors"][0]["source"] = ""
    by_ref = doc.issues_by_ref()
    assert [i.message for i in by_ref["electrolyser"]] == [
        "lhv_MJ_per_kg: Input should be greater than 0"
    ]
    assert by_ref["water"][0].message.startswith("emission_factors.0.source")


def test_rule_issues_once_schema_holds() -> None:
    doc = EditorDocument.load(EXAMPLE)
    flow = next(f["id"] for f in doc.flows if f["source"] == "renewable_power")
    doc.disconnect(flow)
    assert [(i.rule, i.ref) for i in doc.issues()] == [
        ("no_orphans", "renewable_power"),
        ("unit_feeds", "electrolyser"),
    ]


def test_new_blocks_do_not_overlap() -> None:
    doc = EditorDocument.new()
    ids = [doc.add_block("input") for _ in range(14)]
    spots = [doc.position(i) for i in ids]
    assert len(set(spots)) == len(spots)


def test_config_without_layout_gets_an_automatic_one() -> None:
    raw = load_config(EXAMPLE).model_dump(mode="json", exclude={"layout"})
    doc = EditorDocument(raw)
    assert (
        doc.position("renewable_power")[0]
        < doc.position("electrolyser")[0]
        < doc.position("h2_storage")[0]
        < doc.position("nh3_synthesis")[0]
    )
    assert len({doc.position(b["id"]) for b in doc.blocks}) == len(doc.blocks)


def test_invalid_draft_is_saved_as_is(tmp_path: Path) -> None:
    doc = EditorDocument.new()
    doc.add_block("input")
    issues = doc.save(tmp_path / "draft.json")
    assert issues
    again = EditorDocument.load(tmp_path / "draft.json")
    assert again.data == doc.data


def test_kind_cannot_change() -> None:
    doc = EditorDocument.load(EXAMPLE)
    with pytest.raises(EditorError, match="kind"):
        doc.update_block("water", kind="storage_unit")


MODELS: dict[str, Any] = {
    "input": InputBlock,
    "processing_unit": ProcessingUnit,
    "storage_unit": StorageUnit,
    "rfnbo_supply": RfnboSupply,
    "distribution": DistributionBlock,
    "consignment_output": ConsignmentOutput,
}


@pytest.mark.parametrize("kind", sorted(MODELS))
def test_every_schema_field_has_an_editor(kind: str) -> None:
    specs = BLOCK_FIELDS[kind]
    assert {s.key for s in specs} == set(MODELS[kind].model_fields) - {"kind", "id", "name"}
    for s in specs:
        if isinstance(s, TableSpec):
            row_model = EmissionFactor if s.key == "emission_factors" else OpeningLedgerEntry
            assert {c.key for c in s.columns} == set(row_model.model_fields)


def test_flow_plant_and_settings_fields_have_editors() -> None:
    assert {s.key for s in FLOW_FIELDS} == set(Flow.model_fields) - {"id", "source", "target"}
    assert {s.key for s in PLANT_FIELDS} == set(PlantConfig.model_fields) - {
        "schema_version",
        "settings",
        "blocks",
        "flows",
        "layout",
    }
    assert {s.key for s in SETTINGS_FIELDS} == set(Settings.model_fields)


def test_dirty_until_saved(tmp_path: Path) -> None:
    """The editor marks unsaved changes and warns before they are lost."""
    doc = EditorDocument.new()
    assert not doc.dirty  # nothing drawn yet
    bid = doc.add_block("input")
    assert doc.dirty
    path = tmp_path / "plant.json"
    doc.save(path)
    assert not doc.dirty
    doc.move(bid, 10, 20)  # layout counts: it is saved in the file
    assert doc.dirty
    doc.save(path)
    loaded = EditorDocument.load(path)
    assert not loaded.dirty
    loaded.update_block(bid, name="Grid electricity")
    assert loaded.dirty
    # A diagram opened from an upload is not in the file Save writes to.
    assert EditorDocument.from_json(path.read_text("utf-8")).dirty


def test_automatic_layout_runs_in_lanes() -> None:
    """Test plant 4 laid out as drawn in the design mockup: columns by flow, the LH2 route on
    the top lane, spot sales in the middle, the CH2 route below, inputs left of x = 0."""
    golden = Path(__file__).resolve().parents[1] / "golden" / "tc4.plant.json"
    doc = EditorDocument.load(golden)
    at = doc.auto_layout()
    lanes = {}
    for block, (x, y) in at.items():
        lanes.setdefault(y, []).append((x, block))
    top, middle, bottom = ([b for _, b in sorted(lanes[y])] for y in sorted(lanes))
    assert top == [
        "renewable_power", "h2_buffer", "liquefier", "lh2_storage", "dist_customer_a",
        "consignment_a",
    ]  # fmt: skip
    assert middle == ["grid_power", "electrolyser", "h2_supplier", "dist_spot", "spot"]
    assert bottom == [
        "water", "pt_compressor", "compressor", "ch2_storage", "dist_customer_b",
        "consignment_b",
    ]  # fmt: skip
    assert all(x < 0 for b, (x, _) in at.items() if doc.block(b)["kind"] == "input")
    assert at["h2_buffer"][0] == at["h2_supplier"][0]  # a supplier sits beside what it feeds


def test_ordered_rows_take_the_nearest_free_rows() -> None:
    from rfnbo.ui.bfd_editor.document import _ordered_rows

    assert _ordered_rows([0.33, 1.67], 3) == [0, 2]
    assert _ordered_rows([1.0], 3) == [1]
    assert _ordered_rows([2.0, 2.0, 2.0], 3) == [0, 1, 2]
