"""The Worker tells whether a Workspace has a key the way the API does (#241).

Revision ID: zzs241a1b2c3
Revises: zzr233a1b2c3

``workspace_provider_bindings`` (``zn9a0b1c2d3e``) answers whether a Workspace has an active key for
a provider -- binding id and revision, no secret column -- so the API can check it without reading
encrypted credentials. Only the API was granted it. Since #193 the Worker admits a first batch's next
steps (``app.marketing.first_batch.advance``), and that admission checks the key through the same
view; on PostgreSQL it was refused (``permission denied for view workspace_provider_bindings``), so a
first batch with a real key stopped after reading the product.

The Worker gains SELECT on the view. It already reads ``credential_bindings`` and
``credential_versions`` in full, the encrypted secret included, so this lets it read nothing it could
not read before; the view still answers only for the Workspace in the signed context
(``marketai_context_is_current``). The grant is mirrored in ``deploy/postgres/domain-tenant-roles.sql``.

The previous release keeps running while this migrates: the grant only widens what the Worker may read.
Downgrade revokes it.
"""
from alembic import op

revision = "zzs241a1b2c3"
down_revision = "zzr233a1b2c3"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("GRANT SELECT ON workspace_provider_bindings TO marketai_domain_worker")


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("REVOKE SELECT ON workspace_provider_bindings FROM marketai_domain_worker")
