"""Containerlab CLI wrapper.

Every command here is a fixed, allowlisted argv list executed with
``shell=False`` and a timeout. The only interpolated values are the lab
name and topology path taken from configuration (never user input from
the UI). If containerlab or Docker is missing, functions report that
state instead of raising - the dashboard then shows "lab unavailable".
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass

from config import get_settings

_CMD_TIMEOUT_SECONDS = 120
_OUTPUT_LIMIT_BYTES = 512_000


@dataclass
class CommandResult:
    ok: bool
    returncode: int
    stdout: str
    error: str | None = None
    #: True when the command was killed by the hard timeout. Callers must
    #: treat a timeout as "state unknown" - never as "the change failed
    #: cleanly", because the command may have taken effect before it died.
    timed_out: bool = False


def _run(argv: list[str], timeout: int = _CMD_TIMEOUT_SECONDS) -> CommandResult:
    exe = shutil.which(argv[0])
    if exe is None:
        return CommandResult(False, -1, "", f"{argv[0]!r} is not installed")
    try:
        proc = subprocess.run(
            [exe, *argv[1:]], shell=False,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(False, -1, "",
                             f"{argv[0]} timed out ({timeout}s)",
                             timed_out=True)
    except OSError as exc:
        return CommandResult(False, -1, "", f"{type(exc).__name__}: {exc}")
    out = (proc.stdout or "")[:_OUTPUT_LIMIT_BYTES]
    err = ((proc.stderr or "")[:4000] or None) if proc.returncode != 0 else None
    return CommandResult(proc.returncode == 0, proc.returncode, out, err)


def availability() -> dict[str, bool]:
    """What this host supports - shown on the System Status page."""
    return {
        "docker": shutil.which("docker") is not None,
        "containerlab": shutil.which("containerlab") is not None,
    }


def inspect_nodes(lab_name: str | None = None,
                  topology_file: str | None = None) -> list[dict]:
    """Return containerlab's node table (parsed JSON) or [] if unavailable.

    Uses ``containerlab inspect -g <topology> --format json`` which reports
    container state and management IPv4 for each node.
    """
    settings = get_settings()
    lab_name = lab_name or settings.lab_name
    topology_file = topology_file or settings.lab_topology_file
    result = _run(["containerlab", "inspect", "-g", topology_file,
                   "--format", "json", "--snip"])
    if not result.ok:
        # Older/newer versions may not support --snip; retry without it.
        result = _run(["containerlab", "inspect", "-g", topology_file,
                       "--format", "json"])
    if not result.ok:
        return []
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []
    nodes = data if isinstance(data, list) else data.get("nodes", data.get("results", []))
    return nodes if isinstance(nodes, list) else []


def deploy(reconfigure: bool = False) -> CommandResult:
    settings = get_settings()
    argv = ["containerlab", "deploy", "-t", settings.lab_topology_file]
    if reconfigure:
        argv.append("--reconfigure")
    return _run(argv)


def destroy() -> CommandResult:
    settings = get_settings()
    return _run(["containerlab", "destroy", "-t", settings.lab_topology_file,
                 "--cleanup"])


def redeploy() -> CommandResult:
    """Destroy then deploy - documented as the 'redeploy' operation."""
    destroy_result = destroy()
    if not destroy_result.ok:
        return destroy_result
    return deploy(reconfigure=True)


def docker_exec(container: str, argv: list[str],
                timeout: int = 20) -> CommandResult:
    """Run a fixed command inside a lab container (used by the collector).

    ``container`` must come from the inventory (which itself is parsed from
    the topology file / containerlab inspect); ``argv`` is always a
    hard-coded list from metrics_collector - never user input.
    """
    if not container or len(container) > 128 or not shutil.which("docker"):
        return CommandResult(False, -1, "", "docker unavailable or bad container name")
    return _run(["docker", "exec", container, *argv], timeout=timeout)


def docker_inspect_labels(container: str, timeout: int = 15
                          ) -> dict[str, str] | None:
    """Return the Docker labels of a container, or None when unavailable.

    Containerlab stamps every node it creates with its own labels
    (``clab_node_name``, ``clab_topology_file``, ...). Reading them back is
    how the executor PROVES that a container really is a node of the
    configured lab topology, instead of trusting a name it was handed.
    """
    if not container or len(container) > 128 or not shutil.which("docker"):
        return None
    result = _run(["docker", "inspect", "--format", "{{json .Config.Labels}}",
                   container], timeout=timeout)
    if not result.ok or not (result.stdout or "").strip():
        return None
    try:
        data = json.loads((result.stdout or "").strip())
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
