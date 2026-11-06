"""Server-calculated action eligibility (spec §2).

Clients must not infer which commands are currently valid on an aggregate
from client-side state; the server computes and returns eligibility for
every known action code so the UI can render enabled/disabled affordances
without duplicating domain rules.

``requirements`` declares what the browser must provide before the command
can proceed (e.g. ``impact_confirmation``).  Absence of
proof does not make an otherwise eligible command impossible to initiate —
it means the browser must gather the requirement first.
"""
from __future__ import annotations

from typing import TypedDict

__all__ = ["ActionEligibility", "compute_actions"]


class ActionEligibility(TypedDict, total=False):
    action: str
    allowed: bool
    reason_code: str | None
    requirements: list[str]


def compute_actions(
    action_codes: list[str],
    checks: dict[str, tuple[bool, str | None]],
    requirements: dict[str, list[str]] | None = None,
) -> list[ActionEligibility]:
    """Build a sorted ``ActionEligibility`` list from a checks dict.

    ``checks`` maps each action code to ``(allowed, reason_code_if_denied)``.
    Every code in ``action_codes`` must appear in the result; codes missing
    from ``checks`` are denied with reason code ``"not_evaluated"``.

    ``requirements`` optionally maps action codes to a list of requirement
    tokens (e.g. ``["impact_confirmation"]``).
    """
    reqs = requirements or {}
    result: list[ActionEligibility] = []
    for code in sorted(action_codes):
        allowed, reason = checks.get(code, (False, "not_evaluated"))
        entry: ActionEligibility = {
            "action": code,
            "allowed": allowed,
            "reason_code": None if allowed else reason,
        }
        action_reqs = reqs.get(code)
        if action_reqs:
            entry["requirements"] = action_reqs
        result.append(entry)
    return result
