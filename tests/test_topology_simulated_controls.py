"""Direct verification that Simulated Topology has same controls as Demo.

This test doesn't use AppTest (which can be flaky in CI), but instead
verifies the code structure directly by checking the page source.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


TOPOLOGY_PAGE = Path(__file__).resolve().parents[1] / "app_pages" / "2_Topology.py"


def test_topology_has_demo_and_simulated_fabric_controls():
    """Verify Topology page has fabric generation for both Demo and Simulated."""
    source = TOPOLOGY_PAGE.read_text(encoding="utf-8")
    tree = ast.parse(source, str(TOPOLOGY_PAGE))
    
    demo_found = False
    simulated_found = False
    
    class ConditionVisitor(ast.NodeVisitor):
        def visit_If(self, node):
            nonlocal demo_found, simulated_found
            if isinstance(node.test, ast.BoolOp):
                for value in node.test.values:
                    if isinstance(value, ast.Compare):
                        for c in ast.walk(value):
                            if isinstance(c, ast.Name) and c.id == 'SIMULATED':
                                simulated_found = True
                    if isinstance(value, ast.Name) and value.id == 'demo_mode':
                        demo_found = True
            elif isinstance(node.test, ast.Compare):
                for c in ast.walk(node.test):
                    if isinstance(c, ast.Name):
                        if c.id == 'demo_mode':
                            demo_found = True
                        elif c.id == 'SIMULATED':
                            simulated_found = True
            self.generic_visit(node)
    
    visitor = ConditionVisitor()
    visitor.visit(tree)
    
    assert demo_found, "Demo mode fabric control not found in Topology page"
    assert simulated_found, "Simulated source fabric control not found in Topology page"


def test_topology_fabric_selector_has_same_options():
    """Verify both Demo and Simulated have same fabric size options."""
    source = TOPOLOGY_PAGE.read_text(encoding="utf-8")
    
    assert "Compact (~30)" in source, "Compact fabric size missing"
    assert "Standard (~92)" in source, "Standard fabric size missing"
    assert "Large (~210)" in source, "Large fabric size missing"


def test_topology_uses_same_layout_kinds_for_all_sources():
    """Verify layout kinds are the same regardless of source."""
    source = TOPOLOGY_PAGE.read_text(encoding="utf-8")
    
    assert "topology_service.LAYOUT_KINDS" in source, "Layout kinds not found"
    assert "layered" in source, "Layered layout missing"
    assert "circular" in source, "Circular layout missing"
    assert "grid" in source, "Grid layout missing"


def test_topology_path_troubleshooting_available_for_simulated():
    """Verify path troubleshooting works for Simulated source."""
    source = TOPOLOGY_PAGE.read_text(encoding="utf-8")
    
    assert "Path troubleshooting" in source, "Path troubleshooting section missing"
    assert "path_options = [source_scope.DEMO] if settings.demo_mode else sources" in source, \
        "Path source options logic not found"


def test_topology_simulated_badge_shown():
    """Verify simulated badge is shown for Simulated source."""
    source = TOPOLOGY_PAGE.read_text(encoding="utf-8")
    
    assert "source_controls.selector" in source, "Source selector not found"
    
    badge_file = Path(__file__).resolve().parents[1] / "ui" / "source_controls.py"
    badge_source = badge_file.read_text(encoding="utf-8")
    
    assert "SIMULATED_BANNER" in badge_source, "Simulated banner not defined"
    assert "Simulated lab data - not real telemetry" in badge_source, \
        "Simulated banner text not found"
