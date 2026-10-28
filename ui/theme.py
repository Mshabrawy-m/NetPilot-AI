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
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&family=JetBrains+Mono:wght@400;600&display=swap');

/* ---------- canvas & chrome ---------- */
html, body, [class*="css"] { font-family: 'Inter', system-ui, sans-serif; }
h1, h2, h3, h4 { font-family: 'Space Grotesk', 'Inter', sans-serif; letter-spacing: .2px; }
code, pre, [data-testid="stCode"] { font-family: 'JetBrains Mono', monospace; }

[data-testid="stAppViewContainer"] { background:
    radial-gradient(1200px 600px at 85% -10%, rgba(56,189,248,.10), transparent 60%),
    radial-gradient(900px 500px at -10% 110%, rgba(167,139,250,.08), transparent 55%),
    #0A0E1A; }
[data-testid="stHeader"] { background: rgba(10,14,26,.55); backdrop-filter: blur(12px);
    border-bottom: 1px solid rgba(35,48,73,.6); }
[data-testid="stHeader"] .stAppToolbar { opacity: .85; }
[data-testid="stDecoration"], footer, #MainMenu { visibility: hidden; display: none; }
[data-testid="stMainBlockContainer"] { padding-top: 2.2rem; padding-bottom: 3rem; }

/* thin animated accent line under the header */
[data-testid="stMain"]::before { content:""; position: sticky; top:0; display:block;
    height:2px; margin:0 0 -.5rem 0;
    background: linear-gradient(90deg,#38BDF8,#A78BFA,#3DDC97,#38BDF8);
    background-size:300% 100%; animation: npflow 9s linear infinite; opacity:.7; }
@keyframes npflow { to { background-position:300% 0; } }

/* ---------- sidebar ---------- */
[data-testid="stSidebar"] { border-right: 1px solid #1C2540; background:
    linear-gradient(180deg,#0B1120,#0A0E1A); }
[data-testid="stSidebar"] h3 { font-family:'Space Grotesk'; letter-spacing:2px;
    text-transform:uppercase; font-size:.86rem; color:#7DD3FC; }
[data-testid="stSidebar"] hr { border-color:#1C2540; }

/* ---------- section titles: accent rule ---------- */
[data-testid="stMain"] h2 { position: relative; padding-left: .75rem; }
[data-testid="stMain"] h2::before { content:""; position:absolute; left:0; top:.15em;
    bottom:.15em; width:4px; border-radius:4px;
    background:linear-gradient(180deg,#38BDF8,#A78BFA); }

/* ---------- KPI metric tiles ---------- */
[data-testid="stMetric"] { background:#131A2B; border:1px solid #233049;
    border-radius:14px; padding:1rem 1.1rem; transition:.18s ease; }
[data-testid="stMetric"]:hover { transform:translateY(-3px);
    border-color:#38BDF8; box-shadow:0 10px 30px -14px rgba(56,189,248,.55); }
[data-testid="stMetricLabel"] { text-transform:uppercase; letter-spacing:1.2px;
    font-size:.72rem !important; color:#8FA1BF !important; }
[data-testid="stMetricValue"] { font-family:'JetBrains Mono'; font-weight:600;
    color:#E6EDF7; }
[data-testid="stMetricValue"] > div { font-size:1.7rem; }

/* ---------- buttons ---------- */
.stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {
    border-radius:10px; border:1px solid #243049; background:#131A2B; color:#DCE6FA;
    font-weight:600; letter-spacing:.2px; padding:.5rem .95rem; transition:.16s ease; }
.stButton > button:hover, .stDownloadButton > button:hover { border-color:#38BDF8;
    color:#fff; box-shadow:0 6px 20px -10px rgba(56,189,248,.6); transform:translateY(-1px); }
.stButton > button[kind="primary"], .stFormSubmitButton > button[data-testid*="primary"] {
    background:linear-gradient(90deg,#0EA5E9,#6366F1); border:none; color:#fff; }
[data-testid="stPageLink"] a, .stPageLink a { border-radius:10px; border:1px solid #243049;
    background:#131A2B; color:#DCE6FA; font-weight:600; padding:.5rem .9rem; transition:.16s ease; }
[data-testid="stPageLink"] a:hover, .stPageLink a:hover { border-color:#38BDF8; color:#fff;
    box-shadow:0 6px 20px -10px rgba(56,189,248,.6); transform:translateY(-1px); }
.np-sec { text-transform:uppercase; letter-spacing:2px; font-size:.72rem;
    color:#7DD3FC; font-weight:700; margin:.1rem 0 .35rem 0; }

/* ---------- inputs ---------- */
[data-testid="stTextInput"] input, [data-testid="stTextArea"] textarea,
[data-testid="stSelectbox"] > div > div, .stRadio label { border-radius:10px; }
[data-testid="stRadio"] span { color:#C7D2E6; }

/* ---------- alerts / callouts ---------- */
[data-testid="stAlert"] { border-radius:12px; border:1px solid #233049;
    background:#0F1626; box-shadow: inset 3px 0 0 currentColor; }

/* ---------- dataframe ---------- */
[data-testid="stDataFrame"] { border:1px solid #233049; border-radius:14px; padding:2px;
    background:#0F1626; overflow:hidden; }

/* ---------- tabs / expander ---------- */
button[data-testid="stTab"] { font-weight:600; letter-spacing:.3px; }
[data-testid="stExpander"] { border:1px solid #233049; border-radius:12px;
    background:#0F1626; }
[data-testid="stExpander"] summary { font-weight:600; color:#BBD0F2; }

/* ---------- progress / spinners ---------- */
[data-testid="stProgress"] > div > div { background:linear-gradient(90deg,#38BDF8,#A78BFA); }

/* ---------- scrollbar ---------- */
::-webkit-scrollbar { width:10px; height:10px; }
::-webkit-scrollbar-thumb { background:#233049; border-radius:8px; }
::-webkit-scrollbar-thumb:hover { background:#33456A; }

/* ---------- NetPilot HTML components (page_header / kpi / pill) ---------- */
.np-hero { display:flex; align-items:center; gap:1rem; margin:.2rem 0 1.1rem 0;
    padding:1.05rem 1.25rem; border-radius:16px;
    background:linear-gradient(120deg,rgba(56,189,248,.14),rgba(167,139,250,.10));
    border:1px solid #233049; }
.np-hero .np-ico { font-size:1.9rem; line-height:1; }
.np-hero .np-t { font-family:'Space Grotesk'; font-weight:700; font-size:1.5rem; color:#EAF2FF; }
.np-hero .np-s { color:#9DB1D4; font-size:.9rem; margin-top:.1rem; }
.np-band { height:44px; width:6px; border-radius:6px;
    background:linear-gradient(180deg,#38BDF8,#A78BFA,#3DDC97); }
.np-grid { display:flex; flex-wrap:wrap; gap:.75rem; }
.np-kpi { flex:1 1 150px; min-width:150px; background:#131A2B; border:1px solid #233049;
    border-radius:14px; padding:.9rem 1rem; transition:.18s ease; }
.np-kpi:hover { transform:translateY(-3px); border-color:#38BDF8; }
.np-kpi .np-k-l { text-transform:uppercase; letter-spacing:1.2px; font-size:.68rem; color:#8FA1BF; }
.np-kpi .np-k-v { font-family:'JetBrains Mono'; font-weight:600; font-size:1.55rem; color:#E6EDF7; margin-top:.15rem; }
.np-kpi .np-k-h { font-size:.75rem; color:#7C89A6; margin-top:.1rem; }
.np-kpi::before { content:""; display:block; width:28px; height:3px; border-radius:3px;
    margin-bottom:.55rem; background:var(--np-a,#38BDF8); }
.np-pill { display:inline-flex; align-items:center; gap:.4rem; padding:.22rem .7rem;
    border-radius:999px; font-size:.74rem; font-weight:600; letter-spacing:.3px;
    color:#0A0E1A; background:var(--np-a,#38BDF8); }
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
          <div><div class="np-t">{_esc(title)}</div>{sub_html}</div>
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
    return f'<span class="np-pill" style="--np-a:{accent}">{_esc(text)}</span>'
