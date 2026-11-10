"""The reverse walk over the DependencyIndex (contract §14).

Every impact preview (``service.impact_analysis``, the Withdraw and Archive
projections in ``command_impacts``) and the Withdraw command itself list
dependents through :func:`dependent_versions`, so a preview and the command
it authorises never disagree on who is affected.

Most edges pin one version and are matched by that version. An edge of an
``ENTRY_RESOLVED_RELATIONS`` relation is different (#88): its content names
the target entry, which resolves to the entry's current effective version,
while the stored edge still points at the version that was current at
publish. When the walk reaches an entry's *current effective* version, that
version stands for the whole entry: such an edge to any version of the entry
counts, because every one of those dependents resolves to it. A superseded
version is matched by its exact edges only, as before #88: withdrawing it
does not change what the entry resolves to. Stored edges are neither added
nor rewritten.

The rule reads the state of each version as the walk finds it, so a command
walks before it changes any state (``service.withdraw_version``).
"""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import and_, or_, select

from app.platform.tenancy import TenantContext

from ..adapters.persistence.models import LibraryDependencyRecord, LibraryVersionRecord
from .contracts import ENTRY_RESOLVED_RELATIONS, VERSION_EFFECTIVE

_ENTRY_RESOLVED = sorted(ENTRY_RESOLVED_RELATIONS)


def dependent_versions(
    session, context: TenantContext, root_ids: Iterable[str]
) -> list[LibraryVersionRecord]:
    """Transitive dependents of ``root_ids`` in this Workspace, roots excluded."""
    found: dict[str, LibraryVersionRecord] = {}
    seen: set[str] = set(root_ids)
    frontier = list(seen)
    while frontier:
        current = session.get(LibraryVersionRecord, frontier.pop())
        if current is None or current.workspace_id != context.workspace_id:
            continue
        reaches = LibraryDependencyRecord.to_version_id == current.id
        if current.state == VERSION_EFFECTIVE:
            # At most one effective version per entry, so each entry's
            # siblings are read at most once per walk.
            siblings = select(LibraryVersionRecord.id).where(
                LibraryVersionRecord.workspace_id == context.workspace_id,
                LibraryVersionRecord.entry_id == current.entry_id,
            )
            reaches = or_(reaches, and_(
                LibraryDependencyRecord.relation.in_(_ENTRY_RESOLVED),
                LibraryDependencyRecord.to_version_id.in_(list(session.scalars(siblings))),
            ))
        rows = session.scalars(
            select(LibraryDependencyRecord).where(
                LibraryDependencyRecord.workspace_id == context.workspace_id, reaches,
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
