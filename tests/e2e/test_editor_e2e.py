"""Phases 2 and 11 acceptance, in a real browser: test plants 1 and 3 can be drawn, saved,
reloaded and pass validation; a unit dragged onto a unit is linked through a pass-through
storage.

Needs the ``ui`` extra and a Chromium for Playwright; skipped otherwise. Set
``RFNBO_CHROMIUM`` to use a specific Chromium binary.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("nicegui")
playwright = pytest.importorskip("playwright.sync_api")

from rfnbo.config import load_config  # noqa: E402
from rfnbo.config.schema import PlantConfig  # noqa: E402
from tests.conftest import EXAMPLE  # noqa: E402
from tests.e2e.conftest import free_port  # noqa: E402

pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parents[2]
expect = playwright.expect


@pytest.fixture
def editor(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """Start ``rfnbo edit`` on a fresh file; yield (url, path)."""
    path = tmp_path / "plant.json"
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "rfnbo.cli", "edit", str(path), "--port", str(port), "--no-browser"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        # NiceGUI switches to its own test mode when it sees pytest's variable.
        env={k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"},
    )
    url = f"http://127.0.0.1:{port}/"
    deadline = time.monotonic() + 60
    try:
        while True:
            try:
                urllib.request.urlopen(url, timeout=1)
                break
            except OSError:
                if proc.poll() is not None or time.monotonic() > deadline:
                    proc.kill()
                    out = proc.communicate()[0].decode(errors="replace")
                    raise RuntimeError(f"editor did not start:\n{out}") from None
                time.sleep(0.2)
        yield url, path
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _open(browser: Any, url: str) -> Any:
    page = browser.new_page(viewport={"width": 1920, "height": 1080})
    page.goto(url)
    page.wait_for_function("window.bfd && window.bfd.editor")
    return page


def _input(page: Any, testid: str) -> Any:
    # Quasar puts the attribute on the <input> itself for text fields.
    return page.locator(f'input[data-testid="{testid}"], [data-testid="{testid}"] input').first


def _fill(page: Any, testid: str, value: Any) -> None:
    box = _input(page, testid)
    box.fill("" if value is None else str(value))
    box.press("Tab")


def _choose(page: Any, testid: str, option: str) -> None:
    page.locator(f'[data-testid="{testid}"]').click()
    page.get_by_role("option", name=option, exact=True).click()


def _check(page: Any, testid: str, on: bool) -> None:
    box = page.locator(f'[data-testid="{testid}"]')
    if (box.get_attribute("aria-checked") == "true") != on:
        box.click()


def _node(page: Any, block_id: str) -> Any:
    return page.locator(".drawflow-node").filter(
        has=page.locator(".bfd-sub", has_text=re.compile(rf"· {re.escape(block_id)}$"))
    )


def _drag(page: Any, a: Any, b: Any) -> None:
    ba, bb = a.bounding_box(), b.bounding_box()
    page.mouse.move(ba["x"] + ba["width"] / 2, ba["y"] + ba["height"] / 2)
    page.mouse.down()
    page.mouse.move(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2, steps=10)
    page.mouse.up()


def _set_fields(page: Any, raw: dict[str, Any], prefix: str = "field-") -> None:
    for key, value in raw.items():
        testid = prefix + key
        if isinstance(value, bool):
            _check(page, testid, value)
        elif key in ("quantity_unit", "unit", "distribution_block"):
            _choose(page, testid, value)
        else:
            _fill(page, testid, value)


def _assert_valid(page: Any) -> None:
    try:
        expect(page.get_by_test_id("issue-summary")).to_have_text("No issues: the diagram is valid")
    except AssertionError:
        issues = page.locator(".text-xs.text-negative").all_inner_texts()
        raise AssertionError(f"editor reports issues: {issues}") from None


def _draw(page: Any, spec: PlantConfig) -> None:
    page.get_by_test_id("plant-settings").click()
    _set_fields(
        page,
        {
            "plant": spec.plant,
            "version": spec.version,
            "effective_from": spec.effective_from.isoformat(),
            "timezone": spec.timezone,
        },
    )
    _set_fields(page, spec.settings.model_dump(mode="json"))

    for b in spec.blocks:
        raw = b.model_dump(mode="json", exclude_none=True)
        kind = raw.pop("kind")
        page.get_by_test_id(f"add-{kind}").click()
        expect(_input(page, "field-id")).to_have_value(re.compile(rf"^{kind}_\d+$"))
        _fill(page, "field-id", raw.pop("id"))
        expect(_input(page, "field-id")).to_have_value(b.id)
        tables = {k: raw.pop(k) for k in ("emission_factors", "opening_ledger") if k in raw}
        # Set the unit before the emission factors that depend on it.
        ordered = sorted(raw.items(), key=lambda kv: kv[0] != "quantity_unit")
        _set_fields(page, dict(ordered))
        for table, rows in tables.items():
            existing = page.locator(f'[data-testid^="{table}-"][data-testid$="-remove"]').count()
            for i, row in enumerate(rows):
                if i >= existing:
                    page.get_by_test_id(f"{table}-add").click()
                    expect(page.get_by_test_id(f"{table}-{i}-remove")).to_be_visible()
                _set_fields(page, row, prefix=f"{table}-{i}-")

    for f in spec.flows:
        _drag(
            page,
            _node(page, f.source).locator(".output_1"),
            _node(page, f.target).locator(".input_1"),
        )
        expect(page.get_by_text(f"Flow {f.source} → {f.target}")).to_be_visible()
        expect(_input(page, "field-material")).to_have_value(f.material)
        _fill(page, "field-id", f.id)
        expect(_input(page, "field-id")).to_have_value(f.id)
        if f.series:
            _fill(page, "field-series", f.series)


PLANT_3 = ROOT / "tests" / "golden" / "tc3.plant.json"


@pytest.mark.parametrize("plant", [EXAMPLE, PLANT_3], ids=["test_plant", "test_plant_3"])
def test_test_plant_drawn_saved_reloaded_and_valid(
    browser: Any, editor: tuple[str, Path], plant: Path
) -> None:
    """Phases 2 and 11: test plants 1 and 3 (split output, pass-through, parallel units)."""
    url, path = editor
    spec = load_config(plant)
    page = _open(browser, url)
    _draw(page, spec)
    _assert_valid(page)
    page.get_by_test_id("save").click()
    expect(page.get_by_text("Saved plant.json: valid")).to_be_visible()

    saved = load_config(path)  # raises if invalid
    assert saved.model_dump(exclude={"layout"}) == spec.model_dump(exclude={"layout"})
    assert set(saved.layout) == {b.id for b in spec.blocks}

    page.reload()
    page.wait_for_function("window.bfd && window.bfd.editor")
    expect(page.locator(".drawflow-node")).to_have_count(len(spec.blocks))
    expect(page.locator(".drawflow svg.connection")).to_have_count(len(spec.flows))
    expect(page.get_by_test_id("issue-summary")).to_have_text("No issues: the diagram is valid")
    page.close()


def test_editing_an_existing_plant(browser: Any, editor: tuple[str, Path]) -> None:
    url, path = editor
    shutil.copy(EXAMPLE, path)
    page = _open(browser, url)
    expect(page.locator(".drawflow-node")).to_have_count(13)

    # A disallowed connection is refused and not drawn.
    _drag(
        page,
        _node(page, "water").locator(".output_1"),
        _node(page, "h2_storage").locator(".input_1"),
    )
    expect(page.get_by_text("an input cannot flow into a storage unit")).to_be_visible()
    expect(page.locator(".drawflow svg.connection")).to_have_count(13)

    # Moving a block is saved as layout.
    node = _node(page, "water")
    box = node.bounding_box()
    page.mouse.move(box["x"] + 30, box["y"] + 10)
    page.mouse.down()
    page.mouse.move(box["x"] + 30, box["y"] + 160, steps=8)
    page.mouse.up()

    # Breaking the plant shows issues, marks the block and points at it.
    _node(page, "electrolyser").click()
    _fill(page, "field-lhv_MJ_per_kg", "0")
    expect(page.get_by_test_id("issue-summary")).to_have_text("1 issue(s)")
    expect(_node(page, "electrolyser")).to_have_class(re.compile("bfd-bad"))
    _fill(page, "field-lhv_MJ_per_kg", "120")
    expect(page.get_by_test_id("issue-summary")).to_have_text("No issues: the diagram is valid")

    # Deleting a block removes its flows; the remaining plant reports what is now missing.
    _node(page, "dist_spot").click()
    page.get_by_test_id("delete-block").click()
    expect(page.locator(".drawflow-node")).to_have_count(12)
    expect(page.locator(".drawflow svg.connection")).to_have_count(11)
    page.get_by_test_id("save").click()
    expect(page.get_by_text(re.compile(r"Saved plant\.json: \d+ issue"))).to_be_visible()

    # A draft with issues is still saved, so work is never lost.
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "dist_spot" not in {b["id"] for b in saved["blocks"]}
    spot = next(b for b in saved["blocks"] if b["id"] == "spot")
    assert spot["distribution_block"] == ""
    assert saved["layout"]["water"]["y"] > load_config(EXAMPLE).layout["water"].y + 100
    page.close()


def test_unit_to_unit_link_adds_a_pass_through_storage(
    browser: Any, editor: tuple[str, Path]
) -> None:
    url, path = editor
    page = _open(browser, url)
    for block_id, product in (("electrolyser", "H2"), ("compressor", "CH2")):
        page.get_by_test_id("add-processing_unit").click()
        expect(_input(page, "field-id")).to_have_value(re.compile(r"^processing_unit_\d+$"))
        _fill(page, "field-id", block_id)
        expect(_input(page, "field-id")).to_have_value(block_id)
        _fill(page, "field-product", product)
    _drag(
        page,
        _node(page, "electrolyser").locator(".output_1"),
        _node(page, "compressor").locator(".input_1"),
    )
    expect(
        page.get_by_text("through pass-through storage pt_electrolyser_compressor")
    ).to_be_visible()
    expect(page.locator(".drawflow-node")).to_have_count(3)
    expect(page.locator(".drawflow svg.connection")).to_have_count(2)
    expect(page.locator(".drawflow svg.connection.bfd-dashed")).to_have_count(2)
    expect(_node(page, "pt_electrolyser_compressor")).to_have_class(re.compile("bfd-pass_through"))
    page.get_by_test_id("save").click()
    expect(page.get_by_text(re.compile(r"Saved plant\.json"))).to_be_visible()
    saved = json.loads(path.read_text(encoding="utf-8"))
    pt = next(b for b in saved["blocks"] if b["id"] == "pt_electrolyser_compressor")
    assert pt["pass_through"] is True and pt["product"] == "H2"
    assert {(f["source"], f["target"]) for f in saved["flows"]} == {
        ("electrolyser", "pt_electrolyser_compressor"),
        ("pt_electrolyser_compressor", "compressor"),
    }
    page.close()
