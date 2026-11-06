"""HTTP adapter seam for an already-verified delegated identity.

The control-plane/BFF integration (#17) owns ES256 verification and writes the
resulting :class:`TenantContext` to ``request.state``.  Domain handlers consume
only that typed result: they never accept a browser-provided Workspace header,
query parameter, or deployment default as authority.
"""
from __future__ import annotations

from fastapi import HTTPException, Request

from .tenancy import TenantContext, new_request_id, ui_language_from_header


def request_tenant_context(request: Request) -> TenantContext:
    context = getattr(request.state, "tenant_context", None)
    if context is None:
        from .. import config
        from ..adapters.persistence.database import engine

        if config.LOCAL_IDENTITY_ENABLED:
            if engine.dialect.name != "sqlite":
                raise HTTPException(
                    status_code=500,
                    detail={
                        "code": "local_identity_forbidden",
                        "message": "Local identity is available only on SQLite.",
                    },
                )
            request_id = new_request_id()
            context = TenantContext(
                workspace_id=config.LOCAL_WORKSPACE_ID,
                role="owner",
                initiator_id=config.LOCAL_INITIATOR_ID,
                request_id=request_id,
                trace_id=request_id,
                ui_language=ui_language_from_header(request.headers.get("x-ui-language")),
            )
            request.state.tenant_context = context
    if not isinstance(context, TenantContext):
        raise HTTPException(
            status_code=401,
            detail={
                "code": "delegated_identity_required",
                "message": "A verified delegated identity is required.",
            },
        )
    return context
