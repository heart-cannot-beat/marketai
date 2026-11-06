"""Committed blob refs on asset_versions (Issue #49 Task 1).

Adds `committed_object_key` and `committed_storage_version_id` so an
AssetVersion can carry the exact committed blob it was produced from (spec
§7.4's `CommittedBlobRef`), independent of the pre-existing `object_key`
column. Both are nullable: legacy AssetVersions predate this pairing and
Task 2's `resolve_asset_blob_ref` falls back to `object_key` when they are
null.

Revision ID: v1a2b3c4d5e6
Revises: u1a2b3c4d5e6
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "v1a2b3c4d5e6"
down_revision = "u1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("asset_versions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("committed_object_key", sa.String(512), nullable=True))
        batch_op.add_column(sa.Column("committed_storage_version_id", sa.String(256), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("asset_versions", schema=None) as batch_op:
        batch_op.drop_column("committed_storage_version_id")
        batch_op.drop_column("committed_object_key")
