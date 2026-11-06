"""Tests for server-calculated action eligibility (spec §2)."""
from __future__ import annotations

from app.platform.action_eligibility import compute_actions


def test_all_allowed():
    result = compute_actions(
        ["edit", "delete"],
        {"edit": (True, None), "delete": (True, None)},
    )
    assert len(result) == 2
    assert all(a["allowed"] for a in result)
    assert all(a["reason_code"] is None for a in result)


def test_denied_with_reason():
    result = compute_actions(
        ["publish"],
        {"publish": (False, "validation_failed")},
    )
    assert result[0]["allowed"] is False
    assert result[0]["reason_code"] == "validation_failed"


def test_missing_code_denied():
    result = compute_actions(["unknown"], {})
    assert result[0]["allowed"] is False
    assert result[0]["reason_code"] == "not_evaluated"


def test_sorted_by_action_code():
    result = compute_actions(
        ["z_action", "a_action"],
        {"z_action": (True, None), "a_action": (True, None)},
    )
    assert result[0]["action"] == "a_action"
    assert result[1]["action"] == "z_action"
