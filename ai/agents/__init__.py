"""Phase 3 agents.

Each agent builds a policy-bounded prompt, calls the Groq adapter, and returns
a **schema-validated** object. Where a safety property must not depend on the
LLM behaving (language detection, "no config for unknown devices",
"clarify when required fields are missing"), it is enforced in code here.
"""
