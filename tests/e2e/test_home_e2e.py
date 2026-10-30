"""Browser test for the app (``rfnbo app``): start a case from scratch on the home screen."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("nicegui")
playwright = pytest.importorskip("playwright.sync_api")

from tests.e2e.conftest import free_port  # noqa: E402
from tests.e2e.test_data_e2e import _fill  # noqa: E402

pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parents[2]
expect = playwright.expect


def test_create_a_project_and_draw_its_plant(browser: Any, tmp_path: Path) -> None:
    home = tmp_path / "projects"
    port = free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "rfnbo.cli",
            "app",
            "--dir",
            str(home),
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

        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        # With no project open, the Data screen goes to the home screen.
        page.goto(url + "data")
        expect(page.get_by_text("No projects yet: create one above.")).to_be_visible()
        _fill(page, "new-name", "../bad")
        page.get_by_test_id("create").click()
        expect(page.get_by_text("a project name starts with")).to_be_visible()

        _fill(page, "new-name", "site-a")
        page.get_by_test_id("create").click()
        # The new, empty project opens on the Data screen, where the plant is drawn.
        expect(page.get_by_test_id("crumb-0")).to_have_text("Projects", timeout=30000)
        page.get_by_test_id("new-plant").click()
        expect(page.get_by_test_id("saved-state")).to_have_text("saved", timeout=30000)
        assert (home / "site-a" / "project.json").exists()
        assert (home / "site-a" / "plant.json").exists()

        # Back home, the project is listed as set up, and opens again.
        page.get_by_test_id("crumb-0").click()
        expect(page.get_by_test_id("project-site-a")).to_contain_text("set up")
        page.get_by_test_id("open-site-a").click()
        crumbs = page.get_by_role("navigation", name="Breadcrumb")
        expect(crumbs).to_have_text("Projects/site-a", timeout=30000)
        expect(page.get_by_test_id("tab-plant")).to_contain_text("✓")
        page.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
