"""Page 2 - Network Topology.

Rendered from stored lab/demo data (never a static image) and now offers
**four layout types** of the same graph - layered tree, force-directed,
circular ring and grid - so a topology can be read from the angle that suits
the question. Node markers are sized by connection degree and shaped by type
(router / switch / host); links are coloured by health. In demo mode the page
can expand the view with a large, clearly-labelled synthetic campus fabric so
the layouts have meaningful scale - live mode always shows the real lab only.
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from services.app_state import (bootstrap, demo_banner_if_active, has_role,
                                read, sidebar_status)
from ui import theme

bootstrap()
sidebar_status()
theme.page_header("Network Topology",
                  "Rendered live from lab devices & links · multi-layout views",
                  icon="🕸️")
demo_banner_if_active()

from analytics import graph
from config import get_settings
from database.connection import get_session
from services import demo_data, topology_service

settings = get_settings()


def _write(fn):
    """Run a mutation in its own committed session (demo seeding only)."""
    session = get_session()
    try:
        result = fn(session)
        session.commit()
        return result
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ------------------------------------------------------------ demo controls
# Fabric seeding is a demo-mode, operator-gated convenience. In open mode
# (auth disabled) has_role always returns True, so this is invisible in live.
if settings.demo_mode and has_role("operator"):
    with st.expander("🧪 Demo data - expand this view with a large synthetic "
                     "fabric", expanded=False):
        st.caption("Writes clearly-labelled **synthetic** devices/links into "
                   "the separate demo database (never a live lab) so the four "
                   "layouts have real scale. Deterministic & idempotent.")
        size = st.selectbox(
            "Fabric size",
            ["Compact (~30)", "Standard (~92)", "Large (~210)"], index=1)
        params = {"Compact (~30)": dict(core=2, dist=4, access=4,
                                        hosts_per_access=5),
                  "Standard (~92)": dict(core=2, dist=6, access=12,
                                         hosts_per_access=6),
                  "Large (~210)": dict(core=2, dist=8, access=20,
                                       hosts_per_access=9)}[size]
        if st.button("Seed / refresh demo fabric", type="primary"):
            counts = _write(lambda s: demo_data.seed_demo_fabric(**params))
            st.success(f"Fabric seeded: {counts['devices']} devices, "
                       f"{counts['links']} links (demo).")
            st.rerun()

# ------------------------------------------------------------------ data
topology = read(lambda s: topology_service.get_topology(s))
nodes, edges = topology["nodes"], topology["edges"]
status = topology["status"]

if not nodes:
    st.info("No devices in the database yet. Run a collection cycle "
            "(sidebar), or - in demo mode - seed the large fabric above. The "
            "graph is always generated from stored lab/demo data.")
    st.stop()

g = read(lambda s: graph.build_from_session(s))
summary = graph.connectivity_summary(g)
spof = graph.single_points_of_failure(g)
down = sum(1 for n in nodes if n["status"] == "down")

theme.kpi_row([
    ("Devices", len(nodes), "in the topology", "info"),
    ("Links", len(edges), "documented connections", "default"),
    ("Components", summary["components"],
     "1 = fully connected", "ok" if summary["components"] == 1 else "warn"),
    ("SPOF nodes", len(spof["articulation_nodes"]),
     "removal disconnects peers", "bad" if spof["articulation_nodes"] else "ok"),
    ("Down devices", down, f"of {len(nodes)}", "bad" if down else "ok"),
])
st.caption(f"Network status: **{status.overall.upper()}** - {status.summary()}")

# ------------------------------------------------------------- layout choice
c1, c2 = st.columns([3, 1])
with c1:
    kind = st.radio(
        "Layout", topology_service.LAYOUT_KINDS, horizontal=True,
        format_func=lambda k: {
            "layered": "🌳 Layered tree", "force": "🧲 Force-directed",
            "circular": "⭕ Circular", "grid": "🔳 Grid"}[k])
with c2:
    by_degree = st.checkbox("Size by degree", value=True)

positions = topology_service.compute_layout(nodes, edges, kind=kind)
degrees = topology_service.degree_map(nodes, edges)
max_deg = max(degrees.values(), default=1) or 1
SYMBOLS = {"router": "circle", "switch": "diamond", "host": "square"}
show_labels = len(nodes) <= 70        # avoid clutter on very large graphs

# ------------------------------------------------------------------ figure
fig = go.Figure()
# Edges first (drawn beneath nodes), one trace per status colour.
for edge in edges:
    x0, y0 = positions[edge["source"]]
    x1, y1 = positions[edge["target"]]
    fig.add_trace(go.Scatter(
        x=[x0, x1, None], y=[y0, y1, None], mode="lines",
        line=dict(width=2.2, color=edge["color"]),
        hoverinfo="text",
        hovertext=(f"Link {edge['source']} ↔ {edge['target']}<br>"
                   f"Interfaces: {edge['label']}<br>Status: {edge['status']}"
                   f"{'  subnet=' + edge['subnet'] if edge['subnet'] else ''}"),
        showlegend=False,
    ))
# Nodes, grouped into a trace per (type, status) so the legend stays tidy.
groups: dict[tuple, list] = {}
for node in nodes:
    groups.setdefault((node["type"], node["status"]), []).append(node)
for (ntype, nstatus), members in sorted(groups.items(),
                                        key=lambda kv: (str(kv[0][0]),
                                                        str(kv[0][1]))):
    xs = [positions[m["id"]][0] for m in members]
    ys = [positions[m["id"]][1] for m in members]
    sizes = [16 + 22 * (degrees.get(m["id"], 0) / max_deg) if by_degree
             else 22 for m in members]
    colors = [m["color"] for m in members]
    fig.add_trace(go.Scatter(
        x=xs, y=ys, mode="markers+text" if show_labels else "markers",
        marker=dict(size=sizes, color=colors, symbol=SYMBOLS.get(ntype, "circle"),
                    line=dict(width=1.2, color="white")),
        text=[m["label"] for m in members] if show_labels else None,
        textposition="bottom center", textfont=dict(size=10),
        name=f"{ntype} · {nstatus}",
        hoverinfo="text",
        hovertext=[f"{m['label']} ({ntype})<br>IP: {m['ip'] or '-'}"
                   f"<br>Status: {nstatus}<br>Degree: {degrees.get(m['id'], 0)}"
                   for m in members],
    ))
fig.update_layout(
    height=680, margin=dict(l=20, r=20, t=30, b=20),
    xaxis=dict(visible=False), yaxis=dict(visible=False),
    plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
    hovermode="closest", showlegend=len(groups) > 1,
    legend=dict(orientation="h", y=1.06, x=0, title_text="type · status"),
)
st.plotly_chart(fig, width="stretch",
                config={"displayModeBar": True, "scrollZoom": True})

# ------------------------------------------------------------------ detail
st.subheader("Legend & structure")
c1, c2 = st.columns([1, 2])
with c1:
    st.markdown(
        "**Status** · 🟢 up　🔴 down　🟡 degraded　⚪ unknown\n\n"
        "**Type** · ⬤ router　◆ switch　■ host\n\n"
        f"_Marker size ∝ number of links (max degree {max_deg})._"
    )
    if summary["isolated_nodes"]:
        st.warning("Isolated (no links): "
                   + ", ".join(summary["isolated_nodes"][:20]))
    if spof["articulation_nodes"]:
        st.info("Single points of failure: "
                + ", ".join(spof["articulation_nodes"][:20]))
with c2:
    st.dataframe(
        pd.DataFrame([
            {"A": e["source"], "B": e["target"], "Interfaces": e["label"],
             "Subnet": e["subnet"] or "-", "Status": e["status"]}
            for e in edges
        ]),
        hide_index=True, width="stretch", height=260,
    )
