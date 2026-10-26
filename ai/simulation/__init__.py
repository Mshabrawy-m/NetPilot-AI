"""Digital-Twin simulation boundary (Phase 3 spec §7).

An interface for *future* simulation only. Phase 3 ships no safe isolated
simulator, so the default implementation honestly returns ``not_run`` with the
reason. Reporting a pass/fail here would be fabrication and is forbidden.
Full simulation, deployment, post-change verification and rollback are Phase 4.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class SimulationResult:
    #: ``not_run`` (Phase 3 default) or an honest simulation verdict:
    #: ``simulated`` / ``partial`` / ``insufficient_evidence`` / ``invalid`` /
    #: ``not_simulated`` / ``error``. A ``simulated`` result is structural
    #: only and never proves a real change is safe.
    status: str = "not_run"
    checks: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    simulator_version: str | None = None
    reasons: list = field(default_factory=list)
    #: Structured detail added by the Phase D1 engine. The defaults keep the
    #: Phase 3 ``not_run`` boundary backward compatible.
    assumptions: list = field(default_factory=list)
    limitations: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    affected_components: dict = field(default_factory=dict)
    topology_ref: str | None = None
    change_ref: dict = field(default_factory=dict)
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)
    not_proof_of_safety: bool = True
    #: Full engine report for callers that need evidence beyond the summary
    #: fields above. Additive and empty for the Phase 3 ``not_run`` boundary.
    details: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "checks": self.checks,
            "evidence": self.evidence,
            "simulator_version": self.simulator_version,
            "reasons": self.reasons,
            "assumptions": self.assumptions,
            "limitations": self.limitations,
            "conflicts": self.conflicts,
            "affected_components": self.affected_components,
            "topology_ref": self.topology_ref,
            "change_ref": self.change_ref,
            "before": self.before,
            "after": self.after,
            "not_proof_of_safety": self.not_proof_of_safety,
            "details": self.details,
        }


class DigitalTwin(ABC):
    """Interface the graph calls. Inputs describe a scenario; outputs must be
    honest about whether an isolated lab actually executed it."""

    @abstractmethod
    def simulate(self, *, topology_ref: str | None, proposed_change: dict | None,
                 invariants: list[str], scenario: str | None) -> SimulationResult:
        ...


class UnavailableDigitalTwin(DigitalTwin):
    """The Phase 3 default: no isolated simulator is wired up -> not_run."""

    def simulate(self, *, topology_ref=None, proposed_change=None,
                 invariants=None, scenario=None) -> SimulationResult:
        return SimulationResult(
            status="not_run",
            reasons=["No isolated Digital-Twin simulator is available in "
                     "Phase 3; simulation is deferred to Phase 4. This is "
                     "reported as 'not_run' rather than fabricated."],
        )
