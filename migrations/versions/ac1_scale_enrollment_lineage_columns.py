"""Backfill diagnostic for legacy Scale Enrollment lineage (issue #86, Task 12).

Revision ID: ac1_enrollment_lineage
Revises: ab1_scale_lineage

Spec section 4.7 ("Expand/Backfill/Contract") calls for this to be a
three-phase migration: (1) add ``enrollment_candidate_id``,
``delivery_target_id``, ``lineage_guard_id``, ``guard_revision_at_draft``
and ``candidate_fingerprint`` to ``scale_enrollments`` as nullable columns,
(2) backfill them from authoritative persisted evidence where derivable,
and (3) tighten to composite FKs / ``NOT NULL``.

Phase (1) already happened: Task 9 (commit ``34c8005``) added all five
columns directly inside ``ab1_scale_lineage_tables.py``'s
``_add_enrollment_columns()`` -- see that migration's
``_ENROLLMENT_NEW_COLUMNS`` and this revision's own parent,
``ab1_scale_lineage``. ``ScaleEnrollmentRecord`` in ``app/scale/models.py``
already declares the matching nullable ORM columns (see that class's
"Server-derived lineage provenance" comment), so re-adding them here would
raise ``DuplicateColumn`` against PostgreSQL and rewrite the table for no
reason under SQLite's batch mode. This migration is therefore phase (2)
only: the backfill.

Every Enrollment created through the public, candidate-gated
``create_draft_enrollment`` (Task 9 onward) already populates all five
lineage columns at insert time -- there is nothing for this migration to
backfill for those rows. What predates that path (rows created before
issue #86, or through the internal-only
``create_draft_enrollment_from_request`` composition helper kept for
tests) has no candidate, guard or delivery-target row to derive lineage
from: ``enrollment_candidate_id`` is NULL and stays NULL, because
fabricating one would assert a provenance that was never actually proven.
Per spec's D18 (11.7) and this ticket's global invariant against
fabricated evidence, the backfill instead writes the stable
``legacy_scale_lineage_unverifiable`` diagnostic into
``candidate_fingerprint`` for exactly those unprovable rows -- a marker,
not a fabricated identity -- leaving every other lineage column NULL.

Phase (3) -- the composite FK into ``scale_enrollment_candidates`` and the
``NOT NULL`` tightening spec 4.7 describes -- is out of this migration's
and this task's scope (not listed in Task 12's file list, and no later
task in this plan's Tasks 7-16 adds it either). Legacy rows exist that can
never satisfy those constraints without a fabricated candidate, so
applying them now would either violate the same "never fabricate lineage"
invariant this migration protects, or require deleting/closing legacy
Enrollments -- also out of scope. Flagged for a follow-up ticket.

The backfill is PostgreSQL-only, matching every other data-shaping step in
``ab1_scale_lineage_tables.py``: SQLite is a local/test compatibility path
with no production legacy data to reconcile, and the migration test suite
(``tests/test_scale_migration.py``) builds its SQLite fixtures directly
from ``Base.metadata.create_all`` rather than by running Alembic, so a
SQLite branch here would never even execute under ``make verify``.
"""
from __future__ import annotations

from alembic import op

revision = "ac1_enrollment_lineage"
down_revision = "ab1_scale_lineage"
branch_labels = None
depends_on = None

#: Stable diagnostic written into ``candidate_fingerprint`` for legacy
#: Enrollment rows whose lineage cannot be proven during backfill. Never a
#: fabricated ID -- see the module docstring and spec 11.7 (D18).
_LEGACY_UNVERIFIABLE = "legacy_scale_lineage_unverifiable"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        # SQLite: no production legacy data to reconcile; see module
        # docstring.
        return

    # Backfill: any row that already has an enrollment_candidate_id was
    # created through the candidate-gated path (Task 9+) and has genuine,
    # already-populated lineage -- nothing to do for it. Everything else
    # predates that path and has no candidate/guard/delivery-target row to
    # derive lineage from, so it is marked unverifiable rather than left
    # silently blank or given a fabricated identity. The
    # ``candidate_fingerprint IS NULL`` guard makes this idempotent if the
    # migration is ever re-run.
    op.execute(
        f"""
        UPDATE scale_enrollments
        SET candidate_fingerprint = '{_LEGACY_UNVERIFIABLE}'
        WHERE enrollment_candidate_id IS NULL
          AND candidate_fingerprint IS NULL
        """
    )


def downgrade() -> None:
    if not _is_postgres():
        return

    # Only ever undo rows this migration itself marked -- a row that
    # genuinely carries this string as its candidate_fingerprint is
    # indistinguishable from one this migration wrote, but no other writer
    # in this codebase produces that value (it is not a valid fingerprint
    # shape), so this is safe.
    op.execute(
        f"""
        UPDATE scale_enrollments
        SET candidate_fingerprint = NULL
        WHERE candidate_fingerprint = '{_LEGACY_UNVERIFIABLE}'
          AND enrollment_candidate_id IS NULL
        """
    )
