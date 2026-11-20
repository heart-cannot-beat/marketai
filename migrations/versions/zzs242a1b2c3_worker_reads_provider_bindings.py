"""The Worker reads the workspace_provider_bindings view (#242).

Revision ID: zzs242a1b2c3
Revises: zzr233a1b2c3

A first batch checks the Workspace's provider binding before it admits its next step, and the Worker is
what advances a first batch (``app.marketing.first_batch.advance`` -> ``provider_choice.choose`` ->
``generation.selection.credential_binding(include_secret=False)``). On PostgreSQL that lookup reads the
``workspace_provider_bindings`` view, which ``zn9a0b1c2d3e`` granted to the API only, so every real-key
first batch stopped after reading its product with ``permission denied``.

The view shows the Workspace, the provider, the binding id and its revision, under the same tenant check
as its tables, and no secret column. The Worker already reads ``credential_bindings`` and
``credential_versions`` themselves, so this grants nothing it could not read before. The grant is mirrored
in ``deploy/postgres/domain-tenant-roles.sql``.

The previous release keeps running while this migrates: a grant changes nothing it reads or writes.
"""
from alembic import op

revision = "zzs242a1b2c3"
down_revision = "zzr233a1b2c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("GRANT SELECT ON workspace_provider_bindings TO marketai_domain_worker")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("REVOKE SELECT ON workspace_provider_bindings FROM marketai_domain_worker")
