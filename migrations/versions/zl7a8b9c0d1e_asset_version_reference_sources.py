"""Reference lineage on asset_versions (Issue #167).

Revision ID: zl7a8b9c0d1e
Revises: zk6f7a8b9c0d

v9 §3.1 requires a generated asset to trace back to its reference sources.
Generation now sends a product's bound ``reference_asset`` versions to the
provider (OpenAI image edits, Gemini / Seedream input images) when the
frozen recipe decides to; the BYOK evidence recorder writes which versions
the call actually sent -- with their role, viewpoint and derivative SHA-256,
the recipe's reference decision and the confirmed must-match ids -- into
``reference_sources_json`` when it inserts the AssetVersion.

Nullable JSON, written once at insert: Mock output and every row that
predates this revision stay null, and no existing row is rewritten. The
column inherits the table's grants and RLS policy (``asset_versions`` is
granted at table level), so no role change is needed.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "zl7a8b9c0d1e"
down_revision = "zk6f7a8b9c0d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("asset_versions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("reference_sources_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("asset_versions", schema=None) as batch_op:
        batch_op.drop_column("reference_sources_json")
