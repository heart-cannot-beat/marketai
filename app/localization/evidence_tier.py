"""Evidence-tier derivation for Localization Truth Packages and Batches.

Spec: docs/superpowers/specs/2026-09-06-localization-truth-bounded-qc-design.md §3C

Evidence tier is the minimum (most conservative) of the normalized tiers of
every contributing source-generation mode, experiment-evidence mode, the
TruthPackage's `truth_data_mode`, and the Batch's `provider_execution_mode`.
`production > sandbox > mock`.
"""
from __future__ import annotations

from enum import IntEnum


class EvidenceTier(IntEnum):
    MOCK = 0
    SANDBOX = 1
    PRODUCTION = 2


_SOURCE_MAP = {"byok": EvidenceTier.PRODUCTION, "sandbox": EvidenceTier.SANDBOX, "mock": EvidenceTier.MOCK}
_EXPERIMENT_MAP = {"production": EvidenceTier.PRODUCTION, "sandbox": EvidenceTier.SANDBOX, "mock": EvidenceTier.MOCK}
_TRUTH_MAP = {"real": EvidenceTier.PRODUCTION, "mock": EvidenceTier.MOCK}
_PROVIDER_MAP = {"byok": EvidenceTier.PRODUCTION, "sandbox": EvidenceTier.SANDBOX, "mock": EvidenceTier.MOCK}


# Public per-mode lookups (spec §3C: `tier_of_source`, `tier_of_experiment`,
# `tier_of_truth`, `tier_of_provider`). These are the single source of truth
# for mode->tier normalization; other modules (e.g. `app/localization/
# eligibility.py`) must call these rather than redeclaring their own maps.
def tier_of_source(mode: str) -> EvidenceTier:
    """Normalize a source-generation mode (`byok`/`sandbox`/`mock`)."""
    return _SOURCE_MAP[mode]


def tier_of_experiment(mode: str) -> EvidenceTier:
    """Normalize an experiment-evidence mode (`production`/`sandbox`/`mock`)."""
    return _EXPERIMENT_MAP[mode]


def tier_of_truth(mode: str) -> EvidenceTier:
    """Normalize a TruthPackage's `truth_data_mode` (`real`/`mock`)."""
    return _TRUTH_MAP[mode]


def tier_of_provider(mode: str) -> EvidenceTier:
    """Normalize a Batch's `provider_execution_mode` (`byok`/`sandbox`/`mock`)."""
    return _PROVIDER_MAP[mode]


def derive_evidence_tier(
    *,
    source_modes: list[str],
    experiment_modes: list[str],
    truth_data_mode: str,
    provider_mode: str,
) -> EvidenceTier:
    """Fold every contributing mode down to the single most conservative tier.

    The fold includes every Batch source, not only the first (spec §3C).
    """
    tiers = [tier_of_source(m) for m in source_modes]
    tiers += [tier_of_experiment(m) for m in experiment_modes]
    tiers.append(tier_of_truth(truth_data_mode))
    tiers.append(tier_of_provider(provider_mode))
    return min(tiers)
