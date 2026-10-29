from services.metric_summary import summarize_link_state


def test_link_state_summary_uses_labels_and_counts_unavailable_samples():
    result = summarize_link_state([1.0, 1.0, 0.0, None, float("nan")])

    assert result == {
        "latest": "Down",
        "up": 2,
        "down": 1,
        "unknown": 0,
        "unavailable": 2,
    }


def test_link_state_summary_handles_unrecognized_values_and_all_unavailable():
    result = summarize_link_state([2.0, None])

    assert result == {
        "latest": "Unknown",
        "up": 0,
        "down": 0,
        "unknown": 1,
        "unavailable": 1,
    }
    assert summarize_link_state([None])["latest"] == "Unknown"
