"""Scale target preview tables and command identity hash.

Revision ID: ad2_target_preview
Revises: ad1_check_names

Issue #87 Task 1 -- the database foundation for the remaining 501-returning
Scale endpoints. Adds two mode-independent, workspace-scoped preview tables
(spec §1.6-style short-lived, server-derived proofs, matching
`ScaleActivationPreviewRecord`'s shape from the #86 hardening work):

* `scale_target_publication_previews` -- impact of publishing a new Target
  for one exact ProductVersion/OfferVersion/market/currency context.
  `publish_scale_target` consumes it.
* `scale_target_withdrawal_previews` -- impact of withdrawing an active
  Target version. `withdraw_scale_target` consumes it.

Both tables are built from their already-mapped SQLAlchemy `Table` objects
(``model.__table__.create(...)``), not a hand-transcribed
``op.create_table()`` call, so the physical schema and the ORM metadata can
never disagree -- the same pattern ``ab1_scale_lineage_tables`` uses for
`ScaleActivationPreviewRecord` and friends.

Also adds a nullable `identity_hash` column to the existing
`scale_command_journal` table (spec §7.1, `ScaleCommandJournalRecord`) and
widens its `ck_scj_command_type` CHECK constraint to the five new Task 1
command types, so a later task's `claim_command()` calls for
`create_target_publication_preview`, `publish_scale_target`,
`create_target_withdrawal_preview`, `withdraw_scale_target`, and
`create_scale_export` do not violate the constraint. The existing five
command types (`create_candidate`, `create_enrollment`, `create_preview`,
`activate_enrollment`, `resume_enrollment`) are unaffected.

PostgreSQL-only DDL (partial unique index, RLS, grants) is gated behind
`_is_postgres()`, reusing `marketai_context_is_current(workspace_id)` --
the same mode-independent tenant-context function
`f5eeeab4e9b9_scale_deterministic_evidence_and_advice` uses for
`scale_target_versions`/`scale_target_lifecycle_events`, since both new
preview tables carry no `meta_mode` column (Target is mode-independent, per
spec §1.6).

Grants follow the same capability split as `scale_target_versions`
(`f5eeeab4e9b9`'s `GRANT_MATRIX`): Target publication/withdrawal are
explicit user commands owned by the Scale API role
(`marketai_domain_api`), so only that role gets INSERT, plus a
column-restricted UPDATE limited to the consume-time columns each preview
ever legally changes (`consumed_at` plus its own terminal column) -- no
role may ever update `impact_hash`, `expires_at`, or any other preview
field after creation. `marketai_domain_worker` gets no INSERT/UPDATE on
either table, matching `scale_target_versions`'s worker_ops == ()`. All
three roles (api, worker, readonly) get blanket SELECT, per every other
Scale table in this module.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

from app.scale.models import (
    ScaleTargetPublicationPreviewRecord,
    ScaleTargetWithdrawalPreviewRecord,
)

revision = "ad2_target_preview"
down_revision = "ad1_check_names"
branch_labels = None
depends_on = None


#: Picked up by tests/test_postgres_rls.py's all_protected_tables() /
#: CURRENT_AGGREGATE_TABLES coverage checks.
NEW_WORKSPACE_OWNED = (
    "scale_target_publication_previews",
    "scale_target_withdrawal_previews",
)

_TABLES_IN_DEPENDENCY_ORDER = (
    ScaleTargetPublicationPreviewRecord,
    ScaleTargetWithdrawalPreviewRecord,
)

#: (table, consumable columns) -- the only columns any runtime role may ever
#: UPDATE. Both previews are create-once/consume-once: nothing else (not
#: even expires_at) is ever rewritten.
_CONSUME_COLUMNS = {
    "scale_target_publication_previews": ("consumed_at", "result_target_version_id"),
    "scale_target_withdrawal_previews": ("consumed_at", "withdrawn_at"),
}

_OLD_COMMAND_TYPES = (
    "create_candidate", "create_enrollment", "create_preview",
    "activate_enrollment", "resume_enrollment",
)
_NEW_COMMAND_TYPES = (
    "create_target_publication_preview", "publish_scale_target",
    "create_target_withdrawal_preview", "withdraw_scale_target",
    "create_scale_export",
)
ALL_COMMAND_TYPES = _OLD_COMMAND_TYPES + _NEW_COMMAND_TYPES


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _create_tables() -> None:
    bind = op.get_bind()
    for model in _TABLES_IN_DEPENDENCY_ORDER:
        model.__table__.create(bind=bind, checkfirst=False)


def _drop_tables() -> None:
    for model in reversed(_TABLES_IN_DEPENDENCY_ORDER):
        op.drop_table(model.__tablename__)


def _has_identity_hash_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(
        col["name"] == "identity_hash"
        for col in inspector.get_columns("scale_command_journal")
    )


def _add_identity_hash_column() -> None:
    # A from-base fresh install replays ab1_scale_lineage_tables, which
    # builds scale_command_journal from the live
    # ``ScaleCommandJournalRecord.__table__`` -- already carrying
    # identity_hash now that the ORM model declares it. An in-place upgrade
    # of an already-migrated database has no such column yet. Handle both:
    # add it only if ab1's table creation did not already include it.
    if _has_identity_hash_column():
        return
    with op.batch_alter_table("scale_command_journal", schema=None) as batch_op:
        batch_op.add_column(sa.Column("identity_hash", sa.String(64), nullable=True))


def _drop_identity_hash_column() -> None:
    if not _has_identity_hash_column():
        return
    with op.batch_alter_table("scale_command_journal", schema=None) as batch_op:
        batch_op.drop_column("identity_hash")


#: The short logical name, matching the model's ``name="ck_scj_command_type"``
#: exactly. Alembic's batch operations apply the same naming convention
#: (``ck_%(table_name)s_%(constraint_name)s``) to this short name when
#: compiling DROP/CREATE CONSTRAINT, producing the real physical name
#: (``ck_scale_command_journal_ck_scj_command_type``) automatically -- do not
#: pass the already-expanded physical name here, or the convention doubles it.
_COMMAND_TYPE_CHECK_NAME = "ck_scj_command_type"


def _widen_command_type_check() -> None:
    with op.batch_alter_table("scale_command_journal", schema=None) as batch_op:
        batch_op.drop_constraint(_COMMAND_TYPE_CHECK_NAME, type_="check")
        batch_op.create_check_constraint(
            _COMMAND_TYPE_CHECK_NAME,
            "command_type IN ('" + "', '".join(ALL_COMMAND_TYPES) + "')",
        )


def _narrow_command_type_check() -> None:
    with op.batch_alter_table("scale_command_journal", schema=None) as batch_op:
        batch_op.drop_constraint(_COMMAND_TYPE_CHECK_NAME, type_="check")
        batch_op.create_check_constraint(
            _COMMAND_TYPE_CHECK_NAME,
            "command_type IN ('" + "', '".join(_OLD_COMMAND_TYPES) + "')",
        )


def _backfill_identity_hash_postgres() -> None:
    # PostgreSQL only: sha256()/encode() are not portable to SQLite, and the
    # partial unique index this backfill feeds is itself PostgreSQL-only
    # (mirroring every other partial-unique guard in app/scale/models.py, see
    # its "ddl_if(dialect=...)" note). Deterministic per-row hash so a
    # pre-#87 row's identity is exactly its (command_type, dedup_key) pair --
    # command_type is folded into the hash input so two different command
    # types that happen to share a dedup_key never collide.
    op.execute(
        text(
            "UPDATE scale_command_journal "
            "SET identity_hash = encode(sha256((command_type || ':' || dedup_key)::bytea), 'hex') "
            "WHERE identity_hash IS NULL"
        )
    )


def _install_partial_unique_index() -> None:
    # Same from-base-vs-in-place split as _add_identity_hash_column: a fresh
    # install's ab1_scale_lineage_tables already created this index, because
    # it too is now mirrored on ScaleCommandJournalRecord's mapped Table
    # (see app/scale/models.py's ddl_if(dialect="postgresql") index) and
    # Table.create() creates every index attached to the table. Skip if
    # ab1 already installed it.
    inspector = sa.inspect(op.get_bind())
    existing = {ix["name"] for ix in inspector.get_indexes("scale_command_journal")}
    if "ix_scj_ws_mode_identity_hash" in existing:
        return
    op.execute(
        text(
            "CREATE UNIQUE INDEX ix_scj_ws_mode_identity_hash "
            "ON scale_command_journal (workspace_id, meta_mode, identity_hash) "
            "WHERE identity_hash IS NOT NULL"
        )
    )


def _uninstall_partial_unique_index() -> None:
    op.execute(text("DROP INDEX IF EXISTS ix_scj_ws_mode_identity_hash"))


def _install_rls_and_grants() -> None:
    for model in _TABLES_IN_DEPENDENCY_ORDER:
        table = model.__tablename__
        # FORCE applies the policy to the table owner too -- belt and
        # braces against a migration-role mistake reading across tenants.
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY tenant_isolation ON "{table}" '
            "USING (marketai_context_is_current(workspace_id)) "
            "WITH CHECK (marketai_context_is_current(workspace_id))"
        )

        op.execute(
            f'GRANT SELECT ON TABLE "{table}" TO '
            "marketai_domain_api, marketai_domain_worker, marketai_domain_readonly"
        )
        # Target publication/withdrawal are explicit user commands (spec
        # §1.6/§7.1's capability split) -- only the API role creates and
        # consumes previews; the Worker role gets nothing beyond the
        # blanket SELECT above, matching scale_target_versions's
        # worker_ops == () in f5eeeab4e9b9's GRANT_MATRIX.
        op.execute(f'GRANT INSERT ON TABLE "{table}" TO marketai_domain_api')
        consume_columns = ", ".join(_CONSUME_COLUMNS[table])
        op.execute(
            f'GRANT UPDATE ({consume_columns}) ON TABLE "{table}" TO marketai_domain_api'
        )


def _uninstall_rls_and_grants() -> None:
    for model in reversed(_TABLES_IN_DEPENDENCY_ORDER):
        table = model.__tablename__
        op.execute(
            f'REVOKE ALL ON TABLE "{table}" FROM '
            "marketai_domain_api, marketai_domain_worker, marketai_domain_readonly"
        )
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')


def upgrade() -> None:
    _create_tables()
    _add_identity_hash_column()
    _widen_command_type_check()

    if _is_postgres():
        _backfill_identity_hash_postgres()
        _install_partial_unique_index()
        _install_rls_and_grants()


def downgrade() -> None:
    if _is_postgres():
        _uninstall_rls_and_grants()
        _uninstall_partial_unique_index()

    _narrow_command_type_check()
    _drop_identity_hash_column()
    _drop_tables()
