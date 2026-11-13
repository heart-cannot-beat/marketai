"""The SpendGuardrail a Workspace starts with (#126).

docs/architecture/subscription-entitlement-usage-cost-ownership.md §11 sets
the Beta defaults, in the Workspace budget currency: 5 per Operation, 20 per
rolling 24 hours, 100 per Access Grant/Subscription period and 10 of
outstanding (unsettled) exposure.

This module is the only place they are written down. Two paths create a
guardrail row, and both read it:

* PostgreSQL provisioning: migration ``zzg126a1b2c3`` bakes
  ``SPEND_GUARDRAIL_DEFAULTS_V1`` into ``marketai_apply_workspace_projection_v2``
  when it redefines the function.
* ``app.generation.spend.ensure_spend_guardrail``: the SQLite compatibility
  path, where no projection function runs, creates the row on first
  reservation with ``SPEND_GUARDRAIL_DEFAULTS``.

A released migration cannot change, so a version is never edited either: a
new default is a new ``SPEND_GUARDRAIL_DEFAULTS_V<n>``, a new migration that
redefines the function with it, and ``SPEND_GUARDRAIL_DEFAULTS`` pointed at
it. Neither path rewrites an existing row; only its Owner changes it, through
``GuardrailAdmin``'s preview and confirm.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class SpendGuardrailDefaults:
    per_operation_limit: Decimal
    rolling_24h_limit: Decimal
    period_limit: Decimal
    outstanding_exposure_limit: Decimal
    #: ``spend_guardrails.platform_policy_version`` of a provisioned row.
    platform_policy_version: int = 1

    def limits(self) -> dict[str, Decimal]:
        """The four limits keyed by their ``spend_guardrails`` column."""
        return {
            "per_operation_limit": self.per_operation_limit,
            "rolling_24h_limit": self.rolling_24h_limit,
            "period_limit": self.period_limit,
            "outstanding_exposure_limit": self.outstanding_exposure_limit,
        }


#: The contract §11 Beta defaults. Never edit: migration ``zzg126a1b2c3``
#: compiled these values into the provisioning function.
SPEND_GUARDRAIL_DEFAULTS_V1 = SpendGuardrailDefaults(
    per_operation_limit=Decimal("5"),
    rolling_24h_limit=Decimal("20"),
    period_limit=Decimal("100"),
    outstanding_exposure_limit=Decimal("10"),
)

#: What a new Workspace gets today.
SPEND_GUARDRAIL_DEFAULTS = SPEND_GUARDRAIL_DEFAULTS_V1
