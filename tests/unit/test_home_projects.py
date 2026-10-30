"""The projects folder behind the app's home screen."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rfnbo.ui.home import projects as P


def test_default_dir_from_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(P.ENV, str(tmp_path / "cases"))
    assert P.default_dir() == tmp_path / "cases"
    monkeypatch.delenv(P.ENV)
    assert P.default_dir() == Path.home() / "rfnbo-projects"


def test_create_makes_an_empty_project_folder(tmp_path: Path) -> None:
    home = tmp_path / "projects"  # made on first use
    path = P.create_project(home, "  site-a 2026_Q4.v1 ")
    assert path == home / "site-a 2026_Q4.v1" and path.is_dir()
    assert not any(path.iterdir())
    [info] = P.list_projects(home)
    assert (info.name, info.status, info.runs) == ("site-a 2026_Q4.v1", "not set up yet", 0)


@pytest.mark.parametrize("name", ["", "   ", ".hidden", "../escape", "a/b", "a\\b", "-x"])
def test_create_refuses_a_bad_name(tmp_path: Path, name: str) -> None:
    with pytest.raises(P.ProjectsError, match="project name"):
        P.create_project(tmp_path, name)
    assert not any(tmp_path.iterdir())


def test_create_refuses_a_taken_name(tmp_path: Path) -> None:
    P.create_project(tmp_path, "site-a")
    with pytest.raises(P.ProjectsError, match="already exists"):
        P.create_project(tmp_path, "site-a")


def test_list_reads_each_projects_setup_and_runs(tmp_path: Path) -> None:
    b = tmp_path / "B-site"
    (b / "runs" / "2026-Q4-1").mkdir(parents=True)
    (b / "runs" / "2026-Q4-2").mkdir()
    (b / "project.json").write_text(
        json.dumps({"config": "plant.json", "quarter": "2026-Q4"}), "utf-8"
    )
    (tmp_path / "a-site").mkdir()
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "project.json").write_text("{", "utf-8")
    (tmp_path / ".cache").mkdir()  # hidden: not a project
    (tmp_path / "notes.txt").write_text("", "utf-8")  # not a folder
    got = P.list_projects(tmp_path)
    assert [p.name for p in got] == ["a-site", "B-site", "broken"]
    assert (got[1].config, got[1].quarter, got[1].runs, got[1].status) == (
        "plant.json",
        "2026-Q4",
        2,
        "set up",
    )
    assert got[2].status == "project.json unreadable" and got[2].error
    assert P.list_projects(tmp_path / "missing") == []


def test_open_an_existing_folder(tmp_path: Path) -> None:
    assert P.resolve_folder(f" {tmp_path} ") == tmp_path.resolve()
    with pytest.raises(P.ProjectsError, match="no folder"):
        P.resolve_folder(str(tmp_path / "missing"))
    with pytest.raises(P.ProjectsError, match="type the path"):
        P.resolve_folder("")
