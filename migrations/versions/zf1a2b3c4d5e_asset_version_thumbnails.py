"""Thumbnail derivative columns on asset_versions (Issue #159).

Revision ID: zf1a2b3c4d5e
Revises: ze1f2a3b4c5d

The Assets gallery and the Run page load every tile from the committed
original (a GPT-Image PNG is 1.2-1.3 MB), so a 24-tile page transfers ~30 MB
before anything renders. The Worker now also derives a 512px-long-edge JPEG
from the bytes it commits (``app.generation.thumbnails``) and records that
derivative here: its committed key / storage version (the pair a signed
download capability needs), the hex SHA-256 the record convention keeps for
every blob, byte size, MIME type and pixel dimensions.

Every column is nullable. Rows that predate this revision stay null until
``app.generation.thumbnail_backfill`` (``scripts/backfill_asset_thumbnails.py``)
derives their thumbnails; a derivative that cannot be produced (undecodable
bytes, storage failure) is left null and the original remains the only truth
-- readers fall back to it. No existing row is rewritten by this migration.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "zf1a2b3c4d5e"
down_revision = "ze1f2a3b4c5d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("asset_versions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("thumbnail_object_key", sa.String(512), nullable=True))
        batch_op.add_column(
            sa.Column("thumbnail_storage_version_id", sa.String(256), nullable=True)
        )
        batch_op.add_column(sa.Column("thumbnail_content_sha256", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("thumbnail_byte_size", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("thumbnail_mime_type", sa.String(100), nullable=True))
        batch_op.add_column(sa.Column("thumbnail_width", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("thumbnail_height", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("asset_versions", schema=None) as batch_op:
        batch_op.drop_column("thumbnail_height")
        batch_op.drop_column("thumbnail_width")
        batch_op.drop_column("thumbnail_mime_type")
        batch_op.drop_column("thumbnail_byte_size")
        batch_op.drop_column("thumbnail_content_sha256")
        batch_op.drop_column("thumbnail_storage_version_id")
        batch_op.drop_column("thumbnail_object_key")
