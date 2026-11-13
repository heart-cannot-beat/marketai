"""Platform administration routes for Meta credential bootstrap (spec §8.2, §8.7).

Distinct from every other router in this codebase: these routes act
*across* Workspaces on behalf of a platform operator, not within one
Workspace derived from a verified delegated identity. They therefore do
not depend on ``request_tenant_context`` (spec §3's BFF-derived,
single-Workspace identity) -- they use their own
:func:`request_platform_context` dependency.

On PostgreSQL ``marketai_domain_api`` is NOBYPASSRLS and the domain tables
are FORCE RLS, so an untenanted ``platform_session()`` reads nothing. Every
route therefore learns what it needs before a TenantContext exists through
narrow ``SECURITY DEFINER`` functions -- the Workspace's epoch and platform
flag (``marketai_workspace_admission``), the platform Workspace's creation
(``marketai_create_platform_workspace_v1``), a credential version's owning
Workspace (``marketai_credential_version_workspace_v1``), and where Meta
connections live (``marketai_meta_connection_ids_v1`` across Workspaces,
``marketai_meta_connection_workspace_v1`` by id; #102) -- and does everything
else in a TenantContext signed for that Workspace's current epoch (and, for
Meta rows, their Meta mode, which the Meta tables' policy binds). SQLite has
no RLS and reads and writes those rows directly.

Routes (per spec §8.2 platform allowlist / §8.7):

* ``POST /api/platform/v1/meta/bootstrap`` -- creates the initial
  ``CredentialVersionRecord`` row(s), a ``MetaConnectionRecord``, and its
  first ``MetaDataSourceBindingRecord`` in one transaction. This is how a
  Workspace gets its first Meta connection at all (spec §7.3: only the
  platform Workspace may hold ``internal_validation`` connections, and only
  a platform operator may create sandbox connections' initial credential
  material -- there is deliberately no self-service "create my first
  connection" affordance for internal_validation). A customer Workspace
  must already exist: only the control-plane projection creates one, and
  bootstrap answers 409 ``workspace_not_provisioned`` otherwise (#91). The
  platform Workspace, which the control plane does not project, is created
  here on its first ``internal_validation`` bootstrap.
* ``GET /api/platform/v1/meta/connections`` / ``GET .../{id}`` -- the
  platform-wide read view across every Workspace, for operators diagnosing
  a specific tenant's connection without knowing its Workspace up front.
  The list runs oldest first and holds at most ``limit`` connections (at
  most :data:`CONNECTION_LIST_LIMIT`, the default); ``after=<last id>``
  continues from the previous page, and a short page is the last one.
* ``POST /api/platform/v1/meta/connections/{id}/reconcile`` -- re-derives
  and returns the connection's current projection (connection + active
  binding state). See the docstring on :func:`reconcile_connection` for
  what this task scopes in versus the fuller cross-mode reconciliation the
  spec gestures at.
* ``POST /api/platform/v1/credentials/{id}/revoke`` -- platform-operator
  credential revocation. MFA is checked only at login (ADR 0015), so the
  route needs nothing beyond the platform-operator identity.

Platform-operator auth (Task 4, spec §8.2): :func:`request_platform_context`
is JWT-primary now that ``PlatformOperatorJWTVerifier`` exists (Task 3,
``app.platform.platform_operator_identity``) -- it verifies the bearer token
as a ``typ: "platform+jwt"`` token first when a verifier is wired onto
``request.app.state.platform_operator_jwt_verifier``. The former *only*
mechanism, a static bearer token compared with ``config.PLATFORM_ADMIN_TOKEN``,
is retained solely as a deprecated fallback for deployments that have not
yet rolled out platform-operator JWTs: it is tried only when no verifier is
configured, or the verifier is configured but rejects the token, and it logs
a ``WARNING`` on every use so that use is visible and can be tracked toward
retirement. This dependency is distinct from ``require_platform_operator_jwt``
(Task 3), which is JWT-only with no fallback and is what new platform routes
(e.g. Task 6's ``app.scale.platform_router``) should use directly; this
module's routes are the sole remaining callers of the transitional
``request_platform_context``/``LegacyPlatformContext`` pair while they carry
both auth mechanisms. It is overridden directly in tests via
``app.dependency_overrides``.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import and_, or_, select, text
from sqlalchemy.orm import Session, aliased

from . import connections as connections_service
from .models import MetaConnectionRecord, MetaDataSourceBindingRecord
from .. import config
from ..adapters.persistence.database import platform_session, tenant_session
from ..adapters.persistence.models import AuditEventRecord, WorkspaceRecord, utcnow
from ..entitlements.credential_admin import CredentialAdmin
from ..entitlements.credential_protector import LocalKmsAdapter
from ..entitlements.credentials import CredentialVersionRecord
from ..platform.platform_operator_identity import PlatformTokenError
from ..platform.tenancy import VALID_META_MODES, TenantContext, is_postgres

router = APIRouter(prefix="/api/platform/v1", tags=["platform-admin"])

logger = logging.getLogger(__name__)

#: Attribution for domain rows written by a platform operator, distinct
#: from the Worker (`system:worker`) and migrations (`system:migration`)
#: `TenantContext` executors defined in `app.platform.tenancy`.
PLATFORM_EXECUTOR = "system:platform_admin"


# ---------------------------------------------------------------------------
# Platform auth guard (JWT-primary, deprecated static-token fallback -- see
# module docstring)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LegacyPlatformContext:
    """The verified platform-operator identity for one platform-admin request.

    ``auth_method`` records which of the two mechanisms
    :func:`request_platform_context` accepted -- ``"jwt"`` (the primary,
    preferred path) or ``"static_token"`` (the deprecated fallback, logged
    at ``WARNING`` on every use). ``workspace_id`` carries the token's
    ``workspace_id`` claim for a JWT-authenticated request; it is ``None``
    for the static-token fallback, which (like the old ``PlatformContext``
    it replaces) has never carried a Workspace scope of its own -- every
    route here still takes ``workspace_id`` from the request body/path, not
    from this context.
    """

    actor_id: str
    workspace_id: str | None
    auth_method: str  # "jwt" or "static_token"


async def request_platform_context(request: Request) -> LegacyPlatformContext:
    """Verify platform-admin access: JWT primary, static token deprecated fallback.

    See the module docstring's "Platform-operator auth" note. Fails closed
    with 503 when *neither* mechanism is configured (no
    ``platform_operator_jwt_verifier`` wired onto ``request.app.state`` *and*
    no ``config.PLATFORM_ADMIN_TOKEN``) -- matching every other unconfigured
    service-token gate in this codebase (e.g.
    ``app.entitlements.api._verify_service_token``,
    ``require_platform_operator_jwt``'s own 503 branch) -- rather than
    silently reducing to "every request gets a generic 401" once nothing is
    configured. Once at least one mechanism is configured, a missing/
    malformed ``Authorization`` header is 401, and a token that satisfies
    neither the (configured) verifier nor the (configured) static comparison
    is also 401 -- there is deliberately no separate "wrong token" 403 here,
    matching ``require_platform_operator_jwt``'s all-token-errors-are-401
    convention now that a real JWT validator exists on this surface.
    """
    verifier = getattr(request.app.state, "platform_operator_jwt_verifier", None)
    static_token = config.PLATFORM_ADMIN_TOKEN

    if verifier is None and not static_token:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "platform_admin_not_configured",
                "message": "Platform administration routes are not configured.",
            },
        )

    authorization = request.headers.get("Authorization")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail={
                "code": "platform_admin_token_required",
                "message": "A platform-admin bearer token is required.",
            },
        )
    token = authorization[len("Bearer "):]

    if verifier is not None:
        try:
            identity = await verifier.verify(token)
        except PlatformTokenError:
            pass  # Fall through to the deprecated static-token check.
        else:
            return LegacyPlatformContext(
                actor_id=f"platform:{identity.sub}",
                workspace_id=identity.workspace_id,
                auth_method="jwt",
            )

    if static_token and hmac.compare_digest(token, static_token):
        logger.warning(
            "platform_admin_static_token_used: deprecated auth path used for "
            "%s -- migrate this caller to a platform-operator JWT",
            request.url.path,
        )
        return LegacyPlatformContext(
            actor_id="platform:admin", workspace_id=None, auth_method="static_token",
        )

    raise HTTPException(
        status_code=401,
        detail={
            "code": "platform_admin_token_invalid",
            "message": "Invalid platform-admin bearer token.",
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_protector() -> LocalKmsAdapter:
    """Same envelope-encryption seam as ``app.entitlements.api._get_protector``."""
    secret = config.MARKETAI_CREDENTIAL_LOCAL_KMS_SECRET
    if not secret:
        secret = "dev-only-insecure-placeholder-key!!"
    return LocalKmsAdapter(secret.encode() if isinstance(secret, str) else secret)


@dataclass(frozen=True)
class _WorkspaceScope:
    """What a platform route needs to sign a TenantContext for a Workspace."""

    authorization_epoch: int
    is_platform_workspace: bool


def _workspace_scope(workspace_id: str) -> _WorkspaceScope | None:
    """The Workspace's current epoch and platform flag, or ``None`` if the
    domain database has no such Workspace.

    No TenantContext exists yet and ``workspaces`` is FORCE RLS on
    PostgreSQL, so the pre-tenant admission function (``SECURITY DEFINER``,
    granted to the API) is the read the API role has; SQLite has no RLS and
    reads the row directly.
    """
    with platform_session() as session:
        if is_postgres(session):
            row = session.execute(
                text(
                    "SELECT workspace_id, authorization_epoch, is_platform_workspace "
                    "FROM public.marketai_workspace_admission(:workspace_id)"
                ),
                {"workspace_id": workspace_id},
            ).first()
            if row is None or row.workspace_id is None:
                return None
            return _WorkspaceScope(int(row.authorization_epoch), bool(row.is_platform_workspace))
        workspace = session.get(WorkspaceRecord, workspace_id)
        if workspace is None:
            return None
        return _WorkspaceScope(workspace.authorization_epoch, workspace.is_platform_workspace)


#: The audit event a platform Workspace's creation writes, once: its id is
#: derived from the Workspace id, the same way the PostgreSQL function does.
PLATFORM_WORKSPACE_CREATED = "workspace.platform_created"


def _create_platform_workspace(workspace_id: str, *, initiator_id: str) -> bool:
    """Create ``workspace_id`` as the platform Workspace; ``False`` if it exists.

    Customer Workspaces come only from the control-plane projection, so this
    creates a row with ``is_platform_workspace = true`` and nothing else, and
    leaves an existing row exactly as it is (a customer Workspace is never
    promoted). On PostgreSQL the API role holds no INSERT on ``workspaces``:
    ``marketai_create_platform_workspace_v1`` (migration ``zzc2f3a4b5c6``)
    makes the row and its audit event. SQLite has no roles and writes the
    same two rows here.
    """
    with platform_session() as session:
        if is_postgres(session):
            return bool(
                session.execute(
                    text(
                        "SELECT public.marketai_create_platform_workspace_v1("
                        ":workspace_id, :initiator_id)"
                    ),
                    {"workspace_id": workspace_id, "initiator_id": initiator_id},
                ).scalar_one()
            )
        if session.get(WorkspaceRecord, workspace_id) is not None:
            return False
        session.add(
            WorkspaceRecord(
                id=workspace_id,
                name=workspace_id,
                created_at=utcnow(),
                is_platform_workspace=True,
            )
        )
        session.flush()
        audit_key = f"{workspace_id}|platform-workspace-created".encode("utf-8")
        session.add(
            AuditEventRecord(
                id="audit_" + hashlib.md5(audit_key, usedforsecurity=False).hexdigest(),
                workspace_id=workspace_id,
                initiator_id=initiator_id,
                initiator_role="system",
                executor=PLATFORM_EXECUTOR,
                request_id="",
                trace_id="",
                authorization_epoch=0,
                meta_mode=None,
                event_type=PLATFORM_WORKSPACE_CREATED,
                resource_type="workspace",
                resource_id=workspace_id,
                details_json={"is_platform_workspace": True},
            )
        )
        return True


def _credential_version_workspace(credential_version_id: str) -> str | None:
    """The Workspace that owns ``credential_version_id``, or ``None``.

    On PostgreSQL ``credential_versions`` is FORCE RLS and the API may read
    only its non-secret columns, so the owner comes from
    ``marketai_credential_version_workspace_v1``, which returns that id and
    nothing else. SQLite reads the one column directly.
    """
    with platform_session() as session:
        if is_postgres(session):
            return session.execute(
                text("SELECT public.marketai_credential_version_workspace_v1(:version_id)"),
                {"version_id": credential_version_id},
            ).scalar_one()
        return session.scalar(
            select(CredentialVersionRecord.workspace_id).where(
                CredentialVersionRecord.id == credential_version_id
            )
        )


#: The most connections ``GET /meta/connections`` returns; on PostgreSQL
#: ``marketai_meta_connection_ids_v1`` enforces the same cap.
CONNECTION_LIST_LIMIT = 500


@dataclass(frozen=True)
class _ListedConnection:
    """One connection in the platform-wide list: where it lives, not what it says."""

    connection_id: str
    workspace_id: str
    meta_mode: str
    workspace: _WorkspaceScope


def _list_connection_ids(*, after: str | None, limit: int) -> list[_ListedConnection]:
    """Connections across every Workspace, oldest first (``created_at``, then
    ``id``), starting after connection ``after`` when given, at most ``limit``.

    Each carries its Workspace, Meta mode and the Workspace's current epoch
    and platform flag -- what signing a context to read it takes -- and none
    of its content. On PostgreSQL that comes from
    ``marketai_meta_connection_ids_v1`` (migration ``zzd3a4b5c6d7``); SQLite
    reads the same columns directly.
    """
    with platform_session() as session:
        if is_postgres(session):
            rows = session.execute(
                text(
                    "SELECT workspace_id, meta_mode, authorization_epoch, "
                    "is_platform_workspace, connection_id "
                    "FROM public.marketai_meta_connection_ids_v1(:after, :limit)"
                ),
                {"after": after, "limit": limit},
            ).all()
        else:
            query = (
                select(
                    MetaConnectionRecord.workspace_id,
                    MetaConnectionRecord.meta_mode,
                    WorkspaceRecord.authorization_epoch,
                    WorkspaceRecord.is_platform_workspace,
                    MetaConnectionRecord.id.label("connection_id"),
                )
                .join(WorkspaceRecord, WorkspaceRecord.id == MetaConnectionRecord.workspace_id)
                .order_by(MetaConnectionRecord.created_at, MetaConnectionRecord.id)
                .limit(limit)
            )
            if after is not None:
                cursor = aliased(MetaConnectionRecord)
                after_created_at = (
                    select(cursor.created_at).where(cursor.id == after).scalar_subquery()
                )
                query = query.where(
                    or_(
                        MetaConnectionRecord.created_at > after_created_at,
                        and_(
                            MetaConnectionRecord.created_at == after_created_at,
                            MetaConnectionRecord.id > after,
                        ),
                    )
                )
            rows = session.execute(query).all()
    return [
        _ListedConnection(
            connection_id=row.connection_id,
            workspace_id=row.workspace_id,
            meta_mode=row.meta_mode,
            workspace=_WorkspaceScope(int(row.authorization_epoch), bool(row.is_platform_workspace)),
        )
        for row in rows
    ]


def _connection_workspace(connection_id: str) -> tuple[str, str] | None:
    """The Workspace and Meta mode of ``connection_id``, or ``None``.

    On PostgreSQL ``meta_connection_records`` is FORCE RLS behind a context
    bound to one Workspace and Meta mode, so both come from
    ``marketai_meta_connection_workspace_v1``, which returns them and nothing
    else. SQLite reads the two columns directly.
    """
    with platform_session() as session:
        if is_postgres(session):
            row = session.execute(
                text(
                    "SELECT workspace_id, meta_mode "
                    "FROM public.marketai_meta_connection_workspace_v1(:connection_id)"
                ),
                {"connection_id": connection_id},
            ).first()
        else:
            row = session.execute(
                select(MetaConnectionRecord.workspace_id, MetaConnectionRecord.meta_mode).where(
                    MetaConnectionRecord.id == connection_id
                )
            ).first()
    return None if row is None else (row.workspace_id, row.meta_mode)


def _platform_tenant_context(
    workspace_id: str,
    scope: _WorkspaceScope,
    platform: LegacyPlatformContext,
    *,
    meta_mode: str | None = None,
) -> TenantContext:
    """A TenantContext for one Workspace, signed for its current epoch."""
    return TenantContext(
        workspace_id=workspace_id,
        role="owner",
        initiator_id=platform.actor_id,
        executor=PLATFORM_EXECUTOR,
        authorization_epoch=scope.authorization_epoch,
        meta_mode=meta_mode,
        is_platform_workspace=scope.is_platform_workspace,
    )


def _connection_context(connection_id: str, platform: LegacyPlatformContext) -> TenantContext:
    """A context signed to read ``connection_id``: its Workspace's current
    epoch and the connection's Meta mode. 404 if there is no such connection."""
    owner = _connection_workspace(connection_id)
    scope = _workspace_scope(owner[0]) if owner is not None else None
    if owner is None or scope is None:
        raise HTTPException(404, "Meta connection not found")
    workspace_id, meta_mode = owner
    return _platform_tenant_context(workspace_id, scope, platform, meta_mode=meta_mode)


def _scoped_connection(
    session: Session, context: TenantContext, connection_id: str,
) -> MetaConnectionRecord:
    """``connection_id`` read in ``context``; 404 if RLS or a race hides it."""
    row = session.scalar(
        select(MetaConnectionRecord).where(
            MetaConnectionRecord.id == connection_id,
            MetaConnectionRecord.workspace_id == context.workspace_id,
            MetaConnectionRecord.meta_mode == context.meta_mode,
        )
    )
    if row is None:
        raise HTTPException(404, "Meta connection not found")
    return row


def _connection_dict(row: MetaConnectionRecord) -> dict:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "meta_mode": row.meta_mode,
        "ad_account_id": row.ad_account_id,
        "state": row.state,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
        "retired_at": row.retired_at.isoformat() if row.retired_at else None,
    }


def _binding_dict(row: MetaDataSourceBindingRecord) -> dict:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "meta_mode": row.meta_mode,
        "meta_connection_id": row.meta_connection_id,
        "ad_account_id": row.ad_account_id,
        "state": row.state,
        "access_token_version_id": row.access_token_version_id,
        "app_secret_version_id": row.app_secret_version_id,
        "created_at": row.created_at.isoformat(),
    }


def _connection_error_status(exc: connections_service.ConnectionLifecycleError) -> int:
    return 404 if "not found" in str(exc) else 400


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


class BootstrapRequest(BaseModel):
    model_config = {"extra": "forbid"}

    workspace_id: str
    meta_mode: str
    ad_account_id: str
    access_token_material: str | None = None
    app_secret_material: str | None = None


@router.post("/meta/bootstrap", status_code=201)
def bootstrap(
    body: BootstrapRequest,
    _platform: LegacyPlatformContext = Depends(request_platform_context),
) -> JSONResponse:
    if body.meta_mode not in VALID_META_MODES:
        raise HTTPException(
            400, {"code": "invalid_meta_mode", "message": f"unsupported meta_mode: {body.meta_mode!r}"},
        )

    # Mode-appropriate credential material, mirroring
    # `connections_service._validate_binding_credentials`: internal_validation
    # requires both; sandbox modes (customer_sandbox/controlled_mock) must
    # carry none -- the FakeMetaAdapter never dereferences credential
    # material (spec §2, §3.2).
    if body.meta_mode == "internal_validation":
        if not body.access_token_material or not body.app_secret_material:
            raise HTTPException(
                400,
                {
                    "code": "credential_material_required",
                    "message": "internal_validation bootstrap requires both "
                    "access_token_material and app_secret_material",
                },
            )
    elif body.access_token_material is not None or body.app_secret_material is not None:
        raise HTTPException(
            400,
            {
                "code": "credential_material_not_allowed",
                "message": f"{body.meta_mode} bootstrap must not carry credential material",
            },
        )

    # Customer Workspaces exist only once the control plane has projected
    # them. The platform Workspace, which it does not project, is the only
    # one bootstrap creates; an existing Workspace keeps its flag, so
    # internal_validation on a customer Workspace still fails below with
    # meta_platform_workspace_required.
    scope = _workspace_scope(body.workspace_id)
    if scope is None:
        if body.meta_mode != "internal_validation":
            raise HTTPException(
                409,
                {
                    "code": "workspace_not_provisioned",
                    "message": f"Workspace {body.workspace_id!r} has not been provisioned "
                    "by the control plane yet.",
                },
            )
        _create_platform_workspace(body.workspace_id, initiator_id=_platform.actor_id)
        scope = _workspace_scope(body.workspace_id)
        if scope is None:  # pragma: no cover -- the row was just committed
            raise RuntimeError(f"platform Workspace {body.workspace_id!r} was not created")

    context = _platform_tenant_context(
        body.workspace_id, scope, _platform, meta_mode=body.meta_mode,
    )

    protector = _get_protector()
    admin = CredentialAdmin(protector)

    access_token_version_id: str | None = None
    app_secret_version_id: str | None = None

    try:
        with tenant_session(context) as session:
            if body.access_token_material is not None:
                meta = admin.create(
                    session, context,
                    credential_key="meta_access_token", provider="meta",
                    plaintext=body.access_token_material.encode("utf-8"),
                    display_label="Meta access token (platform bootstrap)",
                )
                access_token_version_id = meta.credential_version_id
            if body.app_secret_material is not None:
                meta = admin.create(
                    session, context,
                    credential_key="meta_app_secret", provider="meta",
                    plaintext=body.app_secret_material.encode("utf-8"),
                    display_label="Meta app secret (platform bootstrap)",
                )
                app_secret_version_id = meta.credential_version_id

            connection = connections_service.create_connection(
                session, context=context, ad_account_id=body.ad_account_id,
            )
            binding = connections_service.create_binding(
                session, context=context, connection_id=connection.id,
                ad_account_id=body.ad_account_id,
                access_token_version_id=access_token_version_id,
                app_secret_version_id=app_secret_version_id,
            )
    except ValueError as exc:
        # CredentialAdmin.create raises ValueError when an active credential
        # already exists for this (workspace_id, credential_key) -- bootstrap
        # is not yet idempotent against a repeat call for the same
        # Workspace/credential_key (see the Task 17 report's Concerns).
        raise HTTPException(
            409, {"code": "credential_already_active", "message": str(exc)},
        ) from exc
    except connections_service.ConnectionLifecycleError as exc:
        raise HTTPException(_connection_error_status(exc), str(exc)) from exc

    return JSONResponse(
        status_code=201,
        content={
            "connection_id": connection.id,
            "binding_id": binding.id,
            "access_token_version_id": access_token_version_id,
            "app_secret_version_id": app_secret_version_id,
        },
    )


# ---------------------------------------------------------------------------
# Platform-wide connection reads
# ---------------------------------------------------------------------------


@router.get("/meta/connections")
def list_connections(
    after: str | None = Query(
        None, max_length=64,
        description="Continue after this connection id: the last item of the previous page.",
    ),
    limit: int = Query(CONNECTION_LIST_LIMIT, ge=1, le=CONNECTION_LIST_LIMIT),
    _platform: LegacyPlatformContext = Depends(request_platform_context),
) -> dict:
    # Which connections exist and where is the only thing read untenanted;
    # each Workspace's connections are then read in one TenantContext per
    # Meta mode, signed for that Workspace's current epoch (#102).
    if after is not None and _connection_workspace(after) is None:
        raise HTTPException(
            400,
            {"code": "invalid_cursor", "message": f"unknown Meta connection {after!r} in 'after'"},
        )
    listed = _list_connection_ids(after=after, limit=limit)

    groups: dict[tuple[str, str], list[_ListedConnection]] = {}
    for entry in listed:
        groups.setdefault((entry.workspace_id, entry.meta_mode), []).append(entry)

    items_by_id: dict[str, dict] = {}
    for (workspace_id, meta_mode), entries in groups.items():
        context = _platform_tenant_context(
            workspace_id, entries[0].workspace, _platform, meta_mode=meta_mode,
        )
        with tenant_session(context) as session:
            rows = session.scalars(
                select(MetaConnectionRecord).where(
                    MetaConnectionRecord.workspace_id == workspace_id,
                    MetaConnectionRecord.meta_mode == meta_mode,
                    MetaConnectionRecord.id.in_([entry.connection_id for entry in entries]),
                )
            )
            items_by_id.update((row.id, _connection_dict(row)) for row in rows)

    unread = len(listed) - len(items_by_id)
    if unread:
        # Only a Workspace whose epoch moved between the two reads hides its
        # rows from the context signed for the old one.
        logger.warning(
            "platform_meta_connection_list_unread: %d listed connections were not "
            "readable in their Workspace's context",
            unread,
        )
    return {
        "items": [
            items_by_id[entry.connection_id]
            for entry in listed
            if entry.connection_id in items_by_id
        ]
    }


@router.get("/meta/connections/{connection_id}")
def get_connection(
    connection_id: str,
    _platform: LegacyPlatformContext = Depends(request_platform_context),
) -> dict:
    context = _connection_context(connection_id, _platform)
    with tenant_session(context) as session:
        return _connection_dict(_scoped_connection(session, context, connection_id))


@router.post("/meta/connections/{connection_id}/reconcile")
def reconcile_connection(
    connection_id: str,
    _platform: LegacyPlatformContext = Depends(request_platform_context),
) -> dict:
    """Re-derive and report the connection's current projection.

    Scope note (Task 17 brief: "cross-mode reconciliation ... lower
    priority"): the spec gestures at reconciliation across a Workspace's
    ``internal_validation``/``customer_sandbox``/``controlled_mock``
    connections for the same logical integration, but does not define what
    "reconciled" means for that cross-mode comparison, and no such
    comparison service exists yet in this codebase (`app.meta.service.
    reconcile` reconciles the unrelated legacy process-global read-only
    phase). This endpoint scopes down to what is well-defined today: it
    re-reads the connection plus its current active binding (platform-wide,
    by id) and returns that as the reconciled projection, so platform
    operators have one command to re-fetch a connection's ground truth
    without hand-rolling two queries. See the Task 17 report's Concerns for
    what full cross-mode reconciliation would still need.

    Both reads run in one TenantContext signed for the connection's Workspace
    and Meta mode (#102), so the binding can only be that Workspace's.
    """
    context = _connection_context(connection_id, _platform)
    with tenant_session(context) as session:
        connection = _scoped_connection(session, context, connection_id)
        active_binding = session.scalar(
            select(MetaDataSourceBindingRecord).where(
                MetaDataSourceBindingRecord.workspace_id == connection.workspace_id,
                MetaDataSourceBindingRecord.meta_mode == connection.meta_mode,
                MetaDataSourceBindingRecord.meta_connection_id == connection.id,
                MetaDataSourceBindingRecord.state == "active",
            )
        )
        return {
            "connection": _connection_dict(connection),
            "active_binding": _binding_dict(active_binding) if active_binding else None,
        }


# ---------------------------------------------------------------------------
# Credential revocation
# ---------------------------------------------------------------------------


class RevokeCredentialRequest(BaseModel):
    model_config = {"extra": "forbid"}

    reason: str


@router.post("/credentials/{credential_version_id}/revoke")
async def revoke_credential(
    credential_version_id: str,
    body: RevokeCredentialRequest,
    _platform: LegacyPlatformContext = Depends(request_platform_context),
) -> dict:
    # Which Workspace owns the version is the only thing read untenanted;
    # the version itself is read (non-secret columns only) and revoked in a
    # TenantContext signed for that Workspace (#94).
    workspace_id = _credential_version_workspace(credential_version_id)
    scope = _workspace_scope(workspace_id) if workspace_id is not None else None
    if workspace_id is None or scope is None:
        raise HTTPException(404, "credential version not found")

    context = _platform_tenant_context(workspace_id, scope, _platform)

    protector = _get_protector()
    admin = CredentialAdmin(protector)
    with tenant_session(context) as session:
        version = session.execute(
            select(CredentialVersionRecord.credential_key, CredentialVersionRecord.state).where(
                CredentialVersionRecord.id == credential_version_id,
                CredentialVersionRecord.workspace_id == workspace_id,
            )
        ).first()
        if version is None:
            raise HTTPException(404, "credential version not found")
        # Revocation acts on the key's active version: a version that is no
        # longer active must not revoke whichever one replaced it.
        if version.state != "active":
            raise HTTPException(404, f"credential version is not active (state={version.state!r})")
        try:
            admin.revoke(session, context, credential_key=version.credential_key, reason=body.reason)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    return {"ok": True}
