"""NetPilot AI — bespoke "Mission Control / Network-Ops" styling layer.

Public API (used by every page via ``services.app_state``):
    inject_css()                         -> applies the global stylesheet once
    page_header(title, subtitle, icon)   -> branded hero band for a page
    kpi(label, value, hint, tone)        -> HTML string for a KPI tile
    status_pill(text, tone)              -> HTML string for a status chip

Design intent: a crafted, product-grade console look (deep-navy canvas, electric
cyan accent, glassy blurred header, monospace metrics, accent-ruled section
titles, hover-lift cards) that is visually distinct from Streamlit's defaults.
The functions only render presentation — no business logic, no data access.
"""
from __future__ import annotations

import html

import streamlit as st

_ACCENTS = {
    "default": "#38BDF8",
    "info": "#38BDF8",
    "ok": "#3DDC97",
    "good": "#3DDC97",
    "warn": "#F5A524",
    "bad": "#F97066",
    "violet": "#A78BFA",
    "muted": "#7C89A6",
}


def _esc(value: object) -> str:
    return html.escape(str(value))


_CSS = """
:root { --np-bg:#0b1220; --np-surface:#111b2d; --np-raised:#162238;
  --np-border:#273750; --np-text:#e5edf7; --np-muted:#9aabc1;
  --np-accent:#55b9e8; --np-focus:#8bd7fa; --np-ok:#48c99a;
  --np-warn:#e6ad4e; --np-bad:#f27670; --np-radius:10px; }

html, body, [class*="css"] { font-family:system-ui,-apple-system,"Segoe UI",sans-serif; }
h1, h2, h3, h4 { letter-spacing:0; }
code, pre, [data-testid="stCode"] { font-family:ui-monospace,"Cascadia Code",monospace; }
[data-testid="stAppViewContainer"] { background:var(--np-bg); color:var(--np-text); }
[data-testid="stHeader"] { background:var(--np-bg); border-bottom:1px solid var(--np-border); }
[data-testid="stDecoration"], footer, #MainMenu { visibility:hidden; display:none; }
[data-testid="stMainBlockContainer"] { max-width:1500px; padding-top:1.5rem; padding-bottom:2.5rem; }

[data-testid="stSidebar"] { background:#0e1727; border-right:1px solid var(--np-border); }
[data-testid="stSidebar"] [data-testid="stNavSectionHeader"] { color:#8dbbda; font-weight:700; }
[data-testid="stSidebar"] [aria-current="page"] { background:#172942; border-radius:8px; color:#fff; }
[data-testid="stSidebar"] hr { border-color:var(--np-border); }

[data-testid="stMain"] h2 { position:relative; padding-left:.7rem; }
[data-testid="stMain"] h2::before { content:""; position:absolute; left:0; top:.15em; bottom:.15em;
  width:3px; border-radius:3px; background:var(--np-accent); }
[data-testid="stMetric"] { background:var(--np-surface); border:1px solid var(--np-border);
  border-radius:var(--np-radius); padding:.8rem 1rem; }
[data-testid="stMetricLabel"] { color:var(--np-muted)!important; font-size:.78rem!important; }
[data-testid="stMetricValue"] { color:var(--np-text); }

.stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {
  min-height:2.5rem; border-radius:8px; border:1px solid var(--np-border);
  background:var(--np-surface); color:var(--np-text); font-weight:600; }
.stButton > button:hover, .stDownloadButton > button:hover { border-color:var(--np-accent); color:#fff; }
.stButton > button:focus-visible, .stDownloadButton > button:focus-visible,
[data-testid="stSidebar"] a:focus-visible, input:focus-visible, textarea:focus-visible {
  outline:2px solid var(--np-focus); outline-offset:2px; }
.stButton > button[kind="primary"], .stFormSubmitButton > button[data-testid*="primary"] {
  background:#147da8; border-color:#147da8; color:white; }

[data-testid="stTextInput"] input, [data-testid="stTextArea"] textarea,
[data-testid="stSelectbox"] > div > div, .stRadio label { border-radius:8px; }
[data-testid="stAlert"] { border-radius:9px; border:1px solid var(--np-border); background:var(--np-surface); }
[data-testid="stDataFrame"] { border:1px solid var(--np-border); border-radius:9px; overflow:hidden; }
button[data-testid="stTab"] { font-weight:600; }
[data-testid="stExpander"] { border:1px solid var(--np-border); border-radius:9px; background:var(--np-surface); }
[data-testid="stExpander"] summary { font-weight:600; color:var(--np-text); }
[data-testid="stProgress"] > div > div { background:var(--np-accent); }

.np-hero { display:flex; align-items:center; gap:.85rem; margin:.2rem 0 1rem;
  padding:.9rem 1.05rem; border-radius:11px; background:var(--np-surface);
  border:1px solid var(--np-border); }
.np-hero .np-ico { font-size:1.45rem; line-height:1; color:var(--np-accent); }
.np-hero .np-t { font-weight:700; font-size:1.35rem; color:var(--np-text); }
.np-hero .np-s { color:var(--np-muted); font-size:.88rem; margin-top:.15rem; }
.np-band { height:38px; width:3px; border-radius:3px; background:var(--np-accent); }
.np-grid { display:flex; flex-wrap:wrap; gap:.65rem; }
.np-kpi { flex:1 1 150px; min-width:140px; background:var(--np-surface);
  border:1px solid var(--np-border); border-radius:var(--np-radius); padding:.8rem .9rem; }
.np-kpi .np-k-l { font-size:.72rem; color:var(--np-muted); }
.np-kpi .np-k-v { font-weight:650; font-size:1.35rem; color:var(--np-text); margin-top:.15rem; }
.np-kpi .np-k-h { font-size:.76rem; color:var(--np-muted); margin-top:.15rem; }
.np-kpi::before { content:""; display:block; width:24px; height:2px; border-radius:2px;
  margin-bottom:.5rem; background:var(--np-a,var(--np-accent)); }
.np-pill { display:inline-flex; align-items:center; padding:.2rem .6rem; border-radius:999px;
  font-size:.74rem; font-weight:650; color:#08121b; background:var(--np-a,var(--np-accent)); }

@media (max-width:700px) {
  [data-testid="stMainBlockContainer"] { padding:.9rem .8rem 1.5rem; }
  .np-hero { align-items:flex-start; }
  .np-kpi { min-width:125px; }
}
@media (prefers-reduced-motion:reduce) { *, *::before, *::after { scroll-behavior:auto!important; transition:none!important; animation:none!important; } }
"""


def inject_css() -> None:
    """Apply the global stylesheet (re-emitted on every run).

    A session-level "already injected" flag cannot guard this: the ``<style>``
    element lives in the page body, and switching pages clears that body while
    the session keeps the flag. Every page after the first would then render
    with Streamlit's default theme. Re-emitting the same sheet each run
    replaces the previous element instead of stacking (Streamlit reconciles
    markdown elements by position), so the cost is a few KB and the styling
    survives navigation.
    """
    st.markdown(f"<style>{_CSS}</style>", unsafe_allow_html=True)


def apply_plotly_theme() -> None:
    """Register + make default a dark Plotly template matching the theme.

    One call from ``bootstrap`` restyles every existing ``st.plotly_chart``
    (px *and* go figures) without touching the chart code on each page, via
    ``plotly.io.default_template``. No-op if plotly is unavailable.
    """
    if st.session_state.get("_np_plotly_themed"):
        return
    try:
        import plotly.io as pio
        from plotly.graph_objs.layout import Template
    except Exception:  # pragma: no cover - plotly is a hard dep but be safe
        return
    template = Template(
        layout=dict(
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="#0F1626",
            font=dict(color="#E6EDF7", family="Inter"),
            title_font_color="#EAF2FF",
            colorway=["#38BDF8", "#A78BFA", "#3DDC97", "#F5A524",
                      "#F97066", "#7DD3FC", "#22D3EE"],
            xaxis=dict(gridcolor="#233049", linecolor="#233049",
                       zerolinecolor="#33456A"),
            yaxis=dict(gridcolor="#233049", linecolor="#233049",
                       zerolinecolor="#33456A"),
            legend=dict(bgcolor="rgba(0,0,0,0)", font_color="#C7D2E6"),
            margin=dict(l=40, r=20, t=50, b=30),
            hoverlabel=dict(bgcolor="#131A2B", bordercolor="#233049",
                            font_color="#E6EDF7"),
        )
    )
    pio.templates["NetPilot"] = template
    pio.default_template = "NetPilot"
    st.session_state["_np_plotly_themed"] = True


def page_header(title: str, subtitle: str = "", icon: str = "") -> None:
    """Branded hero band replacing a plain ``st.title``."""
    accent = "#38BDF8"
    icon_html = f'<span class="np-ico">{_esc(icon)}</span>' if icon else ""
    sub_html = f'<div class="np-s">{_esc(subtitle)}</div>' if subtitle else ""
    st.markdown(
        f"""
        <div class="np-hero">
          <div class="np-band" style="background:{accent}"></div>
          {icon_html}
          <div><div class="np-t" role="heading" aria-level="1">{_esc(title)}</div>{sub_html}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def kpi(label: str, value: object, hint: str = "", tone: str = "default") -> str:
    """Return HTML for a KPI tile; inject a row of them with ``st.markdown``."""
    accent = _ACCENTS.get(tone, _ACCENTS["default"])
    hint_html = f'<div class="np-k-h">{_esc(hint)}</div>' if hint else ""
    return (
        f'<div class="np-kpi" style="--np-a:{accent}">'
        f'<div class="np-k-l">{_esc(label)}</div>'
        f'<div class="np-k-v">{_esc(value)}</div>{hint_html}</div>'
    )


def kpi_row(items: list[tuple]) -> None:
    """Render several KPIs side by side. Each item is
    ``(label, value)`` or ``(label, value, hint)`` or
    ``(label, value, hint, tone)``."""
    cells = []
    for it in items:
        label, value = it[0], it[1]
        hint = it[2] if len(it) > 2 else ""
        tone = it[3] if len(it) > 3 else "default"
        cells.append(kpi(label, value, hint, tone))
    st.markdown(f'<div class="np-grid">{"".join(cells)}</div>',
                unsafe_allow_html=True)


def status_pill(text: str, tone: str = "default") -> str:
    accent = _ACCENTS.get(tone, _ACCENTS["default"])
    return f'<span class="np-pill" role="status" style="--np-a:{accent}">{_esc(text)}</span>'
