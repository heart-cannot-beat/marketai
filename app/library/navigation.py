"""Server-verified internal resource navigation (Gate 0.5 / #47).

Internal resource entries (e.g. Offer under Product, Manual Fact Set under
Data Source) are discovered through ``LibraryEntryContextRecord`` rows
that bind a resource entry to its root and optional parent.

All queries enforce Workspace isolation via ``TenantContext``.  Cross-
Workspace, mismatched type, mismatched root, fake parent, and relationship
inferred only by matching display names all fail closed.
"""
from __future__ import annotations

from sqlalchemy import func, select

from app.platform.tenancy import TenantContext

from ..adapters.persistence.database import tenant_session
from ..adapters.persistence.models import (
    LibraryEntryContextRecord,
    LibraryEntryRecord,
    LibraryDraftRecord,
    LibraryVersionRecord,
    new_id,
    utcnow,
)
from .contracts import (
    CONTEXT_REGISTRY,
    ENTRY_REFERENCE_ASSET,
    INTERNAL_TYPES,
    LIFECYCLE_ACTIVE,
    ROOT_CATEGORIES,
    SHARED_INTERNAL_TYPES,
    VERSION_EFFECTIVE,
)


from .service import LibraryConflict, LibraryError, LibraryNotFound, create_entry as _create_entry


class NavigationError(LibraryError):
    pass


class NavigationNotFound(LibraryNotFound):
    pass


class NavigationConflict(LibraryConflict):
    pass


def _validate_root(session, context: TenantContext, root_entry_id: str) -> LibraryEntryRecord:
    root = session.get(LibraryEntryRecord, root_entry_id)
    if root is None or root.workspace_id != context.workspace_id:
        raise NavigationNotFound("根条目不存在")
    if root.entry_type not in ROOT_CATEGORIES:
        raise NavigationConflict(
            f"{root.entry_type!r} 不是根类型",
            {"entry_type": root.entry_type, "allowed": sorted(ROOT_CATEGORIES)},
        )
    return root


def _validate_context_type(root_type: str, resource_type: str) -> str:
    if resource_type in SHARED_INTERNAL_TYPES:
        return f"shared_{resource_type}"
    allowed = CONTEXT_REGISTRY.get(root_type, {})
    relation_kind = allowed.get(resource_type)
    if relation_kind is None:
        raise NavigationConflict(
            f"{resource_type!r} 不能属于 {root_type!r}",
            {"root_type": root_type, "resource_type": resource_type,
             "allowed": sorted(allowed.keys())},
        )
    return relation_kind


def _validate_parent(
    session, context: TenantContext, root: LibraryEntryRecord,
    parent_type: str | None, parent_id: str | None,
) -> LibraryEntryRecord | None:
    if parent_type is None and parent_id is None:
        return None
    if (parent_type is None) != (parent_id is None):
        raise NavigationConflict(
            "parent_type 和 parent_id 必须同时提供或同时省略",
            {"parent_type": parent_type, "parent_id": parent_id},
        )
    parent = session.get(LibraryEntryRecord, parent_id)
    if parent is None or parent.workspace_id != context.workspace_id:
        raise NavigationNotFound("父条目不存在")
    if parent.entry_type != parent_type:
        raise NavigationConflict(
            f"父条目类型不匹配: 期望 {parent_type!r}, 实际 {parent.entry_type!r}",
            {"expected": parent_type, "actual": parent.entry_type},
        )
    if parent.id == root.id:
        return None
    ctx_exists = session.scalars(
        select(LibraryEntryContextRecord).where(
            LibraryEntryContextRecord.workspace_id == context.workspace_id,
            LibraryEntryContextRecord.root_entry_id == root.id,
            LibraryEntryContextRecord.resource_entry_id == parent.id,
        )
    ).first()
    if ctx_exists is None:
        if parent.entry_type not in ROOT_CATEGORIES:
            raise NavigationConflict(
                "父条目不在当前根条目的上下文中",
                {"root_entry_id": root.id, "parent_entry_id": parent.id},
            )
    return parent


# ---------------------------------------------------------------------------
# Context registration
# ---------------------------------------------------------------------------

def register_context(
    session,
    context: TenantContext,
    *,
    root_entry_id: str,
    resource_entry_id: str,
    relation_kind: str,
    parent_entry_id: str | None = None,
    creation_evidence: str = "",
) -> LibraryEntryContextRecord:
    existing = session.scalars(
        select(LibraryEntryContextRecord).where(
            LibraryEntryContextRecord.workspace_id == context.workspace_id,
            LibraryEntryContextRecord.root_entry_id == root_entry_id,
            LibraryEntryContextRecord.resource_entry_id == resource_entry_id,
            LibraryEntryContextRecord.relation_kind == relation_kind,
        )
    ).one_or_none()
    if existing is not None:
        return existing

    record = LibraryEntryContextRecord(
        id=new_id("libctx"),
        workspace_id=context.workspace_id,
        root_entry_id=root_entry_id,
        resource_entry_id=resource_entry_id,
        parent_entry_id=parent_entry_id,
        relation_kind=relation_kind,
        creation_evidence=creation_evidence,
        created_at=utcnow(),
    )
    session.add(record)
    session.flush()
    return record


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

def list_internal_entries(
    context: TenantContext,
    root_entry_id: str,
    entry_type: str,
    *,
    parent_type: str | None = None,
    parent_id: str | None = None,
) -> list[dict]:
    with tenant_session(context) as session:
        root = _validate_root(session, context, root_entry_id)
        _validate_context_type(root.entry_type, entry_type)

        parent = _validate_parent(session, context, root, parent_type, parent_id)

        stmt = (
            select(LibraryEntryRecord)
            .join(
                LibraryEntryContextRecord,
                (LibraryEntryContextRecord.resource_entry_id == LibraryEntryRecord.id)
                & (LibraryEntryContextRecord.workspace_id == LibraryEntryRecord.workspace_id),
            )
            .where(
                LibraryEntryContextRecord.workspace_id == context.workspace_id,
                LibraryEntryContextRecord.root_entry_id == root_entry_id,
                LibraryEntryRecord.entry_type == entry_type,
            )
        )
        if parent is not None:
            stmt = stmt.where(LibraryEntryContextRecord.parent_entry_id == parent.id)

        stmt = stmt.order_by(LibraryEntryRecord.created_at)
        entries = session.scalars(stmt).all()

        return [
            {
                "id": e.id,
                "entry_type": e.entry_type,
                "display_name": e.display_name,
                "lifecycle": e.lifecycle,
                "current_version_id": e.current_version_id,
                "root_entry_id": root_entry_id,
            }
            for e in entries
        ]


# ---------------------------------------------------------------------------
# Single-entry lookups
# ---------------------------------------------------------------------------

def get_internal_entry(
    context: TenantContext,
    entry_type: str,
    entry_id: str,
) -> dict:
    with tenant_session(context) as session:
        entry = session.get(LibraryEntryRecord, entry_id)
        if entry is None or entry.workspace_id != context.workspace_id:
            raise NavigationNotFound("内部资源条目不存在")
        if entry.entry_type != entry_type:
            raise NavigationConflict(
                f"条目类型不匹配: 期望 {entry_type!r}, 实际 {entry.entry_type!r}",
                {"expected": entry_type, "actual": entry.entry_type},
            )

        contexts = session.scalars(
            select(LibraryEntryContextRecord).where(
                LibraryEntryContextRecord.workspace_id == context.workspace_id,
                LibraryEntryContextRecord.resource_entry_id == entry_id,
            )
        ).all()

        host_category = "product" if entry_type == ENTRY_REFERENCE_ASSET else None
        if not host_category and contexts:
            root = session.get(LibraryEntryRecord, contexts[0].root_entry_id)
            if root:
                host_category = root.entry_type

        return {
            "id": entry.id,
            "entry_type": entry.entry_type,
            "display_name": entry.display_name,
            "lifecycle": entry.lifecycle,
            "current_version_id": entry.current_version_id,
            "host_category": host_category,
            "context_count": len(contexts),
        }


def get_internal_entry_context(
    context: TenantContext,
    root_entry_id: str,
    entry_type: str,
    entry_id: str,
    *,
    parent_type: str | None = None,
    parent_id: str | None = None,
) -> dict:
    with tenant_session(context) as session:
        root = _validate_root(session, context, root_entry_id)
        relation_kind = _validate_context_type(root.entry_type, entry_type)
        parent = _validate_parent(session, context, root, parent_type, parent_id)

        entry = session.get(LibraryEntryRecord, entry_id)
        if entry is None or entry.workspace_id != context.workspace_id:
            raise NavigationNotFound("内部资源条目不存在")
        if entry.entry_type != entry_type:
            raise NavigationConflict(
                f"条目类型不匹配: 期望 {entry_type!r}, 实际 {entry.entry_type!r}",
                {"expected": entry_type, "actual": entry.entry_type},
            )

        ctx_record = session.scalars(
            select(LibraryEntryContextRecord).where(
                LibraryEntryContextRecord.workspace_id == context.workspace_id,
                LibraryEntryContextRecord.root_entry_id == root_entry_id,
                LibraryEntryContextRecord.resource_entry_id == entry_id,
                LibraryEntryContextRecord.relation_kind == relation_kind,
            )
        ).one_or_none()

        if ctx_record is None:
            raise NavigationNotFound("该资源不在指定根条目的上下文中")

        if parent is not None and ctx_record.parent_entry_id != parent.id:
            raise NavigationConflict(
                "父条目不匹配",
                {"expected_parent": parent.id, "actual_parent": ctx_record.parent_entry_id},
            )

        return {
            "id": entry.id,
            "entry_type": entry.entry_type,
            "display_name": entry.display_name,
            "lifecycle": entry.lifecycle,
            "current_version_id": entry.current_version_id,
            "root_entry_id": root_entry_id,
            "root_entry_type": root.entry_type,
            "parent_entry_id": ctx_record.parent_entry_id,
            "relation_kind": ctx_record.relation_kind,
            "creation_evidence": ctx_record.creation_evidence,
        }


# ---------------------------------------------------------------------------
# Internal entry creation with context
# ---------------------------------------------------------------------------

def create_internal_entry(
    context: TenantContext,
    root_entry_id: str,
    *,
    entry_type: str,
    display_name: str,
    content: dict | None = None,
    parent_type: str | None = None,
    parent_id: str | None = None,
    idempotency_key: str | None = None,
    request_fingerprint: str | None = None,
    command_identity: str | None = None,
) -> dict:
    if entry_type not in INTERNAL_TYPES:
        raise NavigationConflict(f"{entry_type!r} 不是内部资源类型")

    with tenant_session(context) as session:
        root = _validate_root(session, context, root_entry_id)
        relation_kind = _validate_context_type(root.entry_type, entry_type)
        parent = _validate_parent(session, context, root, parent_type, parent_id)

    entry = _create_entry(
        context,
        entry_type=entry_type,
        display_name=display_name,
        content=content,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        command_identity=command_identity,
    )

    with tenant_session(context) as session:
        register_context(
            session, context,
            root_entry_id=root_entry_id,
            resource_entry_id=entry["id"],
            relation_kind=relation_kind,
            parent_entry_id=parent.id if parent else None,
            creation_evidence=f"create_internal_entry:{entry['id']}",
        )

    entry["root_entry_id"] = root_entry_id
    return entry
