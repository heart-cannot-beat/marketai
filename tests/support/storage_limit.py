"""Give a test Workspace the ``storage.bytes.max`` its admissions read (#158).

Upload and generation admission fail closed without the key. Since #156 the
control plane publishes it in every Workspace's Entitlement Projection; tests
that create Workspaces directly add it here, merged into the projection they
already seeded (or into a minimal one when they seeded none).
"""
from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy.orm import Session

from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import utcnow
from app.entitlements.models import EntitlementProjectionRecord
from app.entitlements.storage_quota import STORAGE_LIMIT_KEY
from app.entitlements.storage_usage import workspace_storage_bytes
from app.platform.tenancy import TenantContext

#: Contract §8's Beta value, the platform default since #156.
BETA_STORAGE_LIMIT_BYTES = 10 * 1024 ** 3


def grant_storage_limit(
    session: Session,
    workspace_id: str,
    limit_bytes: int | None = BETA_STORAGE_LIMIT_BYTES,
) -> EntitlementProjectionRecord:
    """Set (or, with ``None``, remove) the Workspace's storage limit. Flushes."""
    projection = session.get(EntitlementProjectionRecord, workspace_id)
    if projection is None:
        now = utcnow()
        projection = EntitlementProjectionRecord(
            workspace_id=workspace_id,
            entitlement_version=1,
            lease_sequence=1,
            projection_version=1,
            schema_version=2,
            semantic_hash=f"storage-limit-{workspace_id}",
            issued_at=now - timedelta(days=1),
            valid_until=now + timedelta(days=30),
            period_start=now - timedelta(days=1),
            period_end=now + timedelta(days=29),
            resolved_entitlements_json={},
        )
        session.add(projection)
    raw = projection.resolved_entitlements_json or {}
    entitlements = dict(json.loads(raw) if isinstance(raw, str) else raw)
    if limit_bytes is None:
        entitlements.pop(STORAGE_LIMIT_KEY, None)
    else:
        entitlements[STORAGE_LIMIT_KEY] = limit_bytes
    projection.resolved_entitlements_json = entitlements
    session.flush()
    return projection


def set_storage_headroom(context: TenantContext, headroom: int) -> int:
    """Set the Workspace's limit to what it stores now plus ``headroom``.

    ``0`` puts it exactly at its limit (generation is refused), ``1`` one byte
    below (admitted). Returns the bytes stored.
    """
    with tenant_session(context) as session:
        stored = workspace_storage_bytes(session, context)
    with platform_session() as session:
        grant_storage_limit(session, context.workspace_id, stored + headroom)
    return stored
