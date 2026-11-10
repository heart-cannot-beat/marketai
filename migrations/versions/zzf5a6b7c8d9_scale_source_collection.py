"""Scale source collection: manifest collection key and delivery diagnostics (#109, #101 S3).

Revision ID: zzf5a6b7c8d9
Revises: zze4f5a6b7c8

The Worker's ``scale_collect_sources`` Job (``app.scale.collector``) turns a
connection's finished observation batch for target day D into one immutable
``ScaleEvidenceSourceManifestRecord`` per active Episode. Two schema facts
make that safe to replay and complete enough to seal:

* ``scale_evidence_source_manifests.collection_key`` names the batch a
  manifest was collected from, ``scale-collect:{connection_id}:{D}:
  {run_generation}``. The partial unique index
  ``uq_sesm_episode_collection_key`` on ``(workspace_id, meta_mode,
  episode_id, collection_key) WHERE collection_key IS NOT NULL`` makes a
  replayed or concurrent collection find the manifest already written
  instead of allocating a second source revision. Manifests written before
  this revision (tests, e2e seeds) have no key and are not constrained.
* ``scale_delivery_config_sources`` gains what the delivery-configuration
  read reports beyond the configuration itself (Scale contract §19, §8):
  ``diagnostic_state`` (``unknown``, ``none``, ``learning``,
  ``learning_limited``; ``unknown`` unless Meta returned the field),
  ``diagnostic_source_field`` and ``diagnostic_observed_at`` (the field
  contract and observation time of a returned diagnostic), and
  ``status_history`` (the campaign, ad set and ad effective statuses of
  each Episode day in the window, as JSON). Existing rows get ``unknown``
  and an empty history: none of them was read with a diagnostic, and the
  snapshots already sealed from them assemble under their own schema
  version (``scale-snapshot-v1``/``-v2`` never read these columns).

Grants: the Worker already has INSERT on both tables and SELECT on every
Scale table; the API keeps its INSERT on ``scale_delivery_config_sources``
(activation baseline). Column-level additions inherit the table grants, so
``deploy/postgres/domain-tenant-roles.sql`` needs no new statement -- its
Scale matrix comment names this revision. Both tables keep their
append-only triggers: ``ALTER TABLE ... ADD COLUMN ... DEFAULT`` fires no
row trigger.

The previous release keeps running while this migrates: it neither writes
the key nor reads the new columns, and every new column has a server
default. Downgrade drops the index and the columns.
"""

import sqlalchemy as sa
from alembic import op


revision = "zzf5a6b7c8d9"
down_revision = "zze4f5a6b7c8"
branch_labels = None
depends_on = None

_MANIFESTS = "scale_evidence_source_manifests"
_DELIVERY = "scale_delivery_config_sources"
_INDEX = "uq_sesm_episode_collection_key"
_DIAGNOSTIC_CHECK = "ck_sdcs_diagnostic_state"
_DIAGNOSTIC_VALUES = "diagnostic_state IN ('unknown', 'none', 'learning', 'learning_limited')"


def upgrade() -> None:
    with op.batch_alter_table(_MANIFESTS) as batch:
        batch.add_column(sa.Column("collection_key", sa.String(length=200), nullable=True))
    op.create_index(
        _INDEX,
        _MANIFESTS,
        ["workspace_id", "meta_mode", "episode_id", "collection_key"],
        unique=True,
        postgresql_where=sa.text("collection_key IS NOT NULL"),
        sqlite_where=sa.text("collection_key IS NOT NULL"),
    )
    with op.batch_alter_table(_DELIVERY) as batch:
        batch.add_column(sa.Column(
            "diagnostic_state", sa.String(length=20), nullable=False, server_default=sa.text("'unknown'"),
        ))
        batch.add_column(sa.Column("diagnostic_source_field", sa.String(length=100), nullable=True))
        batch.add_column(sa.Column("diagnostic_observed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column(
            "status_history", sa.JSON(), nullable=False, server_default=sa.text("'[]'"),
        ))
        batch.create_check_constraint(_DIAGNOSTIC_CHECK, _DIAGNOSTIC_VALUES)


def downgrade() -> None:
    with op.batch_alter_table(_DELIVERY) as batch:
        batch.drop_constraint(_DIAGNOSTIC_CHECK, type_="check")
        batch.drop_column("status_history")
        batch.drop_column("diagnostic_observed_at")
        batch.drop_column("diagnostic_source_field")
        batch.drop_column("diagnostic_state")
    op.drop_index(_INDEX, table_name=_MANIFESTS)
    with op.batch_alter_table(_MANIFESTS) as batch:
        batch.drop_column("collection_key")
