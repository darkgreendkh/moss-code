"""Trace-to-label aggregation kept separate from report presentation."""

from ..failure_taxonomy import label_trial, summarize_failure_labels


def analyze_trajectories(trials):
    rows = []
    failed = 0
    labeled_failures = 0
    for trial in trials:
        trial = dict(trial)
        labels = label_trial(
            trial.get("trace_events", ()),
            trial.get("task", {}),
            trial.get("diff", ()),
        )
        rows.append({"run_id": trial.get("run_id"), "labels": labels})
        if trial.get("status") == "fail":
            failed += 1
            labeled_failures += int(bool(labels))
    summary = summarize_failure_labels(rows)
    summary["failed_trial_count"] = failed
    summary["automatic_label_coverage"] = labeled_failures / failed if failed else 1.0
    return summary
