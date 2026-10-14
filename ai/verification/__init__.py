"""Independent, deterministic verification (Phase 3 spec §5).

Nothing here consults the LLM's self-assessment. The verifier runs pure-Python
checks against the lab inventory and a fixed policy, returning
``passed`` / ``failed`` / ``inconclusive`` with per-check findings and evidence
references. A static pass never claims live behaviour.
"""
