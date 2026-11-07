"""Permanent parity test for Topology page: Demo vs Simulated source.

Uses Streamlit AppTest with real repository (no mocks) to verify that
Simulated source offers every feature/control/visual that Demo Mode offers
on the Topology page.

The test fails on any new un-allowlisted difference.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from database import connection, source_scope
from services import demo_data
from tools import seed_simulated


ROOT = Path(__file__).resolve().parents[1]
TOPOLOGY_PAGE = ROOT / "app_pages" / "2_Topology.py"

# Allowlisted exceptions: features that truly cannot work on Simulated
ALLOWLISTED_EXCEPTIONS = set()


def _seed_demo_fabric():
    """Seed demo fabric for parity testing."""
    from network.device_inventory import load_inventory
    with connection.session_scope() as session:
        demo_data.seed_demo_fabric(
            session, core=2, dist=4, access=4, hosts_per_access=5)


def _seed_simulated_fabric():
    """Seed simulated fabric for parity testing."""
    from analytics import lab_simulator
    from network.device_inventory import load_inventory
    
    dataset = lab_simulator.simulate(
        duration_hours=0.5, interval_seconds=60, seed=42, events={},
        start=__import__('datetime').datetime(2026, 3, 1))
    
    with connection.session_scope() as session:
        seed_simulated.seed_simulated(
            session, dataset=dataset, inventory=load_inventory())


def _get_widgets_info(at):
    """Extract widget information from AppTest result."""
    widgets = {}
    for widget_type in ['selectbox', 'slider', 'button', 'toggle', 'tabs',
                       'text_input', 'number_input', 'date_input', 'time_input',
                       'file_uploader', 'checkbox', 'radio', 'multiselect']:
        for widget in getattr(at, widget_type, []):
            key = getattr(widget, 'key', None)
            label = str(getattr(widget, 'label', '') or '')
            if key or label:
                widgets[f"{widget_type}_{key or label}"] = {
                    'type': widget_type,
                    'label': label,
                    'key': str(key) if key else '',
                }
    return widgets


def _get_expanders_info(at):
    """Extract expander information from AppTest result."""
    expanders = []
    for expander in getattr(at, 'expander', []):
        label = str(getattr(expander, 'label', '') or '')
        if label:
            expanders.append({
                'label': label,
                'is_expanded': getattr(expander, 'is_expanded', False),
            })
    return expanders


def _get_charts_info(at):
    """Extract chart information from AppTest result."""
    charts = []
    for chart_type in ['plotly_chart', 'altair_chart', 'matplotlib']:
        chart_list = getattr(at, chart_type, [])
        charts.append({
            'type': chart_type,
            'count': len(chart_list),
        })
    return charts


def _get_dataframes_info(at):
    """Extract dataframe information from AppTest result."""
    dataframes = []
    for df in getattr(at, 'dataframe', []):
        columns = list(getattr(df, 'columns', []))
        dataframes.append({
            'columns': columns,
            'n_columns': len(columns),
        })
    return dataframes


def _get_text_elements_info(at):
    """Extract text elements from AppTest result."""
    texts = {}
    for element_type in ['markdown', 'warning', 'info', 'error', 'success',
                        'caption', 'title', 'header', 'subheader', 'text']:
        elements = getattr(at, element_type, [])
        for i, element in enumerate(elements):
            value = getattr(element, 'value', None)
            if value is not None:
                text_key = f"{element_type}_{i}"
                texts[text_key] = str(value)[:200]
    return texts


def _compare_features(demo_features, simulated_features):
    """Compare features between Demo and Simulated modes."""
    differences = []
    
    demo_widget_keys = set(demo_features['widgets'].keys())
    sim_widget_keys = set(simulated_features['widgets'].keys())
    
    missing_in_sim = demo_widget_keys - sim_widget_keys
    extra_in_sim = sim_widget_keys - demo_widget_keys
    
    if missing_in_sim:
        differences.append(f"Missing widgets in Simulated: {sorted(missing_in_sim)}")
    if extra_in_sim:
        differences.append(f"Extra widgets in Simulated: {sorted(extra_in_sim)}")
    
    for key in demo_widget_keys & sim_widget_keys:
        demo_widget = demo_features['widgets'][key]
        sim_widget = simulated_features['widgets'][key]
        if demo_widget['type'] != sim_widget['type']:
            differences.append(f"Widget type mismatch '{key}': Demo={demo_widget['type']}, Simulated={sim_widget['type']}")
    
    demo_expander_count = len(demo_features['expanders'])
    sim_expander_count = len(simulated_features['expanders'])
    if demo_expander_count != sim_expander_count:
        differences.append(f"Expander count: Demo={demo_expander_count}, Simulated={sim_expander_count}")
    
    demo_has_plotly = any(c['type'] == 'plotly_chart' and c['count'] > 0 for c in demo_features['charts'])
    sim_has_plotly = any(c['type'] == 'plotly_chart' and c['count'] > 0 for c in demo_features['charts'])
    if demo_has_plotly != sim_has_plotly:
        differences.append(f"Plotly chart: Demo={'present' if demo_has_plotly else 'missing'}, Simulated={'present' if sim_has_plotly else 'missing'}")
    
    demo_has_df = len(demo_features['dataframes']) > 0
    sim_has_df = len(simulated_features['dataframes']) > 0
    if demo_has_df != sim_has_df:
        differences.append(f"DataFrame: Demo={'present' if demo_has_df else 'missing'}, Simulated={'present' if sim_has_df else 'missing'}")
    
    demo_text = '\n'.join(demo_features['texts'].values()).lower()
    sim_text = '\n'.join(simulated_features['texts'].values()).lower()
    
    if 'demo' not in demo_text:
        differences.append("Demo mode: Missing demo banner/warning")
    
    if 'simulated' not in sim_text:
        differences.append("Simulated mode: Missing simulated badge")
    
    if 'demo' in sim_text and 'simulated' not in sim_text:
        differences.append("Simulated mode: Has demo references but no simulated badge")
    
    return differences


def _extract_features(at):
    """Extract all features from AppTest result."""
    return {
        'widgets': _get_widgets_info(at),
        'expanders': _get_expanders_info(at),
        'charts': _get_charts_info(at),
        'dataframes': _get_dataframes_info(at),
        'texts': _get_text_elements_info(at),
    }


@pytest.mark.parametrize("fabric_size", ["Compact (~30)", "Standard (~92)"])
def test_topology_parity_with_fabric(fabric_size, db_url, monkeypatch):
    """Test that Simulated Topology has same features as Demo Topology."""
    from streamlit.testing.v1 import AppTest
    import os
    
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    monkeypatch.setenv("NETPILOT_CHANGE_LOOP_ENABLED", "false")
    monkeypatch.setenv("NETPILOT_LAB_EXECUTION_ENABLED", "false")
    
    _seed_demo_fabric()
    _seed_simulated_fabric()
    
    os.environ["NETPILOT_DEMO_MODE"] = "true"
    try:
        at_demo = AppTest.from_file(str(TOPOLOGY_PAGE), default_timeout=120)
        at_demo.run()
        demo_features = _extract_features(at_demo)
    finally:
        os.environ["NETPILOT_DEMO_MODE"] = "false"
    
    os.environ["NETPILOT_DEMO_MODE"] = "false"
    try:
        at_sim = AppTest.from_file(str(TOPOLOGY_PAGE), default_timeout=120)
        for box in at_sim.selectbox:
            label = str(getattr(box, 'label', '') or '').lower()
            if 'source' in label:
                options = list(box.options or [])
                match = next((opt for opt in options
                              if str(opt).lower().startswith('simulated')), None)
                if match is not None:
                    box.select(match)
                    at_sim.run()
                    break
        simula
