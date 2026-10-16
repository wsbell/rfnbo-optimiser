from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "test_plant.json"


@pytest.fixture(scope="session")
def _example_raw() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    return data


@pytest.fixture
def raw(_example_raw: dict[str, Any]) -> dict[str, Any]:
    """A fresh, mutable copy of the example plant config."""
    return copy.deepcopy(_example_raw)
