"""What gets *published* and what gets *shipped* are two different filters.

`.gitignore` keeps local verification paperwork out of the repository, but a
container build reads `.dockerignore` instead, and `COPY . .` therefore sees any
git-ignored file that the image filter does not name. That is not cosmetic:
`services/rag.py` indexes every Markdown file it can find, so a report that is
git-ignored yet docker-visible would be baked into a distributed image and then
quoted back to users through the offline retriever.

These tests pin the relationship between the two filters, the `docs/` layout the
README describes, and the claim that the shared Streamlit config holds theme
settings only (nothing that could break or leak on a hosted deployment).
Everything here is pure text/TOML inspection - no Docker engine, no network.
"""
from __future__ import annotations

import fnmatch
import pathlib
import tomllib

from services import rag
from tools import secret_scan_check as scan

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Local-only verification paperwork. Adding to this list means adding the same
#: name to .gitignore *and* .dockerignore (the second test below enforces that).
LOCAL_ONLY_PAPERWORK = [
    "V2_Acceptance_Gate_Report.md",
    "Acceptance_Gate1_Security_Repo_Readiness.md",
    "Phase3_Acceptance_Audit.md",
    "Phase3_Final_Acceptance_Report.md",
    "NETPILOT_GATE2_CI_DOCKER_ACCEPTANCE.md",
    "NETPILOT_GATE2_COMPLETION_REPORT.md",
    "NETPILOT_GATE2_STREAMLIT_RUNTIME_ACCEPTANCE.md",
    "NETPILOT_GATE2_STREAMLIT_CLOUD_DEPLOY_RUNBOOK.md",
    "Phase3_Gate3_Gap_Report.md",
    "Phase3_Gate3_Remediation_Report.md",
    "Phase3_Gate3_Task_Board.md",
    "Phase3_Gate3_Acceptance_Matrix.md",
    "NETPILOT_GATE3_ISOLATED_LAB_RUNBOOK.md",
]

#: Documentation that belongs to the product, published under docs/.
PUBLISHED_DOCS = [
    "NetPilot_Phase_1_Network_Foundation_EN.md",
    "NetPilot_Phase_2_Data_Analysis_Forecasting_EN.md",
    "NetPilot_Phase_3_Multi_Agent_AI_Safe_Operations_EN.md",
    "NetPilot_AI_Advanced_Roadmap.md",
    "Phase2_Delivery_Report.md",
    "Phase3_Delivery_Report.md",
]


def _rules(name: str) -> list[str]:
    """Active (non-comment, non-empty) lines of an ignore file."""
    text = (ROOT / name).read_text(encoding="utf-8")
    return [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def _is_matched(rel: str, patterns: list[str]) -> bool:
    """Approximate Docker's .dockerignore matching for the simple patterns used
    here: a bare name matches at the context root, '**/'-prefixed anywhere."""
    for pat in patterns:
        if pat.startswith("!"):          # negation wins, as in the real syntax
            continue
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, "**/" + pat):
            return True
    return False


def test_local_only_paperwork_is_git_ignored():
    ignored = _rules(".gitignore")
    for name in LOCAL_ONLY_PAPERWORK:
        assert name in ignored, f"{name} must stay out of the published tree"


def test_local_only_paperwork_never_reaches_the_image():
    docker_rules = _rules(".dockerignore")
    for name in LOCAL_ONLY_PAPERWORK:
        assert _is_matched(name, docker_rules), (
            f"{name} is git-ignored but would still be copied into the image by "
            f"'COPY . .' - add it to .dockerignore")


def test_env_and_databases_are_blocked_from_the_image():
    docker_rules = _rules(".dockerignore")
    for name in (".env", "data/netpilot_demo.db", "data/_gate2_auth.db"):
        assert _is_matched(name, docker_rules), f"{name} must not ship"
    # the template is config, not a secret, so it must survive the filter
    assert "!.env.example" in docker_rules


def test_published_docs_live_under_docs_and_are_still_indexable():
    for name in PUBLISHED_DOCS:
        assert (ROOT / "docs" / name).is_file(), f"docs/{name} is expected"
        assert not (ROOT / name).exists(), f"{name} must not sit at the root"
    # the retriever has to see the moved files, or the Knowledge tab goes quiet
    assert "docs/*.md" in rag.DEFAULT_GLOBS
    found = {p.name for p in rag.discover_docs(ROOT)}
    missing = [n for n in PUBLISHED_DOCS if n not in found]
    assert not missing, f"RAG no longer indexes {missing}"


def test_readme_points_at_the_current_doc_paths():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/NetPilot_AI_Advanced_Roadmap.md" in readme
    stale = [f"`{n}`" for n in PUBLISHED_DOCS if f"`{n}`" in readme]
    assert not stale, f"README references root-level doc paths: {stale}"


def test_streamlit_config_is_theme_only_and_cloud_safe():
    """Community Cloud owns host/port/secrets, so the checked-in config must not
    try to override any of that (and must obviously hold no credential).

    The single exception is ``server.fileWatcherType = "none"``. Streamlit's
    watcher evicts watched application modules from ``sys.modules`` at the start
    of a run, and both SQLAlchemy (``Mapped[...]``) and CPython's ``dataclasses``
    resolve a class's defining module through ``sys.modules`` while its class
    body executes. One eviction during a rebuild therefore took the whole app
    down, twice. Nothing edits files in deployment, so the watcher stays off (the
    option is not scriptable, so no env var or CLI flag can override it — local
    working copies flip it back to "auto" by hand).
    """
    cfg = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text(
        encoding="utf-8"))
    assert set(cfg) <= {"theme", "browser", "client", "server"}, sorted(cfg)
    assert "secrets" not in cfg
    # Only the watcher switch - never host, port, address or headless.
    assert cfg.get("server") == {"fileWatcherType": "none"}, cfg.get("server")
    for line in (ROOT / ".streamlit" / "config.toml").read_text(
            encoding="utf-8").splitlines():
        assert not scan._GROQ.search(line)
        assert not scan._ASSIGNED.search(line)


def test_both_deploy_targets_ship_the_ping_binary():
    """The connectivity page executes the real ``ping`` binary. Neither a
    ``python:slim`` image nor Streamlit Community Cloud includes it by
    default, which turned the loopback self-test into a misleading failure.
    """
    packages = _rules("packages.txt")
    assert "iputils-ping" in packages, packages
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "iputils-ping" in dockerfile
