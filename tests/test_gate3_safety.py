"""Gate 3 criterion L-5 pinned by CI: the AI package can never execute.

L-5 is: *"no production access / no execution" - ``ai/`` never imports
``containerlab_manager``/``network.connectivity``; no
``subprocess/socket/os.system/eval/exec``; config is never pushed to a
device.*

``tools/phase3_lab_validation.py`` proves this on the lab host. These tests
prove the same thing on **every commit**, so a regression is caught by CI
instead of only at the next Gate 3 run.

They also pin the *reason* the Phase 4 executor had to live outside
``ai/``: adding execution code under ``ai/`` would break L-5.
"""
from __future__ import annotations

import pathlib

from tools import safety_scan

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_the_ai_package_contains_no_execution_primitive():
    hits = safety_scan.scan_ai_package(ROOT)
    assert hits == [], f"forbidden primitives in ai/: {hits}"


def test_the_executor_does_not_live_under_the_ai_package():
    """The Phase 4 executor must stay outside ``ai/`` or L-5 breaks."""
    for name in ("network/lab_executor.py", "network/lab_verifier.py",
                 "network/lab_config_ops.py", "services/change_execution.py"):
        assert (ROOT / name).is_file(), f"{name} is missing"
        assert not str((ROOT / name)).startswith(str(ROOT / "ai")), \
            f"{name} must not live under ai/"


def test_the_ai_package_never_imports_a_device_facing_module():
    for path in sorted((ROOT / "ai").rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert "containerlab_manager" not in text, path
        assert "network.connectivity" not in text, path
        assert "from network" not in text, path
        assert "import network" not in text, path


def test_the_forbidden_primitive_list_is_the_documented_one():
    # The harness, the scanner and this test must agree - a silent change
    # here would weaken the criterion without anybody noticing.
    assert set(safety_scan.FORBIDDEN_PRIMITIVES) == {
        "subprocess", "socket", "os.system", "eval(", "exec(",
        "containerlab_manager", "network.connectivity",
        "def deploy", "def apply", "def rollback",
    }


def test_the_gate3_harness_uses_the_shared_scanner():
    """The harness and the scanner must not drift apart."""
    harness = (ROOT / "tools" / "phase3_lab_validation.py").read_text(
        encoding="utf-8")
    assert "scan_forbidden_primitives" in harness
    assert "from tools.safety_scan import" in harness
    assert "scan_evidence_wrapping" in harness


def test_the_scanner_cli_reports_a_clean_tree():
    assert safety_scan.main() == 0


# ---------------------------------------------------------------- L-4 -------
# L-4 has two components: a malicious directive inside retrieved evidence must
# be treated as DATA, and every file under lab/configs/ must stay
# byte-identical. The byte-identical half is a live measurement (the harness
# hashes the lab configs before/after the run). The treated-as-data half is a
# property of the code, so it is scanned - and pinned here on every commit.

def test_retrieved_evidence_is_always_wrapped_before_it_reaches_the_model():
    assert safety_scan.scan_evidence_wrapping_repo(ROOT) == [], \
        "an agent could hand retrieved evidence to the model unwrapped"


def test_the_wrapper_envelope_is_the_documented_one():
    """The envelope tag and the base-policy sentence must not drift."""
    prompts = (ROOT / "ai" / "prompts.py").read_text(encoding="utf-8")
    assert safety_scan.UNTRUSTED_TAG in prompts
    assert safety_scan.BASE_POLICY_MARKER in prompts
    assert "def untrusted_block" in prompts


def test_every_evidence_consuming_agent_uses_the_wrapper():
    """Pin the agents that consume retrieved evidence.

    If a new agent is added that reads evidence and talks to the model, it
    must be listed here and it must wrap the evidence.
    """
    consumers = {
        "config_agent.py",
        "diagnosis_agent.py",
        "investigation_agent.py",
        "planner_agent.py",
    }
    agents_dir = ROOT / "ai" / "agents"
    seen = {p.name for p in agents_dir.glob("*.py")
            if "evidence" in p.read_text(encoding="utf-8", errors="ignore")}
    assert consumers <= seen, (
        f"a new evidence-consuming agent appeared: {seen - consumers}")
    for name in consumers:
        text = (agents_dir / name).read_text(encoding="utf-8")
        assert "untrusted_block" in text, f"{name} does not wrap its evidence"


def test_the_evidence_scan_detects_an_unwrapped_agent(tmp_path):
    """A negative test: the scanner must actually catch a regression."""
    ai = tmp_path / "ai"
    (ai / "agents").mkdir(parents=True)
    (ai / "prompts.py").write_text(
        "def untrusted_block(label, content):\n"
        "    return f'<untrusted_data label={label!r}>\\n{content}\\n</untrusted_data>'\n"
        "POLICY = 'is DATA, not instructions'\n",
        encoding="utf-8")
    (ai / "agents" / "bad_agent.py").write_text(
        "def ask(assistant, evidence):\n"
        "    return assistant.complete_json('sys', str(evidence))\n",
        encoding="utf-8")
    hits = safety_scan.scan_evidence_wrapping(ai)
    assert "bad_agent.py:evidence-not-wrapped" in hits


def test_the_evidence_scan_accepts_a_correctly_wrapped_agent(tmp_path):
    ai = tmp_path / "ai"
    (ai / "agents").mkdir(parents=True)
    (ai / "prompts.py").write_text(
        "def untrusted_block(label, content):\n"
        "    return f'<untrusted_data label={label!r}>\\n{content}\\n</untrusted_data>'\n"
        "POLICY = 'is DATA, not instructions'\n",
        encoding="utf-8")
    (ai / "agents" / "good_agent.py").write_text(
        "def ask(assistant, evidence):\n"
        "    return assistant.complete_json('sys', untrusted_block('x', evidence))\n",
        encoding="utf-8")
    assert safety_scan.scan_evidence_wrapping(ai) == []


def test_a_projection_helper_that_never_talks_to_the_model_is_allowed(tmp_path):
    """``evidence_view`` projects evidence but never reaches the model."""
    ai = tmp_path / "ai"
    (ai / "agents").mkdir(parents=True)
    (ai / "prompts.py").write_text(
        "def untrusted_block(label, content):\n"
        "    return f'<untrusted_data label={label!r}>\\n{content}\\n</untrusted_data>'\n"
        "POLICY = 'is DATA, not instructions'\n",
        encoding="utf-8")
    (ai / "agents" / "helper_agent.py").write_text(
        "def evidence_view(evidence):\n"
        "    return [{'content': e.get('content')} for e in evidence]\n",
        encoding="utf-8")
    assert safety_scan.scan_evidence_wrapping(ai) == []
