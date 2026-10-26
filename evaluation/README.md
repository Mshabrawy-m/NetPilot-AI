# Offline AI-quality evaluation (B4)

This directory contains a **deterministic, offline, version-controlled**
evaluation of the AI workflows that already exist in NetPilot AI. It answers
"does the assistant stay grounded, cite real evidence, abstain when evidence is
missing, resist prompt injection, and respect the approval boundary?" — without
calling a provider or touching a network.

## Run it

```powershell
.\.venv\Scripts\python.exe tools\ai_quality_eval.py
```

The command:

- uses only fixed synthetic fixtures and scripted offline assistants;
- needs **no API key**, **no network access**, **no real model call** and **no
  paid service**;
- creates an isolated temporary SQLite database per case;
- writes a machine-readable report to
  `evaluation/reports/ai_quality_report.json` (generated artifacts are
  git-ignored and kept separate from the versioned dataset).

Exit codes:

| Code | Meaning |
| ---- | ------- |
| `0`  | every case passed and every safety gate passed |
| `1`  | one or more cases failed |
| `2`  | the dataset or harness itself errored |
| `3`  | a safety gate failed (takes precedence over all other statuses) |

Optional flags: `--dataset PATH`, `--report PATH`, `--work-dir PATH`, `--quiet`.

## What is evaluated

The dataset is `datasets/ai_quality_v1.json` (evaluation set `ai_quality_v1`,
version `1.0.0`). It contains **27 synthetic cases** across:

- grounded English and Arabic Q&A;
- incomplete evidence and undeterminable questions (must abstain);
- stale and contradictory telemetry (must disclose, not smooth over);
- unsupported model claims (must not become observed facts);
- prompt injection inside stored evidence (must stay untrusted data);
- safety-control bypass attempts (unsafe config, ambiguous change requests);
- provider-unavailable and invalid-schema failures (must fail safe);
- RCA ground truth, anomaly outcomes, valid/insufficient forecasting;
- deterministic NL data queries, RAG retrieval and verifier outcomes;
- the evidence-tool untrusted-data boundary.

Each case records its expected behaviour and a rationale, and is explicitly
labelled `"synthetic": true`. **No real incident, real measurement, provider
response or user data is represented.** Never cite these fixtures as evidence
about a real network.

## Metrics

All metrics are deterministic pass/fail ratios. A metric with a zero
denominator is reported as `unavailable` (never as 100%).

| Metric | Unit | Numerator / denominator |
| ------ | ---- | ----------------------- |
| `groundedness` | attempted factual claim | claims whose every `[ev-N]` citation resolves to real evidence / all claims the scripted model attempted, including claims the guard moved to unverified observations |
| `evidence_relevance` | case | cases where required evidence was gathered and cited / cases declaring a required selector |
| `abstention_correctness` | case | correct abstain/answer decisions / cases with an abstention expectation |
| `safety_compliance` | case | cases where all safety assertions passed / cases with a safety assertion |
| `task_correctness` | case | cases where all task assertions passed / cases with a task assertion |
| `structured_output_validity` | case | typed-JSON or safe-stop outcomes / cases with a structured-output assertion |
| `language_handling` | case | response language/markers matched / cases with a language assertion |
| `robustness` | case | stale/contradiction/failure handling passed / cases with a robustness assertion |

A **case** can pass while still contributing an ungrounded claim to the
`groundedness` denominator: the adversarial unsupported-claim fixture is
designed so the model attempts a claim without evidence and the guard correctly
prevents it from being presented as fact. `safety_compliance` measures the
guard; `groundedness` measures the attempted claim.

### Automated vs human/LLM judged

Everything in this harness is an **automated assertion**. No LLM judge is used
and no LLM judge is the sole correctness measure. Semantic quality beyond the
declared assertions is out of scope; a separate, optional human/LLM-judged
layer would have to be run and reported separately.

## Safety gates

The run fails closed (exit `3`) unless all of these hold:

- `execution_flags_disabled` — change-loop and lab-execution flags are false
  before and after the run;
- `evaluation_isolation` — no shell/provider/approval imports or dynamic code in
  `evaluation/` or the CLI;
- `secret_scan_clean` — the repository secret scan is clean;
- `dataset_immutable` — the dataset is byte-identical before and after;
- `synthetic_fixtures_only` — every case is labelled synthetic;
- `no_live_or_provider_claims` — the report marks live network, Streamlit and
  provider evaluation as not run.

The evaluator is **data-driven**: a dataset may describe *what* to score, never
*what code to run*. Evaluators are selected from a fixed whitelist in
`runner.py`, and an evaluator exception is recorded as an error, never a pass.

## Scope boundaries

This report does **not** test:

- live network behaviour or a real device;
- Streamlit UI rendering;
- Groq/provider response quality;
- Gate 3 isolated-lab validation;
- Gate D apply/verify/rollback.

`live_provider_evaluation.status` is `not_run` and `counted_in_metrics` is
`false`. Test-suite pass rates (pytest) are engineering checks, **not** AI
quality scores; this report is the AI quality score.

## Adding a case

1. Add a fixture under `fixtures` (or reuse one).
2. Add a case with `synthetic: true`, a `rationale`, an explicit `expect`
   block, and the metrics it declares.
3. Use only whitelisted `workflow` values and `expect`/`params` keys.
4. Run the command; the harness rejects unknown keys, undefined fixtures,
   duplicate ids, non-synthetic cases and undeclared metrics.
