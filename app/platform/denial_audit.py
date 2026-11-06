"""Structured denial audit events for cross-workspace access attempts.

Row Level Security (``migrations/versions/f1a2c3d4e5b6_row_level_security.py``,
extended for the Generation -> Assets tables by
``migrations/versions/n1_generation_assets_expand.py``'s ``NEW_WORKSPACE_OWNED``)
is the tenant boundary the hosted PostgreSQL runtime actually enforces: a
workspace-scoped query simply cannot see another Workspace's row, so most of
the time there is nothing here to observe -- the row is invisible, not merely
refused, and application code cannot tell "never existed" from "exists, but
not yours" (see ``app.generation.asset_queries.resolve_asset_blob_ref``'s own
docstring on that point).

This module exists for the one case that split can still surface: an id that
resolves to a *different*, real Workspace than the caller's. A domain
function that fails a workspace-scoped lookup may run a second, id-only
lookup (``find_owning_workspace``) purely to classify which case it hit, and
-- only when the id belongs to a different real Workspace -- append a
``generation.cross_workspace_denial`` audit event before it raises its usual
not-found error. The error the caller sees never changes: this is
observability for operators, never a different response for the requester
(the "identifier guessing does not reveal existence" property tested in
``tests/test_postgres_rls.py`` is unaffected).

On PostgreSQL the id-only lookup runs on the same RLS-scoped connection as
everything else in the request, so it is itself filtered to the caller's
Workspace and returns nothing for a genuine cross-tenant id in the runtime
path -- RLS is already the enforcement there, and FORCE ROW LEVEL SECURITY
means even the owning role cannot see past it. On SQLite, the bounded local
path with no RLS (``app.platform.tenancy``'s documented split of enforcement
between the two databases), the second lookup is what makes the signal
observable at all, which is exactly the path this module's tests exercise.

The audit event is written in its own, independently committed transaction
(``app.platform.audit.record_event``) rather than appended to the caller's
already-open session: the caller is about to raise, its transaction is going
to roll back, and a denial record that vanished along with the denied
operation would defeat the point of auditing it.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from .tenancy import TenantContext

logger = logging.getLogger("denial_audit")

#: The one event_type this module produces. Kept as a module constant so
#: callers and tests share the exact string rather than retyping it.
CROSS_WORKSPACE_DENIAL = "generation.cross_workspace_denial"


def find_owning_workspace(
    session: Session,
    model: type,
    resource_id: str,
    *,
    id_column: str = "id",
    workspace_column: str = "workspace_id",
) -> str | None:
    """The Workspace that actually owns ``resource_id``, ignoring the caller's scope.

    Used only to classify an already-failed workspace-scoped lookup for audit
    purposes -- the result must never be returned to the caller, only logged.
    Returns ``None`` both when the id was never issued and (on PostgreSQL,
    where RLS filters this query too) when it belongs to another Workspace --
    see the module docstring for why the distinction only surfaces on SQLite.
    """
    return session.execute(
        select(getattr(model, workspace_column)).where(
            getattr(model, id_column) == resource_id
        )
    ).scalar_one_or_none()


def record_cross_workspace_denial(
    ctx: TenantContext,
    *,
    resource_kind: str,
    resource_id: str,
    target_workspace_id: str,
    detail: str = "",
) -> None:
    """Append a durable, structured audit event for a denied cross-workspace access.

    Committed independently of the caller's transaction (see module
    docstring) so it survives the not-found error the caller raises right
    after this returns. Also logged at WARNING for log-based alerting/SIEM
    ingestion that does not want to query the audit table.
    """
    from .audit import record_event

    logger.warning(
        "cross_workspace_denial",
        extra={
            "event_type": CROSS_WORKSPACE_DENIAL,
            "resource_kind": resource_kind,
            "resource_id": resource_id,
            "requesting_workspace_id": ctx.workspace_id,
            "target_workspace_id": target_workspace_id,
            "actor_id": ctx.initiator_id,
        },
    )
    record_event(
        ctx,
        event_type=CROSS_WORKSPACE_DENIAL,
        resource_type=resource_kind,
        resource_id=resource_id,
        details={
            "target_workspace_id": target_workspace_id,
            "requesting_workspace_id": ctx.workspace_id,
            "detail": detail,
        },
    )


#: The event_type ``check_and_record_mode_denial`` produces for the
#: same-workspace/different-mode case (issue #86 hardening, Task 9). Distinct
#: from ``CROSS_WORKSPACE_DENIAL`` above: this is not a tenant-boundary
#: violation (RLS's ``(workspace_id, meta_mode)`` policy already made the
#: row invisible to a mismatched mode, exactly as it does for a mismatched
#: workspace -- see ``m23a_meta_foundation``'s
#: ``marketai_meta_context_is_current`` helper), only a same-tenant
#: cross-Meta-mode access attempt worth recording for operators (spec §3,
#: ADR 0009's "a Workspace may operate more than one Meta mode" model).
CROSS_MODE_DENIAL = "scale.cross_mode_denial"


def check_and_record_mode_denial(
    session: Session,
    ctx: TenantContext,
    model: type,
    resource_id: str,
    *,
    resource_kind: str,
    detail: str = "",
    id_column: str = "id",
    workspace_column: str = "workspace_id",
    mode_column: str = "meta_mode",
) -> None:
    """Classify a workspace+mode-scoped lookup miss and record it when
    ``resource_id`` belongs to the caller's own Workspace but a *different*
    Meta mode -- the Scale-specific sibling of ``check_and_record_denial``
    (issue #86, Task 9). Cross-workspace hits are still classified and
    recorded as ``CROSS_WORKSPACE_DENIAL`` by this same call, exactly as
    ``check_and_record_denial`` does, since a mismatched workspace is a
    genuine tenant-boundary violation regardless of mode.

    Call this right after a
    ``WHERE id = :id AND workspace_id = :ctx_workspace AND meta_mode = :ctx_mode``
    lookup comes back empty, before raising the not-found error. A no-op
    (no audit event, no query beyond the classification lookup) when
    ``resource_id`` was never issued to any Workspace.
    """
    row = session.execute(
        select(getattr(model, workspace_column), getattr(model, mode_column)).where(
            getattr(model, id_column) == resource_id
        )
    ).first()
    if row is None:
        return
    target_workspace_id, target_mode = row[0], row[1]

    if target_workspace_id != ctx.workspace_id:
        record_cross_workspace_denial(
            ctx,
            resource_kind=resource_kind,
            resource_id=resource_id,
            target_workspace_id=target_workspace_id,
            detail=detail,
        )
        return

    if target_mode != ctx.meta_mode:
        record_cross_mode_denial(
            ctx,
            resource_kind=resource_kind,
            resource_id=resource_id,
            target_mode=target_mode,
            detail=detail,
        )


def record_cross_mode_denial(
    ctx: TenantContext,
    *,
    resource_kind: str,
    resource_id: str,
    target_mode: str,
    detail: str = "",
) -> None:
    """Append a durable, structured audit event for a denied same-workspace,
    cross-Meta-mode access attempt. Committed independently of the caller's
    transaction (mirrors ``record_cross_workspace_denial``'s reasoning --
    see the module docstring)."""
    from .audit import record_event

    logger.warning(
        "cross_mode_denial",
        extra={
            "event_type": CROSS_MODE_DENIAL,
            "resource_kind": resource_kind,
            "resource_id": resource_id,
            "requesting_workspace_id": ctx.workspace_id,
            "requesting_mode": ctx.meta_mode,
            "target_mode": target_mode,
            "actor_id": ctx.initiator_id,
        },
    )
    record_event(
        ctx,
        event_type=CROSS_MODE_DENIAL,
        resource_type=resource_kind,
        resource_id=resource_id,
        details={
            "requesting_mode": ctx.meta_mode,
            "target_mode": target_mode,
            "detail": detail,
        },
    )


def check_and_record_denial(
    session: Session,
    ctx: TenantContext,
    model: type,
    resource_id: str,
    *,
    resource_kind: str,
    detail: str = "",
    id_column: str = "id",
    workspace_column: str = "workspace_id",
) -> None:
    """Classify a workspace-scoped lookup miss and record it if it was a
    cross-workspace hit rather than a genuine not-found.

    Call this right after a ``WHERE id = :id AND workspace_id = :ctx_workspace``
    lookup comes back empty, before raising the not-found error. A no-op
    (no audit event, no query beyond the classification lookup) when
    ``resource_id`` was never issued to any Workspace.
    """
    target_workspace_id = find_owning_workspace(
        session, model, resource_id, id_column=id_column, workspace_column=workspace_column
    )
    if target_workspace_id is not None and target_workspace_id != ctx.workspace_id:
        record_cross_workspace_denial(
            ctx,
            resource_kind=resource_kind,
            resource_id=resource_id,
            target_workspace_id=target_workspace_id,
            detail=detail,
        )
