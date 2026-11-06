"""创意库核心命令：create/edit/validate/publish/withdraw/archive/bind/fork/clone。

契约 §3 三层模型、§3.3 并发编辑、§3.4 发布事务、§4 角色、§12 复用命令、
§14 影响分析与 Withdraw、§16 审计。

租户边界
--------
所有查询通过 ``TenantContext.workspace_id`` + PostgreSQL RLS 双层作用域。
``tenant_session(context)`` 在事务开始时设置 ``SET LOCAL``，RLS 策略通过
``marketai_current_workspace()`` 在数据库层强制隔离。应用层的 workspace_id
过滤作为 SQLite 兼容路径保留。

审计采用双 Actor 归因（``initiator_id`` + ``initiator_role`` + ``executor``），
匹配 #16 平台审计模式。
"""
from __future__ import annotations

import hashlib as _hashlib
import json as _json
from datetime import date
from pathlib import Path
from typing import Iterable

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError as _IntegrityError

from app.platform.audit import redact as _redact
from app.platform.tenancy import TenantContext

from ..adapters.persistence.database import tenant_session
from ..adapters.persistence.models import (
    LibraryAuditRecord,
    LibraryDependencyRecord,
    LibraryDraftRecord,
    LibraryEntryRecord,
    LibraryEventRecord,
    LibraryIdempotencyRecord,
    LibraryVersionRecord,
    ProductRecord,
    ReferenceBindingRecord,
    ReferenceObjectRecord,
    WorkspaceRecord,
    new_id,
    utcnow,
)
from . import command_impacts, command_outcomes, references
from .contracts import (
    CLONE_PRODUCT_NEVER_COPIED,
    CLONE_PRODUCT_OPTIONAL_KEYS,
    CLONE_PRODUCT_SAFE_KEYS,
    CONTENT_SCHEMA_VERSIONS,
    check_rights_eligible,
    COMMAND_CLONE,
    COMMAND_CREATE,
    COMMAND_EDIT,
    COMMAND_FORK,
    DRAFT_EDITING,
    DRAFT_READY,
    DRAFT_VALIDATION_FAILED,
    ELIGIBILITY_BLOCKED,
    ELIGIBILITY_ELIGIBLE,
    ELIGIBILITY_WARNING,
    ENTRY_DIMENSION_SCHEMA,
    ENTRY_PERSON,
    ENTRY_PRODUCT,
    ENTRY_REFERENCE_ASSET,
    ENTRY_REFERENCE_GROUP,
    ENTRY_TYPES,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_ARCHIVED,
    OWNER_ONLY_PUBLISH,
    REL_SCHEMA_PRODUCT,
    RELATION_TARGET_TYPES,
    SEVERITY_WARNING,
    VERSION_EFFECTIVE,
    VERSION_SUPERSEDED,
    VERSION_WITHDRAWN,
    WITHDRAWAL_DOWNSTREAM_EVENT_TYPES,
    DependencyEdge,
    Finding,
    ValidationReport,
    diff_content,
    extract_dependencies,
    extract_reference_bindings,
    validate_content,
)


def size_params(limit_bytes: int) -> dict:
    """``file_too_large`` copy reads the limit in MB."""
    return {"limit_mb": max(1, round(limit_bytes / (1024 * 1024)))}


class LibraryError(RuntimeError):
    """创意库命令失败。

    ``code`` is the stable ``library_<reason>`` the interface renders
    (``error-<code>``, #253); the message is a diagnostic for logs and audit.
    ``params`` carries the values the rendered copy interpolates.
    """

    default_code = "library_invalid"

    def __init__(self, message: str = "", *, code: str | None = None, params: dict | None = None) -> None:
        super().__init__(message)
        self.code = code or self.default_code
        self.params = params or {}


class LibraryNotFound(LibraryError):
    default_code = "library_not_found"


class LibraryConflict(LibraryError):
    """revision / 状态冲突。附带字段级差异供用户重新加载或合并。"""

    default_code = "library_conflict"

    def __init__(
        self, message: str, detail: dict | None = None, *,
        code: str | None = None, params: dict | None = None,
    ) -> None:
        detail = detail or {}
        super().__init__(message, code=code or detail.get("code"), params=params)
        self.detail = detail


class IdempotencyConflict(LibraryConflict):
    """Idempotency key reuse with mismatched command, actor, or fingerprint."""

    default_code = "idempotency_key_reused"


class RevisionConflict(LibraryConflict):
    """Expected revision does not match current — caller must reload."""

    default_code = "revision_conflict"


class UploadTooLarge(LibraryError):
    """A streamed reference upload exceeded the configured byte limit."""

    default_code = "file_too_large"

    def __init__(self, message: str, *, limit: int) -> None:
        super().__init__(message, params=size_params(limit))
        self.limit = limit


class LibraryForbidden(LibraryError):
    """角色或租户边界拒绝。"""

    default_code = "library_forbidden"


class LibraryValidationError(LibraryError):
    default_code = "library_validation_failed"

    def __init__(self, message: str, report: dict, *, code: str | None = None) -> None:
        super().__init__(message, code=code)
        self.report = report


# --------------------------------------------------------------------------
# Idempotency (§17) — session-internal, atomic with the command transaction
# --------------------------------------------------------------------------


def _fingerprint(payload: dict) -> str:
    """SHA-256 of canonicalized JSON — binds a key to one request shape."""
    canonical = _json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return _hashlib.sha256(canonical.encode()).hexdigest()


def compute_fingerprint(payload: dict) -> str:
    """Public entry point for request fingerprinting."""
    return _fingerprint(payload)


def reject_oversized_upload(
    context: TenantContext, *, reason: str, idempotency_key: str | None = None,
    filename: str = "", declared_mime: str = "", byte_size: int = 0,
    content_sha256: str = "", limit: int, request_fingerprint: str,
    command_identity: str | None = None,
) -> dict:
    """Persist one oversized-upload rejection through the normal idempotency protocol."""

    def body(session, key_hash):
        _require_workspace(session, context.workspace_id)
        _audit(
            session,
            context,
            "upload_reference",
            result="rejected",
            detail={
                "reason": reason,
                "filename": Path(filename or "").name,
                "declared_mime": (declared_mime or "").strip().lower(),
                "byte_size": byte_size,
                "content_sha256": content_sha256,
                **_idem_detail(key_hash),
            },
        )
        return _TerminalFailure(UploadTooLarge(reason, limit=limit))

    return _run_with_idempotency(
        context,
        idempotency_key,
        "upload_reference",
        request_fingerprint,
        body,
        command_identity=command_identity,
    )


def _idempotency_record(session, context: TenantContext, key: str):
    return session.scalars(
        select(LibraryIdempotencyRecord).where(
            LibraryIdempotencyRecord.workspace_id == context.workspace_id,
            LibraryIdempotencyRecord.idempotency_key == key,
        )
    ).one_or_none()


def _idempotency_record_by_identity(
    session, context: TenantContext, command_identity_hash: str,
):
    return session.scalars(
        select(LibraryIdempotencyRecord).where(
            LibraryIdempotencyRecord.workspace_id == context.workspace_id,
            LibraryIdempotencyRecord.command_identity_hash == command_identity_hash,
        )
    ).one_or_none()


def _validate_idempotency_binding(
    record: LibraryIdempotencyRecord,
    context: TenantContext,
    command: str,
    fingerprint: str,
    command_identity_hash: str | None = None,
    idempotency_key: str | None = None,
) -> None:
    """A key is immutable across command, actor and normalized request payload."""
    if record.command != command:
        raise IdempotencyConflict(
            f"Idempotency-Key 已被 {record.command!r} 使用",
            {"code": "idempotency_key_reused", "existing_command": record.command, "attempted_command": command},
        )
    if record.initiator_id != (context.initiator_id or ""):
        raise IdempotencyConflict(
            "Idempotency-Key 不属于当前用户",
            {"code": "idempotency_key_reused"},
        )
    if record.request_fingerprint != fingerprint:
        raise IdempotencyConflict(
            "请求内容与之前使用此 Key 的请求不同",
            {"code": "idempotency_key_reused"},
        )
    if idempotency_key is not None and record.idempotency_key != idempotency_key:
        raise IdempotencyConflict(
            "Command identity 已绑定到另一个幂等 Key",
            {"code": "idempotency_key_reused"},
        )
    # Gate 0.2 backfilled records predate browser command identity. Preserve
    # their legacy Idempotency-Key replay without pretending their placeholder
    # hash is a recoverable browser identity.
    if (
        command_identity_hash is not None
        and record.contract_schema_version >= command_outcomes.COMMAND_OUTCOME_CONTRACT_VERSION
        and record.command_identity_hash != command_identity_hash
    ):
        raise IdempotencyConflict(
            "Idempotency-Key 已绑定到另一个 Command identity",
            {"code": "idempotency_key_reused"},
        )


def _check_idempotency(
    session, context: TenantContext, key: str, command: str, fingerprint: str,
    command_identity_hash: str,
) -> dict | None:
    """Lock a durable reservation and replay it, or claim its pending body.

    Returns cached response dict or None.  Raises ``IdempotencyConflict`` when the
    key exists but command, actor, or request fingerprint do not match.
    Re-raises terminal failures stored in earlier attempts.

    The no-op UPDATE is intentional: PostgreSQL locks the matching row and SQLite
    acquires its write lock before reading ``response_json``.  A concurrent first
    use therefore cannot observe ``pending`` and execute the body twice on either
    supported database.
    """
    session.execute(
        update(LibraryIdempotencyRecord)
        .where(
            LibraryIdempotencyRecord.workspace_id == context.workspace_id,
            LibraryIdempotencyRecord.idempotency_key == key,
        )
        .values(created_at=LibraryIdempotencyRecord.created_at)
    )
    record = _idempotency_record(session, context, key)
    if record is None:
        return None
    _validate_idempotency_binding(
        record, context, command, fingerprint,
        command_identity_hash=command_identity_hash,
        idempotency_key=key,
    )
    cached = dict(record.response_json)
    if not cached or cached.get("_state") == "pending":
        return None
    if record.response_kind == "not_applied" or cached.get("_state") == "not_applied":
        raise LibraryConflict(
            "该命令已被权威确认未执行；请创建新的用户操作",
            {"code": "command_not_applied"},
        )
    terminal = cached.pop("_terminal", None)
    if terminal is None:
        return cached
    message = cached.get("_message", "")
    coded = {"code": cached.get("_code"), "params": cached.get("_params")}
    if terminal == "not_found":
        raise LibraryNotFound(message, **coded)
    elif terminal == "revision_conflict":
        raise RevisionConflict(message, cached.get("_detail", {}), **coded)
    elif terminal == "conflict":
        raise LibraryConflict(message, cached.get("_detail", {}), **coded)
    elif terminal == "validation":
        raise LibraryValidationError(message, cached.get("_report", {}), code=coded["code"])
    elif terminal == "forbidden":
        raise LibraryForbidden(message, **coded)
    elif terminal == "gate_error":
        raise references.ReferenceGateError(message, **coded)
    elif terminal == "oversized":
        raise UploadTooLarge(
            cached.get("_message", "File too large"),
            limit=int(cached.get("_limit") or 0),
        )
    elif terminal == "invalid":
        raise LibraryError(message, **coded)
    return cached


def _reserve_idempotency(
    session, context: TenantContext, key: str, command: str, fingerprint: str,
    command_identity_hash: str,
) -> None:
    """Claim the idempotency key before executing the command body."""
    session.add(LibraryIdempotencyRecord(
        id=new_id("idem"),
        workspace_id=context.workspace_id,
        idempotency_key=key,
        command=command,
        initiator_id=context.initiator_id or "",
        request_fingerprint=fingerprint,
        response_json={"_state": "pending"},
        contract_schema_version=command_outcomes.COMMAND_OUTCOME_CONTRACT_VERSION,
        command_identity_hash=command_identity_hash,
        lifecycle_state="pending",
        response_kind="pending",
        updated_at=utcnow(),
    ))


def _finalize_idempotency(
    session, context: TenantContext, key: str, response: dict
) -> None:
    """Update the reserved record with the actual response after body execution."""
    record = session.scalars(
        select(LibraryIdempotencyRecord).where(
            LibraryIdempotencyRecord.workspace_id == context.workspace_id,
            LibraryIdempotencyRecord.idempotency_key == key,
        )
    ).one()
    record.response_json = response
    record.lifecycle_state = "terminal"
    record.response_kind = "problem" if response.get("_terminal") else "success"
    record.updated_at = utcnow()


def _ensure_idempotency_reservation(
    context: TenantContext,
    key: str,
    command: str,
    fingerprint: str,
    command_identity_hash: str,
) -> None:
    """Commit the key claim before command code can produce external side effects."""
    try:
        with tenant_session(context) as session:
            _require_workspace(session, context.workspace_id)
            key_record = _idempotency_record(session, context, key)
            identity_record = _idempotency_record_by_identity(
                session, context, command_identity_hash,
            )
            if (
                key_record is not None
                and identity_record is not None
                and key_record.id != identity_record.id
            ):
                raise IdempotencyConflict(
                    "Idempotency-Key 与 Command identity 指向不同命令",
                    {"code": "idempotency_key_reused"},
                )
            record = key_record or identity_record
            if record is not None:
                _validate_idempotency_binding(
                    record, context, command, fingerprint,
                    command_identity_hash=command_identity_hash,
                    idempotency_key=key,
                )
                return
            _reserve_idempotency(
                session, context, key, command, fingerprint, command_identity_hash,
            )
            session.flush()
    except _IntegrityError as insert_error:
        # Only the unique-key race is recoverable.  Re-read and validate the
        # winner; if no winner exists, preserve the unrelated IntegrityError.
        with tenant_session(context) as session:
            winner = _idempotency_record(session, context, key)
            if winner is None:
                winner = _idempotency_record_by_identity(
                    session, context, command_identity_hash,
                )
            if winner is None:
                raise insert_error
            _validate_idempotency_binding(
                winner, context, command, fingerprint,
                command_identity_hash=command_identity_hash,
                idempotency_key=key,
            )


def _idem_detail(key_hash: str | None) -> dict:
    return {"idempotency_key_sha256": key_hash} if key_hash else {}


class _TerminalFailure:
    """Signals a terminal command failure whose evidence must be committed and whose
    result must be recorded idempotently (blocked publish, forbidden, etc.)."""
    __slots__ = ("error", "response")

    def __init__(self, error: LibraryError, response: dict | None = None) -> None:
        self.error = error
        self.response = response if response is not None else _terminal_response(error)


def _terminal_response(error: Exception) -> dict:
    """Serialize a deterministic domain failure for exact replay.

    ``_code``/``_params`` replay the same stable code (#253); records written
    before they existed replay the class's default code."""
    response = _terminal_kind(error)
    code = getattr(error, "code", None)
    if code:
        response["_code"] = code
    params = getattr(error, "params", None)
    if params:
        response["_params"] = params
    return response


def _terminal_kind(error: Exception) -> dict:
    if isinstance(error, UploadTooLarge):
        return {
            "_terminal": "oversized",
            "_message": str(error),
            "_limit": error.limit,
        }
    if isinstance(error, LibraryNotFound):
        return {"_terminal": "not_found", "_message": str(error)}
    if isinstance(error, RevisionConflict):
        return {
            "_terminal": "revision_conflict",
            "_message": str(error),
            "_detail": error.detail,
        }
    if isinstance(error, LibraryConflict) and not isinstance(error, IdempotencyConflict):
        return {
            "_terminal": "conflict",
            "_message": str(error),
            "_detail": error.detail,
        }
    if isinstance(error, LibraryValidationError):
        return {
            "_terminal": "validation",
            "_message": str(error),
            "_report": error.report,
        }
    if isinstance(error, LibraryForbidden):
        return {"_terminal": "forbidden", "_message": str(error)}
    if isinstance(error, references.ReferenceGateError):
        return {"_terminal": "gate_error", "_message": str(error)}
    if isinstance(error, LibraryError):
        return {"_terminal": "invalid", "_message": str(error)}
    raise TypeError(f"Unsupported terminal error: {type(error).__name__}")


# library_idempotency.idempotency_key is varchar(128). Domain callers compose
# keys from several ids (e.g. product intake: intake + reference object +
# entry), which can exceed it; SQLite never enforces the width, PostgreSQL
# raises StringDataRightTruncation. Keys that fit are stored unchanged, so
# replays of every existing key keep matching; longer ones are stored as a
# deterministic digest of the whole key.
IDEMPOTENCY_KEY_MAX_LENGTH = 128


def storable_idempotency_key(key: str | None) -> str | None:
    if key and len(key) > IDEMPOTENCY_KEY_MAX_LENGTH:
        return "sha256:" + _hashlib.sha256(key.encode("utf-8")).hexdigest()
    return key


def _run_with_idempotency(
    context, key, command, fingerprint, body_fn, *, command_identity: str | None = None,
):
    """Central idempotency protocol with a durable pre-side-effect reservation.

    ``body_fn(session, key_hash)`` returns a result dict or a ``_TerminalFailure``.
    The reservation is committed before the command transaction.  The command
    then locks that reservation, executes the body, and finalizes the cached
    result atomically with its database writes.  After a process crash a retry
    claims the still-pending row instead of losing all evidence that work began.
    """
    key = storable_idempotency_key(key)
    error_to_raise = None
    result = {}
    normalized_fingerprint = fingerprint or ""
    if key and not normalized_fingerprint:
        raise LibraryError("Idempotency-Key requires a normalized request fingerprint", code="idempotency_fingerprint_missing")
    command_identity_hash = ""
    if key:
        if command_identity is not None:
            try:
                identity = command_outcomes.validate_command_identity(command_identity)
            except command_outcomes.InvalidCommandIdentity as exc:
                raise LibraryError(str(exc), code="library_command_identity_invalid") from exc
            command_identity_hash = command_outcomes.hash_command_identity(
                identity, validate=False,
            )
        else:
            # Compatibility for the pre-BFF Library API and domain callers.
            # New v1/BFF commands always provide the raw opaque identity.
            command_identity_hash = command_outcomes.hash_command_identity(
                f"legacy:{key}", validate=False,
            )
        _ensure_idempotency_reservation(
            context, key, command, normalized_fingerprint, command_identity_hash,
        )
    with tenant_session(context) as session:
        key_hash = _hashlib.sha256(key.encode()).hexdigest() if key else None
        if key:
            cached = _check_idempotency(
                session, context, key, command, normalized_fingerprint,
                command_identity_hash,
            )
            if cached is not None:
                return cached
        if key:
            try:
                # Roll deterministic exceptions back to a savepoint before
                # caching them.  Otherwise a conflict raised after an
                # optimistic UPDATE could accidentally commit the partial
                # mutation along with the idempotency record.
                with session.begin_nested():
                    outcome = body_fn(session, key_hash)
            except (
                LibraryNotFound,
                RevisionConflict,
                LibraryConflict,
                LibraryValidationError,
                LibraryForbidden,
                UploadTooLarge,
                references.ReferenceGateError,
                LibraryError,
            ) as error:
                if isinstance(error, IdempotencyConflict):
                    raise
                error_to_raise = error
                result = _terminal_response(error)
            else:
                if isinstance(outcome, _TerminalFailure):
                    error_to_raise = outcome.error
                    result = outcome.response
                else:
                    result = outcome
        else:
            outcome = body_fn(session, key_hash)
            if isinstance(outcome, _TerminalFailure):
                error_to_raise = outcome.error
                result = outcome.response
            else:
                result = outcome
        if key:
            _finalize_idempotency(session, context, key, result)
        session.flush()
    if error_to_raise is not None:
        raise error_to_raise
    return result


def get_command_outcome(
    context: TenantContext,
    command_identity: str,
    *,
    expected_command_kind: str | None = None,
) -> dict:
    """Return the actor- and Workspace-bound authoritative command outcome."""
    try:
        return command_outcomes.get_outcome(
            context,
            command_identity,
            expected_command_kind=expected_command_kind,
        )
    except command_outcomes.InvalidCommandIdentity as exc:
        raise LibraryError(str(exc), code="library_command_identity_invalid") from exc
    except command_outcomes.CommandOutcomeAccessDenied as exc:
        raise LibraryNotFound("Command outcome 不存在", code="command_outcome_not_found") from exc
    except command_outcomes.CommandOutcomeBindingMismatch as exc:
        raise IdempotencyConflict(
            "Command identity 已绑定到另一个命令类型",
            {
                "code": "idempotency_key_reused",
                "existing_command": exc.existing_command,
                "attempted_command": exc.attempted_command,
            },
        ) from exc


def reconcile_command_outcome(
    context: TenantContext,
    command_identity: str,
    *,
    expected_command_kind: str | None = None,
) -> dict:
    """Resolve a pending reservation only when database evidence is definitive."""
    try:
        return command_outcomes.reconcile_outcome(
            context,
            command_identity,
            expected_command_kind=expected_command_kind,
        )
    except command_outcomes.InvalidCommandIdentity as exc:
        raise LibraryError(str(exc), code="library_command_identity_invalid") from exc
    except command_outcomes.CommandOutcomeAccessDenied as exc:
        raise LibraryNotFound("Command outcome 不存在", code="command_outcome_not_found") from exc
    except command_outcomes.CommandOutcomeBindingMismatch as exc:
        raise IdempotencyConflict(
            "Command identity 已绑定到另一个命令类型",
            {
                "code": "idempotency_key_reused",
                "existing_command": exc.existing_command,
                "attempted_command": exc.attempted_command,
            },
        ) from exc


# --------------------------------------------------------------------------
# 内部读取助手：一律 workspace 作用域
# --------------------------------------------------------------------------


def _require_workspace(session, workspace_id: str) -> WorkspaceRecord:
    """Workspace 必须已经存在。

    领域服务绝不因为收到一个未知 ID 就顺手建租户：Workspace 生命周期由控制面
    拥有，自动建租户会把伪造的租户 ID 变成一个真实的数据边界。
    """
    record = session.get(WorkspaceRecord, workspace_id)
    if record is None:
        raise LibraryNotFound("Workspace 不存在", code="workspace_not_found")
    return record


def _entry(session, context: TenantContext, entry_id: str) -> LibraryEntryRecord:
    record = session.get(LibraryEntryRecord, entry_id)
    if record is None or record.workspace_id != context.workspace_id:
        raise LibraryNotFound("创意库条目不存在")
    return record


def _version(session, context: TenantContext, version_id: str, *, for_update: bool = False) -> LibraryVersionRecord:
    if for_update:
        record = session.scalars(
            select(LibraryVersionRecord)
            .where(LibraryVersionRecord.id == version_id)
            .with_for_update()
        ).one_or_none()
    else:
        record = session.get(LibraryVersionRecord, version_id)
    if record is None or record.workspace_id != context.workspace_id:
        raise LibraryNotFound("创意库版本不存在")
    return record


def _draft(session, context: TenantContext, entry_id: str, *, for_update: bool = False) -> LibraryDraftRecord:
    stmt = select(LibraryDraftRecord).where(
        LibraryDraftRecord.entry_id == entry_id,
        LibraryDraftRecord.workspace_id == context.workspace_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    record = session.scalars(stmt).one_or_none()
    if record is None:
        raise LibraryNotFound("该条目没有 Draft")
    return record


def _claim_draft_revision(
    session,
    context: TenantContext,
    draft: LibraryDraftRecord,
    expected_revision: int,
    *,
    attempted_content: dict | None = None,
) -> int:
    """用条件 UPDATE 争夺 Draft 的下一个 revision（契约 §3.3）。

    读到的 revision 只是提示：真正的判定由数据库完成。并发事务里只有一条
    ``WHERE revision = :expected`` 能命中，其余拿到 rowcount 0 并被拒绝，
    因此最后写入者无法静默覆盖另一个用户的 Draft。

    输家一律拿到 ``LibraryConflict``，并附带 current_revision、expected_revision
    和字段级 field_diff（赢家已落库的内容 vs 本次尝试的内容），不会看到裸的
    数据库错误。
    """
    result = session.execute(
        update(LibraryDraftRecord)
        .where(
            LibraryDraftRecord.id == draft.id,
            LibraryDraftRecord.workspace_id == context.workspace_id,
            LibraryDraftRecord.revision == expected_revision,
        )
        .values(revision=expected_revision + 1, updated_at=utcnow())
    )
    if result.rowcount != 1:
        draft_id = draft.id
        session.expire(draft)
        current = session.get(LibraryDraftRecord, draft_id)
        raise RevisionConflict(
            f"revision 冲突：期望 {expected_revision}，当前 {current.revision if current else '已删除'}",
            {
                "current_revision": current.revision if current else None,
                "expected_revision": expected_revision,
                "field_diff": diff_content(
                    dict(current.content_json or {}) if current else {},
                    dict(attempted_content or {}),
                ),
            },
        )
    session.expire(draft)
    return expected_revision + 1


def _next_sequence(session, entry_id: str) -> int:
    current = session.scalar(
        select(func.max(LibraryEventRecord.sequence)).where(LibraryEventRecord.entry_id == entry_id)
    )
    return int(current or 0) + 1


def _emit(
    session,
    context: TenantContext,
    entry_id: str,
    event_type: str,
    *,
    version_id: str | None = None,
    from_state: str = "",
    to_state: str = "",
    payload: dict | None = None,
) -> None:
    session.add(
        LibraryEventRecord(
            id=new_id("libev"),
            workspace_id=context.workspace_id,
            entry_id=entry_id,
            version_id=version_id,
            sequence=_next_sequence(session, entry_id),
            event_type=event_type,
            from_state=from_state,
            to_state=to_state,
            initiator_id=context.initiator_id or "",
            executor=context.executor,
            payload_json=payload or {},
        )
    )
    session.flush()


def _audit(
    session,
    context: TenantContext,
    command: str,
    *,
    entry_id: str | None = None,
    version_id: str | None = None,
    result: str = "ok",
    detail: dict | None = None,
) -> None:
    session.add(
        LibraryAuditRecord(
            id=new_id("libaudit"),
            workspace_id=context.workspace_id,
            entry_id=entry_id,
            version_id=version_id,
            command=command,
            initiator_id=context.initiator_id or "",
            initiator_role=context.audit_initiator_role,
            executor=context.executor,
            request_id=context.request_id or "",
            trace_id=context.trace_id or "",
            authorization_epoch=context.authorization_epoch,
            result=result,
            detail_json=_redact(detail) if detail else {},
        )
    )


# --------------------------------------------------------------------------
# 跨版本校验（契约 §3.4 通用校验）
# --------------------------------------------------------------------------


def _validate_dependencies(
    session,
    context: TenantContext,
    entry: LibraryEntryRecord,
    edges: Iterable[DependencyEdge],
    findings: list[Finding],
    *,
    today: date,
) -> list[tuple[DependencyEdge, LibraryVersionRecord]]:
    resolved: list[tuple[DependencyEdge, LibraryVersionRecord]] = []
    for edge in edges:
        # REL_SCHEMA_PRODUCT is the one relation whose edge carries an Entry ID
        # (DimensionSchema.product_entry_id: the product binding is a stable
        # identity, not one frozen version) rather than a Version ID. Resolve it
        # through the product's current effective version so the persisted
        # LibraryDependencyRecord stays version-to-version like every other edge.
        if edge.relation == REL_SCHEMA_PRODUCT:
            product_entry = session.get(LibraryEntryRecord, edge.to_version_id)
            if product_entry is None or product_entry.workspace_id != context.workspace_id:
                findings.append(
                    Finding("dependency_not_found", f"引用的产品条目不存在: {edge.to_version_id}", edge.path)
                )
                continue
            if not product_entry.current_version_id:
                findings.append(
                    Finding("dependency_not_found", f"引用的产品条目尚无已发布版本: {edge.to_version_id}", edge.path)
                )
                continue
            edge = DependencyEdge(product_entry.current_version_id, edge.relation, edge.path)
        target = session.get(LibraryVersionRecord, edge.to_version_id)
        if target is None or target.workspace_id != context.workspace_id:
            findings.append(
                Finding("dependency_not_found", f"引用的版本不存在: {edge.to_version_id}", edge.path)
            )
            continue
        if target.entry_id == entry.id:
            findings.append(Finding("circular_reference", "版本不能引用自身条目", edge.path))
            continue
        expected_type = RELATION_TARGET_TYPES.get(edge.relation)
        if expected_type and target.entry_type != expected_type:
            findings.append(
                Finding(
                    "dependency_type_mismatch",
                    f"{edge.relation} 需要 {expected_type} 版本，实际为 {target.entry_type}",
                    edge.path,
                )
            )
            continue
        if target.state == VERSION_WITHDRAWN:
            findings.append(
                Finding("dependency_withdrawn", f"引用的版本已被 Withdraw: {target.id}", edge.path)
            )
            continue
        if target.eligibility == ELIGIBILITY_BLOCKED:
            findings.append(
                Finding("dependency_blocked", f"引用的版本资格为 blocked: {target.id}", edge.path)
            )
            continue
        target_entry = session.get(LibraryEntryRecord, target.entry_id)
        if target_entry is not None and target_entry.lifecycle == LIFECYCLE_ARCHIVED:
            findings.append(
                Finding("dependency_archived", f"引用的条目已归档，不能建立新引用: {target.entry_id}", edge.path)
            )
            continue
        _check_reference_rights(target, findings, edge, today=today)
        if target.eligibility == ELIGIBILITY_WARNING:
            findings.append(
                Finding(
                    "dependency_warning",
                    f"引用的版本资格为 warning: {target.id}",
                    edge.path,
                    SEVERITY_WARNING,
                )
            )
        resolved.append((edge, target))
    _check_circular(session, context, entry, resolved, findings)
    return resolved


def _check_reference_rights(
    target: LibraryVersionRecord, findings: list[Finding], edge: DependencyEdge, *, today: date
) -> None:
    """引用参考素材时重新检查权利状态与有效期（§7.2、§3.4）。"""
    if target.entry_type != ENTRY_REFERENCE_ASSET:
        return
    rights = (target.content_json or {}).get("rights") or {}
    reason = check_rights_eligible(rights, today=today)
    if reason is not None:
        findings.append(Finding("reference_rights_ineligible", f"参考素材不可引用: {reason}", edge.path))


def _check_circular(
    session,
    context: TenantContext,
    entry: LibraryEntryRecord,
    resolved: list[tuple[DependencyEdge, LibraryVersionRecord]],
    findings: list[Finding],
) -> None:
    """沿依赖闭包检测是否回到本条目（§3.4 循环引用）。"""
    if not resolved:
        return
    seen: set[str] = set()
    frontier = [target.id for _edge, target in resolved]
    path_by_version = {target.id: edge.path for edge, target in resolved}
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        version = session.get(LibraryVersionRecord, current)
        if version is None or version.workspace_id != context.workspace_id:
            continue
        if version.entry_id == entry.id:
            findings.append(
                Finding(
                    "circular_reference",
                    f"依赖闭包回到本条目: {version.id}",
                    path_by_version.get(current, ""),
                )
            )
            return
        for row in session.scalars(
            select(LibraryDependencyRecord).where(LibraryDependencyRecord.from_version_id == current)
        ).all():
            frontier.append(row.to_version_id)


def _build_report(
    session,
    context: TenantContext,
    entry: LibraryEntryRecord,
    content: dict,
    *,
    today: date | None = None,
) -> tuple[ValidationReport, list[tuple[DependencyEdge, LibraryVersionRecord]]]:
    today = today or date.today()
    report = validate_content(entry.entry_type, content, today=today)
    resolved = _validate_dependencies(
        session, context, entry, extract_dependencies(entry.entry_type, content), report.findings, today=today
    )
    if entry.entry_type == ENTRY_REFERENCE_ASSET:
        _validate_reference_object(session, context, content, report.findings)
    elif entry.entry_type == ENTRY_DIMENSION_SCHEMA:
        _validate_schema_product_immutable(session, context, entry, content, report.findings)
    return report, resolved


def _validate_schema_product_immutable(
    session, context: TenantContext, entry: LibraryEntryRecord, content: dict, findings: list[Finding]
) -> None:
    """DimensionSchemaVersion.product_entry_id is frozen once the first version publishes (#20 §9.1).

    A schema is a product-local copy of a template; letting product_entry_id
    drift across versions would silently re-point historical evidence at a
    different product. Compared against any already-published version's
    content — as soon as one exists, later drafts must match it.
    """
    new_product_id = content.get("product_entry_id")
    if not new_product_id:
        return
    existing_content = session.scalars(
        select(LibraryVersionRecord.content_json).where(
            LibraryVersionRecord.entry_id == entry.id,
            LibraryVersionRecord.workspace_id == context.workspace_id,
        )
    ).first()
    if not existing_content:
        return
    prior_product_id = existing_content.get("product_entry_id")
    if prior_product_id and prior_product_id != new_product_id:
        findings.append(
            Finding(
                "schema_product_entry_immutable",
                f"DimensionSchema 的 product_entry_id 一旦发布不可跨版本改变: {prior_product_id} → {new_product_id}",
                "product_entry_id",
            )
        )


def _validate_reference_object(session, context: TenantContext, content: dict, findings: list[Finding]) -> None:
    """ReferenceAssetVersion 只能引用已通过全部门禁并生成受控派生的对象（§7.2）。

    证据字段必须与对象记录实际值匹配——只检查存在性会让调用方伪造哈希或尺寸。
    """
    object_id = str(content.get("object_id") or "").strip()
    if not object_id:
        return
    record = session.get(ReferenceObjectRecord, object_id)
    if record is None or record.workspace_id != context.workspace_id:
        findings.append(Finding("reference_object_not_found", "参考对象不存在", "object_id"))
        return
    if record.state != references.STATE_RELEASED:
        findings.append(
            Finding("reference_object_quarantined", f"参考对象状态为 {record.state}，未放行", "object_id")
        )
        return
    if not record.derivative_object_key:
        findings.append(
            Finding("reference_object_no_derivative", "缺少受控派生，Provider 不可读取原件", "object_id")
        )
    file_ev = content.get("file")
    if isinstance(file_ev, dict):
        _cross = [
            ("content_sha256", record.content_sha256),
            ("mime_type", record.mime_type),
            ("byte_size", record.byte_size),
            ("width", record.width),
            ("height", record.height),
        ]
        for field, expected in _cross:
            declared = file_ev.get(field)
            if declared is None:
                findings.append(Finding(
                    f"reference_evidence_missing_{field}",
                    f"file.{field} 缺失，必须与对象记录匹配 (期望 {expected!r})",
                    f"file.{field}",
                ))
            elif declared != expected:
                findings.append(Finding(
                    f"reference_evidence_mismatch_{field}",
                    f"file.{field} 与对象记录不匹配: 声明 {declared!r}, 实际 {expected!r}",
                    f"file.{field}",
                ))
    gate_checks = content.get("gate_checks")
    if isinstance(gate_checks, list):
        actual_checks = (record.checks_json or {}).get("checks", [])
        if gate_checks != actual_checks:
            findings.append(Finding(
                "reference_gate_checks_mismatch",
                "gate_checks 与对象记录不匹配",
                "gate_checks",
            ))
    derivative = content.get("derivative")
    if isinstance(derivative, dict):
        _deriv_cross = [
            ("sha256", record.derivative_sha256),
            ("mime_type", record.derivative_mime),
            ("byte_size", record.derivative_byte_size),
            ("width", record.derivative_width),
            ("height", record.derivative_height),
        ]
        for field, expected in _deriv_cross:
            declared = derivative.get(field)
            if declared is None:
                findings.append(Finding(
                    f"reference_derivative_missing_{field}",
                    f"derivative.{field} 缺失，必须与对象记录匹配 (期望 {expected!r})",
                    f"derivative.{field}",
                ))
            elif declared != expected:
                findings.append(Finding(
                    f"reference_derivative_mismatch_{field}",
                    f"derivative.{field} 与对象记录不匹配: 声明 {declared!r}, 实际 {expected!r}",
                    f"derivative.{field}",
                ))


# --------------------------------------------------------------------------
# 命令：条目与 Draft
# --------------------------------------------------------------------------


def _bridge_product_identity(session, context: TenantContext, entry: LibraryEntryRecord) -> None:
    """Create the ``products`` row a new ProductProfile entry is bridged to.

    The Generation/Experiments schema still uses ``products`` as its aggregate
    root while the Library owns the versioned ProductProfile. Every command
    that mints a product *entry* -- ``create_entry``, ``fork_version`` and
    ``clone_product`` (Issue #157; the latter two used to skip this and their
    products failed preview with ``unresolved_preview_references``) --
    establishes that identity bridge in the same transaction, so the product
    is immediately usable downstream without fixture-only database seeding.
    """
    if entry.entry_type != ENTRY_PRODUCT:
        return
    session.add(
        ProductRecord(
            id=new_id("prod"),
            workspace_id=context.workspace_id,
            name=entry.display_name,
            product_profile_entry_id=entry.id,
        )
    )
    session.flush()


def _sync_bridged_product_name(session, context: TenantContext, entry: LibraryEntryRecord) -> None:
    """Keep the bridged ``products.name`` equal to the published ``content.name``.

    ``publish`` already re-labels the entry from ``content.name``; the
    ``products`` row downstream pages read (Runs, Assets, Experiments) must
    follow, or a renamed product keeps showing its creation-time name there.
    """
    if entry.entry_type != ENTRY_PRODUCT:
        return
    product = session.scalar(
        select(ProductRecord).where(
            ProductRecord.workspace_id == context.workspace_id,
            ProductRecord.product_profile_entry_id == entry.id,
        )
    )
    if product is None or product.name == entry.display_name:
        return
    product.name = entry.display_name
    product.updated_at = utcnow()
    session.flush()


def create_entry(
    context: TenantContext,
    *,
    entry_type: str,
    display_name: str,
    content: dict | None = None,
    idempotency_key: str | None = None,
    request_fingerprint: str | None = None,
    command_identity: str | None = None,
) -> dict:
    """创建稳定身份并开出第一个 Draft。发布前不产生任何版本。"""
    if entry_type not in ENTRY_TYPES:
        raise LibraryError(f"未知创意库条目类型: {entry_type}", code="library_entry_type_unknown")
    name = (display_name or "").strip()
    if not name:
        raise LibraryError("display_name 不能为空", code="library_display_name_required")

    def body(session, key_hash):
        _require_workspace(session, context.workspace_id)
        entry, _draft_record = _create_entry_with_draft(
            session, context, entry_type=entry_type, display_name=name, content=content,
        )
        result = _entry_dict(session, context, entry)
        _audit(session, context, "create_entry", entry_id=entry.id, detail={
            "entry_type": entry_type, **_idem_detail(key_hash),
        })
        return result

    return _run_with_idempotency(
        context, idempotency_key, "create_entry", request_fingerprint, body,
        command_identity=command_identity,
    )


# library_entries.display_name is varchar(300), the same bound the API puts on
# a user-supplied name. A name the server derives from another entry's name
# ("Schema for <product>") can exceed it even when the source fits; PostgreSQL
# would reject it (SQLite does not enforce widths), so derived names are cut
# to the column instead of failing the command.
DISPLAY_NAME_MAX_LENGTH = 300


def derived_display_name(name: str) -> str:
    return name[:DISPLAY_NAME_MAX_LENGTH]


def _create_entry_with_draft(
    session,
    context: TenantContext,
    *,
    entry_type: str,
    display_name: str,
    content: dict | None,
) -> tuple[LibraryEntryRecord, LibraryDraftRecord]:
    """稳定身份 + 第一个 Draft（§3.1）；``create_entry`` 与服务端 promote 共用。"""
    entry = LibraryEntryRecord(
        id=new_id("lib"),
        workspace_id=context.workspace_id,
        entry_type=entry_type,
        display_name=display_name,
        lifecycle=LIFECYCLE_ACTIVE,
    )
    session.add(entry)
    session.flush()
    _bridge_product_identity(session, context, entry)
    draft = LibraryDraftRecord(
        id=new_id("libdraft"),
        workspace_id=context.workspace_id,
        entry_id=entry.id,
        content_json=dict(content or {}),
        revision=1,
        state=DRAFT_EDITING,
        origin_command=COMMAND_CREATE,
    )
    session.add(draft)
    session.flush()
    _emit(session, context, entry.id, "draft_created", to_state=DRAFT_EDITING)
    return entry, draft


def save_draft(
    context: TenantContext,
    entry_id: str,
    content: dict,
    expected_revision: int,
    *,
    idempotency_key: str | None = None,
    request_fingerprint: str | None = None,
    command_identity: str | None = None,
) -> dict:
    """自动保存只更新 Draft（§3.1）。revision 不匹配返回冲突和字段级差异（§3.3）。"""
    def body(session, key_hash):
        entry = _entry(session, context, entry_id)
        if entry.lifecycle == LIFECYCLE_ARCHIVED:
            raise LibraryConflict("条目已归档，不能继续编辑", code="entry_archived")
        draft = _draft(session, context, entry_id)
        _claim_draft_revision(
            session, context, draft, expected_revision, attempted_content=dict(content or {})
        )
        draft.content_json = dict(content or {})
        draft.state = DRAFT_EDITING
        draft.validation_json = None
        draft.origin_command = COMMAND_EDIT if entry.current_version_id else draft.origin_command
        _emit(session, context, entry_id, "draft_saved", to_state=DRAFT_EDITING, payload={"revision": draft.revision})
        result = _draft_dict(draft)
        _audit(session, context, "save_draft", entry_id=entry_id, detail={
            "revision": draft.revision, **_idem_detail(key_hash),
        })
        return result

    return _run_with_idempotency(
        context, idempotency_key, "save_draft", request_fingerprint, body,
        command_identity=command_identity,
    )


def validate_draft(
    context: TenantContext,
    entry_id: str,
    *,
    expected_revision: int,
    today: date | None = None,
    idempotency_key: str | None = None,
    request_fingerprint: str | None = None,
    command_identity: str | None = None,
) -> dict:
    """产生 ValidationReport 并投影 DraftState（§3.2、§3.4）。"""
    def body(session, key_hash):
        entry = _entry(session, context, entry_id)
        draft = _draft(session, context, entry_id, for_update=True)
        if draft.revision != expected_revision:
            raise RevisionConflict(
                "Draft 已被其他操作修改",
                {"current_revision": draft.revision, "expected_revision": expected_revision},
            )
        report, _resolved = _build_report(session, context, entry, draft.content_json or {}, today=today)
        draft.state = DRAFT_READY if report.ok else DRAFT_VALIDATION_FAILED
        draft.validation_json = report.as_dict()
        draft.updated_at = utcnow()
        _emit(
            session,
            context,
            entry_id,
            "validated",
            to_state=draft.state,
            payload={"blocking": len(report.blocking), "warnings": len(report.warnings)},
        )
        result = report.as_dict()
        _audit(
            session,
            context,
            "validate_draft",
            entry_id=entry_id,
            result="ok" if report.ok else "blocked",
            detail={
                "content_hash": report.content_hash,
                **_idem_detail(key_hash),
            },
        )
        return result

    return _run_with_idempotency(
        context, idempotency_key, "validate_draft", request_fingerprint, body,
        command_identity=command_identity,
    )


def publish(
    context: TenantContext,
    entry_id: str,
    expected_revision: int,
    *,
    acknowledged_warnings: list[str] | None = None,
    today: date | None = None,
    idempotency_key: str | None = None,
    request_fingerprint: str | None = None,
    command_identity: str | None = None,
    impact_id: str | None = None,
    impact_hash: str | None = None,
) -> dict:
    """原子发布（§3.4）。任一阻断项失败时不产生部分版本。

    拒绝路径的证据必须落库：ValidationReport、DraftState 与审计行都要提交，
    否则用户点了发布却看不到失败原因。Terminal failures are stored idempotently
    via _TerminalFailure so retries replay the same error.
    """
    acknowledged = set(acknowledged_warnings or [])

    def body(session, key_hash):
        audit_base = _idem_detail(key_hash)
        if impact_id and impact_hash:
            try:
                command_impacts.verify_and_consume_impact(
                    session, context,
                    impact_id=impact_id, impact_hash=impact_hash,
                    command_kind="publish", target_entry_id=entry_id,
                    target_revision=expected_revision,
                )
            except command_impacts.ImpactVerificationError as exc:
                raise LibraryConflict(str(exc), {"code": exc.code, **exc.detail})

        entry = _entry(session, context, entry_id)
        if entry.lifecycle == LIFECYCLE_ARCHIVED:
            raise LibraryConflict("条目已归档，不能发布新版本", code="entry_archived")
        draft = _draft(session, context, entry_id)
        if entry.entry_type in OWNER_ONLY_PUBLISH and context.role not in ("owner", "system"):
            _audit(session, context, "publish", entry_id=entry_id, result="forbidden",
                   detail=audit_base)
            return _TerminalFailure(LibraryForbidden(f"只有 Owner 可以发布 {entry.entry_type} 版本"))

        content = dict(draft.content_json or {})
        report, resolved = _build_report(session, context, entry, content, today=today)
        unacknowledged = [f.code for f in report.warnings if f.code not in acknowledged]
        if not report.ok:
            draft.state = DRAFT_VALIDATION_FAILED
            draft.validation_json = report.as_dict()
            draft.updated_at = utcnow()
            _emit(session, context, entry_id, "validated", to_state=DRAFT_VALIDATION_FAILED)
            _audit(session, context, "publish", entry_id=entry_id, result="blocked",
                   detail={**report.as_dict(), **audit_base})
            return _TerminalFailure(LibraryValidationError("发布被阻断项拒绝", report.as_dict()))
        elif unacknowledged:
            draft.state = DRAFT_READY
            draft.validation_json = report.as_dict()
            draft.updated_at = utcnow()
            _audit(session, context, "publish", entry_id=entry_id, result="needs_acknowledgement",
                   detail={"warnings": unacknowledged, **audit_base})
            return _TerminalFailure(
                LibraryValidationError(
                    "存在未确认的非阻断警告，必须显式确认后再发布", report.as_dict(),
                    code="library_warnings_unacknowledged",
                ),
            )
        return _publish_effective_version(
            session, context, entry, draft, content, report, resolved, acknowledged, expected_revision,
            extra_audit_detail=audit_base,
        )

    return _run_with_idempotency(
        context, idempotency_key, "publish", request_fingerprint, body,
        command_identity=command_identity,
    )


def _publish_effective_version(
    session,
    context: TenantContext,
    entry: LibraryEntryRecord,
    draft: LibraryDraftRecord,
    content: dict,
    report: ValidationReport,
    resolved: list[tuple[DependencyEdge, LibraryVersionRecord]],
    acknowledged: set[str],
    expected_revision: int,
    *,
    extra_audit_detail: dict | None = None,
) -> dict:
    """在一个事务内创建版本、谱系、依赖、绑定、事件、审计与当前版本投影（§3.4）。"""
    entry_id = entry.id

    claimed_revision = _claim_draft_revision(
        session, context, draft, expected_revision, attempted_content=content
    )

    current = _effective_version(session, entry)
    if current is not None and current.content_hash == report.content_hash:
        raise LibraryConflict(
            "内容与当前生效版本相同，不产生新版本",
            {
                "current_revision": claimed_revision,
                "expected_revision": expected_revision,
                "field_diff": {},
                "current_version_id": current.id,
                "content_hash": report.content_hash,
            },
            code="library_version_unchanged",
        )

    version_no = int(
        session.scalar(
            select(func.max(LibraryVersionRecord.version_no)).where(
                LibraryVersionRecord.entry_id == entry_id
            )
        )
        or 0
    ) + 1
    now = utcnow()
    version_id = new_id("libver")

    if current is not None:
        current.state = VERSION_SUPERSEDED
        current.superseded_at = now
        session.flush()
        _emit(
            session,
            context,
            entry_id,
            "superseded",
            version_id=current.id,
            from_state=VERSION_EFFECTIVE,
            to_state=VERSION_SUPERSEDED,
            payload={"superseded_by": version_id},
        )

    version = LibraryVersionRecord(
        id=version_id,
        workspace_id=context.workspace_id,
        entry_id=entry_id,
        entry_type=entry.entry_type,
        version_no=version_no,
        content_json=content,
        content_hash=report.content_hash,
        state=VERSION_EFFECTIVE,
        eligibility=ELIGIBILITY_ELIGIBLE,
        validation_json=report.as_dict(),
        base_version_id=draft.base_version_id,
        source_version_id=draft.source_version_id,
        origin_command=draft.origin_command,
        published_by=context.initiator_id,
        published_at=now,
    )
    session.add(version)
    session.flush()

    dependency_keys: set[tuple[str, str]] = set()
    for edge, target in resolved:
        # One reference can have distinct bindings for several product
        # models/roles. The dependency graph stores one version/relation
        # edge, while ReferenceBinding retains every individual slot.
        dependency_key = (target.id, edge.relation)
        if dependency_key in dependency_keys:
            continue
        dependency_keys.add(dependency_key)
        session.add(
            LibraryDependencyRecord(
                id=new_id("libdep"),
                workspace_id=context.workspace_id,
                from_version_id=version.id,
                to_version_id=target.id,
                relation=edge.relation,
                path=edge.path[:300],
            )
        )
    _materialize_bindings(session, context, entry, version, content)

    entry.current_version_id = version.id
    entry.display_name = str(content.get("name") or entry.display_name)
    entry.updated_at = now
    _sync_bridged_product_name(session, context, entry)

    draft.base_version_id = version.id
    draft.state = DRAFT_EDITING
    draft.origin_command = COMMAND_EDIT
    draft.validation_json = report.as_dict()
    draft.updated_at = now

    _emit(
        session,
        context,
        entry_id,
        "published",
        version_id=version.id,
        to_state=VERSION_EFFECTIVE,
        payload={"version_no": version_no, "content_hash": report.content_hash},
    )
    _audit(
        session,
        context,
        "publish",
        entry_id=entry_id,
        version_id=version.id,
        detail={
            "version_no": version_no,
            "content_hash": report.content_hash,
            "acknowledged_warnings": sorted(acknowledged),
            **(extra_audit_detail or {}),
        },
    )
    session.flush()
    return _version_dict(version)


def _materialize_bindings(
    session, context: TenantContext, entry: LibraryEntryRecord, version: LibraryVersionRecord, content: dict
) -> None:
    """把内容里的参考绑定物化成独立 ReferenceBinding 行（§7.1）。

    绑定挂在不可变的 owner 版本上，因此发布后不再变化；相同文件的不同绑定
    保持独立，不因 hash 相同合并。
    """
    if entry.entry_type not in {ENTRY_PRODUCT, ENTRY_PERSON, ENTRY_REFERENCE_GROUP}:
        return
    for row in extract_reference_bindings(entry.entry_type, content):
        if not row["reference_version_id"]:
            continue
        session.add(
            ReferenceBindingRecord(
                id=new_id("refbind"),
                workspace_id=context.workspace_id,
                owner_version_id=version.id,
                reference_version_id=row["reference_version_id"],
                purpose=row["purpose"][:100],
                viewpoint=row["viewpoint"][:100],
                priority=row["priority"],
                note=row["note"],
                conditions_json=row["conditions"],
                position=row["position"],
            )
        )
    session.flush()


def _effective_version(session, entry: LibraryEntryRecord) -> LibraryVersionRecord | None:
    return session.scalars(
        select(LibraryVersionRecord).where(
            LibraryVersionRecord.entry_id == entry.id,
            LibraryVersionRecord.state == VERSION_EFFECTIVE,
        )
    ).one_or_none()


# --------------------------------------------------------------------------
# 命令：Withdraw / Archive 与影响分析（契约 §14）
# --------------------------------------------------------------------------


def _dependent_versions(session, context: TenantContext, root_ids: set[str]) -> list[LibraryVersionRecord]:
    """沿 DependencyIndex 反向做传递闭包，返回受影响的下游版本。"""
    found: dict[str, LibraryVersionRecord] = {}
    frontier = list(root_ids)
    seen: set[str] = set(root_ids)
    while frontier:
        current = frontier.pop()
        rows = session.scalars(
            select(LibraryDependencyRecord).where(
                LibraryDependencyRecord.to_version_id == current,
                LibraryDependencyRecord.workspace_id == context.workspace_id,
            )
        ).all()
        for row in rows:
            if row.from_version_id in seen:
                continue
            seen.add(row.from_version_id)
            version = session.get(LibraryVersionRecord, row.from_version_id)
            if version is None or version.workspace_id != context.workspace_id:
                continue
            found[version.id] = version
            frontier.append(version.id)
    return list(found.values())


def _impact_payload(
    session, context: TenantContext, version: LibraryVersionRecord, *, reason: str = ""
) -> dict:
    dependents = _dependent_versions(session, context, {version.id})
    payload = {
        "version_id": version.id,
        "entry_id": version.entry_id,
        "entry_type": version.entry_type,
        "state": version.state,
        "eligibility": version.eligibility,
        "dependents": [
            {
                "version_id": item.id,
                "entry_id": item.entry_id,
                "entry_type": item.entry_type,
                "version_no": item.version_no,
                "state": item.state,
                "eligibility": item.eligibility,
            }
            for item in sorted(dependents, key=lambda v: (v.entry_type, v.version_no))
        ],
    }
    if reason:
        payload["reason"] = reason
    return payload


def impact_analysis(context: TenantContext, version_id: str) -> dict:
    """发布、撤销和归档前展示影响分析（§14）。只读，不改状态。"""
    with tenant_session(context) as session:
        return _impact_payload(session, context, _version(session, context, version_id))


def withdraw_version(
    context: TenantContext,
    version_id: str,
    *,
    reason: str,
    idempotency_key: str | None = None,
    request_fingerprint: str | None = None,
    command_identity: str | None = None,
    impact_id: str | None = None,
    impact_hash: str | None = None,
) -> dict:
    """Withdraw 阻止新的外部步骤，并沿创意库内部依赖把下游投影为 blocked（§14）。

    历史内容不变；已冻结的执行快照不被改写。Withdraw 是高风险动作，要求 Owner。

    Effective 和 superseded 版本均可 Withdraw——superseded 版本可能因安全、
    商标、权利或事实问题而需要紧急撤回。
    """
    clean_reason = (reason or "").strip()
    if not clean_reason:
        raise LibraryError("Withdraw 必须记录原因", code="library_withdraw_reason_required")

    def body(session, key_hash):
        audit_base = _idem_detail(key_hash)
        version = _version(session, context, version_id, for_update=True)

        if impact_id and impact_hash:
            try:
                command_impacts.verify_and_consume_impact(
                    session, context,
                    impact_id=impact_id, impact_hash=impact_hash,
                    command_kind="withdraw", target_entry_id=version.entry_id,
                    target_version_id=version_id,
                )
            except command_impacts.ImpactVerificationError as exc:
                raise LibraryConflict(str(exc), {"code": exc.code, **exc.detail})

        if context.role not in ("owner", "system"):
            _audit(session, context, "withdraw", entry_id=version.entry_id,
                   version_id=version.id, result="forbidden", detail=audit_base)
            return _TerminalFailure(LibraryForbidden("只有 Owner 可以 Withdraw 已发布版本"))
        if version.state == VERSION_WITHDRAWN:
            _audit(session, context, "withdraw", entry_id=version.entry_id,
                   version_id=version.id, result="no_op",
                   detail={"reason": version.withdraw_reason, **audit_base})
            return _impact_payload(session, context, version, reason=version.withdraw_reason)
        if version.state not in (VERSION_EFFECTIVE, VERSION_SUPERSEDED):
            raise LibraryConflict(
                f"只有 effective 或 superseded 状态的版本可以 Withdraw，当前为 {version.state!r}",
                {"current_state": version.state},
                code="library_version_not_withdrawable",
            )

        previous_state = version.state
        now = utcnow()
        version.state = VERSION_WITHDRAWN
        version.eligibility = ELIGIBILITY_BLOCKED
        version.lifecycle_revision += 1
        version.withdrawn_at = now
        version.withdrawn_by = context.initiator_id
        version.withdraw_reason = clean_reason
        _emit(
            session,
            context,
            version.entry_id,
            "withdrawn",
            version_id=version.id,
            from_state=previous_state,
            to_state=VERSION_WITHDRAWN,
            payload={"reason": clean_reason},
        )

        entry = _entry(session, context, version.entry_id)
        if entry.current_version_id == version.id:
            entry.current_version_id = None
            entry.updated_at = now

        impacted = _dependent_versions(session, context, {version.id})
        for dependent in impacted:
            if dependent.eligibility != ELIGIBILITY_BLOCKED:
                dependent.eligibility = ELIGIBILITY_BLOCKED
                dependent.lifecycle_revision += 1
            _emit(
                session,
                context,
                dependent.entry_id,
                "upstream_withdrawn",
                version_id=dependent.id,
                to_state=ELIGIBILITY_BLOCKED,
                payload={"withdrawn_version_id": version.id, "reason": clean_reason},
            )
        _emit(
            session,
            context,
            version.entry_id,
            "version.withdrawn",
            version_id=version.id,
            from_state=previous_state,
            to_state="withdrawn",
            payload={
                "reason": clean_reason,
                "impact": {
                    "blocked_versions": [v.id for v in impacted],
                },
                "downstream_event_contract": list(WITHDRAWAL_DOWNSTREAM_EVENT_TYPES.values()),
            },
        )
        for _entity_type, event_type in sorted(WITHDRAWAL_DOWNSTREAM_EVENT_TYPES.items()):
            _emit(
                session,
                context,
                version.entry_id,
                event_type,
                version_id=version.id,
                payload={
                    "withdrawn_version_id": version.id,
                    "reason": clean_reason,
                    "affected_entities": [],
                },
            )
        result = _impact_payload(session, context, version, reason=clean_reason)
        _audit(
            session,
            context,
            "withdraw",
            entry_id=version.entry_id,
            version_id=version.id,
            detail={
                "reason": clean_reason,
                "impacted": [item.id for item in impacted],
                **audit_base,
            },
        )
        return result

    return _run_with_idempotency(
        context, idempotency_key, "withdraw", request_fingerprint, body,
        command_identity=command_identity,
    )


def archive_entry(
    context: TenantContext,
    entry_id: str,
    *,
    expected_revision: int,
    idempotency_key: str | None = None,
    request_fingerprint: str | None = None,
    command_identity: str | None = None,
    impact_id: str | None = None,
    impact_hash: str | None = None,
) -> dict:
    """Archive 阻止新选择，不删除任何版本（§14、§15）。

    Archive 会让整个条目对新任务不可用，影响面与 Withdraw 同级，因此和 Withdraw
    一样要求 Owner。契约 §4 没有逐条列出 Archive 的角色，这是本实现的保守取舍。
    """
    def body(session, key_hash):
        if impact_id and impact_hash:
            try:
                command_impacts.verify_and_consume_impact(
                    session, context,
                    impact_id=impact_id, impact_hash=impact_hash,
                    command_kind="archive", target_entry_id=entry_id,
                    target_revision=expected_revision,
                )
            except command_impacts.ImpactVerificationError as exc:
                raise LibraryConflict(str(exc), {"code": exc.code, **exc.detail})

        entry = _entry(session, context, entry_id)
        draft = _draft(session, context, entry_id, for_update=True)
        if draft.revision != expected_revision:
            raise RevisionConflict(
                "Draft 已被其他操作修改",
                {"current_revision": draft.revision, "expected_revision": expected_revision},
            )
        if context.role not in ("owner", "system"):
            _audit(session, context, "archive", entry_id=entry_id, result="forbidden",
                   detail=_idem_detail(key_hash))
            return _TerminalFailure(LibraryForbidden("只有 Owner 可以归档创意库条目"))
        if entry.lifecycle == LIFECYCLE_ARCHIVED:
            _audit(session, context, "archive", entry_id=entry_id, result="no_op",
                   detail=_idem_detail(key_hash))
            return _entry_dict(session, context, entry)
        entry.lifecycle = LIFECYCLE_ARCHIVED
        entry.archived_at = utcnow()
        entry.updated_at = entry.archived_at
        draft.revision += 1
        draft.updated_at = entry.archived_at
        _emit(session, context, entry_id, "archived", from_state=LIFECYCLE_ACTIVE, to_state=LIFECYCLE_ARCHIVED)
        result = _entry_dict(session, context, entry)
        _audit(session, context, "archive", entry_id=entry_id, detail=_idem_detail(key_hash))
        return result

    return _run_with_idempotency(
        context, idempotency_key, "archive", request_fingerprint, body,
        command_identity=command_identity,
    )


# --------------------------------------------------------------------------
# 命令：Bind / Fork / Clone（契约 §12）
# --------------------------------------------------------------------------


def bind_reference(
    context: TenantContext,
    entry_id: str,
    expected_revision: int,
    *,
    reference_version_id: str,
    purpose: str,
    viewpoint: str,
    priority: int = 0,
    note: str = "",
    conditions: dict | None = None,
    idempotency_key: str | None = None,
    request_fingerprint: str | None = None,
    command_identity: str | None = None,
) -> dict:
    """Bind：在同一 Workspace 内引用一个稳定 Reference 身份（§12）。

    跨 Workspace 不提供 Bind；目标不在本 Workspace 时按"不存在"失败。
    """
    def body(session, key_hash):
        draft, _already = _append_reference_binding(
            session, context, entry_id, expected_revision,
            reference_version_id=reference_version_id,
            purpose=purpose, viewpoint=viewpoint,
            priority=priority, note=note, conditions=conditions,
            tolerate_existing=False,
        )
        result = _draft_dict(draft)
        _audit(
            session,
            context,
            "bind",
            entry_id=entry_id,
            detail={
                "reference_version_id": reference_version_id,
                "purpose": purpose,
                **_idem_detail(key_hash),
            },
        )
        return result

    return _run_with_idempotency(
        context, idempotency_key, "bind", request_fingerprint, body,
        command_identity=command_identity,
    )


def _append_reference_binding(
    session,
    context: TenantContext,
    entry_id: str,
    expected_revision: int | None,
    *,
    reference_version_id: str,
    purpose: str,
    viewpoint: str,
    priority: int = 0,
    note: str = "",
    conditions: dict | None = None,
    tolerate_existing: bool,
) -> tuple[LibraryDraftRecord, bool]:
    """把一个参考版本追加进产品/人物 Draft 的 ``reference_bindings``（§12 Bind）。

    ``expected_revision`` 为 ``None`` 时锁定 Draft 行并以其当前 revision 争夺下一
    版——服务端自动绑定（promote）没有浏览器 ETag 可带；显式命令仍必须给出。
    ``tolerate_existing`` 为真时，同一 slot 已存在不算冲突，返回 ``(draft, True)``
    且不改动 Draft；promote 的幂等重放靠它。
    """
    entry = _entry(session, context, entry_id)
    if entry.entry_type not in {ENTRY_PRODUCT, ENTRY_PERSON}:
        raise LibraryError("只有产品与人物条目可以 Bind 参考素材", code="library_bind_target_invalid")
    if entry.lifecycle == LIFECYCLE_ARCHIVED:
        raise LibraryConflict("条目已归档，不能建立新的参考绑定", code="entry_archived")
    target = session.get(LibraryVersionRecord, reference_version_id)
    if target is None or target.workspace_id != context.workspace_id:
        raise LibraryNotFound("参考版本不存在")
    if target.entry_type != ENTRY_REFERENCE_ASSET:
        raise LibraryError("Bind 目标必须是 ReferenceAssetVersion", code="library_bind_reference_invalid")
    draft = _draft(session, context, entry_id, for_update=expected_revision is None)
    content = dict(draft.content_json or {})
    bindings = list(content.get("reference_bindings") or [])
    slot = (reference_version_id, purpose, viewpoint)
    for existing in bindings:
        if isinstance(existing, dict) and (
            existing.get("reference_version_id"),
            existing.get("purpose"),
            existing.get("viewpoint"),
        ) == slot:
            if tolerate_existing:
                return draft, True
            raise LibraryConflict("同一 purpose/viewpoint 下已绑定该参考版本", code="library_reference_already_bound")
    bindings.append(
        {
            "reference_version_id": reference_version_id,
            "purpose": purpose,
            "viewpoint": viewpoint,
            "priority": int(priority),
            "note": note,
            "conditions": dict(conditions or {}),
        }
    )
    content["reference_bindings"] = bindings
    revision = draft.revision if expected_revision is None else expected_revision
    _claim_draft_revision(session, context, draft, revision, attempted_content=content)
    draft.content_json = content
    draft.state = DRAFT_EDITING
    draft.validation_json = None
    _emit(
        session,
        context,
        entry_id,
        "bound",
        payload={"reference_version_id": reference_version_id, "purpose": purpose, "viewpoint": viewpoint},
    )
    return draft, False


def fork_version(
    context: TenantContext,
    version_id: str,
    *,
    display_name: str,
    idempotency_key: str | None = None,
    request_fingerprint: str | None = None,
    command_identity: str | None = None,
) -> dict:
    """Fork：把一个 Published Version 复制成新条目的 Draft，之后独立演进（§12）。"""
    name = (display_name or "").strip()
    if not name:
        raise LibraryError("display_name 不能为空", code="library_display_name_required")

    def body(session, key_hash):
        source = _version(session, context, version_id)
        entry = LibraryEntryRecord(
            id=new_id("lib"),
            workspace_id=context.workspace_id,
            entry_type=source.entry_type,
            display_name=name,
            lifecycle=LIFECYCLE_ACTIVE,
        )
        session.add(entry)
        session.flush()
        _bridge_product_identity(session, context, entry)
        content = dict(source.content_json or {})
        content["name"] = name
        session.add(
            LibraryDraftRecord(
                id=new_id("libdraft"),
                workspace_id=context.workspace_id,
                entry_id=entry.id,
                content_json=content,
                revision=1,
                state=DRAFT_EDITING,
                origin_command=COMMAND_FORK,
                source_version_id=source.id,
            )
        )
        _emit(
            session,
            context,
            entry.id,
            "forked",
            to_state=DRAFT_EDITING,
            payload={"source_version_id": source.id, "source_entry_id": source.entry_id},
        )
        result = _entry_dict(session, context, entry)
        _audit(
            session,
            context,
            "fork",
            entry_id=entry.id,
            detail={
                "source_version_id": source.id,
                **_idem_detail(key_hash),
            },
        )
        return result

    return _run_with_idempotency(
        context, idempotency_key, "fork", request_fingerprint, body,
        command_identity=command_identity,
    )


def clone_product(
    context: TenantContext,
    version_id: str,
    *,
    display_name: str,
    include_optional: list[str] | None = None,
    idempotency_key: str | None = None,
    request_fingerprint: str | None = None,
    command_identity: str | None = None,
) -> dict:
    """Clone Product：安全默认只复制低风险内容（§5.2、§12）。

    Offer、价格、DataSource Mapping 和 Landing Page 是独立稳定身份，
    Clone Product 永不携带；``include_optional`` 只能追加白名单内的可选字段。
    """
    name = (display_name or "").strip()
    if not name:
        raise LibraryError("display_name 不能为空", code="library_display_name_required")
    requested = set(include_optional or [])
    forbidden = requested & CLONE_PRODUCT_NEVER_COPIED
    if forbidden:
        raise LibraryError(f"Clone Product 不复制这些内容，必须另行创建: {sorted(forbidden)}", code="library_clone_field_forbidden")
    unknown = requested - CLONE_PRODUCT_OPTIONAL_KEYS
    if unknown:
        raise LibraryError(f"不可选择的字段: {sorted(unknown)}", code="library_clone_field_unknown")

    def body(session, key_hash):
        source = _version(session, context, version_id)
        if source.entry_type != ENTRY_PRODUCT:
            raise LibraryError("Clone Product 只适用于 ProductVersion", code="library_clone_source_invalid")
        source_content = dict(source.content_json or {})
        content = {k: v for k, v in source_content.items() if k in CLONE_PRODUCT_SAFE_KEYS}
        for key in requested:
            if key in source_content:
                content[key] = source_content[key]
        content["name"] = name

        entry = LibraryEntryRecord(
            id=new_id("lib"),
            workspace_id=context.workspace_id,
            entry_type=ENTRY_PRODUCT,
            display_name=name,
            lifecycle=LIFECYCLE_ACTIVE,
        )
        session.add(entry)
        session.flush()
        _bridge_product_identity(session, context, entry)
        session.add(
            LibraryDraftRecord(
                id=new_id("libdraft"),
                workspace_id=context.workspace_id,
                entry_id=entry.id,
                content_json=content,
                revision=1,
                state=DRAFT_EDITING,
                origin_command=COMMAND_CLONE,
                source_version_id=source.id,
            )
        )
        skipped = sorted(set(source_content) - set(content))
        _emit(
            session,
            context,
            entry.id,
            "cloned",
            to_state=DRAFT_EDITING,
            payload={"source_version_id": source.id, "included_optional": sorted(requested), "skipped": skipped},
        )
        result = _entry_dict(session, context, entry)
        result["clone"] = {"source_version_id": source.id, "skipped_fields": skipped}
        _audit(
            session,
            context,
            "clone",
            entry_id=entry.id,
            detail={
                "source_version_id": source.id,
                "included_optional": sorted(requested),
                "skipped": skipped,
                **_idem_detail(key_hash),
            },
        )
        return result

    return _run_with_idempotency(
        context, idempotency_key, "clone", request_fingerprint, body,
        command_identity=command_identity,
    )


# --------------------------------------------------------------------------
# 参考素材上传（契约 §7.2）
# --------------------------------------------------------------------------


def upload_reference_object(
    context: TenantContext,
    *,
    raw: bytes,
    declared_mime: str,
    filename: str = "",
    execution_mode: str = "",
    safety_scenario: str = "",
    idempotency_key: str | None = None,
    request_fingerprint: str | None = None,
    command_identity: str | None = None,
) -> dict:
    """上传 → quarantine → 全部门禁 → 受控派生。"""

    def body(session, key_hash):
        _require_workspace(session, context.workspace_id)
        object_id: str | None = None
        reused = False
        try:
            record, reused = references.quarantine_upload(
                session, context, declared_mime=declared_mime, raw=raw, filename=filename
            )
            object_id = record.id
            released = references.run_gate(
                session, context, object_id, execution_mode=execution_mode, safety_scenario=safety_scenario
            )
        except references.ReferenceGateError as exc:
            _audit(
                session, context, "upload_reference", result="blocked",
                detail={
                    "object_id": object_id,
                    "reason": str(exc),
                    "declared_mime": (declared_mime or "").strip().lower(),
                    "byte_size": len(raw or b""),
                    "filename": Path(filename or "").name,
                    "content_sha256": references.sha256_bytes(raw) if raw else "",
                    **_idem_detail(key_hash),
                },
            )
            return _TerminalFailure(exc)
        facts = references.object_facts(released)
        facts["reused_object"] = reused
        _audit(
            session, context, "upload_reference",
            detail={
                "object_id": object_id,
                "reused_object": reused,
                "sha256": released.content_sha256,
                **_idem_detail(key_hash),
            },
        )
        return facts

    return _run_with_idempotency(
        context, idempotency_key, "upload_reference", request_fingerprint, body,
        command_identity=command_identity,
    )


#: 服务端自动绑定时的默认用途/视角：产品内容校验要求两者都是非空字符串
#: （contracts._validate_reference_bindings），浏览器可以在 promote 请求里覆盖。
PROMOTE_DEFAULT_PURPOSE = "product_reference"
PROMOTE_DEFAULT_VIEWPOINT = "unspecified"
_RIGHTS_SCOPE_MAX = 200


def normalize_rights_declaration(
    declaration: dict | None, *, declared_by: str, today: date | None = None
) -> dict | None:
    """把上传/promote 时的权利声明规范成 ReferenceAssetVersion.rights 的形状。

    ``state`` 永远是 ``granted``（声明本身就是操作者对持有权利的断言），
    ``declared_by`` 永远由服务端写入当前发起人，客户端不能替别人声明。
    返回 ``None`` 表示没有声明；形状或有效期不合法时抛 ``LibraryError``。
    """
    if declaration is None:
        return None
    if not isinstance(declaration, dict):
        raise LibraryError("rights 必须是对象", code="library_rights_invalid")
    scope = str(declaration.get("scope") or "").strip()
    if not scope:
        raise LibraryError("rights.scope 不能为空", code="library_rights_scope_required")
    if len(scope) > _RIGHTS_SCOPE_MAX:
        raise LibraryError(f"rights.scope 不能超过 {_RIGHTS_SCOPE_MAX} 个字符", code="library_rights_scope_too_long",
                           params={"limit": _RIGHTS_SCOPE_MAX})
    valid_to = str(declaration.get("valid_to") or "").strip()
    normalized = {
        "state": "granted",
        "scope": scope,
        "valid_to": valid_to,
        "declared_by": declared_by or "",
        "declared_at": utcnow().isoformat(),
    }
    evidence = declaration.get("evidence")
    if evidence not in (None, ""):
        normalized["evidence"] = str(evidence)[:1000]
    reason = check_rights_eligible(normalized, today=today)
    if reason is not None:
        raise LibraryError(f"权利声明无效: {reason}", code="library_rights_invalid")
    return normalized


def promote_reference_object(
    context: TenantContext,
    object_id: str,
    *,
    display_name: str | None = None,
    rights: dict | None = None,
    bind: dict | None = None,
    today: date | None = None,
    idempotency_key: str | None = None,
    request_fingerprint: str | None = None,
    command_identity: str | None = None,
) -> dict:
    """把一个已放行的参考对象发布为 ReferenceAssetVersion，并可选绑定到产品/人物。

    内容全部由服务端从门禁证据生成（object_id、file、gate_checks、derivative、
    execution_mode），用户只提供名称与权利声明——所以这里不需要 raw JSON：
    ``reference_asset`` 不在 ``OWNER_ONLY_PUBLISH`` 内（契约 §4 Member 可发布
    参考），且没有任何客户端自写的生成语义进入版本。Owner 与 Member 都可以
    promote；Workspace 作用域照常。

    幂等：同一对象第二次 promote 回到 ``promoted_entry_id`` 指向的条目与其当前
    生效版本（不发布第二份）；``bind`` 已存在同一 slot 时不再追加。
    """
    requested_name = (display_name or "").strip()
    bind_spec = dict(bind or {}) if bind else None
    if bind_spec is not None and not str(bind_spec.get("entry_id") or "").strip():
        raise LibraryError("bind.entry_id 不能为空", code="library_bind_entry_required")

    def body(session, key_hash):
        audit_base = _idem_detail(key_hash)
        _require_workspace(session, context.workspace_id)
        record = session.get(ReferenceObjectRecord, object_id)
        if record is None or record.workspace_id != context.workspace_id:
            # 跨 Workspace 猜测 ID 不泄露存在性。
            raise LibraryNotFound("参考对象不存在", code="reference_object_not_found")
        if record.state != references.STATE_RELEASED:
            failure_code, failure_detail = references.gate_failure(record)
            raise LibraryConflict(
                "参考对象尚未通过上传门禁，不能发布为参考素材",
                {
                    "code": "reference_object_not_released",
                    "state": record.state,
                    "failure_code": failure_code,
                    "failure_detail": failure_detail,
                },
            )

        meta = dict(record.checks_json or {})
        declaration = normalize_rights_declaration(
            rights, declared_by=context.initiator_id or "", today=today,
        ) if rights is not None else meta.get("rights_declaration")
        if not isinstance(declaration, dict):
            report = ValidationReport(
                ENTRY_REFERENCE_ASSET,
                "",
                [
                    Finding(
                        "reference_rights_required",
                        "参考素材必须带有上传时或 promote 时的权利声明（scope、valid_to）",
                        "rights",
                    )
                ],
            ).as_dict()
            _audit(session, context, "promote_reference", result="blocked",
                   detail={"object_id": record.id, "reason": "reference_rights_required",
                           **audit_base})
            return _TerminalFailure(
                LibraryValidationError(
                    "缺少权利声明，不能发布参考素材", report, code="library_reference_rights_required",
                ),
            )

        filename = str(meta.get("filename") or "")
        name = requested_name or Path(filename).stem.strip() or f"参考 {record.id[-8:]}"
        created = False
        entry = None
        if record.promoted_entry_id:
            entry = session.get(LibraryEntryRecord, record.promoted_entry_id)
            if entry is not None and entry.workspace_id != context.workspace_id:
                entry = None
        if entry is not None and entry.lifecycle == LIFECYCLE_ARCHIVED:
            raise LibraryConflict(
                "该参考素材条目已归档，不能再次发布或绑定",
                {"code": "entry_archived", "entry_id": entry.id},
            )

        if entry is None:
            content = {
                "name": name,
                "object_id": record.id,
                "source": "upload",
                "file": {
                    "content_sha256": record.content_sha256,
                    "mime_type": record.mime_type,
                    "byte_size": record.byte_size,
                    "width": record.width,
                    "height": record.height,
                    "filename": Path(filename).name,
                },
                "rights": dict(declaration),
                "gate_checks": list(meta.get("checks") or []),
                "derivative": {
                    "sha256": record.derivative_sha256,
                    "mime_type": record.derivative_mime,
                    "byte_size": record.derivative_byte_size,
                    "width": record.derivative_width,
                    "height": record.derivative_height,
                },
                "execution_mode": record.execution_mode,
            }
            entry, draft = _create_entry_with_draft(
                session, context, entry_type=ENTRY_REFERENCE_ASSET,
                display_name=name, content=content,
            )
            report, resolved = _build_report(session, context, entry, content, today=today)
            if not report.ok:
                # 服务端生成的内容仍走同一套校验：权利过期、对象证据漂移都在这里
                # 失败关闭，并把 ValidationReport 作为证据留在 Draft 与审计里。
                draft.state = DRAFT_VALIDATION_FAILED
                draft.validation_json = report.as_dict()
                _emit(session, context, entry.id, "validated", to_state=DRAFT_VALIDATION_FAILED)
                _audit(session, context, "promote_reference", entry_id=entry.id, result="blocked",
                       detail={"object_id": record.id, **report.as_dict(), **audit_base})
                return _TerminalFailure(LibraryValidationError("参考素材发布被阻断项拒绝", report.as_dict()))
            version_dict = _publish_effective_version(
                session, context, entry, draft, content, report, resolved, set(), draft.revision,
                extra_audit_detail={"object_id": record.id, "promoted": True, **audit_base},
            )
            record.promoted_entry_id = entry.id
            record.updated_at = utcnow()
            created = True
        else:
            current = _effective_version(session, entry)
            if current is None:
                raise LibraryConflict(
                    "该参考素材条目没有生效版本（可能已被 Withdraw），不能绑定",
                    {"code": "reference_version_unavailable", "entry_id": entry.id},
                )
            version_dict = _version_dict(current)

        binding_result = None
        if bind_spec is not None:
            purpose = str(bind_spec.get("purpose") or "").strip() or PROMOTE_DEFAULT_PURPOSE
            viewpoint = str(bind_spec.get("viewpoint") or "").strip() or PROMOTE_DEFAULT_VIEWPOINT
            expected = bind_spec.get("expected_revision")
            draft_record, already_bound = _append_reference_binding(
                session, context, str(bind_spec["entry_id"]),
                int(expected) if expected is not None else None,
                reference_version_id=version_dict["id"],
                purpose=purpose[:100], viewpoint=viewpoint[:100],
                priority=int(bind_spec.get("priority") or 0),
                note=str(bind_spec.get("note") or ""),
                conditions=dict(bind_spec.get("conditions") or {}),
                tolerate_existing=True,
            )
            binding_result = {
                "entry_id": draft_record.entry_id,
                "draft_revision": draft_record.revision,
                "purpose": purpose[:100],
                "viewpoint": viewpoint[:100],
                "already_bound": already_bound,
            }

        _audit(
            session, context, "promote_reference",
            entry_id=entry.id, version_id=version_dict["id"],
            detail={
                "object_id": record.id,
                "created": created,
                "rights_scope": declaration.get("scope"),
                "rights_valid_to": declaration.get("valid_to"),
                "bound_entry_id": binding_result["entry_id"] if binding_result else None,
                "purpose": binding_result["purpose"] if binding_result else None,
                "viewpoint": binding_result["viewpoint"] if binding_result else None,
                **audit_base,
            },
        )
        return {
            "object_id": record.id,
            "entry_id": entry.id,
            "version_id": version_dict["id"],
            "version_no": version_dict["version_no"],
            "display_name": entry.display_name,
            "created": created,
            "binding": binding_result,
        }

    return _run_with_idempotency(
        context, idempotency_key, "promote_reference", request_fingerprint, body,
        command_identity=command_identity,
    )


def retry_rejected_original_purge(context: TenantContext, *, object_id: str | None = None) -> dict:
    """重试清理被拒绝对象的原件（§7.2、§15）。

    针对 ``state='rejected' AND original_purged=False`` 的记录——也就是当初
    删除文件失败、原件仍滞留在对象存储里的那些。成功则更新记录；再次失败则
    保留同样可查询、可告警的状态，并把尝试次数与错误写进审计，绝不让文件
    无记录地永久滞留。
    """
    with tenant_session(context) as session:
        _require_workspace(session, context.workspace_id)
        stmt = select(ReferenceObjectRecord).where(
            ReferenceObjectRecord.workspace_id == context.workspace_id,
            ReferenceObjectRecord.state == references.STATE_REJECTED,
            ReferenceObjectRecord.original_purged.is_(False),
        )
        if object_id:
            stmt = stmt.where(ReferenceObjectRecord.id == object_id)
        pending = session.scalars(stmt).all()

        purged: list[str] = []
        still_pending: list[dict] = []
        for record in pending:
            ok, error = references.purge_original(record)
            if ok:
                purged.append(record.id)
                _audit(
                    session,
                    context,
                    "purge_rejected_original",
                    detail={"object_id": record.id, "content_sha256": record.content_sha256},
                )
            else:
                attempts = int((record.checks_json or {}).get("purge", {}).get("attempts") or 0)
                still_pending.append({"object_id": record.id, "attempts": attempts, "error": error})
                _audit(
                    session,
                    context,
                    "purge_rejected_original",
                    result="blocked",
                    detail={"object_id": record.id, "attempts": attempts, "error": error},
                )
        session.flush()
        return {"purged": purged, "still_pending": still_pending}


def provider_reference_payload(context: TenantContext, version_id: str) -> dict:
    """给 Provider 的参考素材：只返回受控派生，原件与 quarantine 对象永不外发。"""
    with tenant_session(context) as session:
        version = _version(session, context, version_id)
        if version.entry_type != ENTRY_REFERENCE_ASSET:
            raise LibraryError("只有 ReferenceAssetVersion 可以解析成 Provider 载荷", code="library_bind_reference_invalid")
        if version.state == VERSION_WITHDRAWN or version.eligibility == ELIGIBILITY_BLOCKED:
            raise LibraryConflict("该参考版本已被 Withdraw 或资格为 blocked，不能发出外部调用", code="library_reference_version_unusable")
        content = version.content_json or {}
        rights = content.get("rights") or {}
        reason = check_rights_eligible(rights)
        if reason is not None:
            raise LibraryConflict(f"参考素材不能发出外部调用: {reason}", code="library_reference_rights_ineligible")
        object_id = str(content.get("object_id") or "")
        record = session.get(ReferenceObjectRecord, object_id)
        if record is None or record.workspace_id != context.workspace_id:
            raise LibraryNotFound("参考对象不存在", code="reference_object_not_found")
        return {
            "version_id": version.id,
            "object_key": references.provider_payload_path(record),
            "mime_type": record.derivative_mime,
            "width": record.derivative_width,
            "height": record.derivative_height,
            "sha256": record.derivative_sha256,
            "execution_mode": record.execution_mode,
        }


# --------------------------------------------------------------------------
# 读取投影
# --------------------------------------------------------------------------


def _draft_dict(draft: LibraryDraftRecord) -> dict:
    return {
        "id": draft.id,
        "entry_id": draft.entry_id,
        "revision": draft.revision,
        "state": draft.state,
        "content_schema_version": draft.content_schema_version,
        "base_version_id": draft.base_version_id,
        "source_version_id": draft.source_version_id,
        "origin_command": draft.origin_command,
        "content": draft.content_json,
        "validation": draft.validation_json,
    }


def _version_dict(version: LibraryVersionRecord) -> dict:
    known = CONTENT_SCHEMA_VERSIONS.get(version.entry_type, 1)
    stored = version.content_schema_version or 1
    if stored > known:
        content = {"_unsupported": True, "_schema_version": stored, "_known_version": known}
    else:
        content = version.content_json
    return {
        "id": version.id,
        "entry_id": version.entry_id,
        "entry_type": version.entry_type,
        "version_no": version.version_no,
        "state": version.state,
        "eligibility": version.eligibility,
        "content_schema_version": stored,
        "lifecycle_revision": version.lifecycle_revision,
        "content": content,
        "content_hash": version.content_hash,
        "origin_command": version.origin_command,
        "source_version_id": version.source_version_id,
        "base_version_id": version.base_version_id,
        "published_by": version.published_by,
        "published_at": version.published_at.isoformat() if version.published_at else None,
        "withdraw_reason": version.withdraw_reason,
    }


def _entry_dict(session, context: TenantContext, entry: LibraryEntryRecord) -> dict:
    versions = session.scalars(
        select(LibraryVersionRecord)
        .where(LibraryVersionRecord.entry_id == entry.id)
        .order_by(LibraryVersionRecord.version_no)
    ).all()
    draft = session.scalars(
        select(LibraryDraftRecord).where(LibraryDraftRecord.entry_id == entry.id)
    ).one_or_none()
    return {
        "id": entry.id,
        "entry_type": entry.entry_type,
        "display_name": entry.display_name,
        "lifecycle": entry.lifecycle,
        "current_version_id": entry.current_version_id,
        "draft": _draft_dict(draft) if draft is not None else None,
        "versions": [_version_dict(v) for v in versions],
    }


def get_entry(context: TenantContext, entry_id: str) -> dict:
    with tenant_session(context) as session:
        return _entry_dict(session, context, _entry(session, context, entry_id))


def get_entry_type(context: TenantContext, entry_id: str) -> str:
    """Tenant-scoped entry type lookup for boundary decisions.

    Raises ``LibraryNotFound`` for an unknown or cross-Workspace entry, so a
    caller never learns the type of an entry it could not read.
    """
    with tenant_session(context) as session:
        return _entry(session, context, entry_id).entry_type


def get_version(context: TenantContext, version_id: str) -> dict:
    with tenant_session(context) as session:
        return _version_dict(_version(session, context, version_id))


def list_entries(context: TenantContext, *, entry_type: str | None = None, include_archived: bool = False) -> list[dict]:
    with tenant_session(context) as session:
        stmt = (
            select(
                LibraryEntryRecord,
                LibraryDraftRecord.revision.label("draft_revision"),
                LibraryDraftRecord.state.label("draft_state"),
                LibraryVersionRecord.version_no.label("current_version_no"),
                LibraryVersionRecord.state.label("current_version_state"),
            )
            .outerjoin(LibraryDraftRecord, LibraryDraftRecord.entry_id == LibraryEntryRecord.id)
            .outerjoin(
                LibraryVersionRecord,
                (LibraryVersionRecord.id == LibraryEntryRecord.current_version_id)
                & (LibraryVersionRecord.workspace_id == LibraryEntryRecord.workspace_id),
            )
            .where(LibraryEntryRecord.workspace_id == context.workspace_id)
        )
        if entry_type:
            stmt = stmt.where(LibraryEntryRecord.entry_type == entry_type)
        if not include_archived:
            stmt = stmt.where(LibraryEntryRecord.lifecycle == LIFECYCLE_ACTIVE)
        rows = session.execute(stmt.order_by(LibraryEntryRecord.created_at)).all()
        return [
            {
                "id": row.LibraryEntryRecord.id,
                "entry_type": row.LibraryEntryRecord.entry_type,
                "display_name": row.LibraryEntryRecord.display_name,
                "lifecycle": row.LibraryEntryRecord.lifecycle,
                "current_version_id": row.LibraryEntryRecord.current_version_id,
                "current_version_no": row.current_version_no,
                "current_version_state": row.current_version_state,
                "draft_revision": row.draft_revision,
                "draft_state": row.draft_state,
                "updated_at": row.LibraryEntryRecord.updated_at.isoformat() if row.LibraryEntryRecord.updated_at else None,
            }
            for row in rows
        ]


_EVENT_PAYLOAD_ALLOWLIST = frozenset({
    "version_no", "content_hash", "entry_type", "display_name",
    "command_kind", "origin_command", "source_version_id", "base_version_id",
    "source_entry_id", "included_optional",
    "reason", "withdraw_reason", "withdrawn_version_id", "superseded_by",
    "eligibility", "lifecycle", "validation_ok",
    "blocking", "warnings", "blocking_count", "warning_count",
    "revision", "reference_version_id", "purpose", "viewpoint",
    "downstream_event_contract", "impact", "affected_entities",
    "affected_version_id", "affected_entry_id", "affected_entry_type",
    "previous_eligibility", "new_eligibility",
    "previous_state", "new_state",
})


def _sanitize_event_payload(payload: dict) -> dict:
    return {k: v for k, v in payload.items() if k in _EVENT_PAYLOAD_ALLOWLIST}


def list_events(context: TenantContext, entry_id: str) -> list[dict]:
    with tenant_session(context) as session:
        _entry(session, context, entry_id)
        rows = session.scalars(
            select(LibraryEventRecord)
            .where(LibraryEventRecord.entry_id == entry_id)
            .order_by(LibraryEventRecord.sequence)
        ).all()
        return [
            {
                "id": row.id,
                "sequence": row.sequence,
                "event_type": row.event_type,
                "version_id": row.version_id,
                "from_state": row.from_state,
                "to_state": row.to_state,
                "initiator_id": row.initiator_id,
                "executor": row.executor,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "payload": _sanitize_event_payload(row.payload_json),
            }
            for row in rows
        ]


def list_bindings(context: TenantContext, owner_version_id: str) -> list[dict]:
    with tenant_session(context) as session:
        _version(session, context, owner_version_id)
        rows = session.scalars(
            select(ReferenceBindingRecord)
            .where(
                ReferenceBindingRecord.owner_version_id == owner_version_id,
                ReferenceBindingRecord.workspace_id == context.workspace_id,
            )
            .order_by(ReferenceBindingRecord.position)
        ).all()
        result = []
        for row in rows:
            reference_version = session.get(LibraryVersionRecord, row.reference_version_id)
            reference_entry = (
                session.get(LibraryEntryRecord, reference_version.entry_id)
                if reference_version is not None
                and reference_version.workspace_id == context.workspace_id
                else None
            )
            object_id = (
                (reference_version.content_json or {}).get("object_id")
                if reference_version is not None
                else None
            )
            reference_object = (
                session.get(ReferenceObjectRecord, object_id) if object_id else None
            )
            result.append({
                "id": row.id,
                "owner_version_id": row.owner_version_id,
                "reference_version_id": row.reference_version_id,
                "purpose": row.purpose,
                "viewpoint": row.viewpoint,
                "priority": row.priority,
                "note": row.note,
                "conditions": row.conditions_json,
                "reference_display_name": (
                    reference_entry.display_name if reference_entry is not None else ""
                ),
                "gate_status": (
                    reference_object.state
                    if reference_object is not None
                    and reference_object.workspace_id == context.workspace_id
                    else ""
                ),
            })
        return result
