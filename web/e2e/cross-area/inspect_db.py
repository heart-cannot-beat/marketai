"""Combined DB inspector for the cross-area journey (issue #28 closeout).

Returns stable identities and links across all three areas for the
before/after restart comparison: Winner decision/verdict, Localization
batch/items/attempts/cost/lineage, Scale enrollment/episode/evaluations/
advice/draft, mock provenance, and zero-write evidence.

Unlike the per-area ``inspect_db.py`` scripts, this one returns a single
canonical snapshot covering the complete Experiments -> Localization ->
Scale journey so the spec can compare before and after restart with one
``toEqual``.

Subcommands:

  canonical-snapshot
    Returns the full cross-area snapshot as JSON.

  zero-meta-writes
    Returns zero-Meta-write evidence as JSON.

Required environment: same as seed_prerequisites.py.
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

    workspace_id = os.environ["E2E_CROSS_AREA_WORKSPACE_ID"]
    user_id = os.environ["E2E_OWNER_USER_ID"]
    authorization_epoch = int(os.environ.get("E2E_CROSS_AREA_AUTHORIZATION_EPOCH", "0"))
    return TenantContext(
        workspace_id=workspace_id,
        role="owner",
        initiator_id=user_id,
        initiator_role="owner",
        executor=f"user:{user_id}",
        request_id="e2e-cross-area-inspect",
        trace_id="e2e-cross-area-inspect",
        authorization_epoch=authorization_epoch,
        meta_mode=meta_mode,
    )


def _inspect_winner(ctx, decision_id: str) -> dict:
    from app.adapters.persistence.database import tenant_session
    from app.experiments.models import (
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


def _inspect_localization(ctx, batch_id: str) -> dict:
    from app.adapters.persistence.database import tenant_session
    from app.localization.models import (
        LocalizationBatchRecord,
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
        return {
            "batchId": batch_id,
            "found": True,
            "phase": batch.phase,
            "outcome": batch.outcome,
            "truthPackageId": batch.truth_package_id,
            "items": sorted(
                [
                    {
                        "id": item.id,
                        "state": item.state,
                        "targetMarket": item.target_market,
                        "attemptCount": item.attempt_count,
                        "reasonCode": item.reason_code,
                    }
                    for item in items
                ],
                key=lambda x: x["id"],
            ),
        }


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

        eval_ids = session.execute(
            select(ScaleEvaluationRecord.id)
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
            "scenarioVersionId": enrollment.scenario_version_id if enrollment else None,
            "episodeId": episode_id,
            "episodePhase": episode.phase if episode else None,
            "evaluationCount": eval_count,
            "evaluationIds": list(eval_ids),
            "adviceThreads": sorted(
                [
                    {
                        "id": t.id,
                        "adviceType": t.advice_type,
                        "lifecycle": t.lifecycle,
                        "dispositionKind": t.disposition_kind,
                    }
                    for t in advice_threads
                ],
                key=lambda x: x["id"],
            ),
        }


def _inspect_zero_meta_writes(ctx) -> dict:
    from app.adapters.persistence.database import tenant_session
    from sqlalchemy import text

    with tenant_session(ctx) as session:
        rows = session.execute(
            text(
                "SELECT id, meta_api_method FROM call_intents "
                "WHERE workspace_id = :ws "
                "AND meta_api_method IN ('POST', 'PUT', 'DELETE', 'PATCH')"
            ),
            {"ws": ctx.workspace_id},
        ).mappings().all()
        return {
            "offending": [
                {"id": row["id"], "method": row["meta_api_method"]}
                for row in rows
            ],
        }


def _canonical_snapshot(args: argparse.Namespace) -> dict:
    ctx = _tenant_context()
    result: dict = {
        "inspectedAt": datetime.now(timezone.utc).isoformat(),
        "workspaceId": ctx.workspace_id,
    }
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
    result["zeroMetaWrites"] = _inspect_zero_meta_writes(ctx)
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Cross-area E2E DB inspector")
    commands = parser.add_subparsers(dest="command")

    snap = commands.add_parser("canonical-snapshot")
    snap.add_argument("--decision-id", default=None)
    snap.add_argument("--batch-id", default=None)
    snap.add_argument("--enrollment-id", default=None)
    snap.add_argument("--episode-id", default=None)
    snap.add_argument("--meta-mode", default="controlled_mock")

    zmw = commands.add_parser("zero-meta-writes")

    args = parser.parse_args(argv)
    if args.command == "canonical-snapshot":
        result = _canonical_snapshot(args)
    elif args.command == "zero-meta-writes":
        ctx = _tenant_context()
        result = _inspect_zero_meta_writes(ctx)
    else:
        raise RuntimeError(f"Unknown command: {args.command!r}")

    json.dump(result, sys.stdout, sort_keys=True, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
