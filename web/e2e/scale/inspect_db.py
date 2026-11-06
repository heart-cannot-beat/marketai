"""Read-only PostgreSQL inspection helpers for the Scale E2E suite (issue
#87 Task 9).

Mirrors `web/e2e/experiments/inspect_db.py`: several projections the golden
journey and the Sandbox smoke need to assert on (evidence maturity/health
history, the advice-thread lifecycle across dismiss/snooze/resolve, the
command journal, and export completion) have no bulk-listing HTTP route --
`app/scale/v1_router.py` only exposes single-resource/paginated-item reads
scoped to the specific ids a browser already knows -- so this shells out
inside a `tenant_session`, bound by the same RLS a real tenant-scoped
FastAPI request would be, rather than bypassing tenant isolation.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))


def _context(workspace_id: str, meta_mode: str, authorization_epoch: int):
    from app.platform.tenancy import TenantContext

    return TenantContext(
        workspace_id=workspace_id, role="owner", initiator_id="e2e-inspector",
        authorization_epoch=authorization_epoch, meta_mode=meta_mode,
    )


def enrollment_state(*, workspace_id: str, meta_mode: str, enrollment_id: str, authorization_epoch: int = 0) -> dict:
    from sqlalchemy import select

    from app.adapters.persistence.database import tenant_session
    from app.scale.models import ScaleEnrollmentRecord

    ctx = _context(workspace_id, meta_mode, authorization_epoch)
    with tenant_session(ctx) as session:
        row = session.execute(
            select(ScaleEnrollmentRecord).filter_by(
                workspace_id=workspace_id, meta_mode=meta_mode, id=enrollment_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return {"found": False}
        return {
            "found": True,
            "id": row.id,
            "lifecycle": row.lifecycle,
            "revision": row.revision,
            "current_episode_id": row.current_episode_id,
            "transition_reason": row.transition_reason,
            "enrollment_candidate_id": row.enrollment_candidate_id,
            "delivery_target_id": row.delivery_target_id,
            "candidate_fingerprint": row.candidate_fingerprint,
            "updated_at": row.updated_at.isoformat(),
        }


def episode_state(*, workspace_id: str, meta_mode: str, episode_id: str, authorization_epoch: int = 0) -> dict:
    from sqlalchemy import select

    from app.adapters.persistence.database import tenant_session
    from app.scale.models import ScaleEpisodeRecord

    ctx = _context(workspace_id, meta_mode, authorization_epoch)
    with tenant_session(ctx) as session:
        row = session.execute(
            select(ScaleEpisodeRecord).filter_by(
                workspace_id=workspace_id, meta_mode=meta_mode, id=episode_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return {"found": False}
        return {
            "found": True,
            "id": row.id,
            "phase": row.phase,
            "enrollment_id": row.enrollment_id,
            "revision": row.revision,
            "projection_revision": row.projection_revision,
            "accepted_evaluation_id": row.accepted_evaluation_id,
            "latest_source_manifest_id": row.latest_source_manifest_id,
            "started_local_day": row.started_local_day.isoformat(),
            "ended_at": row.ended_at.isoformat() if row.ended_at else None,
            "closure_reason": row.closure_reason,
            "metric_semantic_contract_set_id": row.metric_semantic_contract_set_id,
        }


def evaluations(*, workspace_id: str, meta_mode: str, episode_id: str, limit: int, authorization_epoch: int = 0) -> list[dict]:
    from sqlalchemy import select

    from app.adapters.persistence.database import tenant_session
    from app.scale.models import ScaleEvaluationRecord

    ctx = _context(workspace_id, meta_mode, authorization_epoch)
    with tenant_session(ctx) as session:
        rows = session.execute(
            select(ScaleEvaluationRecord)
            .filter_by(workspace_id=workspace_id, meta_mode=meta_mode, episode_id=episode_id)
            .order_by(ScaleEvaluationRecord.created_at.desc())
            .limit(limit)
        ).scalars().all()
        return [
            {
                "id": row.id,
                "snapshot_id": row.snapshot_id,
                "maturity": row.maturity,
                "data_quality_state": row.data_quality_state,
                "health": row.health,
                "health_reason": row.health_reason,
                "policy_version_id": row.policy_version_id,
                "target_version_id": row.target_version_id,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]


def advice_threads(*, workspace_id: str, meta_mode: str, enrollment_id: str, authorization_epoch: int = 0) -> list[dict]:
    from sqlalchemy import select

    from app.adapters.persistence.database import tenant_session
    from app.scale.models import ScaleAdviceThreadRecord

    ctx = _context(workspace_id, meta_mode, authorization_epoch)
    with tenant_session(ctx) as session:
        rows = session.execute(
            select(ScaleAdviceThreadRecord)
            .filter_by(workspace_id=workspace_id, meta_mode=meta_mode, enrollment_id=enrollment_id)
            .order_by(ScaleAdviceThreadRecord.created_at)
        ).scalars().all()
        return [
            {
                "id": row.id,
                "advice_type": row.advice_type,
                "severity": row.severity,
                "lifecycle": row.lifecycle,
                "disposition_kind": row.disposition_kind,
                "episode_id": row.episode_id,
                "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]


def target_state(*, workspace_id: str, meta_mode: str, target_id: str, authorization_epoch: int = 0) -> dict:
    from sqlalchemy import select

    from app.adapters.persistence.database import tenant_session
    from app.scale.models import ScaleTargetVersionRecord

    ctx = _context(workspace_id, meta_mode, authorization_epoch)
    with tenant_session(ctx) as session:
        row = session.execute(
            select(ScaleTargetVersionRecord).filter_by(
                workspace_id=workspace_id, id=target_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return {"found": False}
        return {
            "found": True,
            "id": row.id,
            "lifecycle_state": row.lifecycle_state,
            "revision": row.revision,
            "market_code": row.market_code,
            "currency": row.currency,
        }


def export_state(*, workspace_id: str, meta_mode: str, export_id: str, authorization_epoch: int = 0) -> dict:
    from sqlalchemy import select

    from app.adapters.persistence.database import tenant_session
    from app.scale.models import ScaleExportRecord

    ctx = _context(workspace_id, meta_mode, authorization_epoch)
    with tenant_session(ctx) as session:
        row = session.execute(
            select(ScaleExportRecord).filter_by(
                workspace_id=workspace_id, meta_mode=meta_mode, id=export_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return {"found": False}
        return {
            "found": True,
            "id": row.id,
            "operation_id": row.operation_id,
            "export_kind": row.export_kind,
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
            "result_object_reference": row.result_object_reference,
        }


def command_journal_entry(
    *, workspace_id: str, meta_mode: str, command_type: str, dedup_key: str, authorization_epoch: int = 0,
) -> dict:
    from sqlalchemy import select

    from app.adapters.persistence.database import tenant_session
    from app.scale.command_journal import compute_identity_hash
    from app.scale.models import ScaleCommandJournalRecord

    ctx = _context(workspace_id, meta_mode, authorization_epoch)
    with tenant_session(ctx) as session:
        row = session.execute(
            select(ScaleCommandJournalRecord).filter_by(
                workspace_id=workspace_id, meta_mode=meta_mode,
                command_type=command_type, dedup_key=dedup_key,
            )
        ).scalar_one_or_none()
        if row is None:
            return {"found": False}
        return {
            "found": True,
            "id": row.id,
            "identity_hash": compute_identity_hash(command_type, dedup_key),
            "result_resource_id": row.result_resource_id,
            "result_resource_type": row.result_resource_type,
            "request_fingerprint": row.request_fingerprint,
            "created_at": row.created_at.isoformat(),
        }


def zero_meta_writes(*, workspace_id: str, authorization_epoch: int = 0) -> dict:
    """Asserts (spec §4.5's closing sentence, mirrored by
    `tests/test_scale_security.py::TestRuntimeSpyAcrossFlows`) that no
    `MetaCallIntentRecord` row this Workspace's Scale evaluation/advice/
    export pipeline created ever carries a write method. Scale never
    imports a write-capable Meta port (`app.scale.worker`'s module
    docstring), so this is a defence-in-depth check, not the primary proof
    -- the primary proof is architectural (no import path exists)."""
    from sqlalchemy import select

    from app import config as app_config
    from app.adapters.persistence.database import tenant_session
    from app.meta.models import MetaCallIntentRecord
    from app.platform.tenancy import TenantContext

    ctx = TenantContext(workspace_id=workspace_id, role="owner", initiator_id="e2e-inspector", authorization_epoch=authorization_epoch)
    write_methods = ("POST", "DELETE", "PUT", "PATCH")
    with tenant_session(ctx) as session:
        rows = session.execute(
            select(MetaCallIntentRecord.id, MetaCallIntentRecord.meta_api_method).filter_by(
                workspace_id=workspace_id,
            )
        ).all()
        offending = [
            {"id": row.id, "meta_api_method": row.meta_api_method}
            for row in rows
            if any(row.meta_api_method.upper().startswith(m) for m in write_methods)
        ]
        return {
            "total_call_intents": len(rows),
            "offending": offending,
            "meta_write_token_empty": app_config.META_WRITE_ACCESS_TOKEN == "",
            "meta_write_ready": app_config.meta_write_ready(),
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    enr = subparsers.add_parser("enrollment")
    enr.add_argument("--workspace-id", required=True)
    enr.add_argument("--meta-mode", required=True)
    enr.add_argument("--enrollment-id", required=True)
    enr.add_argument("--authorization-epoch", type=int, default=0)

    ep = subparsers.add_parser("episode")
    ep.add_argument("--workspace-id", required=True)
    ep.add_argument("--meta-mode", required=True)
    ep.add_argument("--episode-id", required=True)
    ep.add_argument("--authorization-epoch", type=int, default=0)

    ev = subparsers.add_parser("evaluations")
    ev.add_argument("--workspace-id", required=True)
    ev.add_argument("--meta-mode", required=True)
    ev.add_argument("--episode-id", required=True)
    ev.add_argument("--limit", type=int, default=10)
    ev.add_argument("--authorization-epoch", type=int, default=0)

    at = subparsers.add_parser("advice-threads")
    at.add_argument("--workspace-id", required=True)
    at.add_argument("--meta-mode", required=True)
    at.add_argument("--enrollment-id", required=True)
    at.add_argument("--authorization-epoch", type=int, default=0)

    tg = subparsers.add_parser("target")
    tg.add_argument("--workspace-id", required=True)
    tg.add_argument("--meta-mode", required=True)
    tg.add_argument("--target-id", required=True)
    tg.add_argument("--authorization-epoch", type=int, default=0)

    ex = subparsers.add_parser("export")
    ex.add_argument("--workspace-id", required=True)
    ex.add_argument("--meta-mode", required=True)
    ex.add_argument("--export-id", required=True)
    ex.add_argument("--authorization-epoch", type=int, default=0)

    cj = subparsers.add_parser("command-journal")
    cj.add_argument("--workspace-id", required=True)
    cj.add_argument("--meta-mode", required=True)
    cj.add_argument("--command-type", required=True)
    cj.add_argument("--dedup-key", required=True)
    cj.add_argument("--authorization-epoch", type=int, default=0)

    zmw = subparsers.add_parser("zero-meta-writes")
    zmw.add_argument("--workspace-id", required=True)
    zmw.add_argument("--authorization-epoch", type=int, default=0)

    args = parser.parse_args()
    if args.command == "enrollment":
        result = enrollment_state(
            workspace_id=args.workspace_id, meta_mode=args.meta_mode,
            enrollment_id=args.enrollment_id, authorization_epoch=args.authorization_epoch,
        )
    elif args.command == "episode":
        result = episode_state(
            workspace_id=args.workspace_id, meta_mode=args.meta_mode,
            episode_id=args.episode_id, authorization_epoch=args.authorization_epoch,
        )
    elif args.command == "evaluations":
        result = evaluations(
            workspace_id=args.workspace_id, meta_mode=args.meta_mode,
            episode_id=args.episode_id, limit=args.limit, authorization_epoch=args.authorization_epoch,
        )
    elif args.command == "advice-threads":
        result = advice_threads(
            workspace_id=args.workspace_id, meta_mode=args.meta_mode,
            enrollment_id=args.enrollment_id, authorization_epoch=args.authorization_epoch,
        )
    elif args.command == "target":
        result = target_state(
            workspace_id=args.workspace_id, meta_mode=args.meta_mode,
            target_id=args.target_id, authorization_epoch=args.authorization_epoch,
        )
    elif args.command == "export":
        result = export_state(
            workspace_id=args.workspace_id, meta_mode=args.meta_mode,
            export_id=args.export_id, authorization_epoch=args.authorization_epoch,
        )
    elif args.command == "command-journal":
        result = command_journal_entry(
            workspace_id=args.workspace_id, meta_mode=args.meta_mode,
            command_type=args.command_type, dedup_key=args.dedup_key,
            authorization_epoch=args.authorization_epoch,
        )
    elif args.command == "zero-meta-writes":
        result = zero_meta_writes(workspace_id=args.workspace_id, authorization_epoch=args.authorization_epoch)
    else:  # pragma: no cover
        raise RuntimeError(f"Unknown command {args.command!r}")

    json.dump(result, sys.stdout, sort_keys=True, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
