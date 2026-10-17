"""Shared browser-test helpers: a Chromium for Playwright and free local ports."""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest


def _chromium() -> str | None:
    env = os.environ.get("RFNBO_CHROMIUM")
    if env:
        return env
    local = Path("/opt/pw-browsers/chromium")
    return str(local) if local.exists() else None


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        try:
            b = p.chromium.launch(executable_path=_chromium())
        except playwright.Error as exc:
            pytest.skip(f"no Chromium for Playwright: {exc}")
        yield b
        b.close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port
