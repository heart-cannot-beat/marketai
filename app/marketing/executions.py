"""Planned executions per combination: several images of one expression check execution stability (#225).

The count is a generation quantity, never part of a combination's creative meaning. Plans frozen before
the field existed generated one image per combination and keep that meaning; nothing is backfilled.
"""
from __future__ import annotations

from copy import deepcopy

LEGACY = 1


def planned(plan, combination):
    """Executions the plan asks for one combination; both arguments are stored plan dicts."""
    return combination.get("executions") or plan.get("executions_per_combination", LEGACY)


def keep_counts(stored, plan):
    """A client that omits the counts (pre-#225 BFF, legacy plan) keeps the stored meaning, not the new default."""
    if stored is None:
        return plan
    update = {}
    if "executions_per_combination" not in plan.model_fields_set:
        update["executions_per_combination"] = stored.get("executions_per_combination", LEGACY)
    previous = {c["id"]: c.get("executions") for c in stored.get("combinations", [])}
    if any("executions" not in c.model_fields_set and previous.get(c.id) for c in plan.combinations):
        update["combinations"] = [c if "executions" in c.model_fields_set or not previous.get(c.id)
            else c.model_copy(update={"executions": previous[c.id]}) for c in plan.combinations]
    return plan.model_copy(update=update) if update else plan


def legacy_projection(plan):
    """Retain the pre-#225 wire shape for deployed strict clients."""
    if plan is None:
        return None
    result = deepcopy(plan)
    result.pop("executions_per_combination", None)
    for combo in result.get("combinations", []):
        combo.pop("executions", None)
    return result
