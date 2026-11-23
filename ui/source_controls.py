"""Shared Streamlit controls for honest data-source selection.

Every page that can show more than one source uses these helpers so labels,
defaults, and source badges are consistent. Simulated/Demo data are always
clearly labeled as not real network measurements.
"""
from __future__ import annotations

import streamlit as st

from database import source_scope
from ui import presentation


def format_source(value: str) -> str:
    return presentation.source_caption(value)


def badge(source: str, *, demo_mode: bool = False) -> None:
    """Render the mandatory provenance notice for synthetic sources."""
    if demo_mode or source == source_scope.DEMO:
        st.warning(presentation.source_caption(source_scope.DEMO), icon="⚠️")
    elif source == source_scope.SIMULATED:
        st.warning(presentation.source_caption(source_scope.SIMULATED), icon="🧪")


def selector(sources, *, key: str, label: str = "Data source",
             demo_mode: bool = False) -> str:
    """Render the source selector and its provenance badge.

    Live is the default whenever it is offered; demo mode bypasses the selector
    because its own store and banner already isolate the data.
    """
    if demo_mode:
        badge(source_scope.DEMO, demo_mode=True)
        return source_scope.DEMO
    options = list(sources) or [source_scope.LIVE]
    default = source_scope.default_source(options)
    chosen = st.selectbox(
        label, options,
        index=options.index(default) if default in options else 0,
        format_func=format_source, key=key)
    badge(chosen)
    return chosen