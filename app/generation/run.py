"""GenerationRun state derivation from its RunItems (Issue #21 Task 2).

The Run's own ``status`` column is a projection, not independently tracked:
callers recompute it from the current set of RunItem states whenever an item
changes. Precedence (most to least urgent): any ``uncertain`` item makes the
whole Run ``uncertain`` (a crash-ambiguous provider call needs attention
before anything else is trusted); any item still in flight keeps the Run
``running``; only once every item has reached a terminal state does the Run
itself settle into ``succeeded`` / ``partially_succeeded`` / ``cancelled`` /
``failed``.
"""
from __future__ import annotations

from collections import Counter

#: RunItemRecord.state values that still require the worker to act.
_ACTIVE_STATES = ("pending", "queued", "running", "qc_pending", "paused")

#: RunItemRecord.state values that count as a produced (or reused) asset.
_SUCCESS_STATES = ("succeeded", "reused")


def derive_run_state(item_states: list[str]) -> str:
    """Fold a Run's RunItem states into one Run-level status string."""
    if not item_states:
        return "failed"

    counts = Counter(item_states)

    if counts.get("uncertain", 0) > 0:
        return "uncertain"
    if any(counts.get(state, 0) > 0 for state in _ACTIVE_STATES):
        return "running"

    total = len(item_states)
    success_count = sum(counts.get(state, 0) for state in _SUCCESS_STATES)
    if success_count == total:
        return "succeeded"
    if success_count > 0:
        return "partially_succeeded"
    if counts.get("cancelled", 0) == total:
        return "cancelled"
    return "failed"
