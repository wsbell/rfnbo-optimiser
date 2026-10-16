from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rfnbo.cli import main
from tests.conftest import EXAMPLE


def test_validate_ok(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", str(EXAMPLE)]) == 0
    assert "valid" in capsys.readouterr().out


def test_validate_reports_issues(
    raw: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw["flows"] = [f for f in raw["flows"] if f["id"] != "f_re_el"]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(raw), encoding="utf-8")
    assert main(["validate", str(bad)]) == 1
    assert "unit_feeds [electrolyser]" in capsys.readouterr().err
