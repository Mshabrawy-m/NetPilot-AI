"""Page 13 - Evaluation (read-only evidence from committed result files).

This page deliberately performs **no** database, network or LLM access. It
renders the A/B/C safety ablation and the forecast backtest summary from the
three committed artefacts below, so it behaves identically in Demo,
Simulated and Live modes and on Streamlit Community Cloud:

* ``docs/agent_eval_results.csv``
* ``docs/agent_eval_summary.md``
* ``docs/forecast_backtest_results.csv``

All percentages and confidence intervals are recomputed from the CSV at
render time; the markdown file supplies the human-written failure cases and
interpretation. Nothing is hard-coded from a previous run.
"""
from __future__ import annotations

import csv
import math
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st

from ui import theme

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
RESULTS_CSV = DOCS / "agent_eval_results.csv"
SUMMARY_MD = DOCS / "agent_eval_summary.md"
BACKTEST_CSV = DOCS / "forecast_backtest_results.csv"

CONFIG_LABELS = {
    "A": "A: LLM only",
    "B": "B: LLM + Verifier",
    "C": "C: LLM + Verifier + Digital Twin",
}
CONFIGS = ("A", "B", "C")
CATEGORY_ORDER = (
    "prompt_injection",
    "destructive_command",
    "wrong_device",
    "network_isolation_or_loop",
    "out_of_scope",
)

# Styling only - no bootstrap(), so this page never opens a database
# connection, starts the scheduler, or requires a lab.
theme.inject_css()
theme.apply_plotly_theme()
st.sidebar.caption("Static evidence page - no database, network or LLM access.")
theme.page_header(
    "Evaluation",
    "Offline A/B/C safety ablation and forecast backtest, read from committed files",
    icon="🧾",
)


def _load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _section(markdown: str, heading: str) -> str:
    """Return the body of a level-2 markdown section, without the heading."""
    marker = f"## {heading}"
    start = markdown.find(marker)
    if start == -1:
        return ""
    start = markdown.find("\n", start)
    if start == -1:
        return ""
    end = markdown.find("\n## ", start + 1)
    return markdown[start + 1:end if end != -1 else None].strip()


def _flag(value: object) -> bool:
    return str(value).strip().lower() == "true"


def _wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion (percentages)."""
    if total <= 0:
        return (float("nan"), float("nan"))
    p = successes / total
    denom = 1.0 + z * z / total
    centre = (p + z * z / (2.0 * total)) / denom
    half = z * math.sqrt(p * (1.0 - p) / total + z * z / (4.0 * total * total)) / denom
    return ((centre - half) * 100.0, (centre + half) * 100.0)


def _rate(successes: int, total: int) -> str:
    if total <= 0:
        return "n/a"
    low, high = _wilson(successes, total)
    return (f"{successes}/{total} ({successes / total * 100:.1f}%; "
            f"95% CI {low:.1f}%-{high:.1f}%)")


def _pct(successes: int, total: int) -> float:
    return successes / total * 100.0 if total else 0.0


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _median(values: list[float]) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


missing = [p.name for p in (RESULTS_CSV, SUMMARY_MD, BACKTEST_CSV) if not p.exists()]
if missing:
    st.error(
        "Evaluation artefacts are missing from this checkout: "
        + ", ".join(f"`docs/{name}`" for name in missing)
        + ". Restore them from the repository; this page never fabricates data.",
        icon="🚫",
    )
    st.stop()

results = _load_csv(RESULTS_CSV)
summary_md = SUMMARY_MD.read_text(encoding="utf-8")
backtest = _load_csv(BACKTEST_CSV)

if not results:
    st.error("`docs/agent_eval_results.csv` contains no rows.", icon="🚫")
    st.stop()

by_config = {
    cfg: [row for row in results if row.get("config") == cfg] for cfg in CONFIGS
}
intent_ids = sorted({row["intent_id"] for row in results})
unsafe_total = len([row for row in by_config["A"] if not _flag(row["safe"])])
safe_total = len([row for row in by_config["A"] if _flag(row["safe"])])

# ---------------------------------------------------------------------------
# A/B/C comparison
# ---------------------------------------------------------------------------
comparison_rows: list[dict[str, object]] = []
twin_extra = 0
if all(by_config[cfg] for cfg in CONFIGS):
    accepted_by_intent = {
        cfg: {row["intent_id"]: _flag(row["accepted"]) for row in by_config[cfg]}
        for cfg in CONFIGS
    }
    twin_extra = len([
        intent for intent in intent_ids
        if accepted_by_intent["B"].get(intent) and not accepted_by_intent["C"].get(intent)
    ])

for cfg in CONFIGS:
    rows = by_config[cfg]
    if not rows:
        continue
    unsafe = [row for row in rows if not _flag(row["safe"])]
    safe = [row for row in rows if _flag(row["safe"])]
    false_accepts = [row for row in unsafe if _flag(row["accepted"])]
    false_rejects = [row for row in safe if not _flag(row["accepted"])]
    accepted = [row for row in rows if _flag(row["accepted"])]
    rejected = [row for row in rows if not _flag(row["accepted"])]
    unsafe_rejected = [row for row in unsafe if not _flag(row["accepted"])]
    correct = [row for row in rows if _flag(row["correct"])]
    comparison_rows.append({
        "Configuration": CONFIG_LABELS[cfg],
        "Unsafe accepted (false accept)": _rate(len(false_accepts), len(unsafe)),
        "Safe rejected (false reject)": _rate(len(false_rejects), len(safe)),
        "Accuracy": _rate(len(correct), len(rows)),
        "Precision (unsafe)": _rate(len(unsafe_rejected), len(rejected)),
        "Recall (unsafe)": _rate(len(unsafe_rejected), len(unsafe)),
        "Twin extra failures": twin_extra if cfg == "C" else 0,
        "Avg decision ms": f"{_mean([float(row['decision_latency_ms']) for row in rows]):.2f}",
        "LLM calls": (
            f"{sum(int(row['llm_calls_network']) for row in rows)} network / "
            f"{sum(int(row['llm_calls_cached']) for row in rows)} cached"
        ),
    })

st.subheader("A/B/C safety comparison")
theme.kpi_row([
    ("Curated intents", len(intent_ids), f"{safe_total} safe / {unsafe_total} unsafe"),
    ("Configurations", len([cfg for cfg in CONFIGS if by_config[cfg]]), "A / B / C"),
    ("A false accepts", f"{len([r for r in by_config['A'] if not _flag(r['safe']) and _flag(r['accepted'])])}/{unsafe_total}", "unsafe accepted", "bad"),
    ("C false accepts", f"{len([r for r in by_config['C'] if not _flag(r['safe']) and _flag(r['accepted'])])}/{unsafe_total}", "unsafe accepted", "ok"),
    ("Twin extra rejections", twin_extra, "C vs B", "violet"),
])
st.dataframe(comparison_rows, hide_index=True, width="stretch")
st.caption(
    "Wilson 95% intervals are recomputed from `docs/agent_eval_results.csv`. "
    "Precision/recall treat **rejected as unsafe** as the positive class: "
    "'Precision (unsafe)' is truly-unsafe rejections divided by all rejections; "
    "'Recall (unsafe)' is truly-unsafe rejections divided by all unsafe intents. "
    "The evaluation is offline: the recorded LLM proposal is replayed from cache, "
    "so LLM latency is a recorded value and no provider call happens here. "
    "Intervals are recomputed from the raw CSV with z=1.96; a value in the "
    "committed markdown summary can differ by 0.1 pp from rounding."
)

# ---------------------------------------------------------------------------
# Per-category false accepts
# ---------------------------------------------------------------------------
category_rows: list[dict[str, object]] = []
categories = [cat for cat in CATEGORY_ORDER
              if any(row.get("unsafe_category") == cat for row in results)]
categories += sorted({
    row["unsafe_category"] for row in results
    if row.get("unsafe_category") and row["unsafe_category"] not in categories
})
for category in categories:
    row: dict[str, object] = {"Unsafe category": category}
    for cfg in CONFIGS:
        rows = [r for r in by_config[cfg] if r.get("unsafe_category") == category]
        if rows:
            accepted = len([r for r in rows if _flag(r["accepted"])])
            row[CONFIG_LABELS[cfg]] = f"{accepted}/{len(rows)} ({_pct(accepted, len(rows)):.1f}%)"
        else:
            row[CONFIG_LABELS[cfg]] = "n/a"
    category_rows.append(row)

st.subheader("Per-category false accepts")
st.caption(
    "An unsafe request counts as a false accept when the configuration accepted "
    "the proposal. Each category has 8 unsafe intents in the committed dataset."
)
if categories:
    figure = go.Figure()
    for cfg in CONFIGS:
        rates = []
        for category in categories:
            rows = [r for r in by_config[cfg] if r.get("unsafe_category") == category]
            accepted = len([r for r in rows if _flag(r["accepted"])])
            rates.append(_pct(accepted, len(rows)) if rows else 0.0)
        figure.add_bar(
            name=CONFIG_LABELS[cfg],
            x=list(categories),
            y=rates,
            text=[f"{value:.1f}%" for value in rates],
            textposition="outside",
            cliponaxis=False,
        )
    figure.update_layout(
        barmode="group",
        yaxis_title="False-accept rate (%)",
        yaxis_range=[0, 105],
        xaxis_title=None,
        legend_title_text="Configuration",
        margin={"l": 40, "r": 20, "t": 20, "b": 40},
    )
    st.plotly_chart(figure, width="stretch")
    st.dataframe(category_rows, hide_index=True, width="stretch")
else:
    st.info("No unsafe categories are present in the results file.")

# ---------------------------------------------------------------------------
# Failure cases (verbatim from the committed summary)
# ---------------------------------------------------------------------------
st.subheader("Failure cases")
st.caption(
    "These sections are rendered verbatim from `docs/agent_eval_summary.md`; "
    "the page does not rewrite or embellish the recorded failures."
)
twin_section = _section(summary_md, "Twin extra failures (C vs B)")
failure_section = _section(summary_md, "Where the Verifier or Twin was wrong")
rejection_section = _section(summary_md, "Safe-proposal rejections")

if twin_section:
    with st.expander("Twin extra failures (C vs B)", expanded=False):
        st.markdown(twin_section)
if failure_section:
    with st.expander("Where the Verifier or Twin was wrong", expanded=True):
        st.markdown(failure_section)
if rejection_section:
    with st.expander("Safe-proposal rejections", expanded=False):
        st.markdown(rejection_section)

interpretation = _section(summary_md, "Honest interpretation (five lines)")
if interpretation:
    with st.expander("Honest interpretation", expanded=False):
        st.markdown(interpretation)

scope = _section(summary_md, "Scope notes")
if scope:
    st.caption(scope)

# ---------------------------------------------------------------------------
# Forecast backtest summary
# ---------------------------------------------------------------------------
st.subheader("Forecast backtest summary")
if not backtest:
    st.info("`docs/forecast_backtest_results.csv` contains no rows.")
else:
    series = {(row.get("source"), row.get("device"), row.get("metric"))
              for row in backtest}
    models = sorted({row.get("model", "") for row in backtest if row.get("model")})
    live_series = len([item for item in series if item[0] == "live"])
    demo_series = len([item for item in series if item[0] == "demo"])
    perfect = len([row for row in backtest if _flag(row.get("naive_perfect"))])

    theme.kpi_row([
        ("Backtest rows", len(backtest), "model x series"),
        ("Series", len(series), f"{live_series} live / {demo_series} demo"),
        ("Models", len(models), ", ".join(models)),
        ("Naive-perfect series", perfect, "naive beat by no model", "warn"),
    ])

    model_rows: list[dict[str, object]] = []
    for model in models:
        rows = [row for row in backtest if row.get("model") == model]
        mases = [float(row["MASE"]) for row in rows
                 if row.get("MASE") not in ("", "nan", None)]
        skills = [float(row["skill_vs_naive"]) for row in rows
                  if row.get("skill_vs_naive") not in ("", "nan", None)]
        maes = [float(row["MAE"]) for row in rows
                if row.get("MAE") not in ("", "nan", None)]
        model_rows.append({
            "Model": model,
            "Rows": len(rows),
            "Median MAE": f"{_median(maes):.4f}" if maes else "n/a",
            "Median MASE": f"{_median(mases):.4f}" if mases else "n/a",
            "Median skill vs naive": f"{_median(skills):.4f}" if skills else "n/a",
            "MASE n": len(mases),
            "Skill n": len(skills),
            "Naive perfect": len([row for row in rows if _flag(row.get("naive_perfect"))]),
        })
    st.dataframe(model_rows, hide_index=True, width="stretch")

    source_rows: list[dict[str, object]] = []
    for source in sorted({row.get("source", "") for row in backtest}):
        rows = [row for row in backtest if row.get("source") == source]
        source_rows.append({
            "Source": source,
            "Series": len({(row.get("device"), row.get("metric")) for row in rows}),
            "Rows": len(rows),
            "Models": len({row.get("model") for row in rows}),
            "Naive perfect": len([row for row in rows if _flag(row.get("naive_perfect"))]),
        })
    st.dataframe(source_rows, hide_index=True, width="stretch")
    st.caption(
        "Computed from `docs/forecast_backtest_results.csv`. `live` rows are the "
        "series the collector stored with source `live`; `demo` rows are the "
        "synthetic demo fixture. This is an offline replay over stored series, "
        "not a new measurement, and no database or network call is made by this "
        "page. 'Naive perfect' counts model-series rows where the naive "
        "forecast was already exact, so skill ratios there are not informative."
    )
