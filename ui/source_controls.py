"""Shared Streamlit controls for honest data-source selection.

Every page that can show more than one provenance uses these helpers so the
label, the default and the simulated badge are identical everywhere.  A
simulated source is always announced with :data:`database.source_scope.
SIMULATED_BANNER`; it is never described as live telemetry.
"""
from __future__ import annotations

import streamlit as st

from database import source_scope


def format_source(value: str) -> str:
    return source_scope.SOURCE_LABELS.get(value, value)


def badge(source: str) -> None:
    """Render the mandatory provenance warning for a synthetic source."""
    if source == source_scope.SIMULATED:
        st.warning(source_scope.SIMULATED_BANNER, icon="🧪")


def selector(sources, *, key: str, label: str = "Data source",
             demo_mode: bool = False) -> str:
    """Render the source selector and its provenance badge.

    ``live`` is the default whenever it is offered; demo mode bypasses the
    selector because its own store and banner already isolate the data.
    """
    if demo_mode:
        return source_scope.DEMO
    options = list(sources) or [source_scope.LIVE]
    default = source_scope.default_source(options)
    chosen = st.selectbox(
        label, options,
        index=options.index(default) if default in options else 0,
        format_func=format_source, key=key)
    badge(chosen)
    return chosen
