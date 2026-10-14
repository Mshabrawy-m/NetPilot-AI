"""Read-only evidence tools (Phase 3 spec §5 / §10).

Every function here only *reads* existing Phase 1/2 data through
``database.repository`` / existing services. There is deliberately **no**
arbitrary shell, SQL, Python or filesystem access exposed to the LLM. Bounds
(row counts, byte size) are enforced *inside* the tools, not left to prompts,
and each result carries provenance (tool, source, timestamp, unit) plus an
explicit missing-data / stale flag so nothing absent is ever invented.
"""
