"""Phases 9 and 15 acceptance, in a real browser: a user traces a consignment to 15-minute
data, through every processing step on a multi-step plant."""

from __future__ import annotations

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
from tests.golden.test_golden import _run  # noqa: E402
from tests.golden.test_pipeline_network_golden import _optimised  # noqa: E402
from tests.golden.test_tc3_calc import _inputs  # noqa: E402
from tests.golden.test_tc4_calc import _run as _tc4_run  # noqa: E402

pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parents[2]
expect = playwright.expect


@pytest.fixture(scope="module")
def results(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, str, str, str]]:
    """Seal test case 1, an optimised test case 3 and test case 4 (purchased RFNBO) and serve
    ``rfnbo results`` on them; yield (url, test case 1, 3 and 4 run ids)."""
    root = tmp_path_factory.mktemp("runs")
    run, _, flags = _run("tc1")
    run_id = sealed.load_record(sealed.seal(run, root / "tc1", flags))["run_id"]
    # Test plant 3 as the optimiser makes it: two products, split output, pass-through.
    tc3 = _optimised("base").optimised.run
    tc3_id = sealed.load_record(sealed.seal(tc3, root / "tc3", _inputs()[2]))["run_id"]
    tc4 = _tc4_run()
    tc4_id = sealed.load_record(sealed.seal(tc4, root / "tc4"))["run_id"]
    port = free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "rfnbo.cli",
            "results",
            str(root),
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
                urllib.request.urlopen(url, timeout=1)
                break
            except OSError:
                if proc.poll() is not None or time.monotonic() > deadline:
                    proc.kill()
                    raise RuntimeError(proc.communicate()[0].decode(errors="replace")) from None
                time.sleep(0.2)
        yield url, run_id, tc3_id, tc4_id
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _link(page: Any, name: str) -> Any:
    return page.locator(f'[data-testid="link-{name}"]').first


def test_trace_a_consignment_to_15_minute_data(
    browser: Any, results: tuple[str, str, str, str]
) -> None:
    url, run_id, _, _ = results
    page = browser.new_page(viewport={"width": 1500, "height": 1000})
    page.goto(url)
    _link(page, run_id).click()
    expect(page.get_by_test_id("reproduce")).to_have_text("Reproduces byte for byte", timeout=30000)

    _link(page, "C1").click()
    expect(page.get_by_text("Consignment C1 · Customer A")).to_be_visible()
    # §12: October NH₃ (14.09) sets C1's GHG; it is no longer averaged with September's.
    expect(page.get_by_test_id("cons-shipped")).to_have_text("17.092473")

    _link(page, "N-2026-10").click()
    expect(page.get_by_text("N-2026-10 · NH3 synthesis")).to_be_visible()
    expect(page.get_by_test_id("recompute")).to_contain_text("✓")

    _link(page, "E-2026-10-01").click()
    expect(page.get_by_text("E-2026-10-01 · Electrolyser")).to_be_visible()
    page.get_by_test_id("intervals-link").click()
    expect(page.get_by_test_id("interval-count")).to_have_text("E-2026-10-01: 2,980 intervals")
    expect(page.get_by_test_id("totals-check")).to_have_text(
        "✓ Interval totals equal the batch record"
    )
    first = page.locator('[data-testid="intervals"] tbody tr').first
    expect(first).to_contain_text("2026-10-01T00:00+02:00")
    expect(first).to_contain_text("3,000.000")

    # Back up the breadcrumb to the run, then into the opening entry.
    page.get_by_test_id("crumb-1").click()
    _link(page, "C1").click()
    _link(page, "N-2026-09").click()
    expect(page.get_by_test_id("opening-note")).to_contain_text("no interval data")
    page.close()


def test_download_the_excel_report(
    browser: Any, results: tuple[str, str, str, str], tmp_path: Path
) -> None:
    url, run_id, _, _ = results
    page = browser.new_page()
    page.goto(f"{url}run/{run_id}")
    expect(page.get_by_test_id("reproduce")).to_be_visible(timeout=30000)
    with page.expect_download() as dl:
        page.get_by_test_id("download-report").click()
    path = tmp_path / dl.value.suggested_filename
    dl.value.save_as(path)
    assert path.name == f"report_{run_id}.xlsx" and path.stat().st_size > 10000
    page.close()


def test_trace_a_multi_step_consignment_to_15_minute_data(
    browser: Any, results: tuple[str, str, str, str]
) -> None:
    """Test plant 3: CA (LH₂) → liquefier batch → electrolyser batch (split output) → data."""
    url, _, tc3_id, _ = results
    page = browser.new_page(viewport={"width": 1500, "height": 1000})
    page.goto(f"{url}run/{tc3_id}")
    expect(page.get_by_test_id("reproduce")).to_have_text("Reproduces byte for byte", timeout=30000)
    expect(page.get_by_test_id("products")).to_contain_text("pass-through")

    _link(page, "CA").click()
    expect(page.get_by_text("Consignment CA · Customer A · LH2")).to_be_visible()
    _link(page, "liquefier-2026-11").click()
    expect(page.get_by_test_id("upstream")).to_contain_text("h2_buffer")
    expect(page.get_by_test_id("recompute")).to_contain_text("✓")

    _link(page, "electrolyser-2026-11-001").click()
    expect(page.get_by_test_id("outputs")).to_contain_text("pt_compressor")
    page.get_by_test_id("intervals-link").click()
    expect(page.get_by_test_id("interval-count")).to_have_text(
        "electrolyser-2026-11-001: 2,880 intervals"
    )
    expect(page.get_by_test_id("totals-check")).to_have_text(
        "✓ Interval totals equal the batch record"
    )
    page.close()


def test_trace_a_consignment_to_the_pos_that_fed_it(
    browser: Any, results: tuple[str, str, str, str]
) -> None:
    """Phase 16: the PoS register on the run page, and a liquefier batch drilled down to the
    proof of sustainability whose credentials make one of its parts (§12; CA ships only the
    plant's own LH₂, the purchased part being above its limit)."""
    url, _, _, tc4_id = results
    page = browser.new_page()
    page.goto(f"{url}run/{tc4_id}")
    expect(page.get_by_test_id("reproduce")).to_have_text("Reproduces byte for byte", timeout=30000)
    register = page.get_by_test_id("pos")
    expect(register).to_contain_text("POS-C")
    expect(register).to_contain_text("6,000")
    page.goto(f"{url}run/{tc4_id}/batch/L-2026-12")
    expect(page.get_by_test_id("upstream")).to_contain_text("f_sup_liq")
    _link(page, "POS-B").click()
    expect(page.get_by_text("POS-B: proof of sustainability from H2 Trailers BV")).to_be_visible()
    expect(page.get_by_test_id("pos-allocations")).to_contain_text("L-2026-12")
    _link(page, "L-2026-12").click()
    expect(page.get_by_text("L-2026-12 · Liquefier")).to_be_visible()
    page.close()
