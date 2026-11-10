"""Server-computed impact projections for Publish, Withdraw, and Archive.

The browser requests an impact projection before executing a destructive
command.  The matching command verifies ``impact_id`` + ``impact_hash``
atomically: stale, expired, consumed, wrong-command, wrong-actor,
wrong-Workspace, wrong-revision, or mismatched-hash projections fail
closed with a stable code.

Impact projections are short-lived (5 minutes) and single-use.  The
projection and the command share the same dependency rules — the UI
never patches or recomputes the impact.

Every preview mints its own record and its own ``impact_hash``: the hash
covers the projection *and* the record id, so two previews of the same
unchanged draft (the dialog opened twice, a retry after the first token
expired, a second attempt after a failed command) are two distinct
single-use tokens rather than a ``UniqueViolation`` on
``(workspace_id, impact_hash)``.  Whether the underlying data changed is
judged by :func:`verify_and_consume_impact`'s revision and content-hash
checks, not by recomputing the projection.
"""
from __future__ import annotations

import hashlib as _hashlib
import json as _json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.platform.tenancy import TenantContext

from ..adapters.persistence.database import tenant_session
from ..adapters.persistence.models import (
    LibraryCommandImpactRecord,
    LibraryDraftRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    new_id,
    utcnow,
)
from .contracts import (
    ELIGIBILITY_BLOCKED,
    LIFECYCLE_ARCHIVED,
    VERSION_EFFECTIVE,
    VERSION_SUPERSEDED,
    VERSION_WITHDRAWN,
    validate_content,
)
from .dependents import dependent_versions

IMPACT_TTL = timedelta(minutes=5)

COMMAND_PUBLISH = "publish"
COMMAND_WITHDRAW = "withdraw"
COMMAND_ARCHIVE = "archive"


class ImpactVerificationError(Exception):
    def __init__(self, message: str, *, code: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


def _canonical_json(obj: object) -> str:
    return _json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _hash_projection(projection: dict, *, nonce: str) -> str:
    """Token for one issued projection: the projection bound to its record id."""
    return _hashlib.sha256(
        f"{_canonical_json(projection)}:{nonce}".encode()
    ).hexdigest()


def _mint_impact(
    session,
    context: TenantContext,
    *,
    command_kind: str,
    target_entry_id: str,
    target_version_id: str | None,
    target_revision: int | None,
    content_hash: str,
    projection: dict,
) -> LibraryCommandImpactRecord:
    """Persist one single-use impact token for ``projection``."""
    now = utcnow()
    impact_id = new_id("impact")
    record = LibraryCommandImpactRecord(
        id=impact_id,
        workspace_id=context.workspace_id,
        actor_id=context.initiator_id or "",
        command_kind=command_kind,
        target_entry_id=target_entry_id,
        target_version_id=target_version_id,
        target_revision=target_revision,
        content_hash=content_hash,
        impact_hash=_hash_projection(projection, nonce=impact_id),
        projection_json=projection,
        consumption_state="available",
        expires_at=now + IMPACT_TTL,
        created_at=now,
    )
    session.add(record)
    session.flush()
    return record


# ---------------------------------------------------------------------------
# Lightweight lookups (avoid circular import with service.py)
# ---------------------------------------------------------------------------

def _entry(session, context: TenantContext, entry_id: str) -> LibraryEntryRecord:
    from .service import LibraryNotFound
    record = session.get(LibraryEntryRecord, entry_id)
    if record is None or record.workspace_id != context.workspace_id:
        raise LibraryNotFound("创意库条目不存在")
    return record


def _version(session, context: TenantContext, version_id: str) -> LibraryVersionRecord:
    from .service import LibraryNotFound
    record = session.get(LibraryVersionRecord, version_id)
    if record is None or record.workspace_id != context.workspace_id:
        raise LibraryNotFound("创意库版本不存在")
    return record


def _draft(session, context: TenantContext, entry_id: str) -> LibraryDraftRecord:
    from .service import LibraryNotFound
    record = session.scalars(
        select(LibraryDraftRecord).where(
            LibraryDraftRecord.entry_id == entry_id,
            LibraryDraftRecord.workspace_id == context.workspace_id,
        )
    ).one_or_none()
    if record is None:
        raise LibraryNotFound("该条目没有 Draft")
    return record


def _effective_version(session, entry: LibraryEntryRecord) -> LibraryVersionRecord | None:
    return session.scalars(
        select(LibraryVersionRecord).where(
            LibraryVersionRecord.entry_id == entry.id,
            LibraryVersionRecord.state == VERSION_EFFECTIVE,
        )
    ).one_or_none()


def _build_report(session, context, entry, content, *, today=None):
    from .service import _build_report as _svc_build_report
    return _svc_build_report(session, context, entry, content, today=today)


# ---------------------------------------------------------------------------
# Publish impact
# ---------------------------------------------------------------------------


def preview_publish_impact(
    context: TenantContext,
    entry_id: str,
) -> dict:
    from .service import LibraryConflict

    with tenant_session(context) as session:
        entry = _entry(session, context, entry_id)
        if entry.lifecycle == LIFECYCLE_ARCHIVED:
            raise LibraryConflict("条目已归档，不能发布新版本", code="entry_archived")
        draft = _draft(session, context, entry_id)

        content = dict(draft.content_json or {})
        report, _resolved = _build_report(session, context, entry, content)

        blocking = [f.as_dict() for f in report.findings if f.severity != "warning"]
        warnings = [f.as_dict() for f in report.warnings]

        current = _effective_version(session, entry)
        current_summary = None
        if current is not None:
            current_summary = {
                "version_id": current.id,
                "version_no": current.version_no,
                "state": current.state,
                "content_hash": current.content_hash,
            }

        projection = {
            "command_kind": COMMAND_PUBLISH,
            "target_entry_id": entry.id,
            "entry_type": entry.entry_type,
            "display_name": entry.display_name,
            "draft_revision": draft.revision,
            "content_hash": report.content_hash,
            "validation_ok": report.ok,
            "blocking": blocking,
            "warnings": warnings,
            "current_effective_version": current_summary,
            "will_supersede": current is not None,
        }

        record = _mint_impact(
            session,
            context,
            command_kind=COMMAND_PUBLISH,
            target_entry_id=entry.id,
            target_version_id=None,
            target_revision=draft.revision,
            content_hash=report.content_hash,
            projection=projection,
        )

        return {
            "impact_id": record.id,
            "impact_hash": record.impact_hash,
            "command_kind": COMMAND_PUBLISH,
            "target_entry_id": entry.id,
            "target_revision": draft.revision,
            "content_hash": report.content_hash,
            "expires_at": record.expires_at.isoformat(),
            "projection": projection,
        }


# ---------------------------------------------------------------------------
# Withdraw impact
# ---------------------------------------------------------------------------


def preview_withdraw_impact(
    context: TenantContext,
    version_id: str,
) -> dict:
    from .service import LibraryConflict

    with tenant_session(context) as session:
        version = _version(session, context, version_id)
        if version.state == VERSION_WITHDRAWN:
            raise LibraryConflict(
                "版本已撤回",
                {"current_state": version.state},
                code="library_version_withdrawn",
            )
        if version.state not in (VERSION_EFFECTIVE, VERSION_SUPERSEDED):
            raise LibraryConflict(
                f"只有 effective 或 superseded 状态的版本可以 Withdraw，当前为 {version.state!r}",
                {"current_state": version.state},
                code="library_version_not_withdrawable",
            )

        entry = _entry(session, context, version.entry_id)
        dependents = dependent_versions(session, context, {version.id})
        dependent_summaries = [
            {
                "version_id": d.id,
                "entry_id": d.entry_id,
                "entry_type": d.entry_type,
                "version_no": d.version_no,
                "state": d.state,
                "eligibility": d.eligibility,
                "will_block": d.eligibility != ELIGIBILITY_BLOCKED,
            }
            for d in sorted(dependents, key=lambda v: (v.entry_type, v.version_no))
        ]

        is_current = entry.current_version_id == version.id

        projection = {
            "command_kind": COMMAND_WITHDRAW,
            "target_entry_id": entry.id,
            "target_version_id": version.id,
            "entry_type": entry.entry_type,
            "display_name": entry.display_name,
            "version_no": version.version_no,
            "version_state": version.state,
            "lifecycle_revision": getattr(version, "lifecycle_revision", 1),
            "is_current_effective": is_current,
            "will_clear_current_version": is_current,
            "dependents": dependent_summaries,
            "dependent_count": len(dependent_summaries),
            "newly_blocked_count": sum(1 for d in dependent_summaries if d["will_block"]),
        }

        record = _mint_impact(
            session,
            context,
            command_kind=COMMAND_WITHDRAW,
            target_entry_id=entry.id,
            target_version_id=version.id,
            target_revision=getattr(version, "lifecycle_revision", 1),
            content_hash=version.content_hash or "",
            projection=projection,
        )

        return {
            "impact_id": record.id,
            "impact_hash": record.impact_hash,
            "command_kind": COMMAND_WITHDRAW,
            "target_entry_id": entry.id,
            "target_version_id": version.id,
            "target_revision": record.target_revision,
            "content_hash": version.content_hash or "",
            "expires_at": record.expires_at.isoformat(),
            "projection": projection,
        }


# ---------------------------------------------------------------------------
# Archive impact
# ---------------------------------------------------------------------------


def preview_archive_impact(
    context: TenantContext,
    entry_id: str,
) -> dict:
    from .service import LibraryConflict

    with tenant_session(context) as session:
        entry = _entry(session, context, entry_id)
        if entry.lifecycle == LIFECYCLE_ARCHIVED:
            raise LibraryConflict("条目已归档", code="entry_archived")
        draft = _draft(session, context, entry_id)

        current = _effective_version(session, entry)
        current_summary = None
        if current is not None:
            current_summary = {
                "version_id": current.id,
                "version_no": current.version_no,
                "state": current.state,
                "content_hash": current.content_hash,
                "eligibility": current.eligibility,
            }

        all_versions = session.scalars(
            select(LibraryVersionRecord).where(
                LibraryVersionRecord.entry_id == entry.id,
                LibraryVersionRecord.workspace_id == context.workspace_id,
            )
        ).all()

        version_summaries = [
            {
                "version_id": v.id,
                "version_no": v.version_no,
                "state": v.state,
                "eligibility": v.eligibility,
            }
            for v in sorted(all_versions, key=lambda v: v.version_no)
        ]

        all_version_ids = {v.id for v in all_versions}
        dependents = dependent_versions(session, context, all_version_ids) if all_version_ids else []
        dependent_summaries = [
            {
                "version_id": d.id,
                "entry_id": d.entry_id,
                "entry_type": d.entry_type,
                "version_no": d.version_no,
                "state": d.state,
                "eligibility": d.eligibility,
            }
            for d in sorted(dependents, key=lambda v: (v.entry_type, v.version_no))
            if d.entry_id != entry.id
        ]

        projection = {
            "command_kind": COMMAND_ARCHIVE,
            "target_entry_id": entry.id,
            "entry_type": entry.entry_type,
            "display_name": entry.display_name,
            "draft_revision": draft.revision,
            "current_effective_version": current_summary,
            "versions": version_summaries,
            "version_count": len(version_summaries),
            "dependents": dependent_summaries,
            "dependent_count": len(dependent_summaries),
            "draft_editing_loss": draft.state not in ("editing",) or bool(draft.content_json),
        }

        record = _mint_impact(
            session,
            context,
            command_kind=COMMAND_ARCHIVE,
            target_entry_id=entry.id,
            target_version_id=None,
            target_revision=draft.revision,
            content_hash="",
            projection=projection,
        )

        return {
            "impact_id": record.id,
            "impact_hash": record.impact_hash,
            "command_kind": COMMAND_ARCHIVE,
            "target_entry_id": entry.id,
            "target_revision": draft.revision,
            "content_hash": "",
            "expires_at": record.expires_at.isoformat(),
            "projection": projection,
        }


# ---------------------------------------------------------------------------
# Impact verification (called by commands before execution)
# ---------------------------------------------------------------------------


def verify_and_consume_impact(
    session,
    context: TenantContext,
    *,
    impact_id: str,
    impact_hash: str,
    command_kind: str,
    target_entry_id: str,
    target_version_id: str | None = None,
    target_revision: int | None = None,
    content_hash: str | None = None,
) -> LibraryCommandImpactRecord:
    record = session.get(LibraryCommandImpactRecord, impact_id)

    if record is None or record.workspace_id != context.workspace_id:
        raise ImpactVerificationError(
            "Impact projection not found",
            code="impact_not_found",
        )

    if record.actor_id != (context.initiator_id or ""):
        raise ImpactVerificationError(
            "Impact projection belongs to a different actor",
            code="impact_wrong_actor",
        )

    if record.command_kind != command_kind:
        raise ImpactVerificationError(
            f"Impact projection is for {record.command_kind!r}, not {command_kind!r}",
            code="impact_wrong_command",
        )

    if record.target_entry_id != target_entry_id:
        raise ImpactVerificationError(
            "Impact projection targets a different entry",
            code="impact_wrong_target",
        )

    if target_version_id is not None and record.target_version_id != target_version_id:
        raise ImpactVerificationError(
            "Impact projection targets a different version",
            code="impact_wrong_version",
        )

    if record.consumption_state == "consumed":
        raise ImpactVerificationError(
            "Impact projection has already been consumed",
            code="impact_consumed",
        )

    now = utcnow()
    expires_at = record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if record.consumption_state == "expired" or expires_at <= now:
        if record.consumption_state != "expired":
            record.consumption_state = "expired"
        raise ImpactVerificationError(
            "Impact projection has expired",
            code="impact_expired",
        )

    if record.impact_hash != impact_hash:
        raise ImpactVerificationError(
            "Impact hash mismatch — underlying data has changed",
            code="impact_hash_mismatch",
        )

    if target_revision is not None and record.target_revision != target_revision:
        raise ImpactVerificationError(
            "Target revision has changed since impact was computed",
            code="impact_revision_mismatch",
            detail={"expected": record.target_revision, "actual": target_revision},
        )

    if content_hash is not None and record.content_hash and record.content_hash != content_hash:
        raise ImpactVerificationError(
            "Content hash mismatch — draft content has changed",
            code="impact_content_mismatch",
            detail={"expected": record.content_hash, "actual": content_hash},
        )

    record.consumption_state = "consumed"
    record.consumed_at = now
    session.flush()

    return record
