# app/meta/connections.py
"""Meta connection lifecycle service (spec §8.7).

Owns creation, retirement, and binding management for
``MetaConnectionRecord`` / ``MetaDataSourceBindingRecord`` -- the
workspace-scoped replacement for process-global Meta account selection
(spec §1.1).

Route-to-function mapping (spec §8.2), each a separate BFF-allowlisted
command:

* ``POST .../meta/connections``                        -> :func:`create_connection`
* ``POST .../meta/connections/:id/retire``              -> :func:`retire_connection`
* ``POST .../meta/connections/:id/bindings``             -> :func:`create_binding`
* ``POST .../meta/connections/:id/bindings/:bid/supersede`` -> :func:`supersede_binding`

Connection creation does *not* eagerly create a binding: binding creation
is its own allowlisted route/command (spec §8.2), and treating it as a
side effect of connection creation would make it impossible to create a
connection and its first binding as two independently idempotent,
independently retryable commands. Sandbox/mock connections do get a
default ``ScenarioVersionRecord`` at creation time, because the
FakeMetaAdapter has no other way to discover "the" default scenario for a
brand-new connection (spec §3.4) and no separate route creates one.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.adapters.persistence.models import WorkspaceRecord, new_id, utcnow
from app.entitlements.credentials import CredentialVersionRecord
from app.launch.models import LaunchProvisioningRecord
from app.meta.models import (
    MetaConnectionRecord,
    MetaDataSourceBindingRecord,
    MetaObservationSyncScheduleRecord,
    ScenarioVersionRecord,
)
from app.platform.tenancy import TenantContext

SANDBOX_MODES = frozenset({"customer_sandbox", "controlled_mock"})

# LaunchProvisioningRecord.provisioning_state values (app/launch/models.py
# §3.17) that mean the saga has not yet reached a resting point. Retirement
# must wait for these -- see the retirement guard, spec §8.7.
_NON_TERMINAL_PROVISIONING_STATES = frozenset({"pending", "provisioning"})


class ConnectionLifecycleError(RuntimeError):
    """Raised when a connection/binding lifecycle operation is invalid.

    ``code`` is the stable code the interface renders (#253)."""

    default_code = "meta_connection_invalid"

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code or self.default_code


class MetaForbidden(ConnectionLifecycleError):
    """Raised when the caller's role is insufficient for Meta connection management."""

    default_code = "meta_owner_required"


class ConnectionAlreadyExists(ConnectionLifecycleError):
    """Raised when an active connection already exists for (workspace_id, meta_mode)."""

    default_code = "connection_already_exists"


def _require_owner(context: TenantContext) -> None:
    if context.role not in ("owner", "system"):
        raise MetaForbidden(
            f"只有 Owner 可以管理 Meta 连接 (当前角色: {context.role!r})"
        )


def _check_no_active_connection(
    session: Session, *, workspace_id: str, meta_mode: str,
) -> None:
    existing = session.execute(
        select(MetaConnectionRecord).where(
            MetaConnectionRecord.workspace_id == workspace_id,
            MetaConnectionRecord.meta_mode == meta_mode,
            MetaConnectionRecord.state == "active",
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ConnectionAlreadyExists(
            f"active connection already exists for workspace "
            f"{workspace_id!r} mode {meta_mode!r}: {existing.id!r}"
        )


def _sandbox_ad_account_id(workspace_id: str) -> str:
    h = hashlib.sha256(workspace_id.encode()).hexdigest()[:16]
    return f"act_sandbox_{h}"


def _require_meta_mode(context: TenantContext) -> str:
    if not context.meta_mode:
        raise ConnectionLifecycleError(
            "context.meta_mode is required for Meta connection lifecycle operations",
            code="meta_mode_required",
        )
    return context.meta_mode


def _get_connection(
    session: Session, *, context: TenantContext, connection_id: str,
) -> MetaConnectionRecord:
    connection = session.execute(
        select(MetaConnectionRecord).where(
            MetaConnectionRecord.id == connection_id,
            MetaConnectionRecord.workspace_id == context.workspace_id,
            MetaConnectionRecord.meta_mode == context.meta_mode,
        )
    ).scalar_one_or_none()
    if connection is None:
        raise ConnectionLifecycleError(
            f"connection {connection_id!r} not found for workspace "
            f"{context.workspace_id!r} mode {context.meta_mode!r}",
            code="connection_not_found",
        )
    return connection


def _validate_credential_version(
    session: Session, *, workspace_id: str, credential_version_id: str, expected_key: str,
) -> None:
    # Only the non-secret columns: the API role may not read the ciphertext
    # (encrypted_secret, encrypted_data_key, nonce) on PostgreSQL (#94).
    cred = session.execute(
        select(CredentialVersionRecord.credential_key, CredentialVersionRecord.state).where(
            CredentialVersionRecord.id == credential_version_id,
            CredentialVersionRecord.workspace_id == workspace_id,
        )
    ).first()
    if cred is None:
        raise ConnectionLifecycleError(
            f"credential version {credential_version_id!r} not found in workspace "
            f"{workspace_id!r}",
            code="credential_not_found",
        )
    if cred.credential_key != expected_key:
        raise ConnectionLifecycleError(
            f"credential version {credential_version_id!r} has credential_key "
            f"{cred.credential_key!r}, expected {expected_key!r}",
            code="meta_credential_mismatch",
        )
    if cred.state != "active":
        raise ConnectionLifecycleError(
            f"credential version {credential_version_id!r} is not active "
            f"(state={cred.state!r})",
            code="meta_credential_inactive",
        )


def _validate_binding_credentials(
    session: Session,
    *,
    context: TenantContext,
    access_token_version_id: str | None,
    app_secret_version_id: str | None,
) -> None:
    """Mode-appropriate credential validation (spec §2, §3.2 CHECK constraints).

    ``internal_validation`` bindings carry real, active credential version
    refs; ``customer_sandbox``/``controlled_mock`` bindings carry none --
    the FakeMetaAdapter never dereferences credential material.
    """
    meta_mode = context.meta_mode
    if meta_mode == "internal_validation":
        if not access_token_version_id or not app_secret_version_id:
            raise ConnectionLifecycleError(
                "internal_validation bindings require both "
                "access_token_version_id and app_secret_version_id",
                code="meta_binding_credentials_required",
            )
        _validate_credential_version(
            session, workspace_id=context.workspace_id,
            credential_version_id=access_token_version_id,
            expected_key="meta_access_token",
        )
        _validate_credential_version(
            session, workspace_id=context.workspace_id,
            credential_version_id=app_secret_version_id,
            expected_key="meta_app_secret",
        )
    elif meta_mode in SANDBOX_MODES:
        if access_token_version_id is not None or app_secret_version_id is not None:
            raise ConnectionLifecycleError(
                f"{meta_mode} bindings must not carry credential version references",
                code="meta_binding_credentials_forbidden",
            )
    else:
        raise ConnectionLifecycleError(f"unsupported meta_mode: {meta_mode!r}", code="invalid_meta_mode")


def create_connection(
    session: Session,
    *,
    context: TenantContext,
    ad_account_id: str | None = None,
    scenario_config: dict[str, Any] | None = None,
) -> MetaConnectionRecord:
    """Create a ``MetaConnectionRecord`` for ``(context.workspace_id, context.meta_mode)``.

    ``internal_validation`` connections may only be created in the platform
    Workspace (spec §7.3, §8.7) -- enforced here in addition to the deferred
    constraint trigger and RLS policy on the database side (defense in
    depth per ADR 0003: "FastAPI derives tenant and role from verified
    claims and enforces its own policy").

    For ``customer_sandbox``/``controlled_mock`` connections, also creates
    a default ``ScenarioVersionRecord`` so the FakeMetaAdapter has curated
    data to serve immediately. ``internal_validation`` has no scenarios
    (spec §3.4 CHECK constraint). ``scenario_config`` optionally pre-seeds
    that default scenario (e.g. ``controlled_mock`` fault injection rules,
    ADR 0009); it is ignored for ``internal_validation``, which has no
    scenario at all.
    """
    meta_mode = _require_meta_mode(context)
    _require_owner(context)
    _check_no_active_connection(
        session, workspace_id=context.workspace_id, meta_mode=meta_mode,
    )

    if ad_account_id is None:
        if meta_mode in SANDBOX_MODES:
            ad_account_id = _sandbox_ad_account_id(context.workspace_id)
        else:
            raise ConnectionLifecycleError(
                "ad_account_id is required for non-sandbox connections",
                code="meta_ad_account_required",
            )

    if meta_mode == "internal_validation":
        workspace = session.get(WorkspaceRecord, context.workspace_id)
        if workspace is None:
            raise ConnectionLifecycleError(
                f"workspace {context.workspace_id!r} not found",
                code="workspace_not_found",
            )
        if not workspace.is_platform_workspace:
            raise ConnectionLifecycleError(
                "internal_validation connections require a platform workspace "
                f"(workspace {context.workspace_id!r} is not one)",
                code="meta_platform_workspace_required",
            )

    connection = MetaConnectionRecord(
        id=new_id("mcon"),
        workspace_id=context.workspace_id,
        meta_mode=meta_mode,
        ad_account_id=ad_account_id,
        state="active",
    )
    session.add(connection)
    session.flush()

    if meta_mode in SANDBOX_MODES:
        scenario_config = scenario_config or {}
        canonical = json.dumps(
            scenario_config, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        scenario = ScenarioVersionRecord(
            id=new_id("scen"),
            workspace_id=context.workspace_id,
            meta_mode=meta_mode,
            meta_connection_id=connection.id,
            scenario_config=scenario_config,
            scenario_hash=hashlib.sha256(canonical).hexdigest(),
            is_default=True,
        )
        session.add(scenario)
        session.flush()

    return connection


def create_binding(
    session: Session,
    *,
    context: TenantContext,
    connection_id: str,
    ad_account_id: str,
    access_token_version_id: str | None = None,
    app_secret_version_id: str | None = None,
) -> MetaDataSourceBindingRecord:
    """Create a ``MetaDataSourceBindingRecord`` for an existing connection.

    Credential references are validated per mode (spec §2, §3.2). The
    connection need not be ``active`` -- binding creation also happens
    during ``retiring`` (a new binding replacing one marked
    ``supersession_required``, spec §8.7), which is what
    :func:`supersede_binding` uses this for.
    """
    # TODO(#119): Owner role check deferred — create_binding/supersede_binding
    # currently allow any role. Add _require_owner() when binding management
    # surfaces in the UI.
    connection = _get_connection(session, context=context, connection_id=connection_id)

    _validate_binding_credentials(
        session, context=context,
        access_token_version_id=access_token_version_id,
        app_secret_version_id=app_secret_version_id,
    )

    binding = MetaDataSourceBindingRecord(
        id=new_id("mbind"),
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        meta_connection_id=connection.id,
        ad_account_id=ad_account_id,
        access_token_version_id=access_token_version_id,
        app_secret_version_id=app_secret_version_id,
        state="active",
    )
    session.add(binding)
    session.flush()
    return binding


def supersede_binding(
    session: Session,
    *,
    context: TenantContext,
    old_binding_id: str,
    connection_id: str,
    ad_account_id: str,
    access_token_version_id: str | None = None,
    app_secret_version_id: str | None = None,
) -> MetaDataSourceBindingRecord:
    """Atomically replace ``old_binding_id`` with a new active binding (spec §8.7).

    1. Validates the new binding's scope matches the old binding's
       (same connection *and* same ad_account_id -- supersession replaces
       credentials/state for one account, it does not silently reassign
       the binding to a different account).
    2. Transitions the old binding to ``superseded`` with
       ``superseded_by_id`` set, in the same flush/transaction that
       creates the new binding, so there is never a window where the
       workspace lacks an active binding for the scope.
    3. Creates the new binding as ``active`` with mode-appropriate,
       verified-active credential versions.

    The old binding is flushed to ``superseded`` *before* the new binding
    is inserted: both rows can share the same
    ``(workspace_id, meta_mode, meta_connection_id, ad_account_id)``
    tuple, which is protected by a partial unique index on
    ``state = 'active'``. Inserting the new active row first would
    collide with the still-active old row; retiring the old row first
    frees the slot within the same transaction, so no window opens where
    neither binding is active.
    """
    # TODO(#119): Owner role check deferred — create_binding/supersede_binding
    # currently allow any role. Add _require_owner() when binding management
    # surfaces in the UI.
    old_binding = session.execute(
        select(MetaDataSourceBindingRecord).where(
            MetaDataSourceBindingRecord.id == old_binding_id,
            MetaDataSourceBindingRecord.workspace_id == context.workspace_id,
            MetaDataSourceBindingRecord.meta_mode == context.meta_mode,
        )
    ).scalar_one_or_none()
    if old_binding is None:
        raise ConnectionLifecycleError(f"binding {old_binding_id!r} not found", code="meta_binding_not_found")
    if old_binding.state not in ("active", "supersession_required"):
        raise ConnectionLifecycleError(
            f"binding {old_binding_id!r} cannot be superseded from state "
            f"{old_binding.state!r}",
            code="meta_binding_state_invalid",
        )
    if old_binding.meta_connection_id != connection_id:
        raise ConnectionLifecycleError(
            "supersede scope mismatch: old binding "
            f"{old_binding_id!r} belongs to connection "
            f"{old_binding.meta_connection_id!r}, not {connection_id!r}",
            code="meta_binding_scope_mismatch",
        )
    if old_binding.ad_account_id != ad_account_id:
        raise ConnectionLifecycleError(
            f"ad_account_id mismatch: old binding covers {old_binding.ad_account_id}, "
            f"not {ad_account_id}",
            code="meta_ad_account_mismatch",
        )

    new_binding_id = new_id("mbind")
    old_binding.state = "superseded"
    old_binding.superseded_at = utcnow()
    old_binding.superseded_by_id = new_binding_id
    session.flush()

    connection = _get_connection(session, context=context, connection_id=connection_id)

    _validate_binding_credentials(
        session, context=context,
        access_token_version_id=access_token_version_id,
        app_secret_version_id=app_secret_version_id,
    )

    new_binding = MetaDataSourceBindingRecord(
        id=new_binding_id,
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        meta_connection_id=connection.id,
        ad_account_id=ad_account_id,
        access_token_version_id=access_token_version_id,
        app_secret_version_id=app_secret_version_id,
        state="active",
    )
    session.add(new_binding)
    session.flush()
    return new_binding


def retire_connection(
    session: Session,
    *,
    context: TenantContext,
    connection_id: str,
) -> MetaConnectionRecord:
    """Retire a connection (spec §3.1 state machine, §8.7 retirement guard).

    ``active -> retired`` directly when no non-terminal
    ``LaunchProvisioningRecord`` rows reference the connection.

    ``active -> retiring`` when non-terminal provisionings exist: active
    bindings are marked ``supersession_required`` and active sync
    schedules are paused (not deleted). A background reconciler -- outside
    this service's scope -- transitions ``retiring -> retired`` once every
    provisioning reaches a terminal state.
    """
    connection = _get_connection(session, context=context, connection_id=connection_id)
    _require_owner(context)
    if connection.state != "active":
        raise ConnectionLifecycleError(
            f"connection {connection_id!r} cannot be retired from state "
            f"{connection.state!r}",
            code="meta_connection_state_invalid",
        )

    has_non_terminal_provisioning = session.execute(
        select(LaunchProvisioningRecord.id)
        .where(
            LaunchProvisioningRecord.workspace_id == context.workspace_id,
            LaunchProvisioningRecord.meta_mode == context.meta_mode,
            LaunchProvisioningRecord.meta_connection_id == connection_id,
            LaunchProvisioningRecord.provisioning_state.in_(
                _NON_TERMINAL_PROVISIONING_STATES
            ),
        )
        .limit(1)
    ).first() is not None

    if has_non_terminal_provisioning:
        connection.state = "retiring"
        session.execute(
            update(MetaDataSourceBindingRecord)
            .where(
                MetaDataSourceBindingRecord.workspace_id == context.workspace_id,
                MetaDataSourceBindingRecord.meta_mode == context.meta_mode,
                MetaDataSourceBindingRecord.meta_connection_id == connection_id,
                MetaDataSourceBindingRecord.state == "active",
            )
            .values(state="supersession_required")
        )
        session.execute(
            update(MetaObservationSyncScheduleRecord)
            .where(
                MetaObservationSyncScheduleRecord.workspace_id == context.workspace_id,
                MetaObservationSyncScheduleRecord.meta_mode == context.meta_mode,
                MetaObservationSyncScheduleRecord.meta_connection_id == connection_id,
                MetaObservationSyncScheduleRecord.state == "active",
            )
            .values(state="paused")
        )
    else:
        connection.state = "retired"
        connection.retired_at = utcnow()

    connection.updated_at = utcnow()
    session.flush()
    return connection
