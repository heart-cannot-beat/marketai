"""ResolutionPreview: freeze, hash, invalidate.

Spec §4.4: selector-aware version resolution, canonical hashing,
time-bound vs recipe-only hashing, invalidation.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    ResolutionPreviewRecord, new_id, utcnow,
)
from app.platform.tenancy import TenantContext
from app.library.contracts import canonical_hash
from app.library.service import (
    LibraryError, LibraryNotFound,
    _require_workspace, _audit,
)

SNAPSHOT_SCHEMA_VERSION = "20.1"

# Fields excluded from recipe_input_hash (time-bound)
_TIME_BOUND_KEYS = frozenset({
    "credential_evidence",
    "confirmation_decisions",
})
_TIME_BOUND_NESTED = {
    "cost_quote": frozenset({"expires_at"}),
}


def build_canonical_snapshot(
    resolved_versions: dict,
    constraint_results: dict,
    prompt_assembly: dict,
    reference_choices: list[dict],
    provider_decisions: dict,
    credential_evidence: dict,
    exclusions: list,
    cost_quote: dict,
    eligibility: dict,
    confirmation_decisions: list | None = None,
) -> dict:
    """Assemble the authoritative immutable snapshot aggregate."""
    return {
        "_schema": f"resolution_preview/{SNAPSHOT_SCHEMA_VERSION}",
        "resolved_versions": resolved_versions,
        "constraint_results": constraint_results,
        "prompt_assembly": prompt_assembly,
        "reference_choices": sorted(
            reference_choices, key=lambda r: r.get("run_item_index", 0)
        ),
        "provider_decisions": provider_decisions,
        "credential_evidence": credential_evidence,
        "exclusions": sorted(exclusions, key=str),
        "cost_quote": cost_quote,
        "eligibility": eligibility,
        "confirmation_decisions": confirmation_decisions or [],
    }


def _strip_time_bound(snapshot: dict) -> dict:
    """Remove time-bound fields for recipe_input_hash."""
    result = {}
    for key, value in snapshot.items():
        if key in _TIME_BOUND_KEYS:
            continue
        if key in _TIME_BOUND_NESTED:
            nested = dict(value)
            for nk in _TIME_BOUND_NESTED[key]:
                nested.pop(nk, None)
            result[key] = nested
        else:
            result[key] = value
    return result


def compute_recipe_input_hash(snapshot: dict) -> str:
    """Hash excluding time-bound fields."""
    stripped = _strip_time_bound(snapshot)
    return canonical_hash(stripped)


def compute_preview_hashes(snapshot: dict) -> dict[str, str]:
    """Compute both preview_hash and recipe_input_hash."""
    # Canonicalize: sort reference_choices by run_item_index
    canonical = dict(snapshot)
    if "reference_choices" in canonical:
        canonical["reference_choices"] = sorted(
            canonical["reference_choices"],
            key=lambda r: r.get("run_item_index", 0),
        )
    if "exclusions" in canonical:
        canonical["exclusions"] = sorted(canonical["exclusions"], key=str)
    return {
        "preview_hash": canonical_hash(canonical),
        "recipe_input_hash": compute_recipe_input_hash(canonical),
    }


def persist_preview(
    context: TenantContext,
    generation_draft_id: str,
    draft_revision: int,
    snapshot: dict,
    execution_mode: str,
    valid_until: datetime,
) -> dict:
    """Save a resolved preview to the database."""
    hashes = compute_preview_hashes(snapshot)
    with tenant_session(context) as session:
        _require_workspace(session, context.workspace_id)
        record = ResolutionPreviewRecord(
            id=new_id("rpv"),
            workspace_id=context.workspace_id,
            generation_draft_id=generation_draft_id,
            draft_revision=draft_revision,
            snapshot_json=snapshot,
            snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
            preview_hash=hashes["preview_hash"],
            recipe_input_hash=hashes["recipe_input_hash"],
            execution_mode=execution_mode,
            valid_until=valid_until,
            created_by=context.initiator_id or "",
        )
        session.add(record)
        _audit(session, context, "resolve_preview",
               detail={
                   "preview_hash": hashes["preview_hash"],
                   "generation_draft_id": generation_draft_id,
               })
        session.flush()
        return {
            "id": record.id,
            "preview_hash": hashes["preview_hash"],
            "recipe_input_hash": hashes["recipe_input_hash"],
            "execution_mode": execution_mode,
            "valid_until": valid_until.isoformat(),
        }


def invalidate_previews_for_version(
    context: TenantContext,
    version_id: str,
    reason: str,
    *,
    selector_filter: str | None = None,
) -> int:
    """Invalidate unexpired previews referencing a given version (§4.4 invalidation).

    selector_filter: "latest" only invalidates latest-resolved previews.
    None invalidates all (used for withdrawal).
    """
    now = utcnow()
    with tenant_session(context) as session:
        # Find all non-invalidated, non-expired previews
        stmt = select(ResolutionPreviewRecord).where(
            ResolutionPreviewRecord.workspace_id == context.workspace_id,
            ResolutionPreviewRecord.invalidated_at.is_(None),
            ResolutionPreviewRecord.valid_until > now,
            ResolutionPreviewRecord.submitted_run_id.is_(None),
        )
        previews = session.scalars(stmt).all()
        count = 0
        for preview in previews:
            snap = preview.snapshot_json or {}
            resolved = snap.get("resolved_versions") or {}
            matched = False
            for _key, entry_info in resolved.items():
                if isinstance(entry_info, dict) and entry_info.get("version_id") == version_id:
                    if selector_filter and entry_info.get("selector") != selector_filter:
                        continue
                    matched = True
                    break
                if isinstance(entry_info, list):
                    for item in entry_info:
                        if isinstance(item, dict) and item.get("version_id") == version_id:
                            if selector_filter and item.get("selector") != selector_filter:
                                continue
                            matched = True
                            break
                    if matched:
                        break
            if matched:
                preview.invalidated_at = now
                preview.invalidation_reason = reason[:300]
                count += 1
        session.flush()
    return count


def check_preview_valid(
    context: TenantContext,
    preview_id: str,
    expected_hash: str,
) -> dict:
    """Submission-time check: preview exists, hash matches, not invalidated, not expired, not consumed."""
    with tenant_session(context) as session:
        record = session.get(ResolutionPreviewRecord, preview_id)
        if record is None or record.workspace_id != context.workspace_id:
            raise LibraryNotFound("Preview 不存在", code="preview_not_found")
        now = utcnow()
        if record.preview_hash != expected_hash:
            raise LibraryError("Preview hash 不匹配", code="preview_hash_mismatch")
        if record.invalidated_at is not None:
            raise LibraryError(f"Preview 已失效: {record.invalidation_reason}", code="preview_invalidated")
        if record.valid_until < now:
            raise LibraryError("Preview 已过期", code="preview_expired")
        if record.submitted_run_id is not None:
            raise LibraryError("Preview 已被提交", code="preview_already_submitted")
        return {
            "id": record.id,
            "snapshot": dict(record.snapshot_json),
            "execution_mode": record.execution_mode,
        }
