"""Tenant-safe persisted projections for the Localization workspace.

This module only shapes records already owned by the Issue #24 domain model.
It never decides eligibility, resolves Truth, retries work, or mutates state.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.persistence.models import (
    AssetVersionRecord,
    ExperimentVersionRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    LocalizationClaimDecisionSetRecord,
    LocalizationAttemptOutputRecord,
    LocalizationAttemptRecord,
    LocalizationBatchCommandRecord,
    LocalizationBatchMarketTargetRecord,
    LocalizationBatchRecord,
    LocalizationBatchSourceRecord,
    LocalizationItemProviderCallRecord,
    LocalizationItemRecord,
    LocalizationOutputPublicationRecord,
    LocalizationQcRecordRecord,
    LocalizedAssetLineageRecord,
    LocalizedCopyVersionRecord,
    MarketAssetBindingRecord,
    MarketPolicyRecord,
    MarketPolicyVersionRecord,
    SourceClaimInventoryRecord,
    TruthFactRecord,
    TruthPackageMarketRecord,
    TruthPackageRecord,
    TruthSourceAttemptRecord,
)
from app.entitlements.cost_models import (
    ProviderCallAttemptRecord,
    ProviderCostLedgerEntryRecord,
    SpendReservationRecord,
)
from app.localization.eligibility import list_eligible_winners
from app.localization.batch import (
    _analysis_recipe_state,
    _bound_copy_version_ids,
    _bound_decision_set_ids,
    _bound_inventory_ids,
)
from app.localization.claim_facts import (
    candidate_fact_types,
    is_market_sensitive_claim,
)
from app.localization.source_context import resolve_source_market_snapshot
from app.localization.truth import assess_truth_freshness
from app.platform.tenancy import TenantContext


class LocalizationNotFoundError(LookupError):
    pass


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _label(content: dict[str, Any] | None, fallback: str) -> str:
    content = content or {}
    for key in ("name", "display_name", "title", "label"):
        value = content.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def _reusable_inventory_summaries(
    session: Session, ctx: TenantContext, asset_version_id: str,
) -> list[dict[str, Any]]:
    inventories = session.scalars(
        select(SourceClaimInventoryRecord)
        .where(
            SourceClaimInventoryRecord.workspace_id == ctx.workspace_id,
            SourceClaimInventoryRecord.asset_version_id == asset_version_id,
        )
        .order_by(
            SourceClaimInventoryRecord.analysis_recipe_hash,
            SourceClaimInventoryRecord.version_number.desc(),
            SourceClaimInventoryRecord.id.desc(),
        )
    ).all()
    latest_by_recipe: list[dict[str, Any]] = []
    seen_recipes: set[str] = set()
    for inventory in inventories:
        if inventory.analysis_recipe_hash in seen_recipes:
            continue
        seen_recipes.add(inventory.analysis_recipe_hash)
        claim_types = sorted({
            claim_type
            for claim in (inventory.claims_json or [])
            if isinstance(claim, dict)
            and isinstance((claim_type := claim.get("claim_type")), str)
            and claim_type
        })
        latest_by_recipe.append({
            "id": inventory.id,
            "analysis_recipe_hash": inventory.analysis_recipe_hash,
            "version_number": inventory.version_number,
            "claim_types": claim_types,
        })
    return latest_by_recipe


def _project_fact_policies(content: dict[str, Any]) -> list[dict[str, str]]:
    policies: list[dict[str, str]] = []
    for rule in content.get("rules", []) or []:
        if not isinstance(rule, dict):
            continue
        fact_type = rule.get("fact_type")
        on_failure = rule.get("on_failure")
        if (
            isinstance(fact_type, str)
            and fact_type
            and on_failure in {"block", "degrade", "skip"}
        ):
            policies.append({"fact_type": fact_type, "on_failure": on_failure})
    return sorted(policies, key=lambda policy: policy["fact_type"])


def _batch_actions(
    batch: LocalizationBatchRecord,
    *,
    confirm_ready: bool = False,
    claim_review_ready: bool = False,
    has_reviewable_copy: bool = False,
) -> list[dict[str, Any]]:
    active = batch.phase not in {"closed", "cancelled"}
    return [
        {"action": "start_preparation", "allowed": batch.phase == "draft",
         "reason_code": None if batch.phase == "draft" else "batch_not_draft"},
        {"action": "submit_claim_decisions", "allowed": claim_review_ready,
         "reason_code": None if claim_review_ready else "claim_review_not_ready"},
        {"action": "approve_copy", "allowed": has_reviewable_copy,
         "reason_code": None if has_reviewable_copy else "no_copy_awaiting_review"},
        {"action": "confirm", "allowed": confirm_ready,
         "reason_code": None if confirm_ready else "batch_not_ready"},
        {"action": "cancel", "allowed": active,
         "reason_code": None if active else "batch_terminal"},
    ]


def _item_actions(item: LocalizationItemRecord, max_attempts: int) -> list[dict[str, Any]]:
    retry = item.state == "retry_wait" and item.attempt_count < max_attempts
    resume = item.state == "paused"
    return [
        {"action": "retry", "allowed": retry,
         "reason_code": None if retry else (
             "attempts_exhausted" if item.attempt_count >= max_attempts else "item_not_retry_wait"
         )},
        {"action": "resume", "allowed": resume,
         "reason_code": None if resume else "item_not_paused"},
    ]


def get_workspace(session: Session, ctx: TenantContext) -> dict[str, Any]:
    market_rows = session.execute(
        select(MarketPolicyRecord, MarketPolicyVersionRecord)
        .join(
            MarketPolicyVersionRecord,
            MarketPolicyVersionRecord.market_policy_id == MarketPolicyRecord.id,
        )
        .where(MarketPolicyVersionRecord.version_state == "effective")
        .order_by(MarketPolicyRecord.market_id)
    ).all()
    market_versions = {market.market_id: version for market, version in market_rows}
    markets = [
        {
            "market": market.market_id,
            "locale": version.locale,
            "country": version.country,
            "currency": version.currency,
            "market_policy_version_id": version.id,
            "requires_local_copy": version.requires_local_copy,
            "base_tone": version.base_tone,
        }
        for market, version in market_rows
    ]
    sources: list[dict[str, Any]] = []
    for winner in list_eligible_winners(session, ctx):
        version = session.scalar(select(ExperimentVersionRecord).where(
            ExperimentVersionRecord.workspace_id == ctx.workspace_id,
            ExperimentVersionRecord.id == winner.experiment_version_id,
        ))
        if version is None:
            continue
        asset = session.scalar(select(AssetVersionRecord).where(
            AssetVersionRecord.workspace_id == ctx.workspace_id,
            AssetVersionRecord.id == winner.asset_version_id,
        ))
        product = session.scalar(select(LibraryVersionRecord).where(
            LibraryVersionRecord.workspace_id == ctx.workspace_id,
            LibraryVersionRecord.id == version.product_version_id,
        ))
        offer = session.scalar(select(LibraryVersionRecord).where(
            LibraryVersionRecord.workspace_id == ctx.workspace_id,
            LibraryVersionRecord.id == version.offer_version_id,
        ))
        product_content = product.content_json if product else {}
        offer_content = offer.content_json if offer else {}
        source_market_snapshot, source_context_problem = resolve_source_market_snapshot(
            version, offer_content
        )
        sources.append({
            **winner.__dict__,
            "product_version_id": version.product_version_id,
            "offer_version_id": version.offer_version_id,
            "source_market": version.market,
            "placement": version.placement,
            "source_market_snapshot": source_market_snapshot,
            "source_context_problem": source_context_problem,
            "reusable_inventories": _reusable_inventory_summaries(
                session, ctx, winner.asset_version_id
            ),
            "asset": {
                "id": winner.asset_version_id,
                "aspect_ratio": asset.aspect_ratio if asset else "",
                "language": asset.language if asset else "",
                "currency": asset.currency if asset else None,
            },
            "product": {"id": version.product_version_id,
                        "label": _label(product_content, version.product_version_id)},
            "offer": {"id": version.offer_version_id,
                      "label": _label(offer_content, version.offer_version_id),
                      "content": offer_content},
        })

    configuration_rows = session.execute(
        select(LibraryVersionRecord, LibraryEntryRecord)
        .join(
            LibraryEntryRecord,
            (LibraryEntryRecord.workspace_id == LibraryVersionRecord.workspace_id)
            & (LibraryEntryRecord.id == LibraryVersionRecord.entry_id),
        )
        .where(
            LibraryVersionRecord.workspace_id == ctx.workspace_id,
            LibraryVersionRecord.state == "effective",
            LibraryVersionRecord.eligibility != "blocked",
            LibraryVersionRecord.entry_type.in_(
                ("truth_resolution_policy", "data_source")
            ),
            LibraryEntryRecord.lifecycle == "active",
        )
        .order_by(LibraryVersionRecord.entry_type, LibraryEntryRecord.display_name)
    ).all()
    configuration = [
        {
            "id": version.id,
            "entry_id": version.entry_id,
            "entry_type": version.entry_type,
            "display_name": entry.display_name,
            "fact_policies": (
                _project_fact_policies(version.content_json)
                if version.entry_type == "truth_resolution_policy"
                else []
            ),
        }
        for version, entry in configuration_rows
    ]
    return {
        "role": ctx.role,
        "eligible_sources": sources,
        "markets": markets,
        "truth_resolution_policies": [
            row for row in configuration if row["entry_type"] == "truth_resolution_policy"
        ],
        "data_sources": [
            row for row in configuration if row["entry_type"] == "data_source"
        ],
    }


def _batch_summary(session: Session, ctx: TenantContext,
                   batch: LocalizationBatchRecord) -> dict[str, Any]:
    sources = session.scalars(select(LocalizationBatchSourceRecord).where(
        LocalizationBatchSourceRecord.workspace_id == ctx.workspace_id,
        LocalizationBatchSourceRecord.batch_id == batch.id,
    )).all()
    targets = session.scalars(select(LocalizationBatchMarketTargetRecord).where(
        LocalizationBatchMarketTargetRecord.workspace_id == ctx.workspace_id,
        LocalizationBatchMarketTargetRecord.batch_id == batch.id,
    )).all()
    items = session.scalars(select(LocalizationItemRecord).where(
        LocalizationItemRecord.workspace_id == ctx.workspace_id,
        LocalizationItemRecord.batch_id == batch.id,
    )).all()
    truth = None
    if batch.truth_package_id:
        truth = session.scalar(select(TruthPackageRecord).where(
            TruthPackageRecord.workspace_id == ctx.workspace_id,
            TruthPackageRecord.id == batch.truth_package_id,
        ))
    truth_freshness = None
    if truth is not None:
        freshness = assess_truth_freshness(
            session, ctx, truth.id, at=datetime.now(timezone.utc)
        )
        truth_freshness = "current" if freshness.is_current else "stale"
    return {
        "id": batch.id,
        "phase": batch.phase,
        "preparation_stage": batch.preparation_stage,
        "outcome": batch.outcome,
        "truth_freshness": truth_freshness,
        "truth_status": truth.status if truth else None,
        "truth_valid_until": _iso(truth.valid_until) if truth else None,
        "product_version_id": batch.product_version_id,
        "offer_version_id": batch.offer_version_id,
        "provider_execution_mode": batch.provider_execution_mode,
        "evidence_tier": batch.evidence_tier,
        "source_count": len(sources),
        "market_count": len(targets),
        "item_count": len(items),
        "item_state_counts": dict(Counter(item.state for item in items)),
        "revision": batch.revision,
        "created_at": _iso(batch.created_at),
        "updated_at": _iso(batch.updated_at),
        "allowed_actions": _batch_actions(batch),
    }


def list_batches(session: Session, ctx: TenantContext, *, phase: str | None = None) -> list[dict[str, Any]]:
    stmt = select(LocalizationBatchRecord).where(
        LocalizationBatchRecord.workspace_id == ctx.workspace_id,
    )
    if phase:
        stmt = stmt.where(LocalizationBatchRecord.phase == phase)
    rows = session.scalars(stmt.order_by(LocalizationBatchRecord.created_at.desc())).all()
    return [_batch_summary(session, ctx, row) for row in rows]


def _truth_market(session: Session, ctx: TenantContext,
                  batch: LocalizationBatchRecord,
                  target: LocalizationBatchMarketTargetRecord) -> dict[str, Any]:
    result: dict[str, Any] = {
        "market": target.market,
        "market_policy_version_id": target.market_policy_version_id,
        "readiness": "pending",
        "readiness_detail": {},
        "facts": [],
        "source_attempts": [],
    }
    policy = session.get(MarketPolicyVersionRecord, target.market_policy_version_id)
    if policy:
        result.update({"locale": policy.locale, "country": policy.country,
                       "currency": policy.currency})
    if not batch.truth_package_id:
        return result
    market = session.scalar(select(TruthPackageMarketRecord).where(
        TruthPackageMarketRecord.workspace_id == ctx.workspace_id,
        TruthPackageMarketRecord.truth_package_id == batch.truth_package_id,
        TruthPackageMarketRecord.market == target.market,
    ))
    if market is None:
        result.update({"readiness": "failed",
                       "readiness_detail": {"missing": ["truth_package_market"]}})
        return result
    facts = session.scalars(select(TruthFactRecord).where(
        TruthFactRecord.workspace_id == ctx.workspace_id,
        TruthFactRecord.truth_package_market_id == market.id,
    ).order_by(TruthFactRecord.fact_type, TruthFactRecord.scope_key)).all()
    attempts = session.scalars(select(TruthSourceAttemptRecord).where(
        TruthSourceAttemptRecord.workspace_id == ctx.workspace_id,
        TruthSourceAttemptRecord.truth_package_market_id == market.id,
    ).order_by(TruthSourceAttemptRecord.fact_type,
               TruthSourceAttemptRecord.priority_ordinal)).all()
    result.update({
        "truth_package_market_id": market.id,
        "readiness": market.readiness,
        "readiness_detail": market.readiness_detail_json,
        "facts": [{
            "id": fact.id, "fact_type": fact.fact_type, "scope_key": fact.scope_key,
            "value": fact.value_json, "external_object_ref": fact.external_object_ref,
            "accepted_source_attempt_id": fact.accepted_source_attempt_id,
            "requested_context": fact.requested_context_json,
            "evidence_hash": fact.evidence_hash,
            "mock_scenario": fact.mock_scenario, "mock_version": fact.mock_version,
            "observed_at": _iso(fact.observed_at), "valid_until": _iso(fact.valid_until),
            "source_valid_until": _iso(fact.source_valid_until),
        } for fact in facts],
        "source_attempts": [{
            "id": attempt.id, "fact_type": attempt.fact_type,
            "adapter_kind": attempt.adapter_kind,
            "data_source_version_id": attempt.data_source_version_id,
            "outcome": attempt.outcome, "failure_code": attempt.failure_code,
            "skip_code": attempt.skip_code,
        } for attempt in attempts],
    })
    return result


def get_batch(session: Session, ctx: TenantContext, batch_id: str) -> dict[str, Any]:
    batch = session.scalar(select(LocalizationBatchRecord).where(
        LocalizationBatchRecord.workspace_id == ctx.workspace_id,
        LocalizationBatchRecord.id == batch_id,
    ))
    if batch is None:
        raise LocalizationNotFoundError(f"batch {batch_id} not found")
    sources = session.scalars(select(LocalizationBatchSourceRecord).where(
        LocalizationBatchSourceRecord.workspace_id == ctx.workspace_id,
        LocalizationBatchSourceRecord.batch_id == batch_id,
    ).order_by(LocalizationBatchSourceRecord.created_at)).all()
    targets = session.scalars(select(LocalizationBatchMarketTargetRecord).where(
        LocalizationBatchMarketTargetRecord.workspace_id == ctx.workspace_id,
        LocalizationBatchMarketTargetRecord.batch_id == batch_id,
    ).order_by(LocalizationBatchMarketTargetRecord.market)).all()
    items = session.scalars(select(LocalizationItemRecord).where(
        LocalizationItemRecord.workspace_id == ctx.workspace_id,
        LocalizationItemRecord.batch_id == batch_id,
    ).order_by(LocalizationItemRecord.target_market,
               LocalizationItemRecord.source_asset_version_id)).all()
    analysis_recipe_hash, _, _ = _analysis_recipe_state(batch)
    bound_inventory_ids = _bound_inventory_ids(session, ctx, batch)
    bound_copy_version_ids = _bound_copy_version_ids(session, ctx, batch)
    copies = []
    if bound_copy_version_ids:
        copy_rows = session.scalars(select(LocalizedCopyVersionRecord).where(
            LocalizedCopyVersionRecord.workspace_id == ctx.workspace_id,
            LocalizedCopyVersionRecord.id.in_(
                list(bound_copy_version_ids.values())
            ),
        ).order_by(LocalizedCopyVersionRecord.target_market,
                   LocalizedCopyVersionRecord.source_asset_version_id)).all()
        copies = [{
            "id": copy.id, "source_asset_version_id": copy.source_asset_version_id,
            "target_market": copy.target_market, "state": copy.state,
            "copy_origin": copy.copy_origin, "content": copy.content_json,
            "evidence_hash": copy.evidence_hash,
        } for copy in copy_rows]
    commands = session.scalars(select(LocalizationBatchCommandRecord).where(
        LocalizationBatchCommandRecord.workspace_id == ctx.workspace_id,
        LocalizationBatchCommandRecord.batch_id == batch_id,
    ).order_by(LocalizationBatchCommandRecord.created_at)).all()
    inventories = []
    for source in sources:
        inventory_id = bound_inventory_ids.get(source.asset_version_id)
        if inventory_id is None:
            continue
        inventory = session.scalar(
            select(SourceClaimInventoryRecord)
            .where(
                SourceClaimInventoryRecord.workspace_id == ctx.workspace_id,
                SourceClaimInventoryRecord.id == inventory_id,
                SourceClaimInventoryRecord.asset_version_id == source.asset_version_id,
                SourceClaimInventoryRecord.analysis_recipe_hash == analysis_recipe_hash,
            )
        )
        if inventory is None:
            continue
        inventories.append(
            {
                "id": inventory.id,
                "asset_version_id": inventory.asset_version_id,
                "analysis_recipe_hash": inventory.analysis_recipe_hash,
                "version_number": inventory.version_number,
                "provider": inventory.provider,
                "model": inventory.model,
                "analysis_execution_mode": inventory.analysis_execution_mode,
                "evidence_hash": inventory.evidence_hash,
                "claims": [
                    {
                        "claim_index": index,
                        "claim_type": claim.get("claim_type", "unknown"),
                        "candidate_fact_types": list(
                            candidate_fact_types(claim.get("claim_type"))
                        ),
                        "market_sensitive": is_market_sensitive_claim(
                            claim.get("claim_type")
                        ),
                        "scope_key": claim.get("scope_key", ""),
                        "ocr_text": claim.get("ocr_text"),
                        "confidence": claim.get("confidence"),
                        "risk": claim.get("risk"),
                        "conflicting": bool(claim.get("conflicting", False)),
                        "region": claim.get("region"),
                    }
                    for index, claim in enumerate(inventory.claims_json or [])
                ],
            }
        )
    decision_sets = []
    bound_decision_set_ids = _bound_decision_set_ids(batch)
    if bound_decision_set_ids:
        decision_sets = session.scalars(
            select(LocalizationClaimDecisionSetRecord)
            .where(
                LocalizationClaimDecisionSetRecord.workspace_id == ctx.workspace_id,
                LocalizationClaimDecisionSetRecord.id.in_(
                    list(bound_decision_set_ids.values())
                ),
            )
            .order_by(LocalizationClaimDecisionSetRecord.created_at)
        ).all()
    result = _batch_summary(session, ctx, batch)
    truth_is_current = result["truth_freshness"] == "current"
    confirm_ready = truth_is_current and (batch.phase == "ready" or (
        batch.phase == "awaiting_copy_approval"
        and batch.preparation_stage == "complete"
        and len(copies) == len(sources) * len(targets)
        and bool(copies)
        and all(copy["state"] == "approved" for copy in copies)
    ))
    claim_review_ready = (
        batch.phase == "preparing"
        and batch.preparation_stage in {"claim_review", "copy"}
        and len(inventories) == len(sources)
    )
    has_reviewable_copy = any(copy["state"] == "needs_review" for copy in copies)
    result.update({
        "source_market_snapshot": batch.source_market_snapshot_json,
        "truth_package_id": batch.truth_package_id,
        "truth_data_mode": batch.truth_data_mode,
        "max_attempts": batch.max_attempts,
        "frozen_recipe": batch.frozen_recipe_json,
        "frozen_at": _iso(batch.frozen_at),
        "allowed_actions": _batch_actions(
            batch,
            confirm_ready=confirm_ready,
            claim_review_ready=claim_review_ready,
            has_reviewable_copy=has_reviewable_copy,
        ),
        "sources": [{
            "id": source.id, "asset_version_id": source.asset_version_id,
            "decision_id": source.decision_id, "experiment_id": source.experiment_id,
            "experiment_version_id": source.experiment_version_id,
            "source_generation_mode": source.source_generation_mode,
            "experiment_evidence_mode": source.experiment_evidence_mode,
            "source_market_snapshot": source.source_market_snapshot_json,
            "analysis_recipe_hash": analysis_recipe_hash,
            "inventory_id": bound_inventory_ids.get(source.asset_version_id),
            "reusable_inventories": _reusable_inventory_summaries(
                session, ctx, source.asset_version_id
            ),
        } for source in sources],
        "markets": [_truth_market(session, ctx, batch, target) for target in targets],
        "copies": copies,
        "items": [{
            "id": item.id, "state": item.state, "target_market": item.target_market,
            "source_asset_version_id": item.source_asset_version_id,
            "attempt_count": item.attempt_count, "reason_code": item.reason_code,
            "reason_detail": item.reason_detail, "recoverable": item.recoverable,
            "next_action": item.next_action, "revision": item.revision,
            "allowed_actions": _item_actions(item, batch.max_attempts),
        } for item in items],
        "commands": [{"id": command.id, "command_kind": command.command_kind,
                      "item_id": command.item_id, "operation_id": command.operation_id,
                      "actor_id": command.actor_id, "created_at": _iso(command.created_at)}
                     for command in commands],
        "inventories": inventories,
        "claim_decision_sets": [
            {
                "id": decision_set.id,
                "inventory_id": decision_set.inventory_id,
                "source_asset_version_id": decision_set.source_asset_version_id,
                "target_market": decision_set.target_market,
                "decision_count": decision_set.decision_count,
            }
            for decision_set in decision_sets
        ],
    })
    return result


def get_item(session: Session, ctx: TenantContext, batch_id: str, item_id: str) -> dict[str, Any]:
    batch = session.scalar(select(LocalizationBatchRecord).where(
        LocalizationBatchRecord.workspace_id == ctx.workspace_id,
        LocalizationBatchRecord.id == batch_id,
    ))
    item = session.scalar(select(LocalizationItemRecord).where(
        LocalizationItemRecord.workspace_id == ctx.workspace_id,
        LocalizationItemRecord.batch_id == batch_id,
        LocalizationItemRecord.id == item_id,
    ))
    if batch is None or item is None:
        raise LocalizationNotFoundError(f"item {item_id} not found")
    copy = session.scalar(select(LocalizedCopyVersionRecord).where(
        LocalizedCopyVersionRecord.workspace_id == ctx.workspace_id,
        LocalizedCopyVersionRecord.id == item.copy_version_id,
    )) if item.copy_version_id else None
    attempt_rows = session.scalars(select(LocalizationAttemptRecord).where(
        LocalizationAttemptRecord.workspace_id == ctx.workspace_id,
        LocalizationAttemptRecord.item_id == item_id,
    ).order_by(LocalizationAttemptRecord.attempt_number)).all()
    attempts: list[dict[str, Any]] = []
    job_ids: set[str] = set()
    for attempt in attempt_rows:
        output = session.scalar(select(LocalizationAttemptOutputRecord).where(
            LocalizationAttemptOutputRecord.workspace_id == ctx.workspace_id,
            LocalizationAttemptOutputRecord.attempt_id == attempt.id,
        ))
        publication = session.scalar(select(LocalizationOutputPublicationRecord).where(
            LocalizationOutputPublicationRecord.workspace_id == ctx.workspace_id,
            LocalizationOutputPublicationRecord.attempt_id == attempt.id,
        ))
        qcs = session.scalars(select(LocalizationQcRecordRecord).where(
            LocalizationQcRecordRecord.workspace_id == ctx.workspace_id,
            LocalizationQcRecordRecord.attempt_id == attempt.id,
        ).order_by(LocalizationQcRecordRecord.created_at)).all()
        call_links = session.scalars(select(LocalizationItemProviderCallRecord).where(
            LocalizationItemProviderCallRecord.workspace_id == ctx.workspace_id,
            LocalizationItemProviderCallRecord.attempt_id == attempt.id,
        )).all()
        calls = []
        for link in call_links:
            call = session.scalar(select(ProviderCallAttemptRecord).where(
                ProviderCallAttemptRecord.workspace_id == ctx.workspace_id,
                ProviderCallAttemptRecord.id == link.provider_call_attempt_id,
            ))
            if call is not None:
                job_ids.add(call.job_id)
            reservation = None
            if call and call.spend_reservation_id:
                reservation = session.scalar(select(SpendReservationRecord).where(
                    SpendReservationRecord.workspace_id == ctx.workspace_id,
                    SpendReservationRecord.id == call.spend_reservation_id,
                ))
            calls.append({
                "step": link.provider_step_kind,
                "dispatch_state": call.dispatch_state if call else "unknown",
                "chargeability": call.chargeability if call else "unknown",
                "provider": call.provider if call else attempt.provider,
                "model": call.model if call else attempt.model,
                "error_code": call.error_code if call else None,
                "maximum_cost": ({"amount": str(reservation.maximum_budget_amount),
                                  "currency": reservation.budget_currency,
                                  "state": reservation.state} if reservation else None),
            })
        attempts.append({
            "id": attempt.id, "attempt_number": attempt.attempt_number,
            "state": attempt.state, "provider": attempt.provider, "model": attempt.model,
            "created_at": _iso(attempt.created_at),
            "output": ({"id": output.id, "content_sha256": output.content_sha256,
                        "byte_size": output.byte_size, "mime_type": output.mime_type,
                        "isolated": publication is None} if output else None),
            "publication": ({"id": publication.id,
                              "publication_kind": publication.publication_kind,
                              "asset_version_id": publication.asset_version_id,
                              "created_at": _iso(publication.created_at)} if publication else None),
            "qc": [{"id": qc.id, "check_kind": qc.check_kind,
                    "check_slot_key": qc.check_slot_key, "severity": qc.severity,
                    "result": qc.result, "expected": qc.expected_json,
                    "observed": qc.observed_json, "rule_version": qc.rule_version}
                   for qc in qcs],
            "provider_calls": calls,
        })
    ledger = []
    if job_ids:
        rows = session.scalars(select(ProviderCostLedgerEntryRecord).where(
            ProviderCostLedgerEntryRecord.workspace_id == ctx.workspace_id,
            ProviderCostLedgerEntryRecord.job_id.in_(job_ids),
        ).order_by(ProviderCostLedgerEntryRecord.created_at)).all()
        ledger = [{"entry_type": row.entry_type, "direction": row.direction,
                   "amount": str(row.budget_amount), "currency": row.budget_currency,
                   "provider": row.provider, "model": row.model,
                   "created_at": _iso(row.created_at)} for row in rows]
    totals: dict[str, Decimal] = {}
    actual_entry_types = {"reported_cost", "estimated_settlement", "adjustment"}
    for row in ledger:
        if row["entry_type"] not in actual_entry_types:
            continue
        sign = Decimal("1") if row["direction"] == "increase" else Decimal("-1")
        totals[row["currency"]] = totals.get(row["currency"], Decimal("0")) + sign * Decimal(row["amount"])
    binding = session.scalar(select(MarketAssetBindingRecord).where(
        MarketAssetBindingRecord.workspace_id == ctx.workspace_id,
        MarketAssetBindingRecord.item_id == item_id,
        MarketAssetBindingRecord.active.is_(True),
    ))
    # Publication lineage and experiment-readiness binding are different
    # records. Finalization creates both atomically, while skipped/failed
    # Items legitimately have neither. Resolve lineage only through the
    # complete Item identity bound by ``fk_lal_item``; never substitute a
    # latest AssetVersion or a same-market guess.
    lineage = session.scalars(select(LocalizedAssetLineageRecord).where(
        LocalizedAssetLineageRecord.workspace_id == ctx.workspace_id,
        LocalizedAssetLineageRecord.item_id == item.id,
        LocalizedAssetLineageRecord.batch_id == item.batch_id,
        LocalizedAssetLineageRecord.batch_source_id == item.batch_source_id,
        LocalizedAssetLineageRecord.source_asset_version_id
        == item.source_asset_version_id,
        LocalizedAssetLineageRecord.decision_id == item.decision_id,
        LocalizedAssetLineageRecord.target_market == item.target_market,
        LocalizedAssetLineageRecord.market_policy_version_id
        == item.market_policy_version_id,
        LocalizedAssetLineageRecord.truth_package_id == item.truth_package_id,
        LocalizedAssetLineageRecord.truth_package_market_id
        == item.truth_package_market_id,
        LocalizedAssetLineageRecord.inventory_id == item.inventory_id,
        LocalizedAssetLineageRecord.claim_decision_set_id
        == item.claim_decision_set_id,
        LocalizedAssetLineageRecord.copy_version_id == item.copy_version_id,
    )).one_or_none()
    return {
        "id": item.id, "batch_id": batch_id, "state": item.state,
        "target_market": item.target_market,
        "source_asset_version_id": item.source_asset_version_id,
        "decision_id": item.decision_id,
        "truth_package_id": item.truth_package_id,
        "truth_package_market_id": item.truth_package_market_id,
        "market_policy_version_id": item.market_policy_version_id,
        "inventory_id": item.inventory_id,
        "claim_decision_set_id": item.claim_decision_set_id,
        "copy_version_id": item.copy_version_id,
        "attempt_count": item.attempt_count, "max_attempts": batch.max_attempts,
        "reason_code": item.reason_code, "reason_detail": item.reason_detail,
        "recoverable": item.recoverable, "next_action": item.next_action,
        "revision": item.revision,
        "localized_copy": ({"id": copy.id, "state": copy.state,
                            "origin": copy.copy_origin,
                            "content": copy.content_json,
                            "evidence_hash": copy.evidence_hash}
                           if copy else None),
        "attempts": attempts,
        "cost": {"totals": [{"amount": str(amount), "currency": currency}
                            for currency, amount in sorted(totals.items())],
                 "ledger": ledger},
        "binding": ({"id": binding.id, "kind": binding.binding_kind,
                     "bound_asset_version_id": binding.bound_asset_version_id,
                     "review_state": binding.review_state,
                     "truth_freshness": binding.truth_freshness,
                     "ready_for_experiment": binding.ready_for_experiment}
                    if binding else None),
        "lineage": {"source_asset_version_id": item.source_asset_version_id,
                    "parent_asset_version_id": (lineage.source_asset_version_id
                                                if lineage else None),
                    "decision_id": item.decision_id,
                    "offer_version_id": batch.offer_version_id,
                    "truth_package_id": item.truth_package_id,
                    "copy_version_id": item.copy_version_id,
                    "localized_asset_version_id": (lineage.asset_version_id
                                                   if lineage else None),
                    "attempt_id": lineage.attempt_id if lineage else None,
                    "output_id": lineage.output_id if lineage else None,
                    "publication_id": lineage.publication_id if lineage else None},
        "allowed_actions": _item_actions(item, batch.max_attempts),
    }
