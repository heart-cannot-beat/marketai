"""TenantContext: the explicit tenant and actor scope for one unit of work.

`DEFAULT_WORKSPACE_ID` is deployment configuration, not a tenant boundary. It
makes every query in the process read the same Workspace, which is
indistinguishable from correct behaviour in a single-tenant deployment and
silently wrong in a multi-tenant one. This module replaces it with a scope that
a caller must supply and that the database itself can enforce.

Two enforcement layers sit on the same context:

* PostgreSQL, the hosted path, applies Row Level Security from transaction-local
  settings written by `apply_to_session`. The policies are the authority.
* SQLite, the bounded local/import path, has no RLS. The context is still
  required and still carries the Workspace, so repository code is written the
  same way; what changes is only who enforces it.

Every setting is written with `SET LOCAL`, which PostgreSQL discards at COMMIT
or ROLLBACK. A connection therefore cannot return to the pool still carrying a
previous request's Workspace, which is the failure mode a session-level `SET`
would introduce.
"""

from __future__ import annotations

import hashlib
import hmac
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import Enum
from typing import Iterator, Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

# Namespaced so a setting cannot collide with a PostgreSQL GUC.
SETTING_PREFIX = "marketai"

WORKSPACE_SETTING = f"{SETTING_PREFIX}.workspace_id"
INITIATOR_SETTING = f"{SETTING_PREFIX}.initiator_id"
EXECUTOR_SETTING = f"{SETTING_PREFIX}.executor"
ROLE_SETTING = f"{SETTING_PREFIX}.role"
EPOCH_SETTING = f"{SETTING_PREFIX}.authorization_epoch"
SIGNATURE_SETTING = f"{SETTING_PREFIX}.context_signature"
META_MODE_SETTING = f"{SETTING_PREFIX}.meta_mode"
IDENTITY_KIND_SETTING = f"{SETTING_PREFIX}.identity_kind"
IS_PLATFORM_WORKSPACE_SETTING = f"{SETTING_PREFIX}.is_platform_workspace"

VALID_META_MODES = frozenset({"customer_sandbox", "controlled_mock", "internal_validation"})

#: Interface languages the control plane offers (#252). The BFF names one per
#: request in ``X-UI-Language`` (#255). It only chooses the language of text
#: MarketAI writes for that request -- export notes, column labels -- and takes
#: no part in tenancy, authorization, RLS or idempotency.
UI_LANGUAGES = ("en", "zh-CN")
DEFAULT_UI_LANGUAGE = "en"


def ui_language_from_header(value: str | None) -> str:
    """A missing or unknown language reads as English, never as an error."""
    return value if value in UI_LANGUAGES else DEFAULT_UI_LANGUAGE

ALL_SETTINGS = (
    WORKSPACE_SETTING,
    INITIATOR_SETTING,
    EXECUTOR_SETTING,
    ROLE_SETTING,
    EPOCH_SETTING,
    SIGNATURE_SETTING,
    META_MODE_SETTING,
    IDENTITY_KIND_SETTING,
    IS_PLATFORM_WORKSPACE_SETTING,
)

Role = Literal["owner", "member", "system"]
InitiatorRole = Literal["owner", "member"]


class IdentityKind(str, Enum):
    """The discriminant behind ``role``: who -- or what -- is acting.

    ``role`` alone cannot tell a delegated user from a platform operator: both
    can appear as ``role="system"`` once execution is handed off. This adds
    the missing axis. It is enforced only when a caller names it explicitly
    (see ``TenantContext.__post_init__``); call sites that predate this
    discriminant keep constructing contexts exactly as before.
    """

    DELEGATED_USER = "delegated_user"
    PLATFORM_OPERATOR = "platform_operator"
    SYSTEM = "system"

#: The Worker and migrations act as themselves while preserving the human who
#: asked for the work. An audit record carries both.
SYSTEM_WORKER = "system:worker"
SYSTEM_MIGRATION = "system:migration"
USER_EXECUTOR = "user"


class TenantScopeError(RuntimeError):
    """Raised when domain work is attempted without an explicit tenant scope."""


@dataclass(frozen=True)
class TenantContext:
    """Who is acting, for which Workspace, under what authority.

    ``initiator_id`` is the human whose command produced the work. It survives
    into background execution: when the Worker runs a job it is the *executor*,
    and attributing the row to the Worker would erase the person who asked. Both
    are recorded, never collapsed into one.

    ``authorization_epoch`` is the control plane's view of the Workspace's
    authorization state when the request was admitted. A delegated token issued
    before a membership or entitlement change carries a stale epoch. PostgreSQL
    RLS compares it with the domain Workspace projection on every protected
    access; the bounded SQLite path performs the same comparison when opening a
    tenant session. A mismatch therefore fails closed before domain work runs.

    ``ui_language`` is the requester's interface language. It is a display
    preference, not identity: the Worker does not inherit it from the Job row,
    so a command that writes text for later freezes the language it used.
    """

    workspace_id: str
    role: Role
    initiator_id: str | None = None
    initiator_role: InitiatorRole | None = None
    executor: str = USER_EXECUTOR
    request_id: str = ""
    trace_id: str = ""
    authorization_epoch: int = 0
    meta_mode: str | None = None
    identity_kind: IdentityKind | None = None
    is_platform_workspace: bool = False
    ui_language: str = DEFAULT_UI_LANGUAGE

    def __post_init__(self) -> None:
        if not self.workspace_id:
            raise TenantScopeError("TenantContext requires a workspace_id")
        if "\n" in self.workspace_id:
            raise TenantScopeError(
                "workspace_id must not contain newlines"
            )
        if self.role not in ("owner", "member", "system"):
            raise TenantScopeError(f"unsupported role: {self.role!r}")
        if self.initiator_role not in (None, "owner", "member"):
            raise TenantScopeError(
                f"unsupported initiator role: {self.initiator_role!r}"
            )

        # IdentityKind shape enforcement. A caller that names identity_kind
        # explicitly is asserting the shape it requires and is held to it. A
        # caller that omits it -- every call site that predates this
        # discriminant -- gets a default inferred from role and is left
        # exactly as validated before; the checks below run only when the
        # caller opted in.
        identity_kind_given = self.identity_kind is not None
        if not identity_kind_given:
            inferred_kind = (
                IdentityKind.SYSTEM if self.role == "system" else IdentityKind.DELEGATED_USER
            )
            object.__setattr__(self, "identity_kind", inferred_kind)
        else:
            if self.identity_kind == IdentityKind.DELEGATED_USER:
                if self.role not in ("owner", "member"):
                    raise TenantScopeError(
                        "identity_kind=delegated_user requires role in {owner, member}"
                    )
                if self.executor != USER_EXECUTOR:
                    raise TenantScopeError(
                        "identity_kind=delegated_user requires executor='user'"
                    )
            elif self.identity_kind in (IdentityKind.PLATFORM_OPERATOR, IdentityKind.SYSTEM):
                if self.role != "system":
                    raise TenantScopeError(
                        f"identity_kind={self.identity_kind.value} requires role='system'"
                    )
                if not self.executor.startswith("system:"):
                    raise TenantScopeError(
                        f"identity_kind={self.identity_kind.value} requires a 'system:' executor"
                    )

        if self.executor == USER_EXECUTOR and not self.initiator_id:
            raise TenantScopeError(
                "a user-executed context requires the initiating user"
            )
        if self.executor == USER_EXECUTOR and self.role == "system":
            raise TenantScopeError("a user executor cannot have the system role")
        if self.executor == USER_EXECUTOR and self.initiator_role not in (None, self.role):
            raise TenantScopeError(
                "a direct request's initiator role must match its execution role"
            )
        if self.meta_mode is not None and self.meta_mode not in VALID_META_MODES:
            raise TenantScopeError(f"unsupported meta_mode: {self.meta_mode!r}")
        if self.ui_language not in UI_LANGUAGES:
            raise TenantScopeError(f"unsupported ui_language: {self.ui_language!r}")

    @property
    def is_system(self) -> bool:
        return self.executor != USER_EXECUTOR

    def for_system_execution(self, executor: str = SYSTEM_WORKER) -> "TenantContext":
        """Derive the Worker's context from the request that queued the work.

        The Workspace and the human initiator are carried across unchanged; only
        the executor and role change. This is what lets a resumed job be
        attributed to the person who submitted it after a restart.
        """
        if not executor.startswith("system:"):
            raise TenantScopeError("a system executor must be namespaced 'system:'")

        frozen_role = self.audit_initiator_role
        return replace(
            self,
            executor=executor,
            role="system",
            initiator_role=frozen_role if frozen_role in ("owner", "member") else None,
            identity_kind=IdentityKind.SYSTEM,
        )

    @property
    def audit_initiator_role(self) -> str:
        """The admitted human role, distinct from the current system executor."""
        if self.initiator_role is not None:
            return self.initiator_role
        if self.role in ("owner", "member"):
            return self.role
        return "system"

    def audit_actors(self) -> dict[str, str | None]:
        """The dual-actor attribution every audit record carries."""
        return {
            "workspace_id": self.workspace_id,
            "initiator_id": self.initiator_id,
            "initiator_role": self.audit_initiator_role,
            "executor": self.executor,
            "request_id": self.request_id or None,
            "trace_id": self.trace_id or None,
        }


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex}"


def system_context(
    workspace_id: str,
    *,
    initiator_id: str | None,
    initiator_role: InitiatorRole | None = None,
    executor: str = SYSTEM_WORKER,
    request_id: str = "",
    trace_id: str = "",
    authorization_epoch: int = 0,
) -> TenantContext:
    """A context for background execution that preserves the human initiator."""
    return TenantContext(
        workspace_id=workspace_id,
        role="system",
        initiator_id=initiator_id,
        initiator_role=initiator_role,
        executor=executor,
        request_id=request_id,
        trace_id=trace_id,
        authorization_epoch=authorization_epoch,
        identity_kind=IdentityKind.SYSTEM,
    )


def is_postgres(session: Session) -> bool:
    bind = session.get_bind()
    return bool(bind is not None and bind.dialect.name == "postgresql")


def _write_settings(session: Session, values: dict[str, str]) -> None:
    """Write transaction-local settings, or do nothing off PostgreSQL."""
    if not is_postgres(session):
        return

    # One round-trip for all settings. The setting names are module constants
    # (not input), so they are safe to interpolate; the values are bound.
    items = list(values.items())
    columns = ", ".join(
        f"set_config('{setting}', :v{i}, true)" for i, (setting, _) in enumerate(items)
    )
    params = {f"v{i}": value for i, (_, value) in enumerate(items)}
    session.execute(text(f"SELECT {columns}"), params)


def tenant_context_signature(context: TenantContext, secret: str) -> str:
    """Sign the values the RLS policy treats as tenant authority.

    ``meta_mode`` is appended only when set, and ``is_platform_workspace``
    only when true, so ordinary contexts keep producing the same signature
    they always have.
    """
    if len(secret.encode("utf-8")) < 32:
        raise TenantScopeError(
            "MARKETAI_TENANT_CONTEXT_SECRET must be at least 32 bytes"
        )
    payload_parts = [
        "marketai-tenant-context-v1",
        context.workspace_id,
        str(context.authorization_epoch),
    ]
    if context.meta_mode is not None:
        payload_parts.append(context.meta_mode)
    if context.is_platform_workspace:
        payload_parts.append(str(context.is_platform_workspace).lower())
    payload = "\n".join(payload_parts).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _settings_for(context: TenantContext, signature: str = "") -> dict[str, str]:
    return {
        WORKSPACE_SETTING: context.workspace_id,
        INITIATOR_SETTING: context.initiator_id or "",
        EXECUTOR_SETTING: context.executor,
        ROLE_SETTING: context.role,
        EPOCH_SETTING: str(context.authorization_epoch),
        SIGNATURE_SETTING: signature,
        META_MODE_SETTING: context.meta_mode or "",
        IDENTITY_KIND_SETTING: context.identity_kind.value if context.identity_kind else "",
        IS_PLATFORM_WORKSPACE_SETTING: str(context.is_platform_workspace).lower(),
    }


def apply_to_session(session: Session, context: TenantContext) -> None:
    """Bind the context to the session, and to the open transaction on PostgreSQL.

    `SET LOCAL` is scoped to the transaction, so the values vanish on COMMIT or
    ROLLBACK and cannot leak to whoever checks the connection out next.
    """
    signature = ""
    if is_postgres(session):
        from .. import config

        signature = tenant_context_signature(context, config.TENANT_CONTEXT_SECRET)
    session.info["tenant_context"] = context
    _write_settings(session, _settings_for(context, signature))


def commit_and_rebind(session: Session) -> None:
    """Commit, then bind the session's context to the transaction that follows.

    A service that commits part-way through a `tenant_session` ends the
    transaction its `SET LOCAL` settings lived in. Without re-binding, every
    later statement runs unscoped on PostgreSQL -- RLS hides the rows a
    runtime role just wrote and rejects its writes -- while `current_context()`
    still reports the Workspace. This restores the same context, as the Launch
    saga's durable commit boundary does. For a session with no bound context
    it is a plain commit.
    """
    context = session.info.get("tenant_context")
    session.commit()
    if context is not None:
        session.begin()
        apply_to_session(session, context)


def current_context(session: Session) -> TenantContext:
    """The context bound to this session, or raise.

    Repository code calls this instead of reaching for configuration, so a query
    written without a scope fails loudly at the seam rather than quietly
    returning another Workspace's rows.
    """
    context = session.info.get("tenant_context")

    if context is None:
        raise TenantScopeError(
            "no TenantContext is bound to this session; open it with tenant_session()"
        )

    return context


def clear_session_context(session: Session) -> None:
    """Drop the scope from the session *and* from the open transaction.

    Clearing only the Python side would leave PostgreSQL still filtering by the
    Workspace that was last written: the process would believe it is unscoped
    while the database keeps answering for a tenant. Emptying the settings makes
    both sides agree, and an empty setting is NULL to
    ``marketai_current_workspace()``, so the policies fail closed.
    """
    session.info.pop("tenant_context", None)
    _write_settings(session, {setting: "" for setting in ALL_SETTINGS})


@contextmanager
def tenant_scope(session: Session, context: TenantContext) -> Iterator[Session]:
    """Bind a context to an existing session for the duration of the block.

    Two rules, both about keeping the process and the database on the same
    Workspace:

    * Restoring on exit rewrites the outer scope to PostgreSQL, not just to
      ``session.info``. Restoring only the Python side would leave the code
      believing it is back in the outer Workspace while RLS still filters by the
      inner one -- the query returns another tenant's rows and nothing raises.
    * Nesting a *different* Workspace is refused. The settings are
      transaction-local, so a nested scope necessarily runs inside the outer
      transaction: a single transaction reading two tenants is not something the
      domain should be able to express, and work that spans Workspaces belongs in
      one transaction each. Escalating executor or role within the same Workspace
      -- a request handing off to the Worker's context -- is what this is for, and
      that stays allowed.
    """
    previous = session.info.get("tenant_context")

    if previous is not None and previous.workspace_id != context.workspace_id:
        raise TenantScopeError(
            "cannot nest a second Workspace inside an open tenant scope: "
            f"{previous.workspace_id!r} is already bound and {context.workspace_id!r} "
            "was requested; open a separate transaction per Workspace"
        )

    apply_to_session(session, context)

    try:
        yield session
    finally:
        _leave_scope(session, previous)


def _leave_scope(session: Session, previous: TenantContext | None) -> None:
    """Return to ``previous`` without ever masking the error that got us here.

    Leaving a scope writes SQL, and the ordinary reason a block exits early is
    that one of its statements failed. PostgreSQL then marks the transaction
    aborted and refuses everything until ROLLBACK -- ``set_config`` included --
    so a naive restore raises ``InFailedSqlTransaction`` from the ``finally``
    and the caller sees that instead of the query that actually broke.

    When the restore cannot be written the settings are moot: the only legal
    next statement is ROLLBACK, and that discards every transaction-local
    setting. What must not survive is the process believing it holds a scope it
    could not write, so the binding is dropped and :func:`current_context`
    raises until a new scope is opened.
    """
    failing = sys.exc_info()[1]

    try:
        if previous is None:
            clear_session_context(session)
        else:
            apply_to_session(session, previous)
    except Exception as restore_error:
        session.info.pop("tenant_context", None)

        if failing is None:
            # Nothing was propagating, so this is the only news there is.
            raise

        failing.add_note(
            f"the tenant scope was not restored ({restore_error!r}); the session "
            "is left unscoped and its transaction must be rolled back"
        )
