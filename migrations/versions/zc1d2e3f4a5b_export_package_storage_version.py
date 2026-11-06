"""Store the committed storage revision for export ZIP artifacts.

Revision ID: zc1d2e3f4a5b
Revises: zb1c2d3e4f6a

The storage revision is an opaque backend identifier and is not the ZIP's
content hash. Keeping both lets signed downloads retain the same immutable
revision fence used by every other committed blob.
"""

import sqlalchemy as sa
from alembic import op


revision = "zc1d2e3f4a5b"
down_revision = "zb1c2d3e4f6a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("export_packages", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "committed_storage_version_id",
                sa.String(length=256),
                nullable=False,
                server_default="",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("export_packages", schema=None) as batch_op:
        batch_op.drop_column("committed_storage_version_id")
