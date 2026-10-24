"""The app's look, shared by the Data screen, the diagram editor and the results browser:
colour tokens, type, and the styles of cards, tables, buttons, inputs and pills.

Calm and ledger-like: a near-white ground, deep navy ink, one indigo accent for actions and
links, hairline borders, small radii, tabular figures. Mockups: the "RFNBO optimiser look and
feel" design canvas. No engine imports; screens call :func:`setup` once, :func:`apply` on
every page, and build with the helpers below.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from nicegui import ui

INK = "#0A2540"  # text, headings
SLATE = "#425466"  # secondary text
MUTED = "#697386"  # labels
LINE = "#E3E8EE"  # borders
DIVIDER = "#EBEEF1"  # rules inside cards
CANVAS = "#F6F8FA"  # app ground
SURFACE = "#FFFFFF"  # cards
ACCENT = "#5146E5"  # actions, links, selection
SUCCESS = "#05603A"  # met, valid
CAUTION = "#8A4B08"  # write-offs, warnings
DANGER = "#A3123A"  # not met, errors

# Pill tones: (background, text). Every pill carries words, so colour is never the only cue.
TONES = {
    "success": ("#D7F7E3", SUCCESS),
    "caution": ("#FFF1D6", CAUTION),
    "danger": ("#FDE4E8", DANGER),
    "neutral": ("#EBEEF1", "#2A3B52"),
    "accent": ("#EEF0FF", "#3B30C4"),
}

FONT = 'Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif'
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"

LOGO = (
    '<svg width="24" height="24" viewBox="0 0 24 24" aria-hidden="true">'
    f'<rect x="2" y="2" width="20" height="20" rx="6" fill="{ACCENT}"></rect>'
    '<path d="M7 15.5l3.2-7h1.6l3.2 7M8.4 12.6h5.2" stroke="#FFFFFF" stroke-width="1.6" '
    'fill="none" stroke-linecap="round" stroke-linejoin="round"></path></svg>'
)

CSS = f"""
:root {{
  --rf-ink: {INK}; --rf-slate: {SLATE}; --rf-muted: {MUTED}; --rf-line: {LINE};
  --rf-divider: {DIVIDER}; --rf-canvas: {CANVAS}; --rf-surface: {SURFACE};
  --rf-accent: {ACCENT}; --rf-success: {SUCCESS}; --rf-caution: {CAUTION};
  --rf-danger: {DANGER};
  --rf-shadow: 0 1px 1px rgba(10,37,64,.04), 0 2px 6px rgba(10,37,64,.04);
}}
body {{
  background: var(--rf-canvas); color: var(--rf-ink); font-family: {FONT};
  font-size: 14px; line-height: 20px; font-feature-settings: "tnum" 1, "cv11" 1;
  -webkit-font-smoothing: antialiased;
}}
.nicegui-content {{ padding: 0; gap: 0; }}
a {{ color: var(--rf-accent); text-decoration: none; font-weight: 500; }}
a:hover {{ color: #312E81; }}

/* top bar and page */
.rf-topbar {{
  display: flex; flex-wrap: wrap; align-items: center; gap: 16px; width: 100%;
  box-sizing: border-box; min-height: 60px; padding: 0 24px; background: var(--rf-surface);
  border-bottom: 1px solid var(--rf-line);
}}
.rf-brand {{ display: flex; align-items: center; gap: 10px; font-weight: 600;
  letter-spacing: -.01em; color: var(--rf-ink); }}
.rf-vrule {{ width: 1px; height: 20px; background: var(--rf-line); }}
.rf-crumbs {{ display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: 13px;
  color: var(--rf-muted); }}
.rf-crumbs a {{ color: var(--rf-slate); }}
.rf-crumbs .rf-here {{ color: var(--rf-ink); font-weight: 500; }}
.rf-page {{ box-sizing: border-box; width: 100%; max-width: 1360px; margin: 0 auto;
  padding: 28px 32px 48px; display: flex; flex-direction: column; gap: 20px; }}
.rf-page.rf-wide {{ max-width: none; padding: 24px 24px 40px; }}
.rf-h1 {{ margin: 0; font-size: 28px; line-height: 36px; font-weight: 700;
  letter-spacing: -.02em; }}
.rf-h2 {{ margin: 0; font-size: 16px; line-height: 24px; font-weight: 600; }}
.rf-sub {{ color: var(--rf-slate); font-size: 13px; line-height: 20px; }}
.rf-note {{ color: var(--rf-slate); font-size: 13px; }}
.rf-eyebrow {{ color: var(--rf-muted); font-size: 11px; font-weight: 600;
  letter-spacing: .06em; text-transform: uppercase; }}
.rf-mono {{ font-family: {MONO}; font-size: 13px; }}

/* cards */
.rf-card {{ width: 100%; box-sizing: border-box; background: var(--rf-surface);
  border: 1px solid var(--rf-line); border-radius: 8px; box-shadow: var(--rf-shadow); }}
.rf-card-head {{ display: flex; flex-wrap: wrap; align-items: center; gap: 12px;
  padding: 14px 20px; border-bottom: 1px solid var(--rf-divider); }}
.rf-card-body {{ display: flex; flex-direction: column; gap: 12px; padding: 16px 20px; }}
.rf-card-body > .q-table__container {{
  margin-left: -20px; margin-right: -20px; width: calc(100% + 40px); max-width: none;
  border: 0; border-radius: 0; box-shadow: none;
  border-top: 1px solid var(--rf-divider); border-bottom: 1px solid var(--rf-divider);
}}
.rf-card-body > .q-table__container:first-child {{ margin-top: -16px; border-top: 0; }}
.rf-card-body > .q-table__container:last-child {{ margin-bottom: -16px; border-bottom: 0;
  border-radius: 0 0 8px 8px; }}

/* key figures */
.rf-kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }}
.rf-kpi {{ display: flex; flex-direction: column; gap: 6px; padding: 18px 20px; min-width: 0;
  border-right: 1px solid var(--rf-divider); }}
.rf-kpi .rf-kpi-figure {{ display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px; }}
.rf-kpi:last-child {{ border-right: 0; }}
.rf-kpi-label {{ color: var(--rf-slate); font-size: 13px; font-weight: 500; }}
.rf-kpi-value {{ font-size: 24px; line-height: 32px; font-weight: 600; letter-spacing: -.015em;
  overflow-wrap: anywhere; }}

/* pills */
.rf-pill {{ display: inline-flex; align-items: center; gap: 6px; height: 24px; padding: 0 10px;
  border-radius: 999px; font-size: 12px; font-weight: 500; line-height: 24px;
  white-space: nowrap; }}

/* tables */
.q-table__container {{ border-radius: 8px; box-shadow: var(--rf-shadow);
  background: var(--rf-surface); color: var(--rf-ink); }}
.q-table--bordered {{ border: 1px solid var(--rf-line); }}
.q-table thead tr, .q-table thead th {{ height: auto; }}
/* Headers keep their case: they carry units (kg, gCO2e/MJ). */
.q-table th {{ color: var(--rf-muted); font-size: 12px; font-weight: 600; padding: 10px 12px; }}
.q-table td {{ font-size: 13px; padding: 10px 12px; height: auto; }}
.q-table--dense .q-table th, .q-table--dense .q-table td {{ padding: 8px 12px; }}
.q-table th:first-child, .q-table td:first-child {{ padding-left: 20px; }}
.q-table th:last-child, .q-table td:last-child {{ padding-right: 20px; }}
.q-table tbody td {{ border-color: var(--rf-divider); }}
.q-table thead tr th {{ border-color: var(--rf-divider); }}
.q-table tbody tr:hover > td {{ background: #FAFBFC; }}
.q-table__bottom {{ border-color: var(--rf-divider); font-size: 12px; color: var(--rf-slate); }}

/* buttons */
.q-btn {{ text-transform: none; letter-spacing: 0; font-weight: 500; border-radius: 6px; }}
.q-btn.bg-primary {{ font-weight: 600;
  box-shadow: 0 1px 1px rgba(10,37,64,.12), inset 0 -1px 0 rgba(0,0,0,.12); }}
.q-btn--outline.text-dark {{ background: var(--rf-surface) !important;
  box-shadow: 0 1px 1px rgba(10,37,64,.06); }}
.q-btn--outline.text-dark::before {{ border: 1px solid var(--rf-line); }}
.q-btn--dense {{ min-height: 32px; }}
.q-btn .q-icon {{ font-size: 18px; }}

/* inputs */
.q-field--outlined .q-field__control {{ border-radius: 6px; background: var(--rf-surface); }}
.q-field--outlined .q-field__control:before {{ border-color: #D5DBE1; }}
.q-field--outlined.q-field--focused .q-field__control:after {{ border-width: 1px;
  border-color: var(--rf-accent); box-shadow: 0 0 0 3px rgba(81,70,229,.18); }}
.q-field__label {{ color: var(--rf-muted); }}
.q-field--dense .q-field__control, .q-field--dense .q-field__marginal {{ min-height: 38px; }}

/* side navigation (vertical tabs) */
.rf-nav {{ width: 248px; flex: 0 0 248px; box-sizing: border-box; padding: 20px 12px; }}
.rf-nav .q-tabs__content {{ gap: 2px; }}
.rf-nav .q-tab {{ justify-content: flex-start; min-height: 36px; padding: 0 12px;
  border-radius: 6px; color: var(--rf-slate); text-transform: none; }}
.rf-nav .q-tab__content {{ width: 100%; flex-direction: row; justify-content: space-between;
  gap: 8px; }}
.rf-nav .q-tab__label {{ font-size: 14px; font-weight: 500; }}
.rf-nav .q-tab--active {{ background: var(--rf-surface); box-shadow: 0 0 0 1px var(--rf-line);
  color: var(--rf-accent); }}
.rf-nav .q-tab--active .q-tab__label {{ font-weight: 600; }}
.rf-nav .q-tab__indicator {{ display: none; }}
.rf-nav .q-focus-helper {{ border-radius: 6px; }}
.rf-nav .rf-count {{ height: 20px; line-height: 20px; padding: 0 8px; font-size: 11px; }}
.q-tab-panels {{ background: transparent; }}
.q-tab-panel {{ padding: 0; }}

/* dialogs and toasts */
.q-dialog .q-card {{ border-radius: 10px; box-shadow: 0 12px 32px rgba(10,37,64,.18); }}
.q-notification {{ border-radius: 8px; font-family: {FONT}; }}

@media (max-width: 720px) {{
  .rf-page {{ padding: 20px 16px 40px; }}
  .rf-nav {{ width: 100%; flex-basis: 100%; }}
}}
"""


def setup() -> None:
    """Default props for every element the screens make; call once, before ``ui.run``."""
    for element in (ui.input, ui.number, ui.select):
        element.default_props("outlined dense")
    ui.button.default_props("unelevated no-caps")


def apply() -> None:
    """The theme on the current page: Quasar's colours and the shared styles."""
    ui.colors(
        primary=ACCENT,
        secondary=SLATE,
        accent=ACCENT,
        dark=INK,
        positive=SUCCESS,
        negative=DANGER,
        warning=CAUTION,
        info=SLATE,
    )
    ui.add_head_html(f"<style>{CSS}</style>")


def top_bar(
    crumbs: Sequence[tuple[str, str | None]] = (), back: str | None = None, hint: str = ""
) -> ui.row:
    """The bar across the top: the product, then where you are; returns the row on the right,
    for the page's actions. ``crumbs``: (label, href or None for the current page)."""
    with ui.element("header").classes("rf-topbar"):
        with ui.element("div").classes("rf-brand"):
            ui.html(LOGO, sanitize=False)
            ui.label("RFNBO optimiser")
        if back:
            ui.element("span").classes("rf-vrule")
            ui.link("← Data screen", back).props('data-testid="back"')
        if crumbs:
            ui.element("span").classes("rf-vrule")
            with ui.element("nav").props('aria-label="Breadcrumb"').classes("rf-crumbs") as nav:
                if hint:
                    nav.tooltip(hint)
                for i, (label, href) in enumerate(crumbs):
                    if i:
                        ui.label("/").props('aria-hidden="true"')
                    if href:
                        ui.link(label, href).props(f'data-testid="crumb-{i}"')
                    else:
                        ui.label(label).classes("rf-here")
        ui.space()
        right = ui.row().classes("items-center gap-3 no-wrap")
    return right


def page(wide: bool = False) -> ui.column:
    """The page's content column, below the top bar."""
    return ui.column().classes("rf-page" + (" rf-wide" if wide else ""))


def title(text: str, subtitle: str = "") -> ui.column:
    """The page title, and a line under it; returns the column, for pills under the title."""
    with ui.column().classes("gap-1") as col:
        ui.label(text).classes("rf-h1").props('role="heading" aria-level="1"')
        if subtitle:
            ui.label(subtitle).classes("rf-sub").style("font-size: 14px")
    return col


@contextmanager
def card(
    heading: str = "", subtitle: str = "", testid: str = ""
) -> Iterator[tuple[ui.row, ui.column]]:
    """A card with a heading row and a body; the body is the current context. Yields (head,
    body): put actions into ``head`` with ``with head:``. A table placed straight in the body
    runs edge to edge."""
    with ui.element("section").classes("rf-card") as section:
        if testid:
            section.props(f'data-testid="{testid}"')
        with ui.row().classes("rf-card-head w-full") as head:
            if heading:
                with ui.column().classes("gap-0"):
                    ui.label(heading).classes("rf-h2").props('role="heading" aria-level="2"')
                    if subtitle:
                        ui.label(subtitle).classes("rf-sub")
            ui.space()
        with ui.column().classes("rf-card-body w-full") as body:
            yield head, body
    if not heading:
        head.set_visibility(False)


def pill(text: str, tone: str = "neutral", testid: str = "") -> ui.label:
    bg, fg = TONES[tone]
    label = ui.label(text).classes("rf-pill").style(f"background: {bg}; color: {fg}")
    if testid:
        label.props(f'data-testid="{testid}"')
    return label


def set_pill(label: ui.label, text: str, tone: str) -> None:
    bg, fg = TONES[tone]
    label.set_text(text)
    label.style(f"background: {bg}; color: {fg}")


def kpis(items: Sequence[tuple[str, str, str]], testid: str = "") -> None:
    """Key figures in one card, side by side: (label, value, unit)."""
    with ui.element("section").classes("rf-card rf-kpis") as grid:
        if testid:
            grid.props(f'data-testid="{testid}"')
        for label, value, unit in items:
            with ui.element("div").classes("rf-kpi"):
                ui.label(label).classes("rf-kpi-label")
                with ui.element("div").classes("rf-kpi-figure"):
                    ui.label(value).classes("rf-kpi-value")
                    if unit:
                        ui.label(unit).style(f"color: {MUTED}; font-weight: 500")


def secondary(button: ui.button) -> ui.button:
    """A white button with a hairline border, beside the one primary action."""
    return button.props("outline color=dark")


def text_button(button: ui.button, danger: bool = False) -> ui.button:
    return button.props(f"flat color={'negative' if danger else 'primary'}")
