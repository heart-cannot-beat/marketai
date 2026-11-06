"""Give multi-candidate Launch confirmations their own tenant-safe table.

Revision ID: a79b1c2d3e4f
Revises: f5eeeab4e9b9
"""
from __future__ import annotations

import hashlib
import json

from alembic import op
import sqlalchemy as sa


revision = "a79b1c2d3e4f"
down_revision = "f5eeeab4e9b9"
branch_labels = None
depends_on = None

NEW_WORKSPACE_OWNED = ("launch_confirmation_records",)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_already_present = inspector.has_table("launch_confirmation_records")
    if not table_already_present:
        op.create_table(
            "launch_confirmation_records",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("workspace_id", sa.String(length=64), nullable=False),
            sa.Column("meta_mode", sa.String(length=32), nullable=False),
            sa.Column("preview_id", sa.String(length=64), nullable=False),
            sa.Column("preview_hash", sa.String(length=64), nullable=False),
            sa.Column("context_hash", sa.String(length=64), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("actor_id", sa.String(length=100), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')",
                name=op.f("ck_launch_confirmation_records_mode_valid"),
            ),
            sa.ForeignKeyConstraint(
                ["workspace_id", "meta_mode", "preview_id"],
                [
                    "launch_preview_records.workspace_id",
                    "launch_preview_records.meta_mode",
                    "launch_preview_records.id",
                ],
                name="fk_lconf_workspace_mode_preview",
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint(
                "id", name=op.f("pk_launch_confirmation_records")
            ),
            sa.UniqueConstraint(
                "workspace_id", "meta_mode", "id",
                name="uq_lconf_workspace_mode_id",
            ),
            sa.UniqueConstraint(
                "workspace_id", "meta_mode", "preview_id",
                name="uq_lconf_workspace_mode_preview",
            ),
            sa.UniqueConstraint("token_hash", name="uq_lconf_token_hash"),
        )
        op.create_index(
            op.f("ix_launch_confirmation_records_workspace_id"),
            "launch_confirmation_records",
            ["workspace_id"],
            unique=False,
        )

    existing_fks = sa.inspect(bind).get_foreign_keys("launch_provisioning_records")
    legacy_fk = next(
        (fk for fk in existing_fks if fk["referred_table"] == "launch_confirmations"),
        None,
    )
    if legacy_fk is not None:
        legacy_rows = bind.execute(sa.text(
            "SELECT p.confirmation_id, p.workspace_id, p.meta_mode, p.preview_id, "
            "p.created_at AS provisioning_created_at, pv.preview_hash, "
            "lc.token_hash, lc.actor_id, lc.expires_at, lc.consumed_at, lc.created_at "
            "FROM launch_provisioning_records p "
            "JOIN launch_preview_records pv ON pv.id = p.preview_id "
            "JOIN launch_confirmations lc ON lc.id = p.confirmation_id"
        )).mappings().all()
        provisioning_count = bind.execute(sa.text(
            "SELECT COUNT(*) FROM launch_provisioning_records"
        )).scalar_one()
        if len(legacy_rows) != provisioning_count:
            raise RuntimeError(
                "Cannot migrate launch confirmations: a provisioning has no legacy evidence"
            )
        confirmation_table = sa.table(
            "launch_confirmation_records",
            sa.column("id"), sa.column("workspace_id"), sa.column("meta_mode"),
            sa.column("preview_id"), sa.column("preview_hash"),
            sa.column("context_hash"), sa.column("token_hash"),
            sa.column("actor_id"), sa.column("expires_at"),
            sa.column("consumed_at"), sa.column("created_at"),
        )
        for row in legacy_rows:
            context_payload = json.dumps({
                "migration": "legacy_launch_confirmation",
                "workspace_id": row["workspace_id"],
                "meta_mode": row["meta_mode"],
                "preview_id": row["preview_id"],
                "preview_hash": row["preview_hash"],
                "legacy_confirmation_id": row["confirmation_id"],
            }, sort_keys=True, separators=(",", ":"))
            bind.execute(confirmation_table.insert().values(
                id=row["confirmation_id"],
                workspace_id=row["workspace_id"],
                meta_mode=row["meta_mode"],
                preview_id=row["preview_id"],
                preview_hash=row["preview_hash"],
                context_hash=hashlib.sha256(context_payload.encode("utf-8")).hexdigest(),
                token_hash=row["token_hash"],
                actor_id=row["actor_id"],
                expires_at=row["expires_at"],
                consumed_at=row["consumed_at"] or row["provisioning_created_at"],
                created_at=row["created_at"],
            ))
        with op.batch_alter_table("launch_provisioning_records") as batch_op:
            batch_op.drop_constraint(legacy_fk["name"], type_="foreignkey")
            batch_op.create_foreign_key(
                "fk_lprov_workspace_mode_confirmation",
                "launch_confirmation_records",
                ["workspace_id", "meta_mode", "confirmation_id"],
                ["workspace_id", "meta_mode", "id"],
                ondelete="RESTRICT",
            )

    if _is_postgres():
        op.execute(
            'ALTER TABLE "launch_confirmation_records" ENABLE ROW LEVEL SECURITY'
        )
        op.execute(
            'ALTER TABLE "launch_confirmation_records" FORCE ROW LEVEL SECURITY'
        )
        op.execute(
            'DROP POLICY IF EXISTS tenant_isolation ON "launch_confirmation_records"'
        )
        op.execute(
            """
            CREATE POLICY tenant_isolation ON "launch_confirmation_records"
            USING (marketai_meta_context_is_current(workspace_id, meta_mode))
            WITH CHECK (marketai_meta_context_is_current(workspace_id, meta_mode))
            """
        )
        op.execute(
            'GRANT SELECT, INSERT, UPDATE ON TABLE "launch_confirmation_records" '
            "TO marketai_domain_api, marketai_domain_worker"
        )
        op.execute(
            'GRANT SELECT ON TABLE "launch_confirmation_records" '
            "TO marketai_domain_readonly"
        )
        op.execute(
            """
            CREATE OR REPLACE FUNCTION marketai_consume_launch_confirmation_only()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF OLD.consumed_at IS NULL AND NEW.consumed_at IS NOT NULL
                   AND NEW.id = OLD.id
                   AND NEW.workspace_id = OLD.workspace_id
                   AND NEW.meta_mode = OLD.meta_mode
                   AND NEW.preview_id = OLD.preview_id
                   AND NEW.preview_hash = OLD.preview_hash
                   AND NEW.context_hash = OLD.context_hash
                   AND NEW.token_hash = OLD.token_hash
                   AND NEW.actor_id = OLD.actor_id
                   AND NEW.expires_at = OLD.expires_at
                   AND NEW.created_at = OLD.created_at THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'launch confirmation records are append-only except first consumption';
            END;
            $$
            """
        )
        op.execute(
            "DROP TRIGGER IF EXISTS trg_launch_confirmation_consume_only "
            "ON launch_confirmation_records"
        )
        op.execute(
            "CREATE TRIGGER trg_launch_confirmation_consume_only "
            "BEFORE UPDATE OR DELETE ON launch_confirmation_records "
            "FOR EACH ROW EXECUTE FUNCTION marketai_consume_launch_confirmation_only()"
        )


def downgrade() -> None:
    evidence_count = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM launch_confirmation_records")
    ).scalar_one()
    if evidence_count:
        raise RuntimeError(
            "Refusing downgrade: launch_confirmation_records contains durable evidence"
        )
    if _is_postgres():
        op.execute(
            "DROP TRIGGER IF EXISTS trg_launch_confirmation_consume_only "
            "ON launch_confirmation_records"
        )
        op.execute("DROP FUNCTION IF EXISTS marketai_consume_launch_confirmation_only()")
        op.execute(
            'REVOKE ALL ON TABLE "launch_confirmation_records" FROM '
            "marketai_domain_api, marketai_domain_worker, marketai_domain_readonly"
        )
        op.execute(
            'DROP POLICY IF EXISTS tenant_isolation ON "launch_confirmation_records"'
        )
        op.execute(
            'ALTER TABLE "launch_confirmation_records" NO FORCE ROW LEVEL SECURITY'
        )
        op.execute(
            'ALTER TABLE "launch_confirmation_records" DISABLE ROW LEVEL SECURITY'
        )

    with op.batch_alter_table("launch_provisioning_records") as batch_op:
        batch_op.drop_constraint(
            "fk_lprov_workspace_mode_confirmation", type_="foreignkey"
        )
        batch_op.create_foreign_key(
            "fk_lprov_confirmation",
            "launch_confirmations",
            ["confirmation_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    op.drop_index(
        op.f("ix_launch_confirmation_records_workspace_id"),
        table_name="launch_confirmation_records",
    )
    op.drop_table("launch_confirmation_records")
