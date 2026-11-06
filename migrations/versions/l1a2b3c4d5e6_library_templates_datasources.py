"""library_templates_datasources

Adds four tables backing the #20 Creative Library extensions (dimension
templates/schemas, prompt policy baselines, data-source credential
bindings, and resolution previews).

Note: unlike other library tables, ``library_entries.entry_type`` and
``library_versions.entry_type`` carry no database CHECK constraint (see
5be2989237ed_creative_library_core.py and app/adapters/persistence/models.py
-- both leave entry_type as a plain ``String(40)``). Entry-type membership is
enforced exclusively at the application layer via
``app.library.contracts.ENTRY_TYPES`` (checked in
``app.library.service.create_entry``), so this migration does not touch any
entry_type CHECK constraint -- there is none to widen.

RLS (spec invariant, tests/test_postgres_rls.py): three of the four new
tables are ordinary workspace-owned rows and get the same
``marketai_context_is_current(workspace_id)`` tenant_isolation policy as
every prior workspace-owned table (h1a2b3c4d5e6, i1a2b3c4d5e6,
j1a2b3c4d5e6, k1a2b3c4d5e6). Two are not:

* ``dimension_concept_keys`` has a *nullable* workspace_id (platform-scope
  rows are shared across every tenant, workspace-scope rows are private --
  see the ck_concept_key_scope_workspace CHECK above). Its policy lets every
  tenant *read* platform rows plus its own workspace rows, but a tenant can
  only *write* rows scoped to its own workspace -- seeding a platform-scope
  concept key is an owner/admin operation (bypasses RLS), never something
  ``marketai_domain_api`` does on a tenant's behalf.
* ``prompt_baselines`` has no workspace_id column at all -- it is a single
  platform-wide effective Prompt Policy baseline, not tenant data. Unlike
  e9f0a1b2c3d4.PLATFORM_GLOBAL_TABLES (pure deployment/security metadata
  with zero domain-role access, e.g. ``tenant_context_secrets``), it still
  carries ordinary SELECT/INSERT/UPDATE grants for the runtime roles -- it
  is tracked in this module's own GLOBAL_TABLES export instead, so the RLS
  acceptance sweep still counts it as a deliberate, reviewed table.

Grants across all four tables follow the established
SELECT/INSERT/UPDATE-for-api, SELECT-for-worker-and-readonly split (no
DELETE anywhere -- these are append/supersede tables, not delete-able). The
exact command surface lands in later #20 tasks (T2/T5/T6/T7); grants here
are the safe default those tasks build on, not a final access-pattern
decision.

Revision ID: l1a2b3c4d5e6
Revises: k1a2b3c4d5e6
Create Date: 2026-09-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'l1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = 'k1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Picked up by the postgres RLS acceptance sweep alongside every other
#: migration's NEW_WORKSPACE_OWNED -- see tests/test_postgres_rls.py.
#: prompt_baselines is deliberately excluded: it has no workspace_id (see
#: module docstring) and is not RLS-protected by a tenant_isolation policy.
NEW_WORKSPACE_OWNED = (
    "dimension_concept_keys",
    "datasource_credential_bindings",
    "resolution_previews",
)

#: prompt_baselines is platform-wide *domain* data (unlike
#: e9f0a1b2c3d4.PLATFORM_GLOBAL_TABLES, which is pure deployment/security
#: metadata with zero domain-role access) -- it carries ordinary
#: SELECT/INSERT/UPDATE grants for the runtime roles, just no per-row RLS
#: policy since there is no workspace_id column to filter by. Tracked
#: separately from NEW_WORKSPACE_OWNED so the RLS acceptance sweep still
#: counts it as a deliberate, reviewed access-control decision.
GLOBAL_TABLES = ("prompt_baselines",)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # dimension_concept_keys
    op.create_table('dimension_concept_keys',
        sa.Column('id', sa.String(64), nullable=False),
        sa.Column('workspace_id', sa.String(64), nullable=True),
        sa.Column('scope', sa.String(20), nullable=False),
        sa.Column('name', sa.String(300), nullable=False),
        sa.Column('description', sa.Text(), nullable=False, server_default=''),
        sa.Column('created_by', sa.String(100), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(scope = 'workspace' AND workspace_id IS NOT NULL) "
            "OR (scope = 'platform' AND workspace_id IS NULL)",
            name='ck_concept_key_scope_workspace',
        ),
        sa.CheckConstraint("scope IN ('workspace','platform')", name='ck_concept_key_scope_enum'),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'],
                                name='fk_concept_keys_workspace', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name='pk_dimension_concept_keys'),
    )
    op.create_index(
        op.f('ix_dimension_concept_keys_workspace_id'),
        'dimension_concept_keys', ['workspace_id'],
    )

    # prompt_baselines
    op.create_table('prompt_baselines',
        sa.Column('id', sa.String(64), nullable=False),
        sa.Column('version_no', sa.Integer(), nullable=False),
        sa.Column('content_json', sa.JSON(), nullable=False),
        sa.Column('content_hash', sa.String(64), nullable=False),
        sa.Column('state', sa.String(20), nullable=False),
        sa.Column('published_by', sa.String(100), nullable=False),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('superseded_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('withdrawn_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('withdraw_reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('withdrawn_by', sa.String(100), nullable=False, server_default=''),
        sa.CheckConstraint('version_no >= 1', name='ck_prompt_baseline_version_positive'),
        sa.CheckConstraint("state IN ('effective','superseded','withdrawn')",
                           name='ck_prompt_baseline_state_enum'),
        sa.PrimaryKeyConstraint('id', name='pk_prompt_baselines'),
        sa.UniqueConstraint('version_no', name='uq_prompt_baseline_version_no'),
    )
    # Partial unique index, same "single current row" shape as
    # uq_library_version_single_effective (df25fa1c9683): SQLite supports
    # WHERE-qualified indexes too, so this is created unconditionally with
    # both dialect-specific where clauses -- without sqlite_where here, the
    # index would silently become a non-partial (whole-table) unique index
    # on SQLite, which is both wrong and unusable once a second
    # superseded/withdrawn row exists.
    op.create_index(
        'ix_prompt_baseline_one_effective', 'prompt_baselines',
        ['state'], unique=True,
        sqlite_where=sa.text("state = 'effective'"),
        postgresql_where=sa.text("state = 'effective'"),
    )

    # datasource_credential_bindings
    op.create_table('datasource_credential_bindings',
        sa.Column('id', sa.String(64), nullable=False),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('data_source_entry_id', sa.String(64), nullable=False),
        sa.Column('credential_version_id', sa.String(64), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('state', sa.String(20), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('revision >= 1', name='ck_dcb_revision_positive'),
        sa.CheckConstraint("state IN ('active','superseded')", name='ck_dcb_state_enum'),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'],
                                name='fk_dcb_workspace', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['workspace_id', 'data_source_entry_id'],
                                ['library_entries.workspace_id', 'library_entries.id'],
                                name='fk_dcb_workspace_entry', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['workspace_id', 'credential_version_id'],
            ['credential_versions.workspace_id', 'credential_versions.id'],
            name='fk_dcb_workspace_credential', ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id', name='pk_datasource_credential_bindings'),
        sa.UniqueConstraint('data_source_entry_id', 'revision', name='uq_dcb_entry_revision'),
    )
    op.create_index(
        op.f('ix_datasource_credential_bindings_workspace_id'),
        'datasource_credential_bindings', ['workspace_id'],
    )
    op.create_index(
        'ix_dcb_one_active', 'datasource_credential_bindings',
        ['data_source_entry_id', 'state'], unique=True,
        sqlite_where=sa.text("state = 'active'"),
        postgresql_where=sa.text("state = 'active'"),
    )

    # resolution_previews
    op.create_table('resolution_previews',
        sa.Column('id', sa.String(64), nullable=False),
        sa.Column('workspace_id', sa.String(64), nullable=False),
        sa.Column('generation_draft_id', sa.String(64), nullable=False),
        sa.Column('draft_revision', sa.Integer(), nullable=False),
        sa.Column('snapshot_json', sa.JSON(), nullable=False),
        sa.Column('snapshot_schema_version', sa.String(20), nullable=False, server_default='20.1'),
        sa.Column('preview_hash', sa.String(64), nullable=False),
        sa.Column('recipe_input_hash', sa.String(64), nullable=False),
        sa.Column('execution_mode', sa.String(10), nullable=False),
        sa.Column('valid_until', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_by', sa.String(100), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('invalidated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('invalidation_reason', sa.String(300), nullable=False, server_default=''),
        sa.Column('submitted_run_id', sa.String(64), nullable=True),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'],
                                name='fk_resolution_previews_workspace', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name='pk_resolution_previews'),
    )
    op.create_index(
        op.f('ix_resolution_previews_workspace_id'),
        'resolution_previews', ['workspace_id'],
    )
    op.create_index(
        op.f('ix_resolution_previews_preview_hash'),
        'resolution_previews', ['preview_hash'],
    )
    op.create_index(
        op.f('ix_resolution_previews_recipe_input_hash'),
        'resolution_previews', ['recipe_input_hash'],
    )
    op.create_index(
        op.f('ix_resolution_previews_execution_mode'),
        'resolution_previews', ['execution_mode'],
    )

    # entry_type on library_entries/library_versions carries no database CHECK
    # constraint (see module docstring above) -- nothing to widen here. The
    # six new entry types are accepted purely by contracts.ENTRY_TYPES.

    _install_rls_and_grants()


def _install_rls_and_grants() -> None:
    if not _is_postgres():
        # SQLite: the repository layer is the only enforcement available,
        # same as every other tenant-owned table (see h1a2b3c4d5e6).
        return

    # -- dimension_concept_keys: hybrid platform/workspace scope. Reads see
    # platform rows (workspace_id IS NULL) plus the caller's own workspace
    # rows; writes must target the caller's own workspace -- a tenant-facing
    # role can never write a platform-scope row.
    op.execute('ALTER TABLE "dimension_concept_keys" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE "dimension_concept_keys" FORCE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "dimension_concept_keys"')
    op.execute(
        '''CREATE POLICY tenant_isolation ON "dimension_concept_keys"
           USING (workspace_id IS NULL OR marketai_context_is_current(workspace_id))
           WITH CHECK (workspace_id IS NOT NULL AND marketai_context_is_current(workspace_id))'''
    )
    op.execute("GRANT SELECT, INSERT ON TABLE dimension_concept_keys TO marketai_domain_api")
    op.execute("GRANT SELECT ON TABLE dimension_concept_keys TO marketai_domain_worker")
    op.execute("GRANT SELECT ON TABLE dimension_concept_keys TO marketai_domain_readonly")

    # -- datasource_credential_bindings, resolution_previews: ordinary
    # workspace-owned rows, same tenant_isolation shape as every prior table.
    for table in ("datasource_credential_bindings", "resolution_previews"):
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"')
        op.execute(
            f'''CREATE POLICY tenant_isolation ON "{table}"
               USING (marketai_context_is_current(workspace_id))
               WITH CHECK (marketai_context_is_current(workspace_id))'''
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE {table} TO marketai_domain_api")
        op.execute(f"GRANT SELECT ON TABLE {table} TO marketai_domain_worker")
        op.execute(f"GRANT SELECT ON TABLE {table} TO marketai_domain_readonly")

    # -- prompt_baselines: platform-global (no workspace_id, see module
    # docstring) -- no RLS policy, ordinary table grants only.
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE prompt_baselines TO marketai_domain_api")
    op.execute("GRANT SELECT ON TABLE prompt_baselines TO marketai_domain_worker")
    op.execute("GRANT SELECT ON TABLE prompt_baselines TO marketai_domain_readonly")


def _revoke_rls_and_grants() -> None:
    if not _is_postgres():
        return
    op.execute(
        "REVOKE ALL ON TABLE prompt_baselines FROM "
        "marketai_domain_api, marketai_domain_worker, marketai_domain_readonly"
    )
    for table in ("dimension_concept_keys", "datasource_credential_bindings", "resolution_previews"):
        op.execute(
            f"REVOKE ALL ON TABLE {table} FROM "
            "marketai_domain_api, marketai_domain_worker, marketai_domain_readonly"
        )
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    _revoke_rls_and_grants()
    op.drop_table('resolution_previews')
    op.drop_table('datasource_credential_bindings')
    op.drop_table('prompt_baselines')
    op.drop_table('dimension_concept_keys')
