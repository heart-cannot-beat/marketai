"""Ads-first first batch: one start, one confirmed maximum (#193, ADR 0018).

Revision ID: zzn193a1b2c3
Revises: zzm189a1b2c3

``marketing_first_batches`` holds one row per "start" of the ads-first opening: the product intake it
began with (unique per Workspace), the product draft and ads-first project it created, the maximum the
user confirmed, the maxima of the steps admitted so far, and why it stopped. It is tenant owned: RLS
enabled and forced, ``tenant_isolation`` policy, the project, product and Operation bound by composite
foreign keys. The API reads, inserts and updates it; the Worker reads and updates it; readonly reads it.

The Worker advances a batch (Job ``marketing.first_batch``): when one step has finished it admits the
next one in the same transaction -- the product draft and its ideas after the reading, a planning call
with its reservation, the image drafts, previews and runs with theirs. Until now only the API admitted
paid work, so the Worker gains exactly the privileges those admissions use (listed in ``WORKER_GRANTS``
below). ``spend_guardrails`` gets SELECT and ``UPDATE (workspace_id)`` only -- its key, which forced RLS
keeps in the Workspace: enough to read and lock the row the reservation book checks, not to change a limit.
The ledger totals it checks come from ``marketai_settled_cost_total``, which the Worker may already execute,
so it gains no read of the ledger. RLS still scopes every write to the job's Workspace. All
grants are mirrored in ``deploy/postgres/domain-tenant-roles.sql``; GRANT is idempotent, and the
release applies the roles script before and after Alembic.

The previous release keeps running while this migrates: it never reads the new table, and the new
grants only widen what the Worker may do. Downgrade revokes the Worker grants and drops the table; it
refuses while any first batch exists, because dropping the rows would lose which maximum the user
confirmed for steps that may still be running.
"""
from alembic import op
import sqlalchemy as sa

revision = "zzn193a1b2c3"
down_revision = "zzm189a1b2c3"
branch_labels = None
depends_on = None

TABLE = "marketing_first_batches"
NEW_WORKSPACE_OWNED = (TABLE,)
#: What the Worker newly needs to admit a first batch's next step (#193), per table.
WORKER_GRANTS = (
    # The product draft and its ideas, after the reading.
    ("products", "INSERT"),
    ("marketing_projects", "INSERT"),
    # A planning call: the understanding, the ideas, the one extra drafting.
    ("marketing_planning_calls", "INSERT"),
    # The ads: one generation draft, preview and run per combination of the ideas.
    ("marketing_generation_links", "INSERT"),
    ("generation_drafts", "INSERT"),
    ("resolution_previews", "INSERT"),
    ("resolution_previews", "UPDATE (submitted_at, submitted_run_id)"),
    ("resolution_preview_confirmations", "INSERT"),
    ("generation_recipe_snapshots", "INSERT"),
    ("plans", "INSERT"),
    ("generation_runs", "INSERT"),
    ("run_items", "INSERT"),
    ("generation_run_commands", "INSERT"),
    ("idempotency_records", "INSERT"),
    # Each step's reservation reads and locks the Workspace's spend limits; it never changes one.
    ("spend_guardrails", "SELECT"),
    ("spend_guardrails", "UPDATE (workspace_id)"),
)


def upgrade():
    op.create_table(TABLE,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("intake_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=True),
        sa.Column("product_id", sa.String(64), nullable=True),
        sa.Column("market", sa.String(80), nullable=False),
        sa.Column("ad_language", sa.String(35), nullable=False),
        sa.Column("strategy_language", sa.String(10), nullable=False),
        sa.Column("execution_mode", sa.String(10), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("selected_variant", sa.String(200), nullable=True),
        sa.Column("attention_json", sa.JSON(), nullable=True),
        sa.Column("progress_json", sa.JSON(), nullable=False),
        sa.Column("confirmed_max_cost", sa.String(32), nullable=False),
        sa.Column("reserved_total", sa.String(32), nullable=False),
        sa.Column("quote_hash", sa.String(64), nullable=False),
        sa.Column("operation_id", sa.String(64), nullable=False),
        sa.Column("initiator_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_marketing_first_batches_workspace_id"),
        sa.UniqueConstraint("workspace_id", "intake_id", name="uq_marketing_first_batch_intake"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["marketing_projects.workspace_id", "marketing_projects.id"],
                                ondelete="RESTRICT", name="fk_marketing_first_batch_project"),
        sa.ForeignKeyConstraint(["workspace_id", "product_id"], ["products.workspace_id", "products.id"],
                                ondelete="RESTRICT", name="fk_marketing_first_batch_product"),
        sa.ForeignKeyConstraint(["workspace_id", "operation_id"], ["operation_records.workspace_id", "operation_records.id"],
                                ondelete="RESTRICT", name="fk_marketing_first_batch_operation"),
    )
    op.create_index("ix_marketing_first_batches_workspace_id", TABLE, ["workspace_id"])
    op.create_index("ix_marketing_first_batches_project", TABLE, ["workspace_id", "project_id"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f'ALTER TABLE "{TABLE}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{TABLE}" FORCE ROW LEVEL SECURITY')
        op.execute(f'''CREATE POLICY tenant_isolation ON "{TABLE}"
            USING (marketai_context_is_current(workspace_id))
            WITH CHECK (marketai_context_is_current(workspace_id))''')
        op.execute(f'GRANT SELECT, INSERT, UPDATE ON TABLE "{TABLE}" TO marketai_domain_api')
        op.execute(f'GRANT SELECT, UPDATE ON TABLE "{TABLE}" TO marketai_domain_worker')
        op.execute(f'GRANT SELECT ON TABLE "{TABLE}" TO marketai_domain_readonly')
        for table, privilege in WORKER_GRANTS:
            op.execute(f'GRANT {privilege} ON TABLE "{table}" TO marketai_domain_worker')


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # The table FORCEs RLS: without this a role that cannot bypass it counts no rows and the guard passes.
        bind.execute(sa.text("SELECT set_config('row_security', 'off', true)"))
    if bind.execute(sa.text(f"SELECT COUNT(*) FROM {TABLE}")).scalar():
        raise RuntimeError("first batches exist; dropping them would lose the maximum each user confirmed")
    if bind.dialect.name == "postgresql":
        for table, privilege in WORKER_GRANTS:
            op.execute(f'REVOKE {privilege.split(" ")[0]} ON TABLE "{table}" FROM marketai_domain_worker')
    op.drop_index("ix_marketing_first_batches_project", table_name=TABLE)
    op.drop_index("ix_marketing_first_batches_workspace_id", table_name=TABLE)
    op.drop_table(TABLE)
