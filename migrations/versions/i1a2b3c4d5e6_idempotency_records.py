"""Add idempotency_records (spec §5.3-5.4).

Revision ID: i1a2b3c4d5e6
Revises: h1a2b3c4d5e6

``idempotency_records`` is the pending-reservation ledger backing
``app.platform.idempotency``. Two idempotency patterns share it: async
commands (202 + Operation) bind ``operation_id``; sync resource creation
(201, e.g. upload/download intents) binds ``response_resource_type`` +
``response_resource_id`` instead -- exactly one of the two is ever set. The
``state`` column distinguishes a claimed-but-unresolved reservation
(``pending``) from a resolved outcome (``committed``); the CHECK
constraints below enforce that split at the database level so a partial
write can never leave outcome data on an uncommitted reservation, or a
committed record with no outcome at all.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "i1a2b3c4d5e6"
down_revision = "h1a2b3c4d5e6"
branch_labels = None
depends_on = None


#: Picked up by the postgres RLS acceptance sweep alongside
#: ``h1a2b3c4d5e6.NEW_WORKSPACE_OWNED`` -- see ``tests/test_postgres_rls.py``.
NEW_WORKSPACE_OWNED = ("idempotency_records",)

_IDEMPOTENCY_WORKSPACE_FK = "fk_idempotency_records_workspace_id_workspaces"
_IDEMPOTENCY_OPERATION_FK = "fk_idempotency_records_workspace_operation_operation_records"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _create_table() -> None:
    op.create_table(
        "idempotency_records",
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.String(length=100), nullable=False),
        sa.Column("command_type", sa.String(length=100), nullable=False),
        sa.Column("request_fingerprint_hash", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("operation_id", sa.String(length=64), nullable=True),
        sa.Column("response_resource_type", sa.String(length=64), nullable=True),
        sa.Column("response_resource_id", sa.String(length=100), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f(_IDEMPOTENCY_WORKSPACE_FK),
            ondelete="CASCADE",
        ),
        # Composite, not a plain operation_id -> operation_records.id FK --
        # see the matching comment on the ORM model
        # (app.adapters.persistence.models.IdempotencyRecord) for why: a
        # naked operation_id would let the tenant-safe-foreign-keys
        # acceptance test (test_workspace_owned_foreign_keys_are_tenant_composite)
        # fail, since PostgreSQL's FK triggers are not filtered by RLS.
        sa.ForeignKeyConstraint(
            ["workspace_id", "operation_id"],
            ["operation_records.workspace_id", "operation_records.id"],
            name=op.f(_IDEMPOTENCY_OPERATION_FK),
        ),
        sa.PrimaryKeyConstraint(
            "key_hash", "workspace_id", name=op.f("pk_idempotency_records")
        ),
        sa.CheckConstraint(
            "actor_id != '' AND command_type != ''",
            name="idempotency_actor_binding",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'committed')",
            name="idempotency_state_valid",
        ),
        sa.CheckConstraint(
            "state != 'pending' OR ("
            "response_status IS NULL AND operation_id IS NULL "
            "AND response_resource_type IS NULL AND response_resource_id IS NULL"
            ")",
            name="idempotency_pending_has_no_outcome",
        ),
        sa.CheckConstraint(
            "state != 'committed' OR (response_status IS NOT NULL AND ("
            "(operation_id IS NOT NULL AND response_resource_type IS NULL "
            "AND response_resource_id IS NULL) OR "
            "(operation_id IS NULL AND response_resource_type IS NOT NULL "
            "AND response_resource_id IS NOT NULL)"
            "))",
            name="idempotency_committed_has_outcome",
        ),
    )
    op.create_index(
        op.f("ix_idempotency_records_workspace_id"),
        "idempotency_records",
        ["workspace_id"],
    )
    op.create_index(
        "idx_idempotency_expires", "idempotency_records", ["expires_at"]
    )


def _install_rls_and_grants() -> None:
    if not _is_postgres():
        # SQLite: the repository layer (app.platform.idempotency) is the
        # only enforcement available, same as every other tenant-owned
        # table -- see h1a2b3c4d5e6 for the same note on operation_records.
        return

    op.execute('ALTER TABLE "idempotency_records" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE "idempotency_records" FORCE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "idempotency_records"')
    op.execute(
        '''CREATE POLICY tenant_isolation ON "idempotency_records"
           USING (marketai_context_is_current(workspace_id))
           WITH CHECK (marketai_context_is_current(workspace_id))'''
    )

    # API: claims reservations and commits outcomes -- INSERT and UPDATE.
    # Worker: never claims a reservation itself (only the API-facing command
    # path does), but may read and, per spec §5.4's protocol, could commit
    # an outcome it was waiting on -- SELECT and UPDATE only, no INSERT.
    # Neither may DELETE -- expired rows are reaped by a background sweep,
    # not the request-serving roles.
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON TABLE idempotency_records TO marketai_domain_api"
    )
    op.execute(
        "GRANT SELECT, UPDATE ON TABLE idempotency_records TO marketai_domain_worker"
    )
    op.execute("GRANT SELECT ON TABLE idempotency_records TO marketai_domain_readonly")


def upgrade() -> None:
    _create_table()
    _install_rls_and_grants()


def downgrade() -> None:
    if _is_postgres():
        op.execute(
            "REVOKE ALL ON TABLE idempotency_records FROM "
            "marketai_domain_api, marketai_domain_worker, marketai_domain_readonly"
        )
        op.execute('DROP POLICY IF EXISTS tenant_isolation ON "idempotency_records"')
        op.execute('ALTER TABLE "idempotency_records" NO FORCE ROW LEVEL SECURITY')
        op.execute('ALTER TABLE "idempotency_records" DISABLE ROW LEVEL SECURITY')

    op.drop_table("idempotency_records")
