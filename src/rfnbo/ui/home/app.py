"""The app (``rfnbo``): a home screen listing the projects folder, where a case is started from
scratch or opened, then the Data screen, diagram editor and results browser on that project.

All the folder's state lives in :mod:`rfnbo.ui.home.projects`; this module lays it out. The app
runs locally for one person, so the open project is one per app, not per browser tab.
"""

from __future__ import annotations

from pathlib import Path

from nicegui import ui

from rfnbo.ui import theme
from rfnbo.ui.data import app as data
from rfnbo.ui.home import projects as P

_HOME: Path = P.default_dir()
_CURRENT: Path | None = None


def _open(path: Path) -> None:
    global _CURRENT
    _CURRENT = path
    ui.navigate.to("/data")


def home_page() -> None:
    theme.apply()
    theme.top_bar([("Projects", None)])
    with theme.page():
        theme.title("Projects", str(_HOME))

        def create() -> None:
            try:
                path = P.create_project(_HOME, name.value or "")
            except (P.ProjectsError, OSError) as exc:
                ui.notify(str(exc), type="negative", position="top")
                return
            _open(path)

        with (
            theme.card(
                "New project",
                "An empty project folder: on the Data screen, draw the plant or upload its config, "
                "choose the quarter, add the data and run.",
            ),
            ui.row().classes("items-end gap-2"),
        ):
            name = ui.input("Project name", placeholder="e.g. site-a-2026-q4").props(
                'data-testid="new-name"'
            )
            name.on("keydown.enter", create)
            ui.button("Create", on_click=create).props('data-testid="create"')

        projects = P.list_projects(_HOME)
        with theme.card("Your projects", f"The folders in {_HOME}"):
            if not projects:
                ui.label("No projects yet: create one above.").classes("rf-note")
            for p in projects:
                with (
                    ui.row()
                    .classes("items-center gap-3 w-full")
                    .props(f'data-testid="project-{p.name}"')
                ):
                    ui.button(p.name, on_click=lambda p=p: _open(p.path)).props(
                        f'flat data-testid="open-{p.name}"'
                    )
                    tone = "danger" if p.error else ("success" if p.config else "caution")
                    theme.pill(p.status, tone)
                    details = [p.config, p.quarter]
                    if p.runs:
                        details.append(f"{p.runs} run{'s' if p.runs > 1 else ''}")
                    ui.label(" · ".join(d for d in details if d) or p.error).classes("rf-sub")

        def open_folder() -> None:
            try:
                path = P.resolve_folder(folder.value or "")
            except P.ProjectsError as exc:
                ui.notify(str(exc), type="negative", position="top")
                return
            _open(path)

        with (
            theme.card("Open a folder", "A project folder anywhere else, e.g. my_project"),
            ui.row().classes("items-end gap-2"),
        ):
            folder = ui.input("Folder").props('data-testid="open-path"').classes("min-w-[360px]")
            folder.on("keydown.enter", open_folder)
            theme.secondary(ui.button("Open", on_click=open_folder)).props(
                'data-testid="open-folder"'
            )


def run(home: Path | None = None, port: int = 8090, show: bool = True) -> None:
    global _HOME
    _HOME = (home or P.default_dir()).expanduser()
    _HOME.mkdir(parents=True, exist_ok=True)
    theme.setup()
    ui.page("/")(home_page)
    # The Data screen and diagram editor, and with them the results browser (/runs), on the
    # open project.
    data.register(lambda: _CURRENT, home="/")
    ui.run(
        port=port,
        show=show,
        reload=False,
        title="RFNBO optimiser",
        show_welcome_message=False,
    )
