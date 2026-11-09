"""The reverse walk over the DependencyIndex (contract §14).

Every impact preview (``service.impact_analysis``, the Withdraw and Archive
projections in ``command_impacts``) and the Withdraw command itself list
dependents through :func:`dependent_versions`, so a preview and the command
it authorises never disagree on who is affected.

Most edges pin one version and are matched by that version. An edge of an
``ENTRY_RESOLVED_RELATIONS`` relation is read by entry instead (#88): its
content names the target entry, which resolves to the entry's current version,
and the stored edge still points at the version that was current at publish.
The walk therefore takes a version of the entry as standing for the whole
entry: such an edge to *any* version of it counts. Stored edges are neither
added nor rewritten.
"""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import and_, or_, select

from app.platform.tenancy import TenantContext

from ..adapters.persistence.models import LibraryDependencyRecord, LibraryVersionRecord
from .contracts import ENTRY_RESOLVED_RELATIONS

_ENTRY_RESOLVED = sorted(ENTRY_RESOLVED_RELATIONS)


def dependent_versions(
    session, context: TenantContext, root_ids: Iterable[str]
) -> list[LibraryVersionRecord]:
    """Transitive dependents of ``root_ids`` in this Workspace, roots excluded."""
    found: dict[str, LibraryVersionRecord] = {}
    entry_versions: dict[str, list[str]] = {}
    seen: set[str] = set(root_ids)
    frontier = list(seen)
    while frontier:
        current = session.get(LibraryVersionRecord, frontier.pop())
        if current is None or current.workspace_id != context.workspace_id:
            continue
        if current.entry_id not in entry_versions:
            entry_versions[current.entry_id] = list(session.scalars(
                select(LibraryVersionRecord.id).where(
                    LibraryVersionRecord.workspace_id == context.workspace_id,
                    LibraryVersionRecord.entry_id == current.entry_id,
                )
            ))
        rows = session.scalars(
            select(LibraryDependencyRecord).where(
                LibraryDependencyRecord.workspace_id == context.workspace_id,
                or_(
                    LibraryDependencyRecord.to_version_id == current.id,
                    and_(
                        LibraryDependencyRecord.relation.in_(_ENTRY_RESOLVED),
                        LibraryDependencyRecord.to_version_id.in_(entry_versions[current.entry_id]),
                    ),
                ),
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
