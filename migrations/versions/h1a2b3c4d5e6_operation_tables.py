"""Add operation_records and operation_state_events (spec §5.1-5.2).

Revision ID: h1a2b3c4d5e6
Revises: g1a2b3c4d5e6

An Operation is the persistent, tenant-scoped record of an asynchronous
domain command. ``operation_records`` is the current-state projection;
``operation_state_events`` is the append-only history it is built from --
never updated or deleted, mirroring ``audit_events`` (see
``d8e9f0a1b2c3_append_only_audit_events``).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "h1a2b3c4d5e6"
down_revision = "g1a2b3c4d5e6"
branch_labels = None
depends_on = None


#: Picked up by the postgres RLS acceptance sweep alongside
#: ``d8e9f0a1b2c3.NEW_WORKSPACE_OWNED`` -- see ``tests/test_postgres_rls.py``.
NEW_WORKSPACE_OWNED = ("operation_records", "operation_state_events")

_OPERATION_RECORDS_WORKSPACE_FK = "fk_operation_records_workspace_id_workspaces"
_OPERATION_STATE_EVENTS_OPERATION_FK = (
    "fk_operation_state_events_operation_id_operation_records"
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _create_tables() -> None:
    op.create_table(
        "operation_records",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("initiator_id", sa.String(length=100), nullable=True),
        sa.Column("command_type", sa.String(length=100), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=True),
        sa.Column("result_resource_type", sa.String(length=64), nullable=True),
        sa.Column("result_resource_id", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f(_OPERATION_RECORDS_WORKSPACE_FK),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_operation_records")),
        sa.UniqueConstraint(
            "workspace_id", "id", name="uq_operation_records_workspace_id_id"
        ),
    )
    op.create_index(
        op.f("ix_operation_records_workspace_id"),
        "operation_records",
        ["workspace_id"],
    )
    op.create_index(
        op.f("ix_operation_records_command_type"),
        "operation_records",
        ["command_type"],
    )
    op.create_index(
        op.f("ix_operation_records_state"), "operation_records", ["state"]
    )

    op.create_table(
        "operation_state_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("operation_id", sa.String(length=64), nullable=False),
        sa.Column("from_state", sa.String(length=32), nullable=True),
        sa.Column("to_state", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operation_records.id"],
            name=op.f(_OPERATION_STATE_EVENTS_OPERATION_FK),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_operation_state_events")),
    )
    op.create_index(
        op.f("ix_operation_state_events_operation_id"),
        "operation_state_events",
        ["operation_id"],
    )


def _install_append_only_guard() -> None:
    """Reject UPDATE/DELETE on operation_state_events -- history is append-only."""
    if not _is_postgres():
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS operation_state_events_reject_update
            BEFORE UPDATE ON operation_state_events
            BEGIN
                SELECT RAISE(ABORT, 'operation_state_events is append-only');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS operation_state_events_reject_delete
            BEFORE DELETE ON operation_state_events
            BEGIN
                SELECT RAISE(ABORT, 'operation_state_events is append-only');
            END
            """
        )
        return

    op.execute(
        """
        CREATE OR REPLACE FUNCTION marketai_reject_operation_event_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            RAISE EXCEPTION 'operation_state_events is append-only';
        END
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION marketai_reject_operation_event_mutation() FROM PUBLIC"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS operation_state_events_reject_mutation "
        "ON operation_state_events"
    )
    op.execute(
        """
        CREATE TRIGGER operation_state_events_reject_mutation
        BEFORE UPDATE OR DELETE ON operation_state_events
        FOR EACH ROW EXECUTE FUNCTION marketai_reject_operation_event_mutation()
        """
    )


def _install_rls_and_grants() -> None:
    if not _is_postgres():
        # SQLite: the repository layer (app.platform.operations) is the only
        # enforcement available, same as every other tenant-owned table.
        return

    op.execute('ALTER TABLE "operation_records" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE "operation_records" FORCE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "operation_records"')
    op.execute(
        '''CREATE POLICY tenant_isolation ON "operation_records"
           USING (marketai_context_is_current(workspace_id))
           WITH CHECK (marketai_context_is_current(workspace_id))'''
    )

    # Child table: visible exactly when its parent Operation is visible,
    # mirroring experiment_state_events (see f1a2c3d4e5b6_row_level_security).
    op.execute('ALTER TABLE "operation_state_events" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE "operation_state_events" FORCE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "operation_state_events"')
    op.execute(
        '''CREATE POLICY tenant_isolation ON "operation_state_events"
           USING (
               EXISTS (
                   SELECT 1 FROM "operation_records" AS parent
                   WHERE parent.id = "operation_state_events"."operation_id"
               )
           )
           WITH CHECK (
               EXISTS (
                   SELECT 1 FROM "operation_records" AS parent
                   WHERE parent.id = "operation_state_events"."operation_id"
               )
           )'''
    )

    # API: creates and advances Operations, so it holds INSERT and UPDATE.
    # Worker: advances state on an Operation the API already created while
    # executing the underlying Job -- SELECT and UPDATE only, no INSERT.
    # Neither may DELETE -- history and current-state rows are retained,
    # matching every other operational table.
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON TABLE operation_records TO marketai_domain_api"
    )
    op.execute(
        "GRANT SELECT, UPDATE ON TABLE operation_records TO marketai_domain_worker"
    )
    op.execute("GRANT SELECT ON TABLE operation_records TO marketai_domain_readonly")
    op.execute(
        "GRANT SELECT, INSERT ON TABLE operation_state_events "
        "TO marketai_domain_api, marketai_domain_worker"
    )
    op.execute(
        "GRANT SELECT ON TABLE operation_state_events TO marketai_domain_readonly"
    )


def upgrade() -> None:
    _create_tables()
    _install_append_only_guard()
    _install_rls_and_grants()


def downgrade() -> None:
    if _is_postgres():
        op.execute(
            "REVOKE ALL ON TABLE operation_state_events FROM "
            "marketai_domain_api, marketai_domain_worker, marketai_domain_readonly"
        )
        op.execute(
            "REVOKE ALL ON TABLE operation_records FROM "
            "marketai_domain_api, marketai_domain_worker, marketai_domain_readonly"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS operation_state_events_reject_mutation "
            "ON operation_state_events"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS marketai_reject_operation_event_mutation()"
        )
        op.execute('DROP POLICY IF EXISTS tenant_isolation ON "operation_state_events"')
        op.execute('ALTER TABLE "operation_state_events" NO FORCE ROW LEVEL SECURITY')
        op.execute('ALTER TABLE "operation_state_events" DISABLE ROW LEVEL SECURITY')
        op.execute('DROP POLICY IF EXISTS tenant_isolation ON "operation_records"')
        op.execute('ALTER TABLE "operation_records" NO FORCE ROW LEVEL SECURITY')
        op.execute('ALTER TABLE "operation_records" DISABLE ROW LEVEL SECURITY')
    else:
        op.execute("DROP TRIGGER IF EXISTS operation_state_events_reject_update")
        op.execute("DROP TRIGGER IF EXISTS operation_state_events_reject_delete")

    op.drop_table("operation_state_events")
    op.drop_table("operation_records")
