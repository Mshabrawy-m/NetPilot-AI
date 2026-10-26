"""Static isolation scan for the offline AI-quality evaluation harness (B4).

The evaluator is deliberately data-driven: a dataset may describe *what* to
score, never *what code to run*. This scanner enforces that boundary on every
run (and in CI) by checking that the evaluation package and its CLI contain no
shell/network/provider/approval execution primitives.

It lives outside ``evaluation/`` for the same reason ``tools/safety_scan.py``
lives outside ``ai/``: a scanner that contained the forbidden strings would
otherwise flag itself.
"""
from __future__ import annotations

import ast
from pathlib import Path

#: Imports that would let the evaluator leave its offline/read-only sandbox.
FORBIDDEN_IMPORTS = frozenset({
    "subprocess", "socket", "shutil", "ctypes", "multiprocessing",
    "importlib", "streamlit", "groq", "paramiko", "requests",
})

#: Substrings that would indicate execution, provider calls, or bypassing the
#: application's approval / change-loop gates.
FORBIDDEN_SUBSTRINGS = (
    "os.system", "os.popen", "Popen(", "__import__", "GroqAssistant(",
    "services.change_execution", "services.change_service",
    "services.ops_center_actions", "network.lab_executor",
    "network.connectivity", "containerlab",
    "NETPILOT_CHANGE_LOOP_ENABLED", "NETPILOT_LAB_EXECUTION_ENABLED",
    "NETPILOT_LAB_EXECUTION_ENABLED=true",
)

#: Files/directories the scan covers (relative to the repository root).
SCAN_DIRS = ("evaluation",)
SCAN_FILES = ("tools/ai_quality_eval.py",)


def _iter_targets(repo_root: Path):
    for rel in SCAN_DIRS:
        base = repo_root / rel
        if base.is_dir():
            yield from sorted(base.rglob("*.py"))
    for rel in SCAN_FILES:
        path = repo_root / rel
        if path.is_file():
            yield path


def scan_evaluation_isolation(repo_root: str | Path) -> list[str]:
    """Return one ``"<relative path>:<reason>"`` per isolation violation."""
    root = Path(repo_root)
    hits: list[str] = []
    for path in _iter_targets(root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(text)
        except SyntaxError:
            hits.append(f"{path.relative_to(root).as_posix()}:syntax-error")
            continue
        for needle in FORBIDDEN_SUBSTRINGS:
            if needle in text:
                hits.append(
                    f"{path.relative_to(root).as_posix()}:{needle}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_name = alias.name.split(".", 1)[0]
                    if root_name in FORBIDDEN_IMPORTS:
                        hits.append(
                            f"{path.relative_to(root).as_posix()}:"
                            f"import-{root_name}")
            elif isinstance(node, ast.ImportFrom):
                root_name = (node.module or "").split(".", 1)[0]
                if root_name in FORBIDDEN_IMPORTS:
                    hits.append(
                        f"{path.relative_to(root).as_posix()}:"
                        f"import-{root_name}")
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in {
                        "eval", "exec", "__import__"}:
                    hits.append(
                        f"{path.relative_to(root).as_posix()}:"
                        f"call-{func.id}")
    # Deterministic, de-duplicated output.
    return sorted(set(hits))


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    hits = scan_evaluation_isolation(root)
    if hits:
        print("FAIL: evaluation harness is not isolated:")
        for hit in hits:
            print(f"  - {hit}")
        return 1
    print("OK: evaluation harness has no shell/provider/approval execution "
          "primitives and no dynamic-code imports.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
