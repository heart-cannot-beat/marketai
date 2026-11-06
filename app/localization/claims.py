"""Localization claims module (#24, spec §2F, §16.7).

Spec: docs/superpowers/specs/2026-09-06-localization-truth-bounded-qc-design.md
§2F ("app/localization/claims.py"), §1.4 (source claim tables), §16.7
(claims, locked copy and QC acceptance evidence).

`plan_inventories()` is a read-only reuse plan over the immutable,
versioned `SourceClaimInventoryRecord` rows for a set of source
AssetVersions -- it never creates an Inventory itself; it does not
pretend an asynchronous analysis result already exists.

`create_inventory_from_analysis()` is the write-once completion of one
analysis call (spec §2G: `localization.analyze_source`). It always
appends a new immutable Inventory version; `plan_inventories()` is the
only place that decides whether analysis should run at all.

`submit_claim_decisions()` validates one caller-proposed set of per-Claim
decisions for one Inventory in one target market and frozen Truth/
MarketPolicy context, then delegates to `get_or_create_decision_set()` to
persist (or reuse) the immutable `LocalizationClaimDecisionSetRecord` and
its child `LocalizationClaimDecisionRecord` rows. Findings are collected
rather than short-circuited, so a caller sees every reason a submission
was rejected in one call -- the same pattern
`app.localization.eligibility.validate_source_for_batch` uses.

Per §16.7 / §2F:
- `replace` requires a Fact from the same TruthPackage market and scope
  (`claim_scope_key == replacement_fact.scope_key`);
- `remove` freezes a removal strategy (hashed, immutable once decided);
- `preserve` is accepted only for MarketPolicy-classified market-neutral
  content -- a Claim type is market-neutral exactly when it is absent
  from the frozen `MarketPolicyVersionRecord.restricted_claim_types_json`
  list for the decision's market;
- low-confidence, conflicting, and high-risk Claims require an append-only
  accepted `SourceClaimConfirmationRecord` before any decision for them is
  accepted at all -- this is the "high-risk Claims block copy generation"
  rule; this module only reads confirmations, it never writes them;
- an incomplete ClaimDecisionSet or a canonical hash that no longer
  matches the complete ordered decision rows cannot be reused or frozen
  into a Batch -- `get_or_create_decision_set()` re-verifies every
  existing candidate for the exact same context against its own
  persisted decision rows before treating it as reusable, on both
  SQLite and PostgreSQL (the PostgreSQL DEFERRABLE constraint trigger is
  the second, database-level line of defense, not the only one).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..adapters.persistence.models import (
    LocalizationClaimDecisionRecord,
    LocalizationClaimDecisionSetRecord,
    MarketPolicyVersionRecord,
    SourceClaimConfirmationRecord,
    SourceClaimInventoryRecord,
    TruthFactRecord,
    new_id,
)
from ..platform.tenancy import TenantContext
from .claim_facts import candidate_fact_types, is_market_sensitive_claim

ACTIONS = ("replace", "remove", "preserve")
ROLES = ("owner", "member")

# A Claim requires append-only confirmation before any per-market decision
# is accepted when analysis marked it high-risk, conflicting, or below this
# confidence bound (spec §16.7: "low-confidence, conflicting and high-risk
# Claims block copy generation until append-only confirmation").
LOW_CONFIDENCE_THRESHOLD = 0.7
_HIGH_RISK_LEVELS = ("high",)


class ClaimsError(RuntimeError):
    """Raised for malformed input the public interface should never let a
    well-behaved caller construct (e.g. a claim observation missing its
    required fields)."""


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InventoryPlan:
    """Result of `plan_inventories()`: which source AssetVersions already
    carry a reusable Inventory for the requested analysis recipe, and
    which still need an analysis Job."""

    reused: Mapping[str, str]  # asset_version_id -> inventory_id
    missing: tuple[str, ...]  # asset_version_ids with no matching Inventory


@dataclass(frozen=True)
class ClaimDecisionInput:
    """One caller-proposed decision for one Claim observation."""

    claim_index: int
    action: Literal["replace", "remove", "preserve"]
    replacement_fact_id: str | None = None
    removal_strategy: Mapping[str, object] | None = None
    rationale: str | None = None


@dataclass(frozen=True)
class DecisionSetResult:
    """Result of `submit_claim_decisions()` / `get_or_create_decision_set()`."""

    valid: bool
    findings: tuple[str, ...]
    decision_set_id: str | None
    reused: bool


@dataclass(frozen=True)
class _ResolvedDecision:
    """A `ClaimDecisionInput` after every business rule has resolved its
    action-specific persisted fields -- ready to hash and/or persist."""

    claim_index: int
    claim_scope_key: str
    action: str
    replacement_fact_id: str | None
    replacement_fact_scope_key: str | None
    removal_strategy_json: dict | None
    removal_strategy_hash: str | None
    preserve_policy_evidence_hash: str | None
    rationale: str | None


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _hash(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _claim_requires_confirmation(claim: Mapping[str, object]) -> bool:
    if claim.get("risk") in _HIGH_RISK_LEVELS:
        return True
    if claim.get("conflicting"):
        return True
    confidence = claim.get("confidence")
    if confidence is not None and float(confidence) < LOW_CONFIDENCE_THRESHOLD:
        return True
    return False


# ---------------------------------------------------------------------------
# Inventory planning and write-once creation
# ---------------------------------------------------------------------------


def plan_inventories(
    session: Session,
    ctx: TenantContext,
    *,
    asset_version_ids: Sequence[str],
    analysis_recipe_hash: str,
) -> InventoryPlan:
    """Read-only reuse plan over `SourceClaimInventoryRecord`.

    For each requested source AssetVersion, reuses the latest Inventory
    version already recorded for the exact `analysis_recipe_hash`, or
    reports it as missing analysis work. Never creates a row.
    """
    reused: dict[str, str] = {}
    missing: list[str] = []
    for asset_version_id in asset_version_ids:
        row = session.scalar(
            select(SourceClaimInventoryRecord)
            .where(
                SourceClaimInventoryRecord.workspace_id == ctx.workspace_id,
                SourceClaimInventoryRecord.asset_version_id == asset_version_id,
                SourceClaimInventoryRecord.analysis_recipe_hash == analysis_recipe_hash,
            )
            .order_by(SourceClaimInventoryRecord.version_number.desc())
        )
        if row is not None:
            reused[asset_version_id] = row.id
        else:
            missing.append(asset_version_id)
    return InventoryPlan(reused=reused, missing=tuple(missing))


def create_inventory_from_analysis(
    session: Session,
    ctx: TenantContext,
    *,
    asset_version_id: str,
    analysis_recipe_hash: str,
    claims: Sequence[Mapping[str, object]],
    provider: str,
    model: str,
    rule_version: str,
    analysis_execution_mode: Literal["byok", "sandbox", "mock"],
    adapter_version: str,
    mock_scenario: str | None = None,
    mock_version: str | None = None,
    now: datetime | None = None,
) -> SourceClaimInventoryRecord:
    """Write-once completion of one `localization.analyze_source` call.

    Each entry in `claims` is a typed observation (spec §1.4): at minimum
    `claim_type` and `scope_key`; analysis also typically supplies
    `ocr_text`, `region`, `confidence`, `risk` and `conflicting`, and
    `evidence`. `scope_key` is the stable identity a later per-market
    decision's `claim_scope_key` must equal.

    Always appends a new `version_number` for
    `(workspace_id, asset_version_id, analysis_recipe_hash)` -- it never
    updates or reuses an existing row (the table is immutable).
    """
    for index, claim in enumerate(claims):
        if "claim_type" not in claim or "scope_key" not in claim:
            raise ClaimsError(
                f"claim {index} is missing required 'claim_type'/'scope_key' fields"
            )

    existing_max = session.scalar(
        select(SourceClaimInventoryRecord.version_number)
        .where(
            SourceClaimInventoryRecord.workspace_id == ctx.workspace_id,
            SourceClaimInventoryRecord.asset_version_id == asset_version_id,
            SourceClaimInventoryRecord.analysis_recipe_hash == analysis_recipe_hash,
        )
        .order_by(SourceClaimInventoryRecord.version_number.desc())
    )
    version_number = (existing_max or 0) + 1

    claims_list = [dict(claim) for claim in claims]
    evidence_hash = _hash(
        {
            "claims": claims_list,
            "provider": provider,
            "model": model,
            "rule_version": rule_version,
            "adapter_version": adapter_version,
            "mock_scenario": mock_scenario,
            "mock_version": mock_version,
        }
    )

    inventory = SourceClaimInventoryRecord(
        id=new_id("sci"),
        workspace_id=ctx.workspace_id,
        asset_version_id=asset_version_id,
        analysis_recipe_hash=analysis_recipe_hash,
        version_number=version_number,
        claims_json=claims_list,
        provider=provider,
        model=model,
        rule_version=rule_version,
        analysis_execution_mode=analysis_execution_mode,
        adapter_version=adapter_version,
        mock_scenario=mock_scenario,
        mock_version=mock_version,
        evidence_hash=evidence_hash,
        created_at=now or datetime.now(timezone.utc),
    )
    session.add(inventory)
    session.flush()
    return inventory


# ---------------------------------------------------------------------------
# Claim decisions
# ---------------------------------------------------------------------------


def _latest_confirmation(
    session: Session, ctx: TenantContext, inventory_id: str, claim_index: int
) -> SourceClaimConfirmationRecord | None:
    return session.scalar(
        select(SourceClaimConfirmationRecord)
        .where(
            SourceClaimConfirmationRecord.workspace_id == ctx.workspace_id,
            SourceClaimConfirmationRecord.inventory_id == inventory_id,
            SourceClaimConfirmationRecord.claim_index == claim_index,
        )
        .order_by(SourceClaimConfirmationRecord.created_at.desc())
    )


def submit_claim_decisions(
    session: Session,
    ctx: TenantContext,
    *,
    origin_batch_id: str,
    inventory_id: str,
    truth_package_id: str,
    truth_package_market_id: str,
    target_market: str,
    market_policy_version_id: str,
    decisions: Sequence[ClaimDecisionInput],
    actor_id: str,
    role: str,
    now: datetime | None = None,
) -> DecisionSetResult:
    """Validate then persist (or reuse) one per-market ClaimDecisionSet.

    Findings are collected rather than short-circuited on the first
    problem found, except for the completeness check, which must pass
    before any single decision can be resolved against the Inventory.
    Nothing is persisted unless the entire submission is valid.
    """
    findings: list[str] = []

    if role not in ROLES:
        findings.append("invalid_role")

    inventory = session.scalar(
        select(SourceClaimInventoryRecord).where(
            SourceClaimInventoryRecord.workspace_id == ctx.workspace_id,
            SourceClaimInventoryRecord.id == inventory_id,
        )
    )
    if inventory is None:
        return DecisionSetResult(
            valid=False, findings=("missing_inventory",), decision_set_id=None, reused=False
        )

    market_policy = session.get(MarketPolicyVersionRecord, market_policy_version_id)
    if market_policy is None:
        return DecisionSetResult(
            valid=False,
            findings=("missing_market_policy_version",),
            decision_set_id=None,
            reused=False,
        )
    restricted_claim_types = set(market_policy.restricted_claim_types_json or [])

    claims = inventory.claims_json
    expected_indexes = set(range(len(claims)))
    provided_indexes = {decision.claim_index for decision in decisions}
    if len(decisions) != len(claims) or provided_indexes != expected_indexes:
        return DecisionSetResult(
            valid=False,
            findings=("incomplete_decision_set",),
            decision_set_id=None,
            reused=False,
        )

    resolved: list[_ResolvedDecision] = []
    for decision in sorted(decisions, key=lambda d: d.claim_index):
        claim = claims[decision.claim_index]
        claim_scope_key = claim["scope_key"]
        claim_type = claim["claim_type"]

        if _claim_requires_confirmation(claim):
            confirmation = _latest_confirmation(session, ctx, inventory_id, decision.claim_index)
            if confirmation is None or confirmation.confirmation_kind != "accepted":
                findings.append(f"claim_{decision.claim_index}_requires_confirmation")
                continue

        if decision.action not in ACTIONS:
            findings.append(f"claim_{decision.claim_index}_invalid_action")
            continue

        replacement_fact_id: str | None = None
        replacement_fact_scope_key: str | None = None
        removal_strategy_json: dict | None = None
        removal_strategy_hash: str | None = None
        preserve_policy_evidence_hash: str | None = None

        if decision.action == "replace":
            fact = None
            if decision.replacement_fact_id is not None:
                fact = session.scalar(
                    select(TruthFactRecord).where(
                        TruthFactRecord.workspace_id == ctx.workspace_id,
                        TruthFactRecord.id == decision.replacement_fact_id,
                        TruthFactRecord.truth_package_id == truth_package_id,
                        TruthFactRecord.truth_package_market_id == truth_package_market_id,
                        TruthFactRecord.scope_key == claim_scope_key,
                    )
                )
            if fact is None:
                findings.append(f"claim_{decision.claim_index}_replace_requires_same_scope_fact")
                continue
            if fact.fact_type not in candidate_fact_types(claim_type):
                findings.append(
                    f"claim_{decision.claim_index}_replace_requires_compatible_fact_type"
                )
                continue
            replacement_fact_id = fact.id
            replacement_fact_scope_key = claim_scope_key

        elif decision.action == "remove":
            if not decision.removal_strategy:
                findings.append(f"claim_{decision.claim_index}_remove_requires_removal_strategy")
                continue
            removal_strategy_json = dict(decision.removal_strategy)
            removal_strategy_hash = _hash(removal_strategy_json)

        elif decision.action == "preserve":
            if (
                is_market_sensitive_claim(claim_type)
                or claim_type in restricted_claim_types
            ):
                findings.append(
                    f"claim_{decision.claim_index}_preserve_requires_market_neutral_policy"
                )
                continue
            preserve_policy_evidence_hash = _hash(
                {
                    "market_policy_version_id": market_policy_version_id,
                    "claim_type": claim_type,
                    "classification": "market_neutral",
                }
            )

        resolved.append(
            _ResolvedDecision(
                claim_index=decision.claim_index,
                claim_scope_key=claim_scope_key,
                action=decision.action,
                replacement_fact_id=replacement_fact_id,
                replacement_fact_scope_key=replacement_fact_scope_key,
                removal_strategy_json=removal_strategy_json,
                removal_strategy_hash=removal_strategy_hash,
                preserve_policy_evidence_hash=preserve_policy_evidence_hash,
                rationale=decision.rationale,
            )
        )

    if findings:
        return DecisionSetResult(
            valid=False, findings=tuple(findings), decision_set_id=None, reused=False
        )

    return get_or_create_decision_set(
        session,
        ctx,
        origin_batch_id=origin_batch_id,
        inventory_id=inventory_id,
        source_asset_version_id=inventory.asset_version_id,
        truth_package_id=truth_package_id,
        truth_package_market_id=truth_package_market_id,
        target_market=target_market,
        market_policy_version_id=market_policy_version_id,
        resolved_decisions=tuple(resolved),
        actor_id=actor_id,
        role=role,
        now=now,
    )


def _decision_set_hash(
    *,
    inventory_id: str,
    truth_package_id: str,
    truth_package_market_id: str,
    target_market: str,
    market_policy_version_id: str,
    rows: Sequence[_ResolvedDecision],
) -> str:
    """Hash of Inventory, Truth market, MarketPolicy, and the complete
    ordered decision rows (spec §1.4) -- never the actor, role, rationale,
    or timestamp, so two different users making the identical decisions
    reuse the same set."""
    payload = {
        "inventory_id": inventory_id,
        "truth_package_id": truth_package_id,
        "truth_package_market_id": truth_package_market_id,
        "target_market": target_market,
        "market_policy_version_id": market_policy_version_id,
        "decisions": [
            {
                "claim_index": row.claim_index,
                "claim_scope_key": row.claim_scope_key,
                "action": row.action,
                "replacement_fact_id": row.replacement_fact_id,
                "replacement_fact_scope_key": row.replacement_fact_scope_key,
                "removal_strategy_hash": row.removal_strategy_hash,
                "preserve_policy_evidence_hash": row.preserve_policy_evidence_hash,
            }
            for row in sorted(rows, key=lambda r: r.claim_index)
        ],
    }
    return _hash(payload)


def get_or_create_decision_set(
    session: Session,
    ctx: TenantContext,
    *,
    origin_batch_id: str,
    inventory_id: str,
    source_asset_version_id: str,
    truth_package_id: str,
    truth_package_market_id: str,
    target_market: str,
    market_policy_version_id: str,
    resolved_decisions: Sequence[_ResolvedDecision],
    actor_id: str,
    role: str,
    now: datetime | None = None,
) -> DecisionSetResult:
    """Reuse-or-create the immutable ClaimDecisionSet for one context.

    Every existing `LocalizationClaimDecisionSetRecord` sharing this exact
    `(inventory, truth market, target market, MarketPolicy)` context is
    re-verified against its own persisted `LocalizationClaimDecisionRecord`
    rows before any reuse decision is made: a stored `canonical_hash` that
    no longer matches what its children actually hash to is a hard
    integrity failure ("cannot be reused or frozen into a Batch", spec
    §16.7) and stops the call -- it is never silently treated as a cache
    miss that just creates a fresh duplicate set.
    """
    requested_hash = _decision_set_hash(
        inventory_id=inventory_id,
        truth_package_id=truth_package_id,
        truth_package_market_id=truth_package_market_id,
        target_market=target_market,
        market_policy_version_id=market_policy_version_id,
        rows=resolved_decisions,
    )

    candidates = session.scalars(
        select(LocalizationClaimDecisionSetRecord).where(
            LocalizationClaimDecisionSetRecord.workspace_id == ctx.workspace_id,
            LocalizationClaimDecisionSetRecord.inventory_id == inventory_id,
            LocalizationClaimDecisionSetRecord.truth_package_id == truth_package_id,
            LocalizationClaimDecisionSetRecord.truth_package_market_id
            == truth_package_market_id,
            LocalizationClaimDecisionSetRecord.target_market == target_market,
            LocalizationClaimDecisionSetRecord.market_policy_version_id
            == market_policy_version_id,
        )
    ).all()

    for candidate in candidates:
        child_rows = session.scalars(
            select(LocalizationClaimDecisionRecord)
            .where(
                LocalizationClaimDecisionRecord.workspace_id == ctx.workspace_id,
                LocalizationClaimDecisionRecord.decision_set_id == candidate.id,
            )
            .order_by(LocalizationClaimDecisionRecord.claim_index)
        ).all()

        if len(child_rows) != candidate.decision_count:
            return DecisionSetResult(
                valid=False,
                findings=("incomplete_decision_set",),
                decision_set_id=candidate.id,
                reused=False,
            )

        stored_hash = _decision_set_hash(
            inventory_id=candidate.inventory_id,
            truth_package_id=candidate.truth_package_id,
            truth_package_market_id=candidate.truth_package_market_id,
            target_market=candidate.target_market,
            market_policy_version_id=candidate.market_policy_version_id,
            rows=[
                _ResolvedDecision(
                    claim_index=row.claim_index,
                    claim_scope_key=row.claim_scope_key,
                    action=row.action,
                    replacement_fact_id=row.replacement_fact_id,
                    replacement_fact_scope_key=row.replacement_fact_scope_key,
                    removal_strategy_json=row.removal_strategy_json,
                    removal_strategy_hash=row.removal_strategy_hash,
                    preserve_policy_evidence_hash=row.preserve_policy_evidence_hash,
                    rationale=row.rationale,
                )
                for row in child_rows
            ],
        )
        if stored_hash != candidate.canonical_hash:
            return DecisionSetResult(
                valid=False,
                findings=("canonical_hash_mismatch",),
                decision_set_id=candidate.id,
                reused=False,
            )
        if stored_hash == requested_hash:
            return DecisionSetResult(
                valid=True, findings=(), decision_set_id=candidate.id, reused=True
            )

    decision_set_id = new_id("lcds")
    decision_set = LocalizationClaimDecisionSetRecord(
        id=decision_set_id,
        workspace_id=ctx.workspace_id,
        origin_batch_id=origin_batch_id,
        inventory_id=inventory_id,
        source_asset_version_id=source_asset_version_id,
        truth_package_id=truth_package_id,
        truth_package_market_id=truth_package_market_id,
        target_market=target_market,
        market_policy_version_id=market_policy_version_id,
        decision_count=len(resolved_decisions),
        canonical_hash=requested_hash,
        created_at=now or datetime.now(timezone.utc),
    )
    session.add(decision_set)
    session.flush()

    for row in resolved_decisions:
        # `removal_strategy_json` is a nullable JSON column: SQLAlchemy's
        # JSON type binds an explicitly-assigned Python `None` as the JSON
        # literal `null`, not SQL NULL (`none_as_null` is not configured on
        # this column). Leaving the attribute unset when there is no
        # strategy is what actually produces SQL NULL, which the
        # `ck_lcd_action_replace`/`ck_lcd_action_remove` CHECK constraints
        # require for every non-`remove` decision.
        decision_row = LocalizationClaimDecisionRecord(
            id=new_id("lcd"),
            workspace_id=ctx.workspace_id,
            decision_set_id=decision_set_id,
            inventory_id=inventory_id,
            claim_index=row.claim_index,
            claim_scope_key=row.claim_scope_key,
            target_market=target_market,
            truth_package_id=truth_package_id,
            truth_package_market_id=truth_package_market_id,
            market_policy_version_id=market_policy_version_id,
            action=row.action,
            replacement_fact_id=row.replacement_fact_id,
            replacement_fact_scope_key=row.replacement_fact_scope_key,
            removal_strategy_hash=row.removal_strategy_hash,
            preserve_policy_evidence_hash=row.preserve_policy_evidence_hash,
            rationale=row.rationale,
            actor_id=actor_id,
            role=role,
            created_at=now or datetime.now(timezone.utc),
        )
        if row.removal_strategy_json is not None:
            decision_row.removal_strategy_json = row.removal_strategy_json
        session.add(decision_row)
    session.flush()

    return DecisionSetResult(
        valid=True, findings=(), decision_set_id=decision_set_id, reused=False
    )
