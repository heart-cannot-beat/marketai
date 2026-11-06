"""Read-only PostgreSQL inspection helpers for the Experiments E2E suite.

`/api/internal/fake-meta-objects` and `/api/internal/call-intents` do not
exist in this codebase (grepped `app/` for both routes -- neither is
registered), so `helpers.ts`'s `inspectFakeMetaObjects`/`inspectCallIntents`
shell out to this script instead of making an HTTP call. Every query runs
inside a `tenant_session`, so it is bound by the same RLS a real
tenant-scoped FastAPI request would be -- this never bypasses tenant
isolation, it only replaces the missing HTTP surface.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))


def fake_meta_objects(
    *, workspace_id: str, meta_mode: str, scenario_version_id: str, authorization_epoch: int = 0,
) -> list[dict]:
    from sqlalchemy import select

    from app.adapters.persistence.database import tenant_session
    from app.meta.models import FakeMetaObjectRecord
    from app.platform.tenancy import TenantContext

    ctx = TenantContext(
        workspace_id=workspace_id, role="owner", initiator_id="e2e-inspector",
        authorization_epoch=authorization_epoch,
    )
    with tenant_session(ctx) as session:
        rows = session.scalars(
            select(FakeMetaObjectRecord)
            .where(
                FakeMetaObjectRecord.workspace_id == workspace_id,
                FakeMetaObjectRecord.meta_mode == meta_mode,
                FakeMetaObjectRecord.scenario_version_id == scenario_version_id,
            )
            .order_by(FakeMetaObjectRecord.created_at)
        ).all()
        return [
            {
                "object_type": row.object_type,
                "remote_object_id": row.remote_object_id,
                "state": row.state,
            }
            for row in rows
        ]


def jobs_for_operation(
    *, workspace_id: str, operation_id: str, job_type: str | None = None,
    authorization_epoch: int = 0,
) -> list[dict]:
    """Jobs enqueued for one Operation, for the Worker-restart/lease-expiry
    recovery scenarios (issue #88 Scenarios 5a-c, 9). No HTTP route exposes
    `JobRecord` rows either -- same rationale as `fake_meta_objects`/
    `call_intents` above.
    """
    from sqlalchemy import or_, select

    from app.adapters.persistence.database import tenant_session
    from app.adapters.persistence.models import JobRecord
    from app.platform.tenancy import TenantContext

    ctx = TenantContext(
        workspace_id=workspace_id, role="owner", initiator_id="e2e-inspector",
        authorization_epoch=authorization_epoch,
    )
    with tenant_session(ctx) as session:
        # `launch_provisioning` Jobs carry the Launch operation id only in
        # their payload; `JobRecord.operation_id` references OperationRecord.
        stmt = select(JobRecord).where(
            JobRecord.workspace_id == workspace_id,
            or_(
                JobRecord.operation_id == operation_id,
                JobRecord.payload_json["operation_id"].as_string() == operation_id,
            ),
        )
        if job_type:
            stmt = stmt.where(JobRecord.type == job_type)
        stmt = stmt.order_by(JobRecord.created_at)
        rows = session.scalars(stmt).all()
        return [
            {
                "id": row.id,
                "type": row.type,
                "state": row.state,
                "attempt": row.attempt,
                "max_attempts": row.max_attempts,
                "lease_owner": row.lease_owner,
                "lease_expires_at": (
                    row.lease_expires_at.isoformat() if row.lease_expires_at else None
                ),
                "error_code": row.error_code,
            }
            for row in rows
        ]


def call_intents(
    *, workspace_id: str, operation_id: str, authorization_epoch: int = 0,
) -> list[dict]:
    from sqlalchemy import select

    from app.adapters.persistence.database import tenant_session
    from app.meta.models import MetaCallIntentRecord
    from app.platform.tenancy import TenantContext

    ctx = TenantContext(
        workspace_id=workspace_id, role="owner", initiator_id="e2e-inspector",
        authorization_epoch=authorization_epoch,
    )
    with tenant_session(ctx) as session:
        rows = session.scalars(
            select(MetaCallIntentRecord)
            .where(
                MetaCallIntentRecord.workspace_id == workspace_id,
                MetaCallIntentRecord.operation_id == operation_id,
            )
            .order_by(MetaCallIntentRecord.id)
        ).all()
        return [
            {
                "id": row.id,
                "meta_api_method": row.meta_api_method,
                "network_dispatched_at": (
                    row.network_dispatched_at.isoformat()
                    if row.network_dispatched_at
                    else None
                ),
            }
            for row in rows
        ]


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    fmo = subparsers.add_parser("fake-meta-objects")
    fmo.add_argument("--workspace-id", required=True)
    fmo.add_argument("--meta-mode", required=True)
    fmo.add_argument("--scenario-version-id", required=True)
    fmo.add_argument("--authorization-epoch", type=int, default=0)

    ci = subparsers.add_parser("call-intents")
    ci.add_argument("--workspace-id", required=True)
    ci.add_argument("--operation-id", required=True)
    ci.add_argument("--authorization-epoch", type=int, default=0)

    jfo = subparsers.add_parser("job-for-operation")
    jfo.add_argument("--workspace-id", required=True)
    jfo.add_argument("--operation-id", required=True)
    jfo.add_argument("--job-type", default=None)
    jfo.add_argument("--authorization-epoch", type=int, default=0)

    args = parser.parse_args()
    if args.command == "fake-meta-objects":
        result = fake_meta_objects(
            workspace_id=args.workspace_id,
            meta_mode=args.meta_mode,
            scenario_version_id=args.scenario_version_id,
            authorization_epoch=args.authorization_epoch,
        )
    elif args.command == "call-intents":
        result = call_intents(
            workspace_id=args.workspace_id,
            operation_id=args.operation_id,
            authorization_epoch=args.authorization_epoch,
        )
    else:
        result = jobs_for_operation(
            workspace_id=args.workspace_id,
            operation_id=args.operation_id,
            job_type=args.job_type,
            authorization_epoch=args.authorization_epoch,
        )

    json.dump(result, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
