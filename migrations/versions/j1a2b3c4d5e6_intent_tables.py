"""Add upload_intent_records and download_intent_records (spec §7.9-§7.12).

Revision ID: j1a2b3c4d5e6
Revises: i1a2b3c4d5e6

``upload_intent_records`` backs the three-phase upload lifecycle
(``app.platform.upload_intents``): a ``pending`` intent moves to
``verifying`` under a CAS lease the Worker holds during
``BlobStore.inspect_upload``/``commit_upload``, then to a terminal
``finalized``/``rejected``, or back to ``pending`` on a transient storage
error. ``download_intent_records`` (``app.platform.download_intents``) is a
simpler, immutable per-click audit row for a signed download capability.

Both tables follow the same tenancy shape as ``operation_records`` /
``idempotency_records`` (h1a2b3c4d5e6, i1a2b3c4d5e6): RLS with FORCE on
PostgreSQL, a SELECT/INSERT/UPDATE split between ``marketai_domain_api`` and
``marketai_domain_worker`` (the Worker never INSERTs a new intent -- only
the request-serving API creates one), SELECT for
``marketai_domain_readonly``.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "j1a2b3c4d5e6"
down_revision = "i1a2b3c4d5e6"
branch_labels = None
depends_on = None


#: Picked up by the postgres RLS acceptance sweep alongside
#: ``h1a2b3c4d5e6.NEW_WORKSPACE_OWNED`` / ``i1a2b3c4d5e6.NEW_WORKSPACE_OWNED``
#: -- see ``tests/test_postgres_rls.py``.
NEW_WORKSPACE_OWNED = ("upload_intent_records", "download_intent_records")

_TABLES = ("upload_intent_records", "download_intent_records")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _create_upload_intent_records() -> None:
    op.create_table(
        "upload_intent_records",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("initiator_id", sa.String(length=100), nullable=True),
        sa.Column("authorization_epoch_at_issue", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("owner_kind", sa.String(length=32), nullable=False),
        sa.Column("owner_id", sa.String(length=100), nullable=True),
        sa.Column("staging_key", sa.String(length=1000), nullable=False),
        sa.Column("purpose", sa.String(length=64), nullable=False),
        sa.Column("filename_original", sa.String(length=500), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("content_length_min", sa.Integer(), nullable=False),
        sa.Column("content_length_max", sa.Integer(), nullable=False),
        sa.Column("checksum_sha256_b64", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("verification_lease_id", sa.String(length=64), nullable=True),
        sa.Column("verification_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_code", sa.String(length=100), nullable=True),
        sa.Column("committed_blob_ref_key", sa.String(length=1000), nullable=True),
        sa.Column("committed_blob_ref_version", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_upload_intent_records_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_upload_intent_records")),
        sa.UniqueConstraint(
            "workspace_id", "id", name="uq_upload_intent_records_workspace_id_id"
        ),
        sa.UniqueConstraint(
            "staging_key", name="uq_upload_intent_records_staging_key"
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'verifying', 'finalized', 'rejected', 'expired')",
            name="upload_intent_state_valid",
        ),
    )
    op.create_index(
        op.f("ix_upload_intent_records_workspace_id"),
        "upload_intent_records",
        ["workspace_id"],
    )
    op.create_index(
        op.f("ix_upload_intent_records_state"), "upload_intent_records", ["state"]
    )
    op.create_index(
        op.f("ix_upload_intent_records_expires_at"),
        "upload_intent_records",
        ["expires_at"],
    )


def _create_download_intent_records() -> None:
    op.create_table(
        "download_intent_records",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("initiator_id", sa.String(length=100), nullable=True),
        sa.Column("owner_kind", sa.String(length=32), nullable=False),
        sa.Column("owner_id", sa.String(length=100), nullable=False),
        sa.Column("committed_blob_ref_key", sa.String(length=1000), nullable=False),
        sa.Column("committed_blob_ref_version", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_download_intent_records_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_download_intent_records")),
        sa.UniqueConstraint(
            "workspace_id", "id", name="uq_download_intent_records_workspace_id_id"
        ),
    )
    op.create_index(
        op.f("ix_download_intent_records_workspace_id"),
        "download_intent_records",
        ["workspace_id"],
    )
    op.create_index(
        op.f("ix_download_intent_records_expires_at"),
        "download_intent_records",
        ["expires_at"],
    )


def _install_rls_and_grants() -> None:
    if not _is_postgres():
        # SQLite: the repository layer (app.platform.upload_intents /
        # app.platform.download_intents) is the only enforcement available,
        # same as every other tenant-owned table -- see h1a2b3c4d5e6.
        return

    for table in _TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"')
        op.execute(
            f'''CREATE POLICY tenant_isolation ON "{table}"
               USING (marketai_context_is_current(workspace_id))
               WITH CHECK (marketai_context_is_current(workspace_id))'''
        )

    # API: creates intents (INSERT) and may need to update state on read
    # paths (e.g. cooperative expiry marking) -- SELECT, INSERT, UPDATE, no
    # DELETE. Worker: verifies and transitions an existing intent's state
    # (the CAS lease UPDATEs in app.platform.upload_intents) but never
    # creates one -- SELECT, UPDATE only, no INSERT. Mirrors the
    # operation_records / idempotency_records grant matrix (h1a2b3c4d5e6,
    # i1a2b3c4d5e6).
    for table in _TABLES:
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE ON TABLE {table} TO marketai_domain_api"
        )
        op.execute(
            f"GRANT SELECT, UPDATE ON TABLE {table} TO marketai_domain_worker"
        )
        op.execute(f"GRANT SELECT ON TABLE {table} TO marketai_domain_readonly")


def upgrade() -> None:
    _create_upload_intent_records()
    _create_download_intent_records()
    _install_rls_and_grants()


def downgrade() -> None:
    if _is_postgres():
        op.execute(
            "REVOKE ALL ON TABLE upload_intent_records, download_intent_records FROM "
            "marketai_domain_api, marketai_domain_worker, marketai_domain_readonly"
        )
        for table in _TABLES:
            op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"')
            op.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
            op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')

    op.drop_table("download_intent_records")
    op.drop_table("upload_intent_records")
