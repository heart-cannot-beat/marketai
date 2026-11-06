"""Read-side of the workspace authorization projection (spec §3.3).

`DelegatedIdentityMiddleware` must read `authorization_epoch`,
`entitlement_version`, and `suspended` *before* a `TenantContext` exists --
that is the whole point of the admission check. On PostgreSQL, Row Level
Security would otherwise make the `workspaces` table unreadable at this
point: there is no tenant scope yet to satisfy the RLS predicate. The
`marketai_workspace_admission` SQL function (Alembic migration
`g1a2b3c4d5e6`) is `SECURITY DEFINER`, so it runs as the schema owner
(`marketai_domain_owner`, which has `BYPASSRLS`) and can read the one
Workspace row this check needs without granting the runtime role any direct
read access to the table.

`is_platform_workspace` was added to the function's return row in migration
`aa1_identity_kind` so a platform-operator route (Task 6) can derive
`TenantContext.is_platform_workspace` from the same pre-tenant admission read
rather than a second query. It defaults to `False` on the dataclass and via
`row.get(...)` in `_read_sync` so a caller reading through an older, not-yet
re-migrated function signature still gets a safe default instead of a
`KeyError`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import anyio
from sqlalchemy import text
from sqlalchemy.orm import Session


class WorkspaceNotFoundError(Exception):
    """No Workspace row exists for this ID in the domain projection."""


@dataclass(frozen=True)
class WorkspaceAdmission:
    workspace_id: str
    authorization_epoch: int
    entitlement_version: int
    suspended: bool
    is_platform_workspace: bool = False


class WorkspaceAdmissionReader:
    """Wraps session creation, thread offload, and cleanup behind one call.

    Accepts the same `sessionmaker` the rest of the domain uses -- no
    separate connection pool. `read()` offloads the synchronous SQLAlchemy
    call via `anyio.to_thread.run_sync` so the async middleware never blocks
    the event loop on this query.
    """

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    async def read(self, workspace_id: str) -> WorkspaceAdmission:
        return await anyio.to_thread.run_sync(lambda: self._read_sync(workspace_id))

    def _read_sync(self, workspace_id: str) -> WorkspaceAdmission:
        session = self._session_factory()
        try:
            row = session.execute(
                text("SELECT * FROM marketai_workspace_admission(:ws_id)"),
                {"ws_id": workspace_id},
            ).mappings().first()
            if row is None or row["workspace_id"] is None:
                raise WorkspaceNotFoundError(workspace_id)
            return WorkspaceAdmission(
                workspace_id=row["workspace_id"],
                authorization_epoch=row["authorization_epoch"],
                entitlement_version=row["entitlement_version"],
                suspended=row["suspended"],
                is_platform_workspace=row.get("is_platform_workspace", False) or False,
            )
        finally:
            session.close()
