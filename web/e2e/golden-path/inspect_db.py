"""Canonical DB inspector for the golden-path E2E journey (issue #60).

Returns stable identities and links across all six areas for the
before/after restart comparison:

  - Library: product/offer version state and eligibility
  - Generation: run counts, costs, reservations, settlements, mock provenance
  - Assets: version lineage, review state, export packages/manifests
  - Winner: decision/verdict and cross-area identifiers
  - Localization: batch/items/attempts/outcome
  - Scale: enrollment/episode/evaluations/advice/draft

Plus:
  - Audit events grouped by type
  - Cross-area ID consistency
  - Zero Meta-write evidence
  - Disabled write configuration evidence
  - PAUSED fake-object assertions

Subcommands:

  canonical-snapshot
    Returns the full six-area snapshot as JSON.  Normalizes ordering
    and excludes volatile fields (timestamps).

  zero-meta-writes
    Returns zero-Meta-write evidence as JSON.

  fake-meta-objects
    Returns FakeMetaObject rows for a scenario version.

Required environment:
  - E2E_GOLDEN_PATH_WORKSPACE_ID
  - E2E_OWNER_USER_ID
  - MARKETAI_DATABASE_URL
  - MARKETAI_TENANT_CONTEXT_SECRET
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))


def _tenant_context(*, meta_mode: str | None = None):
    from app.platform.tenancy import TenantContext

    workspace_id = os.environ["E2E_GOLDEN_PATH_WORKSPACE_ID"]
    user_id = os.environ["E2E_OWNER_USER_ID"]
    return TenantContext(
        workspace_id=workspace_id,
        role="owner",
        initiator_id=user_id,
        initiator_role="owner",
        executor=f"user:{user_id}",
        request_id="e2e-golden-path-inspect",
        trace_id="e2e-golden-path-inspect",
        authorization_epoch=0,
        meta_mode=meta_mode,
    )


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------

def _inspect_library(ctx, workspace_id: str) -> dict:
    from app.adapters.persistence.database import tenant_session
    from app.adapters.persistence.models import LibraryVersionRecord
    from sqlalchemy import select

    with tenant_session(ctx) as session:
        versions = session.execute(
            select(LibraryVersionRecord)
            .filter_by(workspace_id=workspace_id)
            .order_by(LibraryVersionRecord.entry_type, LibraryVersionRecord.version_no)
        ).scalars().all()
        return {
            "versionCount": len(versions),
            "versions": sorted(
                [
                    {
                        "id": v.id,
                        "entryId": v.entry_id,
                        "entryType": v.entry_type,
                        "versionNo": v.version_no,
                        "state": v.state,
                        "eligibility": v.eligibility,
                    }
                    for v in versions
                ],
                key=lambda x: (x["entryType"], x["id"]),
            ),
        }


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def _inspect_generation(ctx, workspace_id: str) -> dict:
    from app.adapters.persistence.database import tenant_session
    from app.adapters.persistence.models import AssetVersionRecord, GenerationRunRecord
    from app.generation.models import RunItemRecord
    from sqlalchemy import func, select, text

    with tenant_session(ctx) as session:
        runs = session.execute(
            select(GenerationRunRecord)
            .filter_by(workspace_id=workspace_id)
            .order_by(GenerationRunRecord.id)
        ).scalars().all()

        item_counts = session.execute(
            select(
                RunItemRecord.state,
                func.count(),
            )
            .filter_by(workspace_id=workspace_id)
            .group_by(RunItemRecord.state)
        ).all()
        state_map = {state: count for state, count in item_counts}

        asset_version_count = session.execute(
            select(func.count())
            .select_from(AssetVersionRecord)
            .filter_by(workspace_id=workspace_id, origin_kind="generation")
        ).scalar_one()

        cost_rows = session.execute(
            text(
                "SELECT state, provider_currency, "
                "       SUM(maximum_provider_amount) AS total_provider, "
                "       COUNT(*) AS cnt "
                "FROM spend_reservations "
                "WHERE workspace_id = :ws "
                "GROUP BY state, provider_currency "
                "ORDER BY state, provider_currency"
            ),
            {"ws": workspace_id},
        ).mappings().all()
        cost_reservations = sum(r["cnt"] for r in cost_rows)
        cost_settled = sum(r["cnt"] for r in cost_rows if r["state"] == "settled")

        provider_ledger_rows = session.execute(
            text(
                "SELECT id, entry_type, direction, execution_mode, provider, "
                "       provider_currency, provider_amount, spend_reservation_id "
                "FROM provider_cost_ledger "
                "WHERE workspace_id = :ws "
                "ORDER BY id"
            ),
            {"ws": workspace_id},
        ).mappings().all()
        # Image generation calls only: since Issue #169 every produced image
        # is also quality-checked by its own ``llm_chat`` call (counted
        # separately below), and product intake / plan drafting make
        # ``llm_chat`` calls of their own.
        provider_calls = session.execute(
            text(
                "SELECT COUNT(*) FROM provider_call_attempts "
                "WHERE workspace_id = :ws AND step_kind = 'generation'"
            ),
            {"ws": workspace_id},
        ).scalar_one()
        quality_check_calls = session.execute(
            text(
                "SELECT COUNT(DISTINCT provider_attempt_id) FROM asset_qc_attempts "
                "WHERE workspace_id = :ws AND provider_attempt_id IS NOT NULL"
            ),
            {"ws": workspace_id},
        ).scalar_one()

        run_items = session.execute(
            select(RunItemRecord)
            .filter_by(workspace_id=workspace_id)
            .order_by(RunItemRecord.generation_run_id, RunItemRecord.ordinal)
        ).scalars().all()

        cost_by_state = [
            {
                "state": r["state"],
                "currency": r["provider_currency"],
                "totalProviderAmount": str(r["total_provider"]),
                "count": r["cnt"],
            }
            for r in cost_rows
        ]

        mock_provenance = sorted(
            [
                {
                    "runId": r.id,
                    "executionMode": r.execution_mode,
                    "provider": r.provider,
                    "status": r.status,
                }
                for r in runs
            ],
            key=lambda x: x["runId"],
        )

        run_item_identity = sorted(
            [
                {
                    "id": ri.id,
                    "runId": ri.generation_run_id,
                    "state": ri.state,
                    "combinationFingerprint": ri.combination_fingerprint,
                    "semanticFingerprint": ri.semantic_fingerprint,
                    "executionMode": ri.execution_mode,
                    "intentKind": ri.intent_kind,
                }
                for ri in run_items
            ],
            key=lambda x: (x["runId"], x["id"]),
        )

        cost_ledger = sorted(
            [
                {
                    "id": r["id"],
                    "entryType": r["entry_type"],
                    "direction": r["direction"],
                    "executionMode": r["execution_mode"],
                    "provider": r["provider"],
                    "currency": r["provider_currency"],
                    "amount": str(r["provider_amount"]),
                    "spendReservationId": r["spend_reservation_id"],
                }
                for r in provider_ledger_rows
            ],
            key=lambda x: x["id"],
        )

        return {
            "runCount": len(runs),
            "totalItems": sum(state_map.values()),
            "succeededItems": state_map.get("succeeded", 0),
            "failedItems": state_map.get("failed", 0),
            "assetVersionCount": asset_version_count,
            "costReservationCount": cost_reservations,
            "costSettlementCount": cost_settled,
            "costByState": cost_by_state,
            "providerCallCount": provider_calls,
            "qualityCheckCallCount": quality_check_calls,
            "costLedger": cost_ledger,
            "mockProvenance": mock_provenance,
            "runItemIdentity": run_item_identity,
        }


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------

def _inspect_assets(ctx, workspace_id: str) -> dict:
    from app.adapters.persistence.database import tenant_session
    from app.adapters.persistence.models import AssetVersionRecord, QualityReviewRecord
    from sqlalchemy import func, select, text

    with tenant_session(ctx) as session:
        versions = session.execute(
            select(AssetVersionRecord)
            .filter_by(workspace_id=workspace_id)
            .order_by(AssetVersionRecord.id)
        ).scalars().all()

        approved_count = session.execute(
            select(func.count())
            .select_from(QualityReviewRecord)
            .filter_by(workspace_id=workspace_id, review_value="approved")
        ).scalar_one()

        export_rows = session.execute(
            text(
                "SELECT id, state, zip_hash, manifest_hash, byte_size "
                "FROM export_packages "
                "WHERE workspace_id = :ws "
                "ORDER BY id"
            ),
            {"ws": workspace_id},
        ).mappings().all()

        lineage = sorted(
            [
                {
                    "id": v.id,
                    "parentId": getattr(v, "parent_asset_version_id", None),
                    "originKind": v.origin_kind,
                    "executionMode": v.execution_mode,
                }
                for v in versions
            ],
            key=lambda x: x["id"],
        )

        return {
            "totalVersions": len(versions),
            "approvedCount": approved_count,
            "exportPackageCount": len(export_rows),
            "exportPackages": [
                {
                    "id": r["id"],
                    "state": r["state"],
                    "zipHash": r["zip_hash"],
                    "manifestHash": r["manifest_hash"],
                    "byteSize": r["byte_size"],
                }
                for r in export_rows
            ],
            "lineage": lineage,
        }


# ---------------------------------------------------------------------------
# Winner
# ---------------------------------------------------------------------------

def _inspect_winner(ctx, decision_id: str) -> dict:
    from app.adapters.persistence.database import tenant_session
    from app.adapters.persistence.models import (
        ExperimentDecisionRecord,
        ExperimentVersionCandidateRecord,
        ExperimentVersionRecord,
    )

    with tenant_session(ctx) as session:
        decision = session.get(ExperimentDecisionRecord, decision_id)
        if decision is None:
            return {"decisionId": decision_id, "found": False}
        candidate = session.get(
            ExperimentVersionCandidateRecord,
            decision.experiment_version_candidate_id,
        )
        version = session.get(
            ExperimentVersionRecord,
            decision.experiment_version_id,
        )
        return {
            "decisionId": decision_id,
            "found": True,
            "verdict": decision.verdict,
            "assetVersionId": candidate.asset_version_id if candidate else None,
            "experimentId": version.experiment_id if version else None,
            "experimentVersionId": version.id if version else None,
            "productVersionId": version.product_version_id if version else None,
            "offerVersionId": version.offer_version_id if version else None,
            "market": version.market if version else None,
        }


# ---------------------------------------------------------------------------
# Localization
# ---------------------------------------------------------------------------

def _inspect_localization(ctx, batch_id: str) -> dict:
    from app.adapters.persistence.database import tenant_session
    from app.adapters.persistence.models import (
        LocalizationBatchRecord,
        LocalizationBatchSourceRecord,
        LocalizationItemRecord,
    )
    from sqlalchemy import select

    with tenant_session(ctx) as session:
        batch = session.get(LocalizationBatchRecord, batch_id)
        if batch is None:
            return {"batchId": batch_id, "found": False}
        items = session.execute(
            select(LocalizationItemRecord).filter_by(
                workspace_id=ctx.workspace_id,
                batch_id=batch_id,
            )
        ).scalars().all()
        sources = session.execute(
            select(LocalizationBatchSourceRecord).filter_by(
                workspace_id=ctx.workspace_id,
                batch_id=batch_id,
            )
        ).scalars().all()
        return {
            "batchId": batch_id,
            "found": True,
            "phase": batch.phase,
            "outcome": batch.outcome,
            "truthPackageId": batch.truth_package_id,
            "productVersionId": batch.product_version_id,
            "offerVersionId": batch.offer_version_id,
            "sources": sorted(
                [
                    {
                        "id": s.id,
                        "assetVersionId": s.asset_version_id,
                        "decisionId": s.decision_id,
                        "sourceGenerationMode": s.source_generation_mode,
                        "experimentEvidenceMode": s.experiment_evidence_mode,
                    }
                    for s in sources
                ],
                key=lambda x: x["id"],
            ),
            "items": sorted(
                [
                    {
                        "id": item.id,
                        "state": item.state,
                        "targetMarket": item.target_market,
                        "attemptCount": item.attempt_count,
                        "reasonCode": item.reason_code,
                        "copyVersionId": item.copy_version_id,
                        "sourceAssetVersionId": item.source_asset_version_id,
                    }
                    for item in items
                ],
                key=lambda x: x["id"],
            ),
        }


# ---------------------------------------------------------------------------
# Scale
# ---------------------------------------------------------------------------

def _inspect_scale(ctx, enrollment_id: str, episode_id: str, meta_mode: str) -> dict:
    from app.adapters.persistence.database import tenant_session
    from app.scale.models import (
        ScaleAdviceThreadRecord,
        ScaleEnrollmentRecord,
        ScaleEpisodeRecord,
        ScaleEvaluationRecord,
    )
    from sqlalchemy import func, select

    scale_ctx = _tenant_context(meta_mode=meta_mode)
    with tenant_session(scale_ctx) as session:
        enrollment = session.get(ScaleEnrollmentRecord, enrollment_id)
        episode = session.get(ScaleEpisodeRecord, episode_id)
        eval_count = session.execute(
            select(func.count())
            .select_from(ScaleEvaluationRecord)
            .filter_by(
                workspace_id=ctx.workspace_id,
                meta_mode=meta_mode,
                episode_id=episode_id,
            )
        ).scalar_one()
        evaluations = session.execute(
            select(ScaleEvaluationRecord)
            .filter_by(
                workspace_id=ctx.workspace_id,
                meta_mode=meta_mode,
                episode_id=episode_id,
            )
            .order_by(ScaleEvaluationRecord.id)
        ).scalars().all()
        advice_threads = session.execute(
            select(ScaleAdviceThreadRecord)
            .filter_by(
                workspace_id=ctx.workspace_id,
                meta_mode=meta_mode,
                enrollment_id=enrollment_id,
            )
            .order_by(ScaleAdviceThreadRecord.id)
        ).scalars().all()

        return {
            "enrollmentId": enrollment_id,
            "found": enrollment is not None,
            "lifecycle": enrollment.lifecycle if enrollment else None,
            "revision": enrollment.revision if enrollment else None,
            "metaMode": enrollment.meta_mode if enrollment else meta_mode,
            "episodeId": episode_id,
            "episodePhase": episode.phase if episode else None,
            "evaluationCount": eval_count,
            "evaluations": sorted(
                [
                    {
                        "id": e.id,
                        "snapshotCanonicalHash": e.snapshot_canonical_hash,
                        "resultHash": e.result_hash,
                        "maturity": e.maturity,
                        "dataQualityState": e.data_quality_state,
                        "health": e.health,
                        "algorithmVersion": e.algorithm_version,
                    }
                    for e in evaluations
                ],
                key=lambda x: x["id"],
            ),
            "adviceThreads": sorted(
                [
                    {
                        "id": t.id,
                        "adviceType": t.advice_type,
                        "lifecycle": t.lifecycle,
                        "dispositionKind": t.disposition_kind,
                        "dispositionAt": t.disposition_at.isoformat() if t.disposition_at else None,
                    }
                    for t in advice_threads
                ],
                key=lambda x: x["id"],
            ),
        }


# ---------------------------------------------------------------------------
# Audit events
# ---------------------------------------------------------------------------

def _inspect_audit_events(ctx, workspace_id: str) -> list[dict]:
    from app.adapters.persistence.database import tenant_session
    from sqlalchemy import func, text

    with tenant_session(ctx) as session:
        rows = session.execute(
            text(
                "SELECT event_type, COUNT(*) as cnt "
                "FROM audit_events "
                "WHERE workspace_id = :ws "
                "GROUP BY event_type "
                "ORDER BY event_type"
            ),
            {"ws": workspace_id},
        ).mappings().all()
        return [{"eventType": r["event_type"], "count": r["cnt"]} for r in rows]


# ---------------------------------------------------------------------------
# Cross-area IDs
# ---------------------------------------------------------------------------

def _inspect_cross_area_ids(
    ctx,
    decision_id: str | None,
    batch_id: str | None,
    enrollment_id: str | None,
) -> dict:
    from app.adapters.persistence.database import tenant_session
    from app.adapters.persistence.models import (
        ExperimentDecisionRecord,
        ExperimentVersionCandidateRecord,
        LocalizationBatchSourceRecord,
    )
    from app.scale.models import ScaleDeliveryTargetRecord, ScaleEnrollmentRecord
    from sqlalchemy import select

    winner_av_id = None
    loc_source_av_id = None
    scale_source_av_id = None

    with tenant_session(ctx) as session:
        if decision_id:
            decision = session.get(ExperimentDecisionRecord, decision_id)
            if decision:
                candidate = session.get(
                    ExperimentVersionCandidateRecord,
                    decision.experiment_version_candidate_id,
                )
                winner_av_id = candidate.asset_version_id if candidate else None

        if batch_id:
            batch_source = session.execute(
                select(LocalizationBatchSourceRecord)
                .filter_by(workspace_id=ctx.workspace_id, batch_id=batch_id)
            ).scalars().first()
            loc_source_av_id = batch_source.asset_version_id if batch_source else None

        if enrollment_id:
            enrollment = session.get(ScaleEnrollmentRecord, enrollment_id)
            if enrollment:
                target = session.execute(
                    select(ScaleDeliveryTargetRecord)
                    .filter_by(
                        workspace_id=ctx.workspace_id,
                        id=enrollment.delivery_target_id,
                    )
                ).scalars().first()
                scale_source_av_id = target.creative_asset_version_id if target else None

    ids_consistent = (
        winner_av_id is not None
        and winner_av_id == loc_source_av_id
        and winner_av_id == scale_source_av_id
    )

    return {
        "winnerAssetVersionId": winner_av_id,
        "localizationSourceAssetVersionId": loc_source_av_id,
        "scaleSourceAssetVersionId": scale_source_av_id,
        "idsConsistent": ids_consistent,
    }


# ---------------------------------------------------------------------------
# Zero Meta writes + config
# ---------------------------------------------------------------------------

def _inspect_zero_meta_writes(ctx) -> dict:
    from app.adapters.persistence.database import tenant_session
    from sqlalchemy import text

    with tenant_session(ctx) as session:
        rows = session.execute(
            text(
                "SELECT id, meta_api_method, meta_mode FROM meta_call_intent_records "
                "WHERE workspace_id = :ws "
                "AND meta_api_method IN ('POST', 'PUT', 'DELETE', 'PATCH') "
                "AND network_dispatched_at IS NOT NULL "
                "AND meta_mode NOT IN ('customer_sandbox', 'controlled_mock')"
            ),
            {"ws": ctx.workspace_id},
        ).mappings().all()
        return {
            "offending": [
                {"id": row["id"], "method": row["meta_api_method"], "mode": row["meta_mode"]}
                for row in rows
            ],
        }


def _inspect_meta_write_config() -> dict:
    import subprocess
    compose_project = os.environ.get("E2E_COMPOSE_PROJECT")
    if not compose_project:
        raise RuntimeError("E2E_COMPOSE_PROJECT must be set")
    api_env = subprocess.check_output(
        ["docker", "compose", "-p", compose_project, "exec", "-T", "api", "env"],
        text=True, timeout=10,
    )
    env_lines = dict(
        line.split("=", 1) for line in api_env.strip().splitlines() if "=" in line
    )
    return {
        "metaWriteEnabled": env_lines.get("META_WRITE_ENABLED", "false").lower() == "true",
        "metaWriteAccessToken": env_lines.get("META_WRITE_ACCESS_TOKEN", ""),
    }


# ---------------------------------------------------------------------------
# Evidence sync runs
# ---------------------------------------------------------------------------

def _inspect_evidence_sync_runs(ctx, workspace_id: str) -> list[dict]:
    from app.adapters.persistence.database import tenant_session
    from app.meta.models import MetaObservationSyncRunRecord, MetaEvidencePageRecord
    from sqlalchemy import func, select

    with tenant_session(ctx) as session:
        runs = session.execute(
            select(MetaObservationSyncRunRecord)
            .filter_by(workspace_id=workspace_id)
            .order_by(MetaObservationSyncRunRecord.id)
        ).scalars().all()
        result = []
        for run in runs:
            page_count = session.execute(
                select(func.count())
                .select_from(MetaEvidencePageRecord)
                .filter_by(sync_run_id=run.id)
            ).scalar_one()
            result.append({
                "id": run.id,
                "state": run.state,
                "pageCount": page_count,
                "lastCompletedPageOrder": run.last_completed_page_order,
                "outputCursor": run.output_cursor,
            })
        return sorted(result, key=lambda x: x["id"])


# ---------------------------------------------------------------------------
# Fake Meta objects
# ---------------------------------------------------------------------------

def _inspect_fake_meta_objects(ctx, meta_mode: str, scenario_version_id: str) -> list[dict]:
    from app.adapters.persistence.database import tenant_session
    from sqlalchemy import text

    scale_ctx = _tenant_context(meta_mode=meta_mode)
    with tenant_session(scale_ctx) as session:
        rows = session.execute(
            text(
                "SELECT object_type, remote_object_id, state "
                "FROM fake_meta_object_records "
                "WHERE workspace_id = :ws "
                "AND scenario_version_id = :sv "
                "ORDER BY object_type, remote_object_id"
            ),
            {"ws": ctx.workspace_id, "sv": scenario_version_id},
        ).mappings().all()
        return [
            {
                "object_type": r["object_type"],
                "remote_object_id": r["remote_object_id"],
                "state": dict(r["state"]) if isinstance(r["state"], dict) else r["state"],
            }
            for r in rows
        ]


# ---------------------------------------------------------------------------
# canonical-snapshot (combines all sections)
# ---------------------------------------------------------------------------

def _canonical_snapshot(args: argparse.Namespace) -> dict:
    ctx = _tenant_context()
    workspace_id = ctx.workspace_id
    result: dict = {
        "inspectedAt": datetime.now(timezone.utc).isoformat(),
        "workspaceId": workspace_id,
    }

    result["library"] = _inspect_library(ctx, workspace_id)
    result["generation"] = _inspect_generation(ctx, workspace_id)
    result["assets"] = _inspect_assets(ctx, workspace_id)

    if args.decision_id:
        result["winner"] = _inspect_winner(ctx, args.decision_id)
    if args.batch_id:
        result["localization"] = _inspect_localization(ctx, args.batch_id)
    if args.enrollment_id and args.episode_id:
        result["scale"] = _inspect_scale(
            ctx,
            args.enrollment_id,
            args.episode_id,
            args.meta_mode or "controlled_mock",
        )

    result["auditEvents"] = _inspect_audit_events(ctx, workspace_id)
    if args.decision_id:
        result["crossAreaIds"] = _inspect_cross_area_ids(
            ctx,
            args.decision_id,
            args.batch_id,
            args.enrollment_id,
        )
    result["evidenceSyncRuns"] = _inspect_evidence_sync_runs(ctx, workspace_id)
    result["zeroMetaWrites"] = _inspect_zero_meta_writes(ctx)
    result["metaWriteConfig"] = _inspect_meta_write_config()

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Golden-path E2E DB inspector")
    commands = parser.add_subparsers(dest="command")

    snap = commands.add_parser("canonical-snapshot")
    snap.add_argument("--workspace-id", required=True)
    snap.add_argument("--decision-id", default=None)
    snap.add_argument("--batch-id", default=None)
    snap.add_argument("--enrollment-id", default=None)
    snap.add_argument("--episode-id", default=None)
    snap.add_argument("--meta-mode", default="controlled_mock")

    zmw = commands.add_parser("zero-meta-writes")
    zmw.add_argument("--workspace-id", required=True)

    fmo = commands.add_parser("fake-meta-objects")
    fmo.add_argument("--workspace-id", required=True)
    fmo.add_argument("--meta-mode", required=True)
    fmo.add_argument("--scenario-version-id", required=True)

    args = parser.parse_args(argv)

    if args.command == "canonical-snapshot":
        os.environ["E2E_GOLDEN_PATH_WORKSPACE_ID"] = args.workspace_id
        result = _canonical_snapshot(args)
    elif args.command == "zero-meta-writes":
        os.environ["E2E_GOLDEN_PATH_WORKSPACE_ID"] = args.workspace_id
        ctx = _tenant_context()
        result = _inspect_zero_meta_writes(ctx)
    elif args.command == "fake-meta-objects":
        os.environ["E2E_GOLDEN_PATH_WORKSPACE_ID"] = args.workspace_id
        ctx = _tenant_context()
        result = _inspect_fake_meta_objects(ctx, args.meta_mode, args.scenario_version_id)
    else:
        raise RuntimeError(f"Unknown command: {args.command!r}")

    json.dump(result, sys.stdout, sort_keys=True, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
