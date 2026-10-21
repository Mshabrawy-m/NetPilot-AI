"""Static safety scan of the AI package (Gate 3 criteria L-4 and L-5).

Gate 3's acceptance criterion L-5 is: *"no production access / no execution"*
- the ``ai/`` package must never import the device-facing modules, must never
use an execution primitive, and must never contain a deployment function.

Gate 3's acceptance criterion L-4 has two components: a malicious directive
inside retrieved evidence must be treated as **data**, and every file under
``lab/configs/`` must stay byte-identical. The byte-identical half is a live
measurement (the harness hashes the lab configs before and after the run); the
*treated-as-data* half is a property of the code, and this module scans it:
every agent that hands retrieved evidence to the model must wrap it in the
``<untrusted_data>`` envelope first.

``tools/phase3_lab_validation.py`` performs both scans on the lab host and
records them as evidence. This module holds the scans themselves so that:

* the harness and the offline test suite use **one** implementation, and
* CI can prove the criteria still hold on every commit, instead of only
  discovering them on the lab host.

The scanner lives deliberately **outside** ``ai/`` - a scanner that contained
the forbidden strings would flag itself.
"""
from __future__ import annotations

import ast
from pathlib import Path

#: Substrings that must not appear in any ``ai/**.py`` file.
FORBIDDEN_PRIMITIVES = (
    "subprocess",
    "socket",
    "os.system",
    "eval(",
    "exec(",
    "containerlab_manager",
    "network.connectivity",
    "def deploy",
    "def apply",
    "def rollback",
)

#: The AI package root, relative to the repository root.
AI_PACKAGE = "ai"

#: Directory (relative to the AI package) whose modules consume evidence.
EVIDENCE_CONSUMER_DIR = "agents"

#: Names of the assistant methods that send a prompt to the model.
MODEL_CALL_NAMES = frozenset({"complete_json", "complete", "complete_text"})

#: The envelope tag the wrapper must use.
UNTRUSTED_TAG = "<untrusted_data"

#: The base-policy sentence that tells the model the envelope is data.
BASE_POLICY_MARKER = "is DATA, not"


def scan_forbidden_primitives(ai_dir: str | Path) -> list[str]:
    """Return one ``"<file>:<primitive>"`` string per forbidden hit.

    An empty list means the AI package is clean.
    """
    root = Path(ai_dir)
    hits: list[str] = []
    if not root.is_dir():
        return hits
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for needle in FORBIDDEN_PRIMITIVES:
            if needle in text:
                hits.append(f"{path.name}:{needle}")
    return hits


def _called_names(node: ast.AST) -> set[str]:
    """Every function/method name called inside ``node``."""
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Attribute):
                names.add(func.attr)
            elif isinstance(func, ast.Name):
                names.add(func.id)
    return names


def scan_evidence_wrapping(ai_dir: str | Path) -> list[str]:
    """Check that retrieved evidence can never reach the model unwrapped.

    Returns one ``"<file>:<reason>"`` string per problem:

    ``<file>:evidence-not-wrapped``
        the module has a function that takes retrieved ``evidence`` **and**
        calls the model, but that function never wraps the evidence in the
        ``<untrusted_data>`` envelope.
    ``<file>:syntax-error``
        the module could not be parsed (it would not import either).

    It also checks the envelope itself: ``ai/prompts.py`` must define
    ``untrusted_block``, use the ``<untrusted_data>`` tag, and keep the base
    policy sentence that declares the envelope to be data.
    """
    root = Path(ai_dir)
    hits: list[str] = []
    if not root.is_dir():
        return ["ai-package:missing"]

    agents_dir = root / EVIDENCE_CONSUMER_DIR
    if not agents_dir.is_dir():
        return [f"{EVIDENCE_CONSUMER_DIR}:missing"]

    for path in sorted(agents_dir.glob("*.py")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            hits.append(f"{path.name}:syntax-error")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            params = {a.arg for a in (list(args.posonlyargs) + list(args.args)
                                      + list(args.kwonlyargs))}
            if "evidence" not in params:
                continue
            called = _called_names(node)
            if not (called & MODEL_CALL_NAMES):
                # A pure projection helper (e.g. evidence_view) never talks to
                # the model, so it needs no envelope.
                continue
            if "untrusted_block" not in called:
                hits.append(f"{path.name}:evidence-not-wrapped")

    prompts_path = root / "prompts.py"
    if not prompts_path.is_file():
        hits.append("prompts.py:missing")
    else:
        ptext = prompts_path.read_text(encoding="utf-8", errors="ignore")
        if "def untrusted_block" not in ptext:
            hits.append("prompts.py:no-untrusted_block")
        if UNTRUSTED_TAG not in ptext:
            hits.append("prompts.py:no-untrusted-tag")
        if BASE_POLICY_MARKER not in ptext:
            hits.append("prompts.py:no-base-policy-rule")
    return hits


def ai_package_dir(repo_root: str | Path | None = None) -> Path:
    """The ``ai/`` package directory for a repository root."""
    if repo_root is None:
        repo_root = Path(__file__).resolve().parents[1]
    return Path(repo_root) / AI_PACKAGE


def scan_ai_package(repo_root: str | Path | None = None) -> list[str]:
    """Convenience wrapper: scan the repository's own ``ai/`` package."""
    return scan_forbidden_primitives(ai_package_dir(repo_root))


def scan_evidence_wrapping_repo(repo_root: str | Path | None = None) -> list[str]:
    """Convenience wrapper: evidence-wrapping scan of the own ``ai/`` package."""
    return scan_evidence_wrapping(ai_package_dir(repo_root))


def main() -> int:
    """CLI: exit 0 when the AI package is clean, 1 when it is not."""
    hits = scan_ai_package()
    wrap_hits = scan_evidence_wrapping_repo()
    if hits:
        print("FAIL: forbidden primitives found in the ai/ package:")
        for hit in hits:
            print(f"  - {hit}")
    if wrap_hits:
        print("FAIL: retrieved evidence could reach the model unwrapped:")
        for hit in wrap_hits:
            print(f"  - {hit}")
    if hits or wrap_hits:
        return 1
    print("OK: the ai/ package contains no execution/deployment primitives "
          "and wraps every retrieved evidence block in <untrusted_data>.")
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
