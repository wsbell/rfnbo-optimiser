"""Phase 17 acceptance, in a real browser: set up a quarter on the Data screen (config, data
upload, column mapping), correct a value with a reason, enter a consignment and a PoS, run, and
open the sealed run in the results browser."""

from __future__ import annotations

import contextlib
import json
import os
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

from rfnbo.trace import run as sealed  # noqa: E402
from tests.e2e.conftest import free_port  # noqa: E402

pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "golden"
expect = playwright.expect


@pytest.fixture(scope="module")
def screen(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path]]:
    project = tmp_path_factory.mktemp("project")
    with _serve(project) as url:
        yield url, project


@contextlib.contextmanager
def _serve(project: Path) -> Iterator[str]:
    """``rfnbo data`` on a project folder; yields its URL."""
    port = free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "rfnbo.cli",
            "data",
            str(project),
            "--port",
            str(port),
            "--no-browser",
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"},
    )
    url = f"http://127.0.0.1:{port}/"
    deadline = time.monotonic() + 60
    try:
        while True:
            try:
                urllib.request.urlopen(url + "data", timeout=1)
                break
            except OSError:
                if proc.poll() is not None or time.monotonic() > deadline:
                    proc.kill()
                    raise RuntimeError(proc.communicate()[0].decode(errors="replace")) from None
                time.sleep(0.2)
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _fill(page: Any, testid: str, value: str) -> None:
    """Fill a field by test id, whether the id sits on the <input> or on its wrapper."""
    field = page.get_by_test_id(testid)
    inner = field.locator("input")
    (inner if inner.count() else field).fill(value)


def _choose(page: Any, testid: str, option: str) -> None:
    page.get_by_test_id(testid).click()
    page.get_by_role("option", name=option, exact=True).click()


def test_set_up_correct_and_run_a_quarter(browser: Any, screen: tuple[str, Path]) -> None:
    url, project = screen
    page = browser.new_page()
    page.goto(url + "data")
    expect(page.get_by_test_id("badge-run")).to_have_text("✗")
    # The plant config is uploaded into the folder, checked and summarised; choosing the
    # quarter sets the project up.
    page.locator('[data-testid="upload-config"] input[type=file]').set_input_files(
        str(GOLDEN / "tc4.plant.json")
    )
    expect(page.get_by_test_id("plant-summary")).to_contain_text("Test plant 4", timeout=30000)
    _choose(page, "quarter", "2026-Q4")
    expect(page.get_by_test_id("message")).to_have_text(
        "plant config tc4.plant.json, quarter 2026-Q4"
    )
    # Its diagram opens on the Plant tab of the same page.
    page.get_by_test_id("edit-plant").click()
    expect(page.get_by_test_id("saved-state")).to_have_text("saved", timeout=30000)
    expect(page.locator(".drawflow-node.bfd-processing_unit").first).to_be_visible(timeout=30000)
    page.get_by_test_id("tab-setup").click()
    expect(page.get_by_test_id("plant-summary")).to_contain_text("Test plant 4")

    workbook = GOLDEN / "workbooks" / "RFNBO_optimiser_test4_purchased_Q4_2026.xlsx"
    page.locator('[data-testid="upload-data"] input[type=file]').set_input_files(str(workbook))
    expect(page.get_by_test_id("message")).to_contain_text("added", timeout=30000)
    expect(page.get_by_test_id("sources")).to_contain_text("Data_15min")
    page.locator('[data-testid="upload-mapping"] input[type=file]').set_input_files(
        str(GOLDEN / "tc4.source.json")
    )
    expect(page.get_by_test_id("message")).to_contain_text("20 series mapped", timeout=30000)

    page.get_by_test_id("tab-mapping").click()
    expect(page.get_by_test_id("mapped-count")).to_have_text("20 of 20 series mapped")

    # On the Plant tab each meter shows the data column it is read from.
    page.get_by_test_id("tab-plant").click()
    page.locator(".drawflow-node", has_text="Liquefier").first.click()
    expect(page.get_by_test_id("field-electricity_series")).to_contain_text(
        "Liquefier electricity (kWh)"
    )
    page.get_by_test_id("tab-preview").click()
    expect(page.get_by_test_id("quality")).to_have_text("No data quality errors", timeout=30000)
    expect(page.get_by_test_id("preview")).to_contain_text("2026-10-01T00:00+02:00")
    expect(page.get_by_test_id("badge-mapping")).to_have_text("20/20")

    # The Run tab says what is missing and will not run until it is there.
    page.get_by_test_id("tab-run").click()
    expect(page.get_by_test_id("run-blocked")).to_contain_text("Consignments")
    expect(page.get_by_test_id("check-consignments")).to_contain_text("add at least one")
    expect(page.get_by_test_id("run")).to_be_disabled()

    page.get_by_test_id("tab-corrections").click()
    _choose(page, "corr-series", "compressor_electricity_kWh")
    _fill(page, "corr-from", "2026-10-02 00:00")
    _fill(page, "corr-to", "2026-10-02 00:30")
    _fill(page, "corr-value", "40")
    page.get_by_test_id("add-correction").click()
    expect(page.get_by_test_id("message")).to_contain_text("reason")  # refused without one
    _fill(page, "corr-reason", "checked against the compressor's own log")
    page.get_by_test_id("add-correction").click()
    expect(page.get_by_test_id("corrections")).to_contain_text("C001")

    page.get_by_test_id("tab-consignments").click()
    _fill(page, "cons-id", "CA")
    _choose(page, "cons-output", "consignment_a")
    _fill(page, "cons-volume", "140000")
    _fill(page, "cons-date", "2026-12-15")
    _fill(page, "cons-target", "5")
    page.get_by_test_id("add-consignment").click()
    expect(page.get_by_test_id("consignment-list")).to_contain_text("consignment_a")
    expect(page.get_by_test_id("badge-consignments")).to_have_text("1")
    # Edited in place, in a dialog holding the record's values.
    page.get_by_test_id("consignment-list-edit-CA").click()
    expect(
        page.locator('input[data-testid="edit-cons-id"], [data-testid="edit-cons-id"] input')
    ).to_have_value("CA")
    _fill(page, "edit-cons-volume", "150000")
    page.get_by_test_id("edit-save").click()
    expect(page.get_by_test_id("consignment-list")).to_contain_text("150,000.000")

    page.get_by_test_id("tab-pos").click()
    for testid, value in (
        ("pos-id", "POS-A"),
        ("pos-supplier", "H2 Trailers BV"),
        ("pos-date", "2026-10-20"),
        ("pos-qty", "8000"),
        ("pos-ghg", "28.3"),
    ):
        _fill(page, testid, value)
    page.get_by_test_id("add-pos").click()
    expect(page.get_by_test_id("message")).to_contain_text("pos_compliant")  # above 28.2
    _fill(page, "pos-ghg", "3")
    page.get_by_test_id("add-pos").click()
    expect(page.get_by_test_id("pos-list")).to_contain_text("POS-A")
    # A row's delete button asks first.
    for testid, value in (
        ("pos-id", "POS-B"),
        ("pos-supplier", "H2 Trailers BV"),
        ("pos-date", "2026-10-21"),
        ("pos-qty", "10"),
        ("pos-ghg", "3"),
    ):
        _fill(page, testid, value)
    page.get_by_test_id("add-pos").click()
    expect(page.get_by_test_id("pos-list")).to_contain_text("POS-B")
    page.get_by_test_id("pos-list-remove-POS-B").click()
    page.get_by_test_id("confirm-remove").click()
    expect(page.get_by_test_id("message")).to_have_text("PoS POS-B removed")
    expect(page.get_by_test_id("pos-list")).not_to_contain_text("POS-B")

    page.get_by_test_id("tab-run").click()
    expect(page.get_by_test_id("badge-run")).to_have_text("ready")
    _fill(page, "run-label", "ui")
    page.get_by_test_id("run").click()
    expect(page.get_by_test_id("run-status")).to_contain_text("Sealed to", timeout=300000)
    expect(page.get_by_test_id("run-status")).to_contain_text("consignments met True")
    page.get_by_test_id("open-run").click()
    expect(page.get_by_test_id("reproduce")).to_have_text("Reproduces byte for byte", timeout=60000)
    expect(page.get_by_test_id("pos")).to_contain_text("POS-A")

    rec = sealed.load_record(project / "runs" / "ui" / "optimised")
    assert [c["reason"] for c in rec["provenance"]["corrections"]] == [
        "checked against the compressor's own log"
    ]
    assert (project / "pos.csv").read_text("utf-8").count("POS-A") == 1
    page.close()


def test_draw_the_plant_and_map_its_meters_on_one_page(browser: Any, tmp_path: Path) -> None:
    """One page: a new plant diagram sets the project up; the data is uploaded; a unit drawn
    on the Plant tab reads its meters from columns picked there, mapped in the project."""
    with _serve(tmp_path) as url:
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.goto(url + "data")
        page.get_by_test_id("new-plant").click()
        expect(page.get_by_test_id("saved-state")).to_have_text("saved", timeout=30000)
        page.get_by_test_id("tab-setup").click()
        workbook = GOLDEN / "workbooks" / "RFNBO_optimiser_test4_purchased_Q4_2026.xlsx"
        page.locator('[data-testid="upload-data"] input[type=file]').set_input_files(str(workbook))
        expect(page.get_by_test_id("sources")).to_contain_text("Data_15min", timeout=30000)

        page.get_by_test_id("tab-plant").click()
        page.get_by_test_id("add-block").click()
        page.get_by_test_id("add-processing_unit").click()
        _choose(page, "field-production_series", "Electrolyser H₂ produced (kg)")
        expect(page.get_by_text("series processing_unit_1_production_kg")).to_be_visible()

        project = json.loads((tmp_path / "project.json").read_text("utf-8"))
        assert project["sources"][0]["columns"] == {
            "processing_unit_1_production_kg": "Electrolyser H₂ produced (kg)"
        }
        plant = json.loads((tmp_path / "plant.json").read_text("utf-8"))
        assert plant["blocks"][0]["production_series"] == "processing_unit_1_production_kg"
        page.close()
