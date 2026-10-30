"""The projects folder behind the home screen (no UI imports).

Each subfolder is one project folder (:mod:`rfnbo.project.folder`). A new project is an empty
folder; the Data screen sets it up (plant config, quarter, data) and saves ``project.json``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from rfnbo.project import folder as F

ENV = "RFNBO_PROJECTS"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.-]*")


class ProjectsError(ValueError):
    pass


def default_dir() -> Path:
    """Where projects live unless ``--dir`` says otherwise: ``$RFNBO_PROJECTS``, else
    ``~/rfnbo-projects``."""
    env = os.environ.get(ENV)
    return Path(env).expanduser() if env else Path.home() / "rfnbo-projects"


@dataclass(frozen=True)
class ProjectInfo:
    name: str
    path: Path
    config: str  # plant config file, "" until the project is set up
    quarter: str
    runs: int  # sealed run folders under runs/
    error: str = ""  # project.json could not be read

    @property
    def status(self) -> str:
        if self.error:
            return "project.json unreadable"
        return "set up" if self.config else "not set up yet"


def info(path: Path) -> ProjectInfo:
    runs_dir = path / "runs"
    runs = sum(1 for p in runs_dir.iterdir() if p.is_dir()) if runs_dir.is_dir() else 0
    pf = path / F.PROJECT_FILE
    if not pf.exists():
        return ProjectInfo(path.name, path, "", "", runs)
    try:
        file = F.ProjectFile.model_validate_json(pf.read_text("utf-8"))
    except (OSError, ValueError, ValidationError) as exc:
        return ProjectInfo(path.name, path, "", "", runs, error=str(exc).splitlines()[0])
    return ProjectInfo(path.name, path, file.config, file.quarter, runs)


def list_projects(home: Path) -> list[ProjectInfo]:
    """The project folders in ``home``, by name; hidden folders are left out."""
    if not home.is_dir():
        return []
    dirs = [p for p in home.iterdir() if p.is_dir() and not p.name.startswith(".")]
    return [info(p) for p in sorted(dirs, key=lambda p: p.name.lower())]


def create_project(home: Path, name: str) -> Path:
    """Make an empty project folder ``home/name``; refuse a bad or taken name."""
    name = name.strip()
    if not NAME.fullmatch(name):
        raise ProjectsError(
            "a project name starts with a letter or digit and holds only letters, digits, "
            "spaces, '_', '-' and '.'"
        )
    path = home / name
    if path.exists():
        raise ProjectsError(f"a project called {name!r} already exists in {home}")
    path.mkdir(parents=True)
    return path


def resolve_folder(text: str) -> Path:
    """An existing folder typed on the home screen, to open as a project."""
    text = text.strip()
    if not text:
        raise ProjectsError("type the path of a project folder")
    path = Path(text).expanduser()
    if not path.is_dir():
        raise ProjectsError(f"no folder {path}")
    return path.resolve()
