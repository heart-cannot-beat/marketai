"""Let reference objects point at BlobStore objects and at their promoted entry.

Revision ID: zg2b3c4d5e6f
Revises: zf1a2b3c4d5e

Production stores reference uploads in the Railway bucket, not on the API or
Worker filesystem (Issue #158). The Worker now reads the committed original
through ``BlobStore.open_committed`` and writes the controlled derivative back
through ``BlobStore.commit_generated_artifact``; both calls address an object
by ``(key, storage_version_id)``, the same immutable revision fence every
other committed blob keeps (``asset_versions.committed_storage_version_id``,
``export_packages.committed_storage_version_id``). The two new
``*_storage_version_id`` columns carry that fence; the dev-only local object
directory path keeps them empty.

``promoted_entry_id`` records the ``reference_asset`` Library entry the
server created from a released object (``POST
/library/reference-objects/{id}/promote``), so promoting the same object
twice -- from a retried upload flow or two tabs -- resolves to the same entry
and version instead of publishing a duplicate.
"""

import sqlalchemy as sa
from alembic import op


revision = "zg2b3c4d5e6f"
down_revision = "zf1a2b3c4d5e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("reference_objects", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "original_storage_version_id",
                sa.String(length=256),
                nullable=False,
                server_default="",
            )
        )
        batch_op.add_column(
            sa.Column(
                "derivative_storage_version_id",
                sa.String(length=256),
                nullable=False,
                server_default="",
            )
        )
        batch_op.add_column(
            sa.Column("promoted_entry_id", sa.String(length=64), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("reference_objects", schema=None) as batch_op:
        batch_op.drop_column("promoted_entry_id")
        batch_op.drop_column("derivative_storage_version_id")
        batch_op.drop_column("original_storage_version_id")
