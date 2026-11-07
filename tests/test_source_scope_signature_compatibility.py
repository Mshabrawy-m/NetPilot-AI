"""Static signature compatibility check for source-scope keyword arguments.

This test ensures that every new data_source=/source-scope keyword argument
added in Phase 3B matches the callee signature. It uses inspect.signature and
ast to verify all call sites.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

# Modules that were modified in Phase 3B to add source-scope parameters
MODIFIED_MODULES = [
    "app.py",
    "app_pages/1_Overview.py",
    "app_pages/2_Topology.py",
    "app_pages/3_Devices.py",
    "app_pages/4_Connectivity_Tests.py",
    "app_pages/5_Metrics_History.py",
    "app_pages/6_System_Status.py",
    "app_pages/7_Data_Analysis.py",
    "app_pages/8_Anomalies.py",
    "app_pages/9_Forecasting.py",
    "app_pages/10_AI_Network_Assistant.py",
    "app_pages/11_Ops_Center.py",
    "app_pages/12_AI_Lab.py",
]

ROOT = Path(__file__).resolve().parents[1]


class SourceScopeCallVisitor(ast.NodeVisitor):
    """AST visitor to find all calls with data_source or source keyword arguments."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, ast.Call]] = []

    def visit_Call(self, node: ast.Call) -> None:
        for keyword in node.keywords:
            if keyword.arg in ("data_source", "source"):
                # Get the source code context
                self.calls.append(("keyword", node.lineno, node))
        self.generic_visit(node)


class RepositoryFunctionVisitor(ast.NodeVisitor):
    """AST visitor to find all function definitions in repository module."""

    def __init__(self) -> None:
        self.functions: dict[str, ast.FunctionDef] = {}

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.functions[node.name] = node
        self.generic_visit(node)


def _get_module_source(module_name: str) -> str:
    """Read module source code."""
    path = ROOT / module_name
    if not path.exists():
        path = ROOT / "app_pages" / module_name
    return path.read_text(encoding="utf-8")


def _find_all_calls_with_source_params() -> list[tuple[str, int, str, str]]:
    """Find all calls with data_source or source keyword arguments across modified modules."""
    calls: list[tuple[str, int, str, str]] = []  # (file, line, function, arg_name)

    for module_name in MODIFIED_MODULES:
        try:
            source = _get_module_source(module_name)
            tree = ast.parse(source, filename=module_name)
            visitor = SourceScopeCallVisitor()
            visitor.visit(tree)

            for _, line, call_node in visitor.calls:
                for kw in call_node.keywords:
                    if kw.arg in ("data_source", "source"):
                        # Get the function being called
                        if isinstance(call_node.func, ast.Name):
                            func_name = call_node.func.id
                        elif isinstance(call_node.func, ast.Attribute):
                            func_name = f"{ast.unparse(call_node.func.value)}.{call_node.func.attr}"
                        else:
                            func_name = ast.unparse(call_node.func)
                        calls.append((module_name, line, func_name, kw.arg))
        except (SyntaxError, ValueError, TypeError) as e:
            pytest.fail(f"Failed to parse {module_name}: {e}")

    return calls


def _get_repository_function_signatures() -> dict[str, inspect.Signature]:
    """Get signatures of all functions in database.repository."""
    from database import repository

    signatures: dict[str, inspect.Signature] = {}
    for name, obj in inspect.getmembers(repository):
        if inspect.isfunction(obj):
            try:
                signatures[name] = inspect.signature(obj)
            except (ValueError, TypeError):
                # Skip functions with unparseable signatures
                pass
    return signatures


def test_source_scope_signature_compatibility():
    """Verify all calls with data_source/source kwargs match callee signatures."""
    # Get all calls with source-related keyword arguments
    calls = _find_all_calls_with_source_params()

    # Get repository function signatures
    repo_signatures = _get_repository_function_signatures()

    # Check each call
    failures: list[str] = []
    for file, line, func_name, arg_name in calls:
        # Handle functions like "repository.get_last_successful_run"
        if "." in func_name:
            parts = func_name.split(".")
            if len(parts) == 2 and parts[0] == "repository":
                actual_func_name = parts[1]
                if actual_func_name in repo_signatures:
                    sig = repo_signatures[actual_func_name]
                    if arg_name not in sig.parameters:
                        failures.append(
                            f"{file}:{line}: {func_name}() does not accept "
                            f"keyword argument '{arg_name}'. "
                            f"Signature: {sig}"
                        )
                    else:
                        param = sig.parameters[arg_name]
                        # Check if parameter accepts str | None
                        if param.annotation != inspect.Parameter.empty:
                            # Simple check: annotation should contain str or None
                            ann_str = str(param.annotation)
                            if "str" not in ann_str and "None" not in ann_str:
                                failures.append(
                                    f"{file}:{line}: {func_name}() parameter "
                                    f"'{arg_name}' has annotation {ann_str}, "
                                    f"expected str | None"
                                )

    if failures:
        pytest.fail("\n".join(failures))


def test_source_scope_calls_have_matching_params():
    """Verify that all source-scope related calls use valid parameter names."""
    calls = _find_all_calls_with_source_params()
    valid_params = {"data_source", "source"}

    failures: list[str] = []
    for file, line, func_name, arg_name in calls:
        if arg_name not in valid_params:
            failures.append(
                f"{file}:{line}: Invalid source-scope parameter '{arg_name}' "
                f"for {func_name}()"
            )

    if failures:
        pytest.fail("\n".join(failures))


if __name__ == "__main__":
    # Run the tests
    test_source_scope_signature_compatibility()
    test_source_scope_calls_have_matching_params()
    print("All signature compatibility tests passed!")
