# Templates, Prompt Policy, DataSources & ResolutionPreview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the Creative Library with dimension templates, prompt policy, data sources, and deterministic ResolutionPreview — issue #20.

**Architecture:** Reuse the existing `LibraryEntry → LibraryDraft → LibraryVersion` three-layer model for six new entry types. Generic CRUD (create/edit/validate/publish) flows through existing `service.py`; domain-specific commands (`apply_template`, `compose_prompt`, `resolve_preview`, `bind_credential`) live in new per-domain modules under `app/library/`. Four new database tables for domain-specific concerns. Test fixtures move to a shared support module.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x (mapped_column), Alembic, pytest, Hypothesis, PostgreSQL + SQLite.

**Spec:** `docs/superpowers/specs/2026-09-01-templates-prompt-policy-datasources-preview-design.md`

## Global Constraints

- All IDs: `new_id("prefix")` → `"prefix_{uuid4hex}"`, `String(64)`.
- Every tenant-scoped table: `workspace_id` FK → `workspaces.id` (CASCADE) + index.
- Timestamps: `DateTime(timezone=True)`, `default=utcnow`.
- Enum columns: `String(20-40)` + `CheckConstraint`.
- JSON payloads: `JSON` mapped column, `default=dict`.
- Entry types register in `contracts.ENTRY_TYPES`; owner-only types in `OWNER_ONLY_PUBLISH`.
- Commands use `_run_with_idempotency(context, key, command, fingerprint, body_fn)`.
- Dual-actor audit: `initiator_id` + `initiator_role` + `executor`.
- Monetary values: canonical decimal strings (`"29.99"`), never JSON floats.
- No credential secrets in content, recipe, or logs.

---

### Task 1: Migration, ORM Models & Entry Type Registration

**Files:**
- Create: `migrations/versions/l1a2b3c4d5e6_library_templates_datasources.py`
- Modify: `app/adapters/persistence/models.py`
- Modify: `app/library/contracts.py`
- Create: `tests/support/__init__.py`
- Create: `tests/support/library_fixtures.py`
- Modify: `tests/test_library_core.py` (import from support)
- Test: `tests/test_library_migration.py` (extend existing)

**Interfaces:**
- Consumes: existing `Base`, `new_id`, `utcnow`, `WorkspaceRecord` from `models.py`
- Produces: `DimensionConceptKeyRecord`, `PromptBaselineRecord`, `DatasourceCredentialBindingRecord`, `ResolutionPreviewRecord` ORM classes; six new `ENTRY_*` constants and updated `ENTRY_TYPES`, `OWNER_ONLY_PUBLISH` in `contracts.py`; `LibraryFixtures` in `tests/support/library_fixtures.py`

- [ ] **Step 1: Write test for new entry type constants**

```python
# tests/test_entry_types.py
import unittest
from app.library.contracts import (
    ENTRY_TYPES, OWNER_ONLY_PUBLISH, ORIGIN_COMMANDS,
    ENTRY_DIMENSION_TEMPLATE, ENTRY_DIMENSION_SCHEMA,
    ENTRY_PROMPT_POLICY, ENTRY_DATA_SOURCE,
    ENTRY_MANUAL_FACT_SET, ENTRY_TRUTH_RESOLUTION_POLICY,
    COMMAND_APPLY,
)


class TestEntryTypeRegistration(unittest.TestCase):
    def test_new_types_registered(self):
        for t in (
            ENTRY_DIMENSION_TEMPLATE, ENTRY_DIMENSION_SCHEMA,
            ENTRY_PROMPT_POLICY, ENTRY_DATA_SOURCE,
            ENTRY_MANUAL_FACT_SET, ENTRY_TRUTH_RESOLUTION_POLICY,
        ):
            self.assertIn(t, ENTRY_TYPES)

    def test_owner_only_publish(self):
        for t in (ENTRY_PROMPT_POLICY, ENTRY_DATA_SOURCE,
                  ENTRY_MANUAL_FACT_SET, ENTRY_TRUTH_RESOLUTION_POLICY):
            self.assertIn(t, OWNER_ONLY_PUBLISH)
        for t in (ENTRY_DIMENSION_TEMPLATE, ENTRY_DIMENSION_SCHEMA):
            self.assertNotIn(t, OWNER_ONLY_PUBLISH)

    def test_command_apply(self):
        self.assertIn(COMMAND_APPLY, ORIGIN_COMMANDS)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_entry_types.py -v`
Expected: FAIL with ImportError (constants not defined)

- [ ] **Step 3: Add entry type constants and COMMAND_APPLY to contracts.py**

Add after the existing `ENTRY_MARKET_MAPPING` definition (around line 27):

```python
ENTRY_DIMENSION_TEMPLATE = "dimension_template"
ENTRY_DIMENSION_SCHEMA = "dimension_schema"
ENTRY_PROMPT_POLICY = "prompt_policy"
ENTRY_DATA_SOURCE = "data_source"
ENTRY_MANUAL_FACT_SET = "manual_fact_set"
ENTRY_TRUTH_RESOLUTION_POLICY = "truth_resolution_policy"
```

Update `ENTRY_TYPES` frozenset to include all six. Update `OWNER_ONLY_PUBLISH` to include `{ENTRY_OFFER, ENTRY_PROMPT_POLICY, ENTRY_DATA_SOURCE, ENTRY_MANUAL_FACT_SET, ENTRY_TRUTH_RESOLUTION_POLICY}`. Add `COMMAND_APPLY = "apply"` and update `ORIGIN_COMMANDS`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_entry_types.py -v`
Expected: PASS

- [ ] **Step 5: Write ORM models for four new tables**

Add to `app/adapters/persistence/models.py`:

```python
class DimensionConceptKeyRecord(Base):
    __tablename__ = "dimension_concept_keys"
    __table_args__ = (
        CheckConstraint(
            "(scope = 'workspace' AND workspace_id IS NOT NULL) "
            "OR (scope = 'platform' AND workspace_id IS NULL)",
            name="ck_concept_key_scope_workspace",
        ),
        CheckConstraint(
            "scope IN ('workspace', 'platform')",
            name="ck_concept_key_scope_enum",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True, index=True
    )
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class PromptBaselineRecord(Base):
    __tablename__ = "prompt_baselines"
    __table_args__ = (
        CheckConstraint("version_no >= 1", name="ck_prompt_baseline_version_positive"),
        CheckConstraint(
            "state IN ('effective', 'superseded', 'withdrawn')",
            name="ck_prompt_baseline_state_enum",
        ),
        UniqueConstraint("version_no", name="uq_prompt_baseline_version_no"),
        Index(
            "ix_prompt_baseline_one_effective",
            "state",
            unique=True,
            postgresql_where=text("state = 'effective'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    content_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    published_by: Mapped[str] = mapped_column(String(100), nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    withdrawn_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    withdraw_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")


class DatasourceCredentialBindingRecord(Base):
    __tablename__ = "datasource_credential_bindings"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_dcb_revision_positive"),
        CheckConstraint(
            "state IN ('active', 'superseded')",
            name="ck_dcb_state_enum",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "data_source_entry_id"],
            ["library_entries.workspace_id", "library_entries.id"],
            name="fk_dcb_workspace_entry",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "data_source_entry_id", "revision", name="uq_dcb_entry_revision"
        ),
        Index(
            "ix_dcb_one_active",
            "data_source_entry_id", "state",
            unique=True,
            postgresql_where=text("state = 'active'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    data_source_entry_id: Mapped[str] = mapped_column(String(64), nullable=False)
    credential_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class ResolutionPreviewRecord(Base):
    __tablename__ = "resolution_previews"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_resolution_previews_workspace",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    generation_draft_id: Mapped[str] = mapped_column(String(64), nullable=False)
    draft_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    snapshot_schema_version: Mapped[str] = mapped_column(
        String(20), nullable=False, default="20.1"
    )
    preview_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    recipe_input_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    execution_mode: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    valid_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    invalidation_reason: Mapped[str] = mapped_column(
        String(300), nullable=False, default=""
    )
    submitted_run_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

- [ ] **Step 6: Write the Alembic migration**

Create `migrations/versions/l1a2b3c4d5e6_library_templates_datasources.py`:

```python
"""library_templates_datasources

Revision ID: l1a2b3c4d5e6
Revises: k1a2b3c4d5e6
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'l1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = 'k1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

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
    op.create_index('ix_concept_keys_workspace', 'dimension_concept_keys', ['workspace_id'])

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
        sa.CheckConstraint('version_no >= 1', name='ck_prompt_baseline_version_positive'),
        sa.CheckConstraint("state IN ('effective','superseded','withdrawn')",
                           name='ck_prompt_baseline_state_enum'),
        sa.PrimaryKeyConstraint('id', name='pk_prompt_baselines'),
        sa.UniqueConstraint('version_no', name='uq_prompt_baseline_version_no'),
    )
    if _is_postgres():
        op.create_index('ix_prompt_baseline_one_effective', 'prompt_baselines',
                        ['state'], unique=True,
                        postgresql_where=sa.text("state = 'effective'"))

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
        sa.PrimaryKeyConstraint('id', name='pk_datasource_credential_bindings'),
        sa.UniqueConstraint('data_source_entry_id', 'revision', name='uq_dcb_entry_revision'),
    )
    op.create_index('ix_dcb_workspace', 'datasource_credential_bindings', ['workspace_id'])
    if _is_postgres():
        op.create_index('ix_dcb_one_active', 'datasource_credential_bindings',
                        ['data_source_entry_id', 'state'], unique=True,
                        postgresql_where=sa.text("state = 'active'"))

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
    op.create_index('ix_rpv_workspace', 'resolution_previews', ['workspace_id'])
    op.create_index('ix_rpv_preview_hash', 'resolution_previews', ['preview_hash'])
    op.create_index('ix_rpv_recipe_input_hash', 'resolution_previews', ['recipe_input_hash'])
    op.create_index('ix_rpv_execution_mode', 'resolution_previews', ['execution_mode'])


    # Update entry_type CHECK constraints on library_entries and library_versions
    _new_types = (
        "'product','offer','person','reference_asset','reference_group',"
        "'reference_selection_rule','market_source_mapping',"
        "'dimension_template','dimension_schema','prompt_policy',"
        "'data_source','manual_fact_set','truth_resolution_policy'"
    )
    with op.batch_alter_table('library_entries') as batch_op:
        batch_op.drop_constraint('ck_entry_type', type_='check')
        batch_op.create_check_constraint(
            'ck_entry_type', f"entry_type IN ({_new_types})")
    with op.batch_alter_table('library_versions') as batch_op:
        batch_op.drop_constraint('ck_version_entry_type', type_='check')
        batch_op.create_check_constraint(
            'ck_version_entry_type', f"entry_type IN ({_new_types})")


def downgrade() -> None:
    # Restore original entry_type CHECK constraints
    _old_types = (
        "'product','offer','person','reference_asset','reference_group',"
        "'reference_selection_rule','market_source_mapping'"
    )
    with op.batch_alter_table('library_versions') as batch_op:
        batch_op.drop_constraint('ck_version_entry_type', type_='check')
        batch_op.create_check_constraint(
            'ck_version_entry_type', f"entry_type IN ({_old_types})")
    with op.batch_alter_table('library_entries') as batch_op:
        batch_op.drop_constraint('ck_entry_type', type_='check')
        batch_op.create_check_constraint(
            'ck_entry_type', f"entry_type IN ({_old_types})")
    op.drop_table('resolution_previews')
    op.drop_table('datasource_credential_bindings')
    op.drop_table('prompt_baselines')
    op.drop_table('dimension_concept_keys')
```

- [ ] **Step 7: Move LibraryFixtures to support module**

Create `tests/support/__init__.py` (empty) and `tests/support/library_fixtures.py` by moving `LibraryFixtures`, `png_bytes`, `jpeg_with_exif`, `FUTURE`, `PAST` from `tests/test_library_core.py`. Update imports in `test_library_core.py` to `from tests.support.library_fixtures import LibraryFixtures, ...`.

- [ ] **Step 8: Run migration and existing tests**

Run: `python -m pytest tests/test_library_migration.py tests/test_entry_types.py tests/test_library_core.py -v`
Expected: All PASS. Migration creates tables; existing tests unaffected.

- [ ] **Step 9: Commit**

```bash
git add app/adapters/persistence/models.py app/library/contracts.py \
       migrations/versions/l1a2b3c4d5e6_library_templates_datasources.py \
       tests/support/ tests/test_entry_types.py tests/test_library_core.py
git commit -m "feat(library): migration, ORM models and entry types for #20"
```

---

### Task 2: Content Validators for New Entry Types

**Files:**
- Modify: `app/library/contracts.py`
- Test: `tests/test_template_contracts.py`

**Interfaces:**
- Consumes: `ENTRY_*` constants, `Finding`, `ValidationReport`, `canonical_hash`, `validate_content` from Task 1
- Produces: `_validate_dimension_template`, `_validate_dimension_schema`, `_validate_prompt_policy`, `_validate_data_source`, `_validate_manual_fact_set`, `_validate_truth_resolution_policy` validators registered in `_VALIDATORS`; `extract_dependencies` extended for new types; new `REL_*` dependency relations

- [ ] **Step 1: Write failing tests for dimension_template validation**

```python
# tests/test_template_contracts.py
import unittest
from app.library.contracts import (
    validate_content, extract_dependencies, canonical_hash,
    ENTRY_DIMENSION_TEMPLATE, ENTRY_DIMENSION_SCHEMA,
    ENTRY_PROMPT_POLICY, ENTRY_DATA_SOURCE,
    ENTRY_MANUAL_FACT_SET, ENTRY_TRUTH_RESOLUTION_POLICY,
)


class TestDimensionTemplateValidation(unittest.TestCase):

    def _template_content(self, **overrides):
        base = {
            "dimensions": [
                {
                    "id": "dim-color",
                    "name": "Color",
                    "values": [
                        {"id": "val-red", "name": "Red",
                         "positive_fragments": ["vibrant red"], "negative_fragments": []},
                        {"id": "val-blue", "name": "Blue",
                         "positive_fragments": ["deep blue"], "negative_fragments": []},
                    ],
                }
            ],
            "constraints": [],
            "concept_key_mappings": [],
            "combination_cardinality": "one_per_dimension",
        }
        base.update(overrides)
        return base

    def test_valid_template_passes(self):
        report = validate_content(ENTRY_DIMENSION_TEMPLATE, self._template_content())
        self.assertTrue(report.ok, report.blocking)

    def test_missing_dimensions_is_blocking(self):
        report = validate_content(ENTRY_DIMENSION_TEMPLATE, {"constraints": []})
        self.assertFalse(report.ok)
        codes = {f.code for f in report.blocking}
        self.assertIn("template_dimensions_required", codes)

    def test_empty_dimensions_is_blocking(self):
        report = validate_content(ENTRY_DIMENSION_TEMPLATE, self._template_content(dimensions=[]))
        self.assertFalse(report.ok)

    def test_duplicate_dimension_ids_blocking(self):
        dims = [
            {"id": "dim-a", "name": "A", "values": [{"id": "v1", "name": "V1",
             "positive_fragments": [], "negative_fragments": []}]},
            {"id": "dim-a", "name": "B", "values": [{"id": "v2", "name": "V2",
             "positive_fragments": [], "negative_fragments": []}]},
        ]
        report = validate_content(ENTRY_DIMENSION_TEMPLATE, self._template_content(dimensions=dims))
        self.assertFalse(report.ok)
        codes = {f.code for f in report.blocking}
        self.assertIn("template_duplicate_dimension_id", codes)

    def test_duplicate_value_ids_blocking(self):
        dims = [{
            "id": "dim-a", "name": "A",
            "values": [
                {"id": "same", "name": "V1", "positive_fragments": [], "negative_fragments": []},
                {"id": "same", "name": "V2", "positive_fragments": [], "negative_fragments": []},
            ],
        }]
        report = validate_content(ENTRY_DIMENSION_TEMPLATE, self._template_content(dimensions=dims))
        self.assertFalse(report.ok)

    def test_constraint_references_unknown_dim_blocking(self):
        content = self._template_content(constraints=[
            {"type": "requires", "from_dimension_id": "dim-color", "from_value_id": "val-red",
             "to_dimension_id": "nonexistent", "to_value_id": "v1"},
        ])
        report = validate_content(ENTRY_DIMENSION_TEMPLATE, content)
        self.assertFalse(report.ok)

    def test_invalid_combination_cardinality_blocking(self):
        report = validate_content(ENTRY_DIMENSION_TEMPLATE,
                                  self._template_content(combination_cardinality="invalid"))
        self.assertFalse(report.ok)


class TestDimensionSchemaValidation(unittest.TestCase):

    def _schema_content(self, **overrides):
        base = {
            "product_entry_id": "lib_abc123",
            "derived_from_template_version_id": "lib_tmpl_v1",
            "dimensions": [
                {
                    "id": "local-dim-1",
                    "name": "Color",
                    "source_dimension_id": "dim-color",
                    "values": [
                        {"id": "local-val-1", "name": "Red", "source_value_id": "val-red",
                         "positive_fragments": ["vibrant red"], "negative_fragments": []},
                    ],
                }
            ],
            "constraints": [],
            "combination_cardinality": "one_per_dimension",
        }
        base.update(overrides)
        return base

    def test_valid_schema_passes(self):
        report = validate_content(ENTRY_DIMENSION_SCHEMA, self._schema_content())
        self.assertTrue(report.ok, report.blocking)

    def test_missing_product_entry_id_blocking(self):
        report = validate_content(ENTRY_DIMENSION_SCHEMA, self._schema_content(product_entry_id=""))
        self.assertFalse(report.ok)
        codes = {f.code for f in report.blocking}
        self.assertIn("schema_product_entry_required", codes)


class TestPromptPolicyValidation(unittest.TestCase):

    def _policy_content(self, **overrides):
        base = {
            "registered_tokens": [
                {"name": "product_name", "description": "Product name", "required": True}
            ],
            "locked_phrases": [],
            "brand_tone": {"style": "professional"},
            "allowed_template_entry_ids": [],
            "optional_clauses": [],
        }
        base.update(overrides)
        return base

    def test_valid_policy_passes(self):
        report = validate_content(ENTRY_PROMPT_POLICY, self._policy_content())
        self.assertTrue(report.ok, report.blocking)

    def test_duplicate_token_names_blocking(self):
        tokens = [
            {"name": "x", "description": "X", "required": True},
            {"name": "x", "description": "Y", "required": False},
        ]
        report = validate_content(ENTRY_PROMPT_POLICY, self._policy_content(registered_tokens=tokens))
        self.assertFalse(report.ok)


class TestDataSourceValidation(unittest.TestCase):

    def _ds_content(self, **overrides):
        base = {
            "adapter_kind": "manual",
            "capabilities": ["price", "shipping"],
            "market_context": ["US"],
            "config": {},
            "config_schema_version": "manual/1.0",
            "execution_mode": "real",
        }
        base.update(overrides)
        return base

    def test_valid_manual_source_passes(self):
        report = validate_content(ENTRY_DATA_SOURCE, self._ds_content())
        self.assertTrue(report.ok, report.blocking)

    def test_invalid_adapter_kind_blocking(self):
        report = validate_content(ENTRY_DATA_SOURCE, self._ds_content(adapter_kind="unknown"))
        self.assertFalse(report.ok)

    def test_invalid_execution_mode_blocking(self):
        report = validate_content(ENTRY_DATA_SOURCE, self._ds_content(execution_mode="invalid"))
        self.assertFalse(report.ok)


class TestManualFactSetValidation(unittest.TestCase):

    def _fact_content(self, **overrides):
        base = {
            "data_source_entry_id": "lib_ds_001",
            "facts": [
                {
                    "fact_id": "fact-001",
                    "fact_type": "price",
                    "offer_version_id": "lib_offer_v1",
                    "country_code": "US",
                    "amount": "29.99",
                    "currency": "USD",
                    "valid_from": "2026-09-01",
                    "valid_to": "2026-12-31",
                    "evidence_source": "manual_entry",
                    "evidence_note": "Q4 pricing",
                }
            ],
        }
        base.update(overrides)
        return base

    def test_valid_price_fact_passes(self):
        report = validate_content(ENTRY_MANUAL_FACT_SET, self._fact_content())
        self.assertTrue(report.ok, report.blocking)

    def test_float_amount_blocking(self):
        facts = [{**self._fact_content()["facts"][0], "amount": 29.99}]
        report = validate_content(ENTRY_MANUAL_FACT_SET, self._fact_content(facts=facts))
        self.assertFalse(report.ok)
        codes = {f.code for f in report.blocking}
        self.assertIn("fact_amount_not_string", codes)

    def test_nan_amount_blocking(self):
        facts = [{**self._fact_content()["facts"][0], "amount": "NaN"}]
        report = validate_content(ENTRY_MANUAL_FACT_SET, self._fact_content(facts=facts))
        self.assertFalse(report.ok)

    def test_lowercase_currency_blocking(self):
        facts = [{**self._fact_content()["facts"][0], "currency": "usd"}]
        report = validate_content(ENTRY_MANUAL_FACT_SET, self._fact_content(facts=facts))
        self.assertFalse(report.ok)

    def test_missing_valid_to_blocking(self):
        fact = {k: v for k, v in self._fact_content()["facts"][0].items() if k != "valid_to"}
        report = validate_content(ENTRY_MANUAL_FACT_SET, self._fact_content(facts=[fact]))
        self.assertFalse(report.ok)


class TestTruthResolutionPolicyValidation(unittest.TestCase):

    def _policy_content(self, **overrides):
        base = {
            "rules": [
                {
                    "fact_type": "price",
                    "source_priority": ["lib_ds_001"],
                    "allow_fallback": False,
                    "max_staleness_seconds": 86400,
                    "on_failure": "block",
                }
            ],
        }
        base.update(overrides)
        return base

    def test_valid_policy_passes(self):
        report = validate_content(ENTRY_TRUTH_RESOLUTION_POLICY, self._policy_content())
        self.assertTrue(report.ok, report.blocking)

    def test_duplicate_fact_type_blocking(self):
        rules = [
            {"fact_type": "price", "source_priority": ["ds1"],
             "allow_fallback": False, "max_staleness_seconds": 0, "on_failure": "block"},
            {"fact_type": "price", "source_priority": ["ds2"],
             "allow_fallback": False, "max_staleness_seconds": 0, "on_failure": "block"},
        ]
        report = validate_content(ENTRY_TRUTH_RESOLUTION_POLICY, self._policy_content(rules=rules))
        self.assertFalse(report.ok)

    def test_price_cannot_degrade(self):
        rules = [{"fact_type": "price", "source_priority": ["ds1"],
                  "allow_fallback": False, "max_staleness_seconds": 0, "on_failure": "degrade"}]
        report = validate_content(ENTRY_TRUTH_RESOLUTION_POLICY, self._policy_content(rules=rules))
        self.assertFalse(report.ok)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_template_contracts.py -v`
Expected: FAIL — validator functions not yet implemented

- [ ] **Step 3: Implement all six validators in contracts.py**

Add validation functions after the existing `_validate_selection_rule`:

```python
ADAPTER_KINDS = frozenset({"manual", "shopify", "mock"})
EXECUTION_MODES = frozenset({"real", "mock"})
CARDINALITIES = frozenset({"one_per_dimension"})
CONSTRAINT_TYPES = frozenset({"requires", "excludes", "max_occurrence"})
FACT_TYPES = frozenset({"price", "shipping_availability", "delivery_time", "tax", "discount"})
ON_FAILURE_MODES = frozenset({"block", "degrade", "skip"})
PRICE_FORBIDDEN_ON_FAILURE = frozenset({"degrade"})


def _is_str(value) -> bool:
    return isinstance(value, str) and len(value.strip()) > 0


def _validate_dimension_template(content: dict, findings: list[Finding]) -> None:
    dims = content.get("dimensions")
    if not isinstance(dims, list) or not dims:
        findings.append(Finding("template_dimensions_required", "至少需要一个维度", "dimensions"))
        return
    dim_ids: set[str] = set()
    all_value_ids: set[str] = set()
    for i, dim in enumerate(dims):
        path = f"dimensions[{i}]"
        if not isinstance(dim, dict):
            findings.append(Finding("template_dim_shape", "维度必须是对象", path))
            continue
        dim_id = dim.get("id")
        if not _is_str(dim_id):
            findings.append(Finding("template_dim_id", "维度需要稳定 ID", path))
        elif dim_id in dim_ids:
            findings.append(Finding("template_duplicate_dimension_id",
                                    f"重复维度 ID: {dim_id}", path))
        else:
            dim_ids.add(str(dim_id))
        if not _is_str(dim.get("name")):
            findings.append(Finding("template_dim_name", "维度需要名称", path))
        values = dim.get("values")
        if not isinstance(values, list) or not values:
            findings.append(Finding("template_dim_values", "维度至少需要一个取值", f"{path}.values"))
            continue
        for j, val in enumerate(values):
            vpath = f"{path}.values[{j}]"
            if not isinstance(val, dict):
                findings.append(Finding("template_val_shape", "取值必须是对象", vpath))
                continue
            val_id = val.get("id")
            if not _is_str(val_id):
                findings.append(Finding("template_val_id", "取值需要稳定 ID", vpath))
            elif val_id in all_value_ids:
                findings.append(Finding("template_duplicate_value_id",
                                        f"重复取值 ID: {val_id}", vpath))
            else:
                all_value_ids.add(str(val_id))
            if not isinstance(val.get("positive_fragments"), list):
                findings.append(Finding("template_val_pos_frag", "positive_fragments 必须是列表", vpath))
            if not isinstance(val.get("negative_fragments"), list):
                findings.append(Finding("template_val_neg_frag", "negative_fragments 必须是列表", vpath))
    # Validate constraints reference known IDs
    constraints = content.get("constraints") or []
    if not isinstance(constraints, list):
        findings.append(Finding("template_constraints_shape", "constraints 必须是列表", "constraints"))
    else:
        for i, c in enumerate(constraints):
            cpath = f"constraints[{i}]"
            if not isinstance(c, dict):
                findings.append(Finding("template_constraint_shape", "约束必须是对象", cpath))
                continue
            ctype = c.get("type")
            if ctype not in CONSTRAINT_TYPES:
                findings.append(Finding("template_constraint_type",
                                        f"type 必须是 {sorted(CONSTRAINT_TYPES)} 之一", cpath))
                continue
            if ctype == "requires":
                for fld in ("from_dimension_id", "to_dimension_id"):
                    if c.get(fld) not in dim_ids:
                        findings.append(Finding("template_constraint_ref",
                                                f"{fld} 引用了未知维度", f"{cpath}.{fld}"))
            elif ctype == "excludes":
                for fld in ("dimension_id_a", "dimension_id_b"):
                    if c.get(fld) not in dim_ids:
                        findings.append(Finding("template_constraint_ref",
                                                f"{fld} 引用了未知维度", f"{cpath}.{fld}"))
            elif ctype == "max_occurrence":
                if c.get("dimension_id") not in dim_ids:
                    findings.append(Finding("template_constraint_ref",
                                            "dimension_id 引用了未知维度", f"{cpath}.dimension_id"))
                max_val = c.get("max")
                if not isinstance(max_val, int) or isinstance(max_val, bool) or max_val < 1:
                    findings.append(Finding("template_max_occurrence",
                                            "max 必须是正整数", f"{cpath}.max"))
    cardinality = content.get("combination_cardinality")
    if cardinality not in CARDINALITIES:
        findings.append(Finding("template_cardinality",
                                f"combination_cardinality 必须是 {sorted(CARDINALITIES)} 之一",
                                "combination_cardinality"))


def _validate_dimension_schema(content: dict, findings: list[Finding]) -> None:
    if not _is_str(content.get("product_entry_id")):
        findings.append(Finding("schema_product_entry_required",
                                "DimensionSchema 必须绑定 product_entry_id", "product_entry_id"))
    dims = content.get("dimensions")
    if not isinstance(dims, list) or not dims:
        findings.append(Finding("schema_dimensions_required", "至少需要一个维度", "dimensions"))
        return
    dim_ids: set[str] = set()
    for i, dim in enumerate(dims):
        path = f"dimensions[{i}]"
        if not isinstance(dim, dict):
            findings.append(Finding("schema_dim_shape", "维度必须是对象", path))
            continue
        dim_id = dim.get("id")
        if not _is_str(dim_id):
            findings.append(Finding("schema_dim_id", "维度需要稳定 ID", path))
        elif dim_id in dim_ids:
            findings.append(Finding("schema_duplicate_dim_id", f"重复维度 ID: {dim_id}", path))
        else:
            dim_ids.add(str(dim_id))
        values = dim.get("values")
        if not isinstance(values, list) or not values:
            findings.append(Finding("schema_dim_values", "维度至少需要一个取值", f"{path}.values"))
    cardinality = content.get("combination_cardinality")
    if cardinality not in CARDINALITIES:
        findings.append(Finding("schema_cardinality",
                                f"combination_cardinality 必须是 {sorted(CARDINALITIES)} 之一",
                                "combination_cardinality"))


def _validate_prompt_policy(content: dict, findings: list[Finding]) -> None:
    tokens = content.get("registered_tokens")
    if not isinstance(tokens, list):
        findings.append(Finding("policy_tokens_shape", "registered_tokens 必须是列表", "registered_tokens"))
    else:
        seen_names: set[str] = set()
        for i, tok in enumerate(tokens):
            tpath = f"registered_tokens[{i}]"
            if not isinstance(tok, dict):
                findings.append(Finding("policy_token_shape", "token 必须是对象", tpath))
                continue
            name = tok.get("name")
            if not _is_str(name):
                findings.append(Finding("policy_token_name", "token 需要 name", tpath))
            elif name in seen_names:
                findings.append(Finding("policy_duplicate_token", f"重复 token: {name}", tpath))
            else:
                seen_names.add(str(name))
    phrases = content.get("locked_phrases")
    if phrases is not None and not isinstance(phrases, list):
        findings.append(Finding("policy_phrases_shape", "locked_phrases 必须是列表", "locked_phrases"))
    # Recursive token detection: token default containing {{other_token}}
    import re
    _tok_re = re.compile(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}")
    if isinstance(tokens, list):
        token_names = {t.get("name") for t in tokens if isinstance(t, dict) and t.get("name")}
        for i, tok in enumerate(tokens):
            if not isinstance(tok, dict):
                continue
            default = tok.get("default")
            if isinstance(default, str):
                refs = _tok_re.findall(default)
                for ref in refs:
                    if ref in token_names:
                        findings.append(Finding(
                            "policy_recursive_token",
                            f"token {tok.get('name')!r} 的 default 引用了另一个 token {{{{{ref}}}}}",
                            f"registered_tokens[{i}].default",
                        ))
    entries = content.get("allowed_template_entry_ids")
    if entries is not None and not isinstance(entries, list):
        findings.append(Finding("policy_allowed_templates_shape",
                                "allowed_template_entry_ids 必须是列表", "allowed_template_entry_ids"))


def _validate_data_source(content: dict, findings: list[Finding]) -> None:
    if content.get("adapter_kind") not in ADAPTER_KINDS:
        findings.append(Finding("ds_adapter_kind", f"adapter_kind 必须是 {sorted(ADAPTER_KINDS)} 之一",
                                "adapter_kind"))
    if content.get("execution_mode") not in EXECUTION_MODES:
        findings.append(Finding("ds_execution_mode", "必须显式声明 execution_mode", "execution_mode"))
    config = content.get("config")
    if config is not None and not isinstance(config, dict):
        findings.append(Finding("ds_config_shape", "config 必须是对象", "config"))


def _validate_manual_fact_set(content: dict, findings: list[Finding]) -> None:
    if not _is_str(content.get("data_source_entry_id")):
        findings.append(Finding("fact_set_ds_required",
                                "必须显式声明 data_source_entry_id", "data_source_entry_id"))
    facts = content.get("facts")
    if not isinstance(facts, list) or not facts:
        findings.append(Finding("fact_set_empty", "至少需要一条事实", "facts"))
        return
    seen_ids: set[str] = set()
    for i, fact in enumerate(facts):
        fpath = f"facts[{i}]"
        if not isinstance(fact, dict):
            findings.append(Finding("fact_shape", "事实必须是对象", fpath))
            continue
        fact_id = fact.get("fact_id")
        if not _is_str(fact_id):
            findings.append(Finding("fact_id_required", "事实需要稳定 fact_id", fpath))
        elif fact_id in seen_ids:
            findings.append(Finding("fact_duplicate_id", f"重复 fact_id: {fact_id}", fpath))
        else:
            seen_ids.add(str(fact_id))
        fact_type = fact.get("fact_type")
        if fact_type not in FACT_TYPES:
            findings.append(Finding("fact_type_unknown",
                                    f"fact_type 必须是 {sorted(FACT_TYPES)} 之一", f"{fpath}.fact_type"))
        # Country code
        cc = fact.get("country_code")
        if not isinstance(cc, str) or len(cc) != 2 or cc != cc.upper():
            findings.append(Finding("fact_country_code", "country_code 必须是大写 2 位 ISO", f"{fpath}.country_code"))
        # Validity
        if not _is_str(fact.get("valid_from")):
            findings.append(Finding("fact_valid_from", "必须声明 valid_from", f"{fpath}.valid_from"))
        if not _is_str(fact.get("valid_to")):
            findings.append(Finding("fact_valid_to", "必须声明 valid_to", f"{fpath}.valid_to"))
        # Type-specific
        if fact_type == "price":
            amount = fact.get("amount")
            if not isinstance(amount, str):
                findings.append(Finding("fact_amount_not_string",
                                        "金额必须是 decimal 字符串", f"{fpath}.amount"))
            elif amount.lower() in ("nan", "inf", "-inf", "infinity", "-infinity"):
                findings.append(Finding("fact_amount_invalid",
                                        "金额不能是 NaN 或 Inf", f"{fpath}.amount"))
            else:
                from decimal import Decimal, InvalidOperation
                try:
                    Decimal(amount)
                except InvalidOperation:
                    findings.append(Finding("fact_amount_parse",
                                            f"无法解析金额: {amount!r}", f"{fpath}.amount"))
            currency = fact.get("currency")
            if not isinstance(currency, str) or len(currency) != 3 or currency != currency.upper():
                findings.append(Finding("fact_currency",
                                        "currency 必须是大写 3 位 ISO 4217", f"{fpath}.currency"))


def _validate_truth_resolution_policy(content: dict, findings: list[Finding]) -> None:
    rules = content.get("rules")
    if not isinstance(rules, list) or not rules:
        findings.append(Finding("trp_rules_required", "至少需要一条规则", "rules"))
        return
    seen_types: set[str] = set()
    for i, rule in enumerate(rules):
        rpath = f"rules[{i}]"
        if not isinstance(rule, dict):
            findings.append(Finding("trp_rule_shape", "规则必须是对象", rpath))
            continue
        ft = rule.get("fact_type")
        if not _is_str(ft):
            findings.append(Finding("trp_fact_type", "规则需要 fact_type", rpath))
        elif ft in seen_types:
            findings.append(Finding("trp_duplicate_fact_type",
                                    f"重复 fact_type: {ft}", rpath))
        else:
            seen_types.add(str(ft))
        sources = rule.get("source_priority")
        if not isinstance(sources, list) or not sources:
            findings.append(Finding("trp_source_priority_empty",
                                    "source_priority 不能为空", f"{rpath}.source_priority"))
        elif len(sources) != len(set(sources)):
            findings.append(Finding("trp_source_priority_dups",
                                    "source_priority 不能有重复", f"{rpath}.source_priority"))
        staleness = rule.get("max_staleness_seconds")
        if not isinstance(staleness, (int, float)) or staleness < 0:
            findings.append(Finding("trp_staleness", "max_staleness_seconds 必须 >= 0",
                                    f"{rpath}.max_staleness_seconds"))
        on_fail = rule.get("on_failure")
        if on_fail not in ON_FAILURE_MODES:
            findings.append(Finding("trp_on_failure",
                                    f"on_failure 必须是 {sorted(ON_FAILURE_MODES)} 之一",
                                    f"{rpath}.on_failure"))
        elif ft == "price" and on_fail in PRICE_FORBIDDEN_ON_FAILURE:
            findings.append(Finding("trp_price_no_degrade",
                                    "price 类型不能使用 degrade 模式", f"{rpath}.on_failure"))
```

Register them in `_VALIDATORS` dict and update `validate_content` to pass appropriate kwargs.

- [ ] **Step 4: Add dependency extraction for new types**

Add new relation constants and extend `extract_dependencies`:

```python
REL_SCHEMA_TEMPLATE = "schema_template"
REL_SCHEMA_PRODUCT = "schema_product"
REL_POLICY_TEMPLATE = "policy_template"
REL_FACT_SET_DATA_SOURCE = "fact_set_data_source"
REL_TRUTH_POLICY_DATA_SOURCE = "truth_policy_data_source"
```

Update `extract_dependencies` to handle new entry types, and `RELATION_TARGET_TYPES` to enforce correct target types.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_template_contracts.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add app/library/contracts.py tests/test_template_contracts.py
git commit -m "feat(library): content validators for six new entry types (#20)"
```

---

### Task 3: Constraint Solver

**Files:**
- Create: `app/library/templates.py`
- Test: `tests/test_constraint_solver.py`

**Interfaces:**
- Consumes: dimension template/schema content shape from Task 2
- Produces: `detect_cycles(constraints, dim_ids) → list[Finding]`; `check_satisfiability(dimensions, constraints, cardinality) → list[Finding]`; `evaluate_constraints(schema_content, planned_items=None) → ConstraintResult`; `ConstraintResult` dataclass with `valid_items`, `skip_reasons`, `is_satisfiable`

- [ ] **Step 1: Write failing Hypothesis property tests**

```python
# tests/test_constraint_solver.py
import unittest
from dataclasses import dataclass
from hypothesis import given, settings, assume
import hypothesis.strategies as st

from app.library.templates import (
    detect_cycles, check_satisfiability, evaluate_constraints,
    ConstraintResult, SkipReason, COMBINATION_LIMIT,
)


def _dims(n_dims=2, n_vals=2):
    """Build a simple dimension list."""
    return [
        {
            "id": f"d{i}",
            "name": f"Dim{i}",
            "values": [
                {"id": f"d{i}v{j}", "name": f"Val{j}",
                 "positive_fragments": [], "negative_fragments": []}
                for j in range(n_vals)
            ],
        }
        for i in range(n_dims)
    ]


class TestCycleDetection(unittest.TestCase):

    def test_no_cycles(self):
        constraints = [
            {"type": "requires", "from_dimension_id": "d0", "from_value_id": "d0v0",
             "to_dimension_id": "d1", "to_value_id": "d1v0"},
        ]
        findings = detect_cycles(constraints, {"d0", "d1"})
        self.assertEqual(findings, [])

    def test_direct_cycle(self):
        constraints = [
            {"type": "requires", "from_dimension_id": "d0", "from_value_id": "d0v0",
             "to_dimension_id": "d1", "to_value_id": "d1v0"},
            {"type": "requires", "from_dimension_id": "d1", "from_value_id": "d1v0",
             "to_dimension_id": "d0", "to_value_id": "d0v0"},
        ]
        findings = detect_cycles(constraints, {"d0", "d1"})
        self.assertTrue(len(findings) > 0)
        self.assertTrue(any(f.code == "constraint_cycle" for f in findings))

    def test_transitive_cycle(self):
        constraints = [
            {"type": "requires", "from_dimension_id": "d0", "from_value_id": "d0v0",
             "to_dimension_id": "d1", "to_value_id": "d1v0"},
            {"type": "requires", "from_dimension_id": "d1", "from_value_id": "d1v0",
             "to_dimension_id": "d2", "to_value_id": "d2v0"},
            {"type": "requires", "from_dimension_id": "d2", "from_value_id": "d2v0",
             "to_dimension_id": "d0", "to_value_id": "d0v0"},
        ]
        findings = detect_cycles(constraints, {"d0", "d1", "d2"})
        self.assertTrue(len(findings) > 0)


class TestSatisfiability(unittest.TestCase):

    def test_no_constraints_satisfiable(self):
        findings = check_satisfiability(_dims(2, 2), [], "one_per_dimension")
        self.assertEqual(findings, [])

    def test_excludes_all_blocking(self):
        dims = _dims(1, 2)
        constraints = [
            {"type": "excludes", "dimension_id_a": "d0", "value_id_a": "d0v0",
             "dimension_id_b": "d0", "value_id_b": "d0v1"},
        ]
        # With one_per_dimension, each combo picks one value per dim,
        # so excluding within the same dim doesn't reduce combos.
        # Need cross-dim exclusion to be unsatisfiable.
        dims = _dims(2, 1)  # 2 dims, 1 value each
        constraints = [
            {"type": "excludes", "dimension_id_a": "d0", "value_id_a": "d0v0",
             "dimension_id_b": "d1", "value_id_b": "d1v0"},
        ]
        findings = check_satisfiability(dims, constraints, "one_per_dimension")
        self.assertTrue(any(f.code == "unsatisfiable" for f in findings))


class TestEvaluateConstraints(unittest.TestCase):

    def test_no_constraints_all_valid(self):
        schema = {
            "dimensions": _dims(2, 2),
            "constraints": [],
            "combination_cardinality": "one_per_dimension",
        }
        result = evaluate_constraints(schema)
        self.assertTrue(result.is_satisfiable)
        self.assertEqual(len(result.valid_items), 4)  # 2 * 2
        self.assertEqual(len(result.skip_reasons), 0)

    def test_excludes_produces_skip_reason(self):
        schema = {
            "dimensions": _dims(2, 2),
            "constraints": [
                {"type": "excludes", "dimension_id_a": "d0", "value_id_a": "d0v0",
                 "dimension_id_b": "d1", "value_id_b": "d1v0"},
            ],
            "combination_cardinality": "one_per_dimension",
        }
        result = evaluate_constraints(schema)
        self.assertTrue(result.is_satisfiable)
        self.assertEqual(len(result.valid_items), 3)
        self.assertEqual(len(result.skip_reasons), 1)
        self.assertTrue(len(result.skip_reasons[0].violations) >= 1)

    def test_combination_limit(self):
        schema = {
            "dimensions": _dims(20, 3),  # 3^20 > 3 billion
            "constraints": [],
            "combination_cardinality": "one_per_dimension",
        }
        result = evaluate_constraints(schema)
        self.assertFalse(result.is_satisfiable)
        self.assertTrue(any(f.code == "combination_limit_exceeded"
                            for f in result.limit_findings))

    def test_planned_items_filtering(self):
        schema = {
            "dimensions": _dims(2, 2),
            "constraints": [
                {"type": "excludes", "dimension_id_a": "d0", "value_id_a": "d0v0",
                 "dimension_id_b": "d1", "value_id_b": "d1v0"},
            ],
            "combination_cardinality": "one_per_dimension",
        }
        planned = [{"d0": "d0v0", "d1": "d1v0"}, {"d0": "d0v0", "d1": "d1v1"}]
        result = evaluate_constraints(schema, planned_items=planned)
        self.assertEqual(len(result.valid_items), 1)
        self.assertEqual(len(result.skip_reasons), 1)


@given(
    n_dims=st.integers(min_value=2, max_value=5),
    n_vals=st.integers(min_value=1, max_value=5),
)
@settings(max_examples=50)
def test_solver_agrees_with_brute_force(n_dims, n_vals):
    """Property: solver and brute-force produce the same valid set."""
    import itertools
    dims = _dims(n_dims, n_vals)
    schema = {"dimensions": dims, "constraints": [], "combination_cardinality": "one_per_dimension"}
    result = evaluate_constraints(schema)
    expected = n_vals ** n_dims
    assume(expected <= COMBINATION_LIMIT)
    assert len(result.valid_items) == expected
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_constraint_solver.py -v`
Expected: FAIL — `templates` module doesn't exist yet

- [ ] **Step 3: Implement constraint solver in templates.py**

```python
# app/library/templates.py
"""Dimension template commands: constraint solver, apply, reapply.

Spec §4.1, §4.6.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product as cartesian
from typing import Any

from .contracts import Finding, SEVERITY_BLOCKING

COMBINATION_LIMIT = 10_000


@dataclass(frozen=True)
class SkipReason:
    combination: dict[str, str]
    primary_reason: str
    violations: list[str]


@dataclass
class ConstraintResult:
    valid_items: list[dict[str, str]]
    skip_reasons: list[SkipReason]
    is_satisfiable: bool
    limit_findings: list[Finding] = field(default_factory=list)


def detect_cycles(constraints: list[dict], dim_ids: set[str]) -> list[Finding]:
    """Detect authoring cycles in requires chains (§9.3, spec §4.1)."""
    graph: dict[str, list[str]] = {}
    for c in constraints:
        if c.get("type") != "requires":
            continue
        src = f"{c.get('from_dimension_id')}:{c.get('from_value_id')}"
        dst = f"{c.get('to_dimension_id')}:{c.get('to_value_id')}"
        graph.setdefault(src, []).append(dst)

    visited: set[str] = set()
    in_stack: set[str] = set()
    findings: list[Finding] = []

    def dfs(node: str) -> bool:
        if node in in_stack:
            return True
        if node in visited:
            return False
        visited.add(node)
        in_stack.add(node)
        for neighbor in graph.get(node, []):
            if dfs(neighbor):
                findings.append(Finding(
                    "constraint_cycle",
                    f"requires 链存在循环: {node} → {neighbor}",
                    "constraints",
                ))
                return True
        in_stack.discard(node)
        return False

    for node in graph:
        if node not in visited:
            dfs(node)
    return findings


def check_satisfiability(
    dimensions: list[dict],
    constraints: list[dict],
    cardinality: str,
) -> list[Finding]:
    """Bounded check: at least one valid combination exists (§4.6 call site 1)."""
    if not dimensions:
        return [Finding("unsatisfiable", "没有维度", "dimensions")]
    total = 1
    for dim in dimensions:
        vals = dim.get("values") or []
        total *= max(len(vals), 1)
        if total > COMBINATION_LIMIT:
            return []  # Too large to check here; expansion will catch it
    combos = _expand_combinations(dimensions)
    valid = [c for c in combos if _is_valid(c, constraints)]
    if not valid:
        return [Finding("unsatisfiable", "约束导致零个有效组合", "constraints")]
    return []


def evaluate_constraints(
    schema_content: dict,
    planned_items: list[dict[str, str]] | None = None,
) -> ConstraintResult:
    """Dual call-site function (§4.6).

    planned_items=None → satisfiability-only (no expansion if too large).
    planned_items=list → filter planned items and produce SkipReasons.
    """
    dims = schema_content.get("dimensions") or []
    constraints = schema_content.get("constraints") or []
    cardinality = schema_content.get("combination_cardinality", "one_per_dimension")

    if planned_items is not None:
        valid = []
        skips = []
        for combo in planned_items:
            violations = _collect_violations(combo, constraints)
            if violations:
                skips.append(SkipReason(
                    combination=combo,
                    primary_reason=violations[0],
                    violations=violations,
                ))
            else:
                valid.append(combo)
        return ConstraintResult(
            valid_items=valid,
            skip_reasons=skips,
            is_satisfiable=len(valid) > 0,
        )

    total = 1
    for dim in dims:
        total *= max(len(dim.get("values") or []), 1)
    if total > COMBINATION_LIMIT:
        return ConstraintResult(
            valid_items=[],
            skip_reasons=[],
            is_satisfiable=False,
            limit_findings=[Finding(
                "combination_limit_exceeded",
                f"组合数 {total} 超过限制 {COMBINATION_LIMIT}",
                "dimensions",
            )],
        )

    combos = _expand_combinations(dims)
    valid = []
    skips = []
    for combo in combos:
        violations = _collect_violations(combo, constraints)
        if violations:
            skips.append(SkipReason(
                combination=combo,
                primary_reason=violations[0],
                violations=violations,
            ))
        else:
            valid.append(combo)
    return ConstraintResult(
        valid_items=valid,
        skip_reasons=skips,
        is_satisfiable=len(valid) > 0,
    )


def _expand_combinations(dimensions: list[dict]) -> list[dict[str, str]]:
    """Generate all one-value-per-dimension combinations."""
    if not dimensions:
        return [{}]
    dim_values = []
    for dim in dimensions:
        dim_id = dim["id"]
        vals = [(dim_id, v["id"]) for v in (dim.get("values") or [])]
        dim_values.append(vals)
    results = []
    for combo in cartesian(*dim_values):
        results.append({dim_id: val_id for dim_id, val_id in combo})
    return results


def _collect_violations(combo: dict[str, str], constraints: list[dict]) -> list[str]:
    """Return all constraint violations for a single combination."""
    violations = []
    for c in constraints:
        ctype = c.get("type")
        if ctype == "requires":
            from_dim = c.get("from_dimension_id")
            from_val = c.get("from_value_id")
            to_dim = c.get("to_dimension_id")
            to_val = c.get("to_value_id")
            if combo.get(from_dim) == from_val and combo.get(to_dim) != to_val:
                violations.append(
                    f"requires: {from_dim}={from_val} → {to_dim}={to_val}"
                )
        elif ctype == "excludes":
            dim_a = c.get("dimension_id_a")
            val_a = c.get("value_id_a")
            dim_b = c.get("dimension_id_b")
            val_b = c.get("value_id_b")
            if combo.get(dim_a) == val_a and combo.get(dim_b) == val_b:
                violations.append(f"excludes: {dim_a}={val_a} ⊗ {dim_b}={val_b}")
    return violations
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_constraint_solver.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add app/library/templates.py tests/test_constraint_solver.py
git commit -m "feat(library): constraint solver with cycle detection and satisfiability (#20)"
```

---

### Task 4: Apply & Reapply Template

**Files:**
- Modify: `app/library/templates.py`
- Test: `tests/test_templates.py`

**Interfaces:**
- Consumes: `service.create_entry`, `service._run_with_idempotency`, `service._require_workspace`, `service._entry`, `service._audit`, `service._emit`, `TenantContext`, ORM models from Task 1, constraint solver from Task 3
- Produces: `apply_template(context, template_version_id, product_entry_id, ...) → dict`; `reapply_template(context, new_template_version_id, existing_schema_version_id, ...) → TemplateDiffReport`; `TemplateDiffReport` dataclass

- [ ] **Step 1: Write failing test for apply_template**

```python
# tests/test_templates.py
import unittest
from tests.support.library_fixtures import LibraryFixtures, FUTURE
from app.library.contracts import (
    ENTRY_DIMENSION_TEMPLATE, ENTRY_DIMENSION_SCHEMA, ENTRY_PRODUCT,
    ROLE_MEMBER, VERSION_EFFECTIVE,
)
from app.library import service
from app.library.templates import apply_template, reapply_template


class TestApplyTemplate(LibraryFixtures):

    def _publish_template(self, ctx):
        content = {
            "dimensions": [{
                "id": "dim-color", "name": "Color",
                "values": [
                    {"id": "val-red", "name": "Red",
                     "positive_fragments": ["red"], "negative_fragments": []},
                    {"id": "val-blue", "name": "Blue",
                     "positive_fragments": ["blue"], "negative_fragments": []},
                ],
            }],
            "constraints": [],
            "concept_key_mappings": [],
            "combination_cardinality": "one_per_dimension",
        }
        return self._publish(ctx, ENTRY_DIMENSION_TEMPLATE, "Color Template", content)

    def _publish_product(self, ctx):
        content = {
            "name": "Test Product", "description": "A product", "audience": "everyone",
        }
        return self._publish(ctx, ENTRY_PRODUCT, "Test Product", content)

    def test_apply_creates_schema_draft(self):
        ctx = self.ctx()
        tmpl_entry_id, tmpl_version = self._publish_template(ctx)
        prod_entry_id, _ = self._publish_product(ctx)
        result = apply_template(
            ctx, tmpl_version["id"], prod_entry_id,
            idempotency_key="apply-1",
            request_fingerprint="fp1",
        )
        self.assertEqual(result["entry_type"], ENTRY_DIMENSION_SCHEMA)
        draft = result["draft"]
        self.assertEqual(draft["content"]["product_entry_id"], prod_entry_id)
        self.assertEqual(draft["content"]["derived_from_template_version_id"], tmpl_version["id"])
        # Product-local IDs are different from template IDs
        local_dim_id = draft["content"]["dimensions"][0]["id"]
        self.assertNotEqual(local_dim_id, "dim-color")
        # Provenance preserved
        self.assertEqual(draft["content"]["dimensions"][0]["source_dimension_id"], "dim-color")

    def test_apply_deep_copies(self):
        """Later template changes cannot mutate applied schemas."""
        ctx = self.ctx()
        tmpl_entry_id, tmpl_v1 = self._publish_template(ctx)
        prod_entry_id, _ = self._publish_product(ctx)
        result = apply_template(
            ctx, tmpl_v1["id"], prod_entry_id,
            idempotency_key="apply-2", request_fingerprint="fp2",
        )
        original_dims = result["draft"]["content"]["dimensions"]
        # Publish a new template version with a changed name
        service.save_draft(ctx, tmpl_entry_id, {
            "dimensions": [{
                "id": "dim-color", "name": "Color CHANGED",
                "values": [{"id": "val-red", "name": "Red CHANGED",
                            "positive_fragments": ["red"], "negative_fragments": []}],
            }],
            "constraints": [], "concept_key_mappings": [],
            "combination_cardinality": "one_per_dimension",
        }, expected_revision=2)
        # Applied schema is unchanged
        self.assertEqual(original_dims[0]["values"][0]["name"], "Red")

    def test_apply_idempotent(self):
        ctx = self.ctx()
        tmpl_entry_id, tmpl_version = self._publish_template(ctx)
        prod_entry_id, _ = self._publish_product(ctx)
        r1 = apply_template(ctx, tmpl_version["id"], prod_entry_id,
                            idempotency_key="apply-idem", request_fingerprint="fp3")
        r2 = apply_template(ctx, tmpl_version["id"], prod_entry_id,
                            idempotency_key="apply-idem", request_fingerprint="fp3")
        self.assertEqual(r1["id"], r2["id"])

    def test_apply_cross_workspace_blocked(self):
        ctx_a = self.ctx()
        ctx_b = self.ctx()
        tmpl_entry_id, tmpl_version = self._publish_template(ctx_a)
        prod_entry_id, _ = self._publish_product(ctx_b)
        with self.assertRaises(Exception):
            apply_template(ctx_b, tmpl_version["id"], prod_entry_id,
                           idempotency_key="apply-cross", request_fingerprint="fp4")

    def test_apply_nonexistent_product_blocked(self):
        ctx = self.ctx()
        _, tmpl_version = self._publish_template(ctx)
        with self.assertRaises(Exception):
            apply_template(ctx, tmpl_version["id"], "nonexistent",
                           idempotency_key="apply-noent", request_fingerprint="fp5")

    def test_product_entry_id_immutable_across_versions(self):
        ctx = self.ctx()
        _, tmpl_version = self._publish_template(ctx)
        prod1_id, _ = self._publish_product(ctx)
        prod2_id, _ = self._publish_product(ctx)
        result = apply_template(ctx, tmpl_version["id"], prod1_id,
                                idempotency_key="apply-imm", request_fingerprint="fp6")
        schema_entry_id = result["id"]
        # Publish the schema
        rev = result["draft"]["revision"]
        service.validate_draft(ctx, schema_entry_id, expected_revision=rev)
        service.publish(ctx, schema_entry_id, rev)
        # Try to save draft with different product_entry_id
        new_content = dict(result["draft"]["content"])
        new_content["product_entry_id"] = prod2_id
        service.save_draft(ctx, schema_entry_id, new_content, expected_revision=rev + 1)
        report = service.validate_draft(ctx, schema_entry_id, expected_revision=rev + 2)
        self.assertFalse(report["ok"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_templates.py -v`
Expected: FAIL — `apply_template` not yet implemented

- [ ] **Step 3: Implement apply_template and reapply_template**

Add to `app/library/templates.py`:

```python
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    LibraryDraftRecord, LibraryEntryRecord, LibraryVersionRecord,
    new_id, utcnow,
)
from app.platform.tenancy import TenantContext
from app.library.contracts import (
    COMMAND_APPLY, DRAFT_EDITING, ENTRY_DIMENSION_SCHEMA,
    ENTRY_DIMENSION_TEMPLATE, ENTRY_PRODUCT, LIFECYCLE_ACTIVE,
    LIFECYCLE_ARCHIVED, VERSION_EFFECTIVE, canonical_hash,
)
from app.library.service import (
    LibraryError, LibraryForbidden, LibraryNotFound,
    _run_with_idempotency, _require_workspace, _entry, _version,
    _audit, _emit, _entry_dict,
)


def apply_template(
    context: TenantContext,
    template_version_id: str,
    product_entry_id: str,
    *,
    idempotency_key: str | None = None,
    request_fingerprint: str | None = None,
) -> dict:
    """Deep-copy a template version into a product-local DimensionSchema draft (§9.1)."""

    def body(session, key_hash):
        _require_workspace(session, context.workspace_id)
        # Validate template version
        tmpl_ver = session.get(LibraryVersionRecord, template_version_id)
        if tmpl_ver is None or tmpl_ver.workspace_id != context.workspace_id:
            raise LibraryNotFound("模板版本不存在")
        if tmpl_ver.entry_type != ENTRY_DIMENSION_TEMPLATE:
            raise LibraryError("引用的版本不是维度模板")
        # Validate product entry
        product = session.get(LibraryEntryRecord, product_entry_id)
        if product is None or product.workspace_id != context.workspace_id:
            raise LibraryNotFound("产品条目不存在")
        if product.entry_type != ENTRY_PRODUCT:
            raise LibraryError("引用的条目不是产品")
        if product.lifecycle == LIFECYCLE_ARCHIVED:
            raise LibraryError("产品已归档")

        # Deep-copy template content with new local IDs
        tmpl_content = dict(tmpl_ver.content_json or {})
        id_map = {}  # old_id → new_local_id
        new_dims = []
        for dim in tmpl_content.get("dimensions") or []:
            old_dim_id = dim["id"]
            new_dim_id = f"ldim_{_uuid.uuid4().hex[:12]}"
            id_map[old_dim_id] = new_dim_id
            new_values = []
            for val in dim.get("values") or []:
                old_val_id = val["id"]
                new_val_id = f"lval_{_uuid.uuid4().hex[:12]}"
                id_map[old_val_id] = new_val_id
                new_values.append({
                    **val,
                    "id": new_val_id,
                    "source_value_id": old_val_id,
                })
            new_dims.append({
                **dim,
                "id": new_dim_id,
                "source_dimension_id": old_dim_id,
                "values": new_values,
            })

        # Remap constraint references
        new_constraints = []
        for c in tmpl_content.get("constraints") or []:
            remapped = dict(c)
            for key in ("from_dimension_id", "to_dimension_id",
                        "dimension_id_a", "dimension_id_b", "dimension_id"):
                if key in remapped and remapped[key] in id_map:
                    remapped[key] = id_map[remapped[key]]
            for key in ("from_value_id", "to_value_id",
                        "value_id_a", "value_id_b"):
                if key in remapped and remapped[key] in id_map:
                    remapped[key] = id_map[remapped[key]]
            new_constraints.append(remapped)

        schema_content = {
            "product_entry_id": product_entry_id,
            "derived_from_template_version_id": template_version_id,
            "dimensions": new_dims,
            "constraints": new_constraints,
            "combination_cardinality": tmpl_content.get("combination_cardinality", "one_per_dimension"),
        }

        # Atomically create Entry + Draft
        entry = LibraryEntryRecord(
            id=new_id("lib"),
            workspace_id=context.workspace_id,
            entry_type=ENTRY_DIMENSION_SCHEMA,
            display_name=f"Schema for {product.display_name}",
            lifecycle=LIFECYCLE_ACTIVE,
        )
        session.add(entry)
        session.flush()
        session.add(LibraryDraftRecord(
            id=new_id("libdraft"),
            workspace_id=context.workspace_id,
            entry_id=entry.id,
            content_json=schema_content,
            revision=1,
            state=DRAFT_EDITING,
            origin_command=COMMAND_APPLY,
            source_version_id=template_version_id,
        ))
        _emit(session, context, entry.id, "draft_created", to_state=DRAFT_EDITING,
              payload={"origin": "apply_template", "template_version_id": template_version_id})
        _audit(session, context, "apply_template", entry_id=entry.id,
               detail={"template_version_id": template_version_id,
                        "product_entry_id": product_entry_id})
        return _entry_dict(session, context, entry)

    return _run_with_idempotency(
        context, idempotency_key, "apply_template", request_fingerprint, body
    )


@dataclass
class DiffItem:
    dimension_id: str
    status: str  # "template_only" | "local_only" | "conflict" | "unchanged"
    template_value: dict | None = None
    local_value: dict | None = None


@dataclass
class TemplateDiffReport:
    base_template_version_id: str
    new_template_version_id: str
    schema_version_id: str
    items: list[DiffItem] = field(default_factory=list)


def reapply_template(
    context: TenantContext,
    new_template_version_id: str,
    existing_schema_version_id: str,
    *,
    idempotency_key: str | None = None,
    request_fingerprint: str | None = None,
) -> dict:
    """Three-way diff: old template base, new template, local schema (§9.2)."""

    def body(session, key_hash):
        _require_workspace(session, context.workspace_id)
        schema_ver = _version(session, context, existing_schema_version_id)
        if schema_ver.entry_type != ENTRY_DIMENSION_SCHEMA:
            raise LibraryError("版本不是维度 Schema")
        schema_content = dict(schema_ver.content_json or {})

        base_tmpl_id = schema_content.get("derived_from_template_version_id")
        if not base_tmpl_id:
            raise LibraryError("该 Schema 没有模板来源")
        base_tmpl = session.get(LibraryVersionRecord, base_tmpl_id)
        if base_tmpl is None or base_tmpl.workspace_id != context.workspace_id:
            raise LibraryNotFound("原始模板版本不存在")

        new_tmpl = _version(session, context, new_template_version_id)
        if new_tmpl.entry_type != ENTRY_DIMENSION_TEMPLATE:
            raise LibraryError("新版本不是维度模板")

        base_content = dict(base_tmpl.content_json or {})
        new_content = dict(new_tmpl.content_json or {})

        # Build lookup maps
        base_dims = {d["id"]: d for d in base_content.get("dimensions") or []}
        new_dims = {d["id"]: d for d in new_content.get("dimensions") or []}
        # Local dims keyed by source_dimension_id
        local_by_source = {}
        for d in schema_content.get("dimensions") or []:
            src = d.get("source_dimension_id")
            if src:
                local_by_source[src] = d

        items = []
        all_ids = set(base_dims) | set(new_dims)
        for dim_id in sorted(all_ids):
            in_base = dim_id in base_dims
            in_new = dim_id in new_dims
            in_local = dim_id in local_by_source
            if in_new and not in_base:
                items.append(DiffItem(dim_id, "template_only", template_value=new_dims[dim_id]))
            elif in_base and not in_new and in_local:
                items.append(DiffItem(dim_id, "template_removed", local_value=local_by_source[dim_id]))
            elif in_base and in_new and in_local:
                base_hash = canonical_hash(base_dims[dim_id])
                new_hash = canonical_hash(new_dims[dim_id])
                local_hash = canonical_hash(local_by_source[dim_id])
                if base_hash == new_hash:
                    items.append(DiffItem(dim_id, "unchanged"))
                elif base_hash == local_hash:
                    items.append(DiffItem(dim_id, "template_only", template_value=new_dims[dim_id]))
                elif new_hash == local_hash:
                    items.append(DiffItem(dim_id, "unchanged"))
                else:
                    items.append(DiffItem(dim_id, "conflict",
                                         template_value=new_dims[dim_id],
                                         local_value=local_by_source[dim_id]))
            elif not in_local and in_base and in_new:
                items.append(DiffItem(dim_id, "local_only"))

        report = TemplateDiffReport(
            base_template_version_id=base_tmpl_id,
            new_template_version_id=new_template_version_id,
            schema_version_id=existing_schema_version_id,
            items=items,
        )
        _audit(session, context, "reapply_template",
               detail={"new_template": new_template_version_id,
                        "schema": existing_schema_version_id})
        return {
            "base_template_version_id": report.base_template_version_id,
            "new_template_version_id": report.new_template_version_id,
            "schema_version_id": report.schema_version_id,
            "items": [
                {"dimension_id": it.dimension_id, "status": it.status}
                for it in report.items
            ],
        }

    return _run_with_idempotency(
        context, idempotency_key, "reapply_template", request_fingerprint, body
    )
```

- [ ] **Step 4: Add product_entry_id immutability check to schema validation**

In `contracts.py`, update `_validate_dimension_schema` to store `product_entry_id` immutability check. The cross-version check happens in `service.py`'s `_build_report` — add a hook for schema-specific cross-version validation that checks existing published versions have the same `product_entry_id`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_templates.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add app/library/templates.py app/library/contracts.py tests/test_templates.py
git commit -m "feat(library): apply and reapply template commands (#20)"
```

---

### Task 5: Prompt Policy — Baseline, Composition & Locked Phrases

**Files:**
- Create: `app/library/prompt_policy.py`
- Test: `tests/test_prompt_policy.py`

**Interfaces:**
- Consumes: `PromptBaselineRecord` from Task 1, service helpers, `canonical_hash`
- Produces: `publish_baseline(content, published_by) → dict`; `withdraw_baseline(baseline_id, reason, withdrawn_by) → dict`; `normalize_draft_phrases(content, effective_baseline) → tuple[dict, list[Finding]]`; `compose_prompt(baseline_version, policy_version, run_inputs) → ComposedPrompt`; `ComposedPrompt` dataclass

- [ ] **Step 1: Write failing tests for compose_prompt**

```python
# tests/test_prompt_policy.py
import unittest
from app.library.prompt_policy import (
    compose_prompt, ComposedPrompt, normalize_draft_phrases,
    publish_baseline, withdraw_baseline,
    TOKEN_PATTERN,
)
from app.library.contracts import canonical_hash


class TestComposePrompt(unittest.TestCase):

    def _baseline(self):
        return {
            "clauses": [
                {"clause_id": "safety-1", "category": "safety",
                 "text": "Do not generate harmful content.",
                 "tokens": [], "locked": True},
            ],
            "platform_tokens": [],
        }

    def _policy(self):
        return {
            "registered_tokens": [
                {"name": "product_name", "description": "Product", "required": True},
                {"name": "tone", "description": "Tone", "required": False, "default": "neutral"},
            ],
            "locked_phrases": [],
            "brand_tone": {"style": "professional"},
            "allowed_template_entry_ids": [],
            "optional_clauses": [],
        }

    def _run_inputs(self):
        return {
            "product_name": "Widget Pro",
            "product_description": "The best widget",
            "must_match": ["logo visible"],
            "person_fragments": [],
            "dimension_values": {"color": "red"},
            "intent": "product_hero",
            "aspect_ratio": "1:1",
            "negatives": ["blurry"],
        }

    def test_deterministic(self):
        b, p, r = self._baseline(), self._policy(), self._run_inputs()
        c1 = compose_prompt(b, p, r)
        c2 = compose_prompt(b, p, r)
        self.assertEqual(c1.final_text, c2.final_text)
        self.assertEqual(c1.composition_hash, c2.composition_hash)
        self.assertEqual(c1.layer_hashes, c2.layer_hashes)

    def test_baseline_clauses_first(self):
        result = compose_prompt(self._baseline(), self._policy(), self._run_inputs())
        self.assertTrue(result.final_text.startswith("Do not generate harmful content."))

    def test_unknown_token_error(self):
        policy = self._policy()
        policy["registered_tokens"].append(
            {"name": "unknown", "description": "x", "required": True}
        )
        run = self._run_inputs()  # Does not provide "unknown"
        with self.assertRaises(ValueError) as cm:
            compose_prompt(self._baseline(), policy, run)
        self.assertIn("unknown", str(cm.exception))

    def test_token_in_value_stays_literal(self):
        """Runtime value containing {{token}} is not rescanned."""
        run = self._run_inputs()
        run["product_name"] = "Widget {{tone}} Pro"
        result = compose_prompt(self._baseline(), self._policy(), run)
        self.assertIn("Widget {{tone}} Pro", result.final_text)

    def test_single_pass_no_recursion(self):
        policy = self._policy()
        policy["registered_tokens"][1]["default"] = "{{product_name}}"
        run = {**self._run_inputs()}
        del run["product_name"]
        # If "tone" were provided but its value is "{{product_name}}",
        # the result should contain the literal string, not be expanded.
        run["tone"] = "{{product_name}}"
        result = compose_prompt(self._baseline(), policy, {**run, "product_name": "Widget"})
        # product_name is substituted first; tone's value is literal
        self.assertIn("{{product_name}}", result.final_text)


class TestRecursiveTokenBlocked(unittest.TestCase):
    """Spec §4.2: recursive token definition → blocked at publish validation."""

    def test_recursive_token_definition_blocked(self):
        """Token whose default contains another {{token}} is a blocking finding."""
        from app.library.contracts import validate_content, ENTRY_PROMPT_POLICY
        content = {
            "registered_tokens": [
                {"name": "product_name", "description": "Name", "required": True},
                {"name": "greeting", "description": "Greeting",
                 "required": False, "default": "Hello {{product_name}}!"},
            ],
            "locked_phrases": [],
            "brand_tone": {"style": "neutral"},
            "allowed_template_entry_ids": [],
            "optional_clauses": [],
        }
        report = validate_content(ENTRY_PROMPT_POLICY, content)
        self.assertFalse(report.ok)
        codes = {f.code for f in report.blocking}
        self.assertIn("policy_recursive_token", codes)


class TestNormalizeDraftPhrases(unittest.TestCase):

    def test_removing_protected_phrase_restores(self):
        baseline = {
            "clauses": [
                {"clause_id": "safety-1", "category": "safety",
                 "text": "Never remove safety warnings.",
                 "tokens": [], "locked": True},
            ],
            "platform_tokens": [],
        }
        content = {
            "registered_tokens": [],
            "locked_phrases": [],  # Protected phrase removed
            "brand_tone": {},
            "allowed_template_entry_ids": [],
            "optional_clauses": [],
        }
        normalized, findings = normalize_draft_phrases(content, baseline)
        # Should have restored the phrase
        self.assertTrue(len(normalized.get("locked_phrases", [])) > 0
                        or len(findings) > 0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_prompt_policy.py -v`
Expected: FAIL — module doesn't exist

- [ ] **Step 3: Implement prompt_policy.py**

```python
# app/library/prompt_policy.py
"""Prompt policy: platform baseline, workspace policy, deterministic composition.

Spec §4.2: three-layer composition, registered tokens, locked phrases.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, update

from app.adapters.persistence.database import platform_session
from app.adapters.persistence.models import (
    PromptBaselineRecord, new_id, utcnow,
)
from app.library.contracts import Finding, SEVERITY_WARNING, canonical_hash

TOKEN_PATTERN = re.compile(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}")


@dataclass(frozen=True)
class ComposedPrompt:
    final_text: str
    layer_hashes: dict[str, str]
    token_resolution_log: list[dict]
    composition_hash: str


def publish_baseline(
    content: dict,
    published_by: str,
) -> dict:
    """Publish a new PlatformPromptBaselineVersion (platform authorization required)."""
    with platform_session() as session:
        # Supersede current effective
        current = session.scalars(
            select(PromptBaselineRecord).where(PromptBaselineRecord.state == "effective")
        ).one_or_none()
        next_no = 1
        if current is not None:
            current.state = "superseded"
            current.superseded_at = utcnow()
            next_no = current.version_no + 1
        record = PromptBaselineRecord(
            id=new_id("pbl"),
            version_no=next_no,
            content_json=content,
            content_hash=canonical_hash(content),
            state="effective",
            published_by=published_by,
        )
        session.add(record)
        session.flush()
        return {
            "id": record.id,
            "version_no": record.version_no,
            "content_hash": record.content_hash,
            "state": record.state,
        }


def withdraw_baseline(
    baseline_id: str,
    reason: str,
    withdrawn_by: str,
) -> dict:
    """Emergency withdraw a baseline. Persists domain event for downstream (#21)."""
    with platform_session() as session:
        record = session.get(PromptBaselineRecord, baseline_id)
        if record is None:
            raise ValueError("Baseline not found")
        if record.state != "effective":
            raise ValueError(f"Cannot withdraw: state is {record.state}")
        record.state = "withdrawn"
        record.withdrawn_at = utcnow()
        record.withdraw_reason = reason
        # TODO(#21): persist outbox/domain event for downstream task pausing
        session.flush()
        return {
            "id": record.id,
            "state": record.state,
            "withdraw_reason": reason,
        }


def get_effective_baseline() -> dict | None:
    """Return the current effective baseline content, or None."""
    with platform_session() as session:
        record = session.scalars(
            select(PromptBaselineRecord).where(PromptBaselineRecord.state == "effective")
        ).one_or_none()
        if record is None:
            return None
        return dict(record.content_json)


def normalize_draft_phrases(
    content: dict,
    effective_baseline: dict,
) -> tuple[dict, list[Finding]]:
    """Save-time normalization: restore removed protected phrases (§4.2).

    Returns (normalized_content, findings). Caller increments draft revision.
    """
    findings: list[Finding] = []
    baseline_clauses = effective_baseline.get("clauses") or []
    protected = [c for c in baseline_clauses if c.get("locked")]
    existing_phrases = {
        p.get("clause_id") for p in (content.get("locked_phrases") or [])
    }
    restored = list(content.get("locked_phrases") or [])
    for clause in protected:
        if clause["clause_id"] not in existing_phrases:
            restored.append({
                "phrase": clause["text"],
                "clause_id": clause["clause_id"],
                "protected": True,
            })
            findings.append(Finding(
                "protected_phrase_restored",
                f"受保护短语 {clause['clause_id']!r} 已被恢复",
                f"locked_phrases",
                SEVERITY_WARNING,
            ))
    normalized = dict(content)
    normalized["locked_phrases"] = restored
    return normalized, findings


def compose_prompt(
    baseline_content: dict,
    policy_content: dict,
    run_inputs: dict,
) -> ComposedPrompt:
    """Deterministic single-pass prompt composition (§10, spec §4.2).

    Raises ValueError for unknown or missing required tokens.
    """
    parts: list[str] = []
    log: list[dict] = []

    # Layer 1: Platform baseline clauses (ordered by clause_id)
    baseline_clauses = sorted(
        baseline_content.get("clauses") or [],
        key=lambda c: c.get("clause_id", ""),
    )
    baseline_text = "\n".join(c["text"] for c in baseline_clauses)
    parts.append(baseline_text)

    # Layer 2: Workspace policy
    policy_phrases = policy_content.get("locked_phrases") or []
    phrase_text = "\n".join(p["phrase"] for p in policy_phrases if p.get("phrase"))
    if phrase_text:
        parts.append(phrase_text)
    tone = policy_content.get("brand_tone") or {}
    if tone.get("style"):
        parts.append(f"Tone: {tone['style']}")

    # Layer 3: Run inputs
    run_parts = []
    if run_inputs.get("product_description"):
        run_parts.append(f"Product: {run_inputs['product_description']}")
    for mm in run_inputs.get("must_match") or []:
        run_parts.append(f"Must match: {mm}")
    for frag in run_inputs.get("person_fragments") or []:
        run_parts.append(frag)
    dims = run_inputs.get("dimension_values") or {}
    for dim_name, val in sorted(dims.items()):
        run_parts.append(f"{dim_name}: {val}")
    if run_inputs.get("intent"):
        run_parts.append(f"Intent: {run_inputs['intent']}")
    if run_inputs.get("aspect_ratio"):
        run_parts.append(f"Aspect ratio: {run_inputs['aspect_ratio']}")
    for neg in run_inputs.get("negatives") or []:
        run_parts.append(f"Avoid: {neg}")
    if run_parts:
        parts.append("\n".join(run_parts))

    raw_prompt = "\n\n".join(parts)

    # Single-pass token substitution
    registered = {t["name"]: t for t in policy_content.get("registered_tokens") or []}
    # Also include platform tokens
    for pt in baseline_content.get("platform_tokens") or []:
        if pt["name"] not in registered:
            registered[pt["name"]] = pt

    def replace_token(match: re.Match) -> str:
        name = match.group(1)
        if name not in registered:
            raise ValueError(f"未知 token: {{{{{name}}}}}")
        # Check run_inputs first, then default
        if name in run_inputs:
            value = str(run_inputs[name])
        elif "default" in registered[name]:
            value = str(registered[name]["default"])
        elif registered[name].get("required"):
            raise ValueError(f"缺少必填 token: {{{{{name}}}}}")
        else:
            value = ""
        log.append({"token": name, "resolved_to": value, "source": "run_input" if name in run_inputs else "default"})
        return value

    final_text = TOKEN_PATTERN.sub(replace_token, raw_prompt)

    # Check all required tokens were provided
    for name, token_def in registered.items():
        if token_def.get("required") and name not in run_inputs and "default" not in token_def:
            if not any(entry["token"] == name for entry in log):
                raise ValueError(f"缺少必填 token: {{{{{name}}}}}")

    baseline_hash = canonical_hash(baseline_text)
    policy_hash = canonical_hash(phrase_text + str(tone))
    run_hash = canonical_hash(run_parts)
    composition_hash = canonical_hash(final_text)

    return ComposedPrompt(
        final_text=final_text,
        layer_hashes={
            "baseline": baseline_hash,
            "policy": policy_hash,
            "run_inputs": run_hash,
        },
        token_resolution_log=log,
        composition_hash=composition_hash,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_prompt_policy.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add app/library/prompt_policy.py tests/test_prompt_policy.py
git commit -m "feat(library): prompt policy baseline, composition and locked phrases (#20)"
```

---

### Task 6: DataSources — CredentialBinding, ManualFacts & TruthPolicy

**Files:**
- Create: `app/library/datasources.py`
- Test: `tests/test_datasources.py`

**Interfaces:**
- Consumes: `DatasourceCredentialBindingRecord` from Task 1, service helpers, `TenantContext`
- Produces: `bind_credential(context, data_source_entry_id, credential_version_id, ...) → dict`; `get_active_binding(context, data_source_entry_id) → dict | None`

- [ ] **Step 1: Write failing test for bind_credential**

```python
# tests/test_datasources.py
import unittest
from tests.support.library_fixtures import LibraryFixtures
from app.library.contracts import (
    ENTRY_DATA_SOURCE, ROLE_OWNER, ROLE_MEMBER,
)
from app.library import service
from app.library.datasources import bind_credential, get_active_binding
from app.library.service import LibraryForbidden


class TestCredentialBinding(LibraryFixtures):

    def _publish_data_source(self, ctx):
        content = {
            "adapter_kind": "manual",
            "capabilities": ["price"],
            "market_context": ["US"],
            "config": {},
            "config_schema_version": "manual/1.0",
            "execution_mode": "real",
        }
        owner = self.owner_ctx(ctx)
        return self._publish(owner, ENTRY_DATA_SOURCE, "Test DS", content)

    def _create_credential(self, ctx):
        """Stub: create a credential version for binding tests."""
        from app.adapters.persistence.database import tenant_session
        from app.adapters.persistence.models import new_id, utcnow
        cred_id = new_id("cred")
        with tenant_session(ctx) as session:
            from sqlalchemy import text
            # Insert minimal credential_versions row for FK satisfaction
            session.execute(text(
                "INSERT INTO credential_versions (id, workspace_id, credential_type, "
                "state, encrypted_value, encryption_key_id, created_by, created_at) "
                "VALUES (:id, :ws, 'api_key', 'active', '', '', 'test', :now)"
            ), {"id": cred_id, "ws": ctx.workspace_id, "now": utcnow()})
        return cred_id

    def test_bind_creates_active_binding(self):
        ctx = self.ctx(role=ROLE_OWNER)
        ds_entry_id, _ = self._publish_data_source(ctx)
        cred_id = self._create_credential(ctx)
        result = bind_credential(
            ctx, ds_entry_id, cred_id,
            idempotency_key="bind-1", request_fingerprint="fp1",
        )
        self.assertEqual(result["state"], "active")
        self.assertEqual(result["revision"], 1)

    def test_rebind_supersedes_previous(self):
        ctx = self.ctx(role=ROLE_OWNER)
        ds_entry_id, _ = self._publish_data_source(ctx)
        cred1 = self._create_credential(ctx)
        cred2 = self._create_credential(ctx)
        r1 = bind_credential(ctx, ds_entry_id, cred1,
                             idempotency_key="bind-2a", request_fingerprint="fp2a")
        r2 = bind_credential(ctx, ds_entry_id, cred2,
                             idempotency_key="bind-2b", request_fingerprint="fp2b")
        self.assertEqual(r2["revision"], 2)
        active = get_active_binding(ctx, ds_entry_id)
        self.assertEqual(active["credential_version_id"], cred2)

    def test_member_cannot_bind(self):
        ctx_owner = self.ctx(role=ROLE_OWNER)
        ds_entry_id, _ = self._publish_data_source(ctx_owner)
        cred_id = self._create_credential(ctx_owner)
        ctx_member = self.ctx(role=ROLE_MEMBER, workspace=ctx_owner.workspace_id, provision=False)
        with self.assertRaises(LibraryForbidden):
            bind_credential(ctx_member, ds_entry_id, cred_id,
                            idempotency_key="bind-3", request_fingerprint="fp3")

    def test_cross_workspace_blocked(self):
        ctx_a = self.ctx(role=ROLE_OWNER)
        ctx_b = self.ctx(role=ROLE_OWNER)
        ds_entry_id, _ = self._publish_data_source(ctx_a)
        cred_id = self._create_credential(ctx_b)
        with self.assertRaises(Exception):
            bind_credential(ctx_b, ds_entry_id, cred_id,
                            idempotency_key="bind-4", request_fingerprint="fp4")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_datasources.py -v`
Expected: FAIL

- [ ] **Step 3: Implement datasources.py**

```python
# app/library/datasources.py
"""DataSource credential binding, manual fact set and truth resolution policy.

Spec §4.3.
"""
from __future__ import annotations

from sqlalchemy import func, select, update

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    DatasourceCredentialBindingRecord, LibraryEntryRecord,
    new_id, utcnow,
)
from app.platform.tenancy import TenantContext
from app.library.contracts import ENTRY_DATA_SOURCE, LIFECYCLE_ACTIVE
from app.library.service import (
    LibraryConflict, LibraryError, LibraryForbidden, LibraryNotFound,
    _run_with_idempotency, _require_workspace, _entry, _audit,
)


def bind_credential(
    context: TenantContext,
    data_source_entry_id: str,
    credential_version_id: str,
    *,
    idempotency_key: str | None = None,
    request_fingerprint: str | None = None,
) -> dict:
    """Bind a CredentialVersion to a DataSource entry. Owner-only (§4.3)."""

    def body(session, key_hash):
        if context.role not in ("owner", "system"):
            raise LibraryForbidden("只有 Owner 可以绑定凭据")
        _require_workspace(session, context.workspace_id)
        entry = _entry(session, context, data_source_entry_id)
        if entry.entry_type != ENTRY_DATA_SOURCE:
            raise LibraryError("条目不是数据源")
        if entry.lifecycle != LIFECYCLE_ACTIVE:
            raise LibraryError("数据源已归档")

        # Supersede current active binding
        current_max = session.scalar(
            select(func.max(DatasourceCredentialBindingRecord.revision)).where(
                DatasourceCredentialBindingRecord.data_source_entry_id == data_source_entry_id,
                DatasourceCredentialBindingRecord.workspace_id == context.workspace_id,
            )
        )
        next_rev = (current_max or 0) + 1
        # Supersede any active binding
        session.execute(
            update(DatasourceCredentialBindingRecord).where(
                DatasourceCredentialBindingRecord.data_source_entry_id == data_source_entry_id,
                DatasourceCredentialBindingRecord.workspace_id == context.workspace_id,
                DatasourceCredentialBindingRecord.state == "active",
            ).values(state="superseded", updated_at=utcnow())
        )
        binding = DatasourceCredentialBindingRecord(
            id=new_id("dcb"),
            workspace_id=context.workspace_id,
            data_source_entry_id=data_source_entry_id,
            credential_version_id=credential_version_id,
            revision=next_rev,
            state="active",
        )
        session.add(binding)
        try:
            session.flush()
        except Exception:
            raise LibraryConflict("凭据绑定冲突，可能存在并发操作")
        _audit(session, context, "bind_credential",
               entry_id=data_source_entry_id,
               detail={"credential_version_id": credential_version_id,
                        "revision": next_rev})
        return {
            "id": binding.id,
            "data_source_entry_id": data_source_entry_id,
            "credential_version_id": credential_version_id,
            "revision": next_rev,
            "state": "active",
        }

    return _run_with_idempotency(
        context, idempotency_key, "bind_credential", request_fingerprint, body
    )


def get_active_binding(
    context: TenantContext,
    data_source_entry_id: str,
) -> dict | None:
    """Return the active credential binding for a data source, or None."""
    with tenant_session(context) as session:
        record = session.scalars(
            select(DatasourceCredentialBindingRecord).where(
                DatasourceCredentialBindingRecord.data_source_entry_id == data_source_entry_id,
                DatasourceCredentialBindingRecord.workspace_id == context.workspace_id,
                DatasourceCredentialBindingRecord.state == "active",
            )
        ).one_or_none()
        if record is None:
            return None
        return {
            "id": record.id,
            "credential_version_id": record.credential_version_id,
            "revision": record.revision,
            "state": record.state,
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_datasources.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add app/library/datasources.py tests/test_datasources.py
git commit -m "feat(library): data source credential binding (#20)"
```

---

### Task 7: Resolution Preview

**Files:**
- Create: `app/library/preview.py`
- Test: `tests/test_resolution_preview.py`

**Interfaces:**
- Consumes: All prior tasks — constraint solver, compose_prompt, service helpers, ORM models, `ResolutionPreviewRecord`
- Produces: `resolve_preview(context, generation_draft_id, draft_revision, ...) → dict`; `invalidate_previews_for_version(context, entry_id, event_type) → int`; `check_preview_valid(context, preview_id, preview_hash) → dict`

- [ ] **Step 1: Write failing tests for preview hashing and invalidation**

```python
# tests/test_resolution_preview.py
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests.support.library_fixtures import LibraryFixtures
from app.library.contracts import (
    ENTRY_PRODUCT, ENTRY_DIMENSION_TEMPLATE, ENTRY_DIMENSION_SCHEMA,
    ENTRY_PROMPT_POLICY, ENTRY_DATA_SOURCE,
    ROLE_OWNER, canonical_hash,
)
from app.library import service
from app.library.templates import apply_template
from app.library.preview import (
    build_canonical_snapshot, compute_preview_hashes,
    compute_recipe_input_hash, SNAPSHOT_SCHEMA_VERSION,
)


class TestCanonicalSnapshot(unittest.TestCase):
    """Deterministic hashing with fixed clock and inputs."""

    def test_same_inputs_same_recipe_hash(self):
        snapshot = _sample_snapshot()
        h1 = compute_recipe_input_hash(snapshot)
        h2 = compute_recipe_input_hash(snapshot)
        self.assertEqual(h1, h2)

    def test_same_inputs_same_preview_hash(self):
        snapshot = _sample_snapshot()
        h1 = compute_preview_hashes(snapshot)
        h2 = compute_preview_hashes(snapshot)
        self.assertEqual(h1["preview_hash"], h2["preview_hash"])

    def test_different_time_bound_fields_same_recipe_hash(self):
        s1 = _sample_snapshot()
        s2 = _sample_snapshot()
        s2["cost_quote"]["expires_at"] = "2026-12-31T00:00:00Z"
        s2["credential_evidence"]["binding_revision"] = 99
        self.assertEqual(
            compute_recipe_input_hash(s1),
            compute_recipe_input_hash(s2),
        )

    def test_different_time_bound_fields_different_preview_hash(self):
        s1 = _sample_snapshot()
        s2 = _sample_snapshot()
        s2["cost_quote"]["expires_at"] = "2026-12-31T00:00:00Z"
        self.assertNotEqual(
            compute_preview_hashes(s1)["preview_hash"],
            compute_preview_hashes(s2)["preview_hash"],
        )

    def test_snapshot_schema_version_in_hash(self):
        s = _sample_snapshot()
        self.assertEqual(s["_schema"], f"resolution_preview/{SNAPSHOT_SCHEMA_VERSION}")

    def test_canonicalization_sorts(self):
        s1 = _sample_snapshot()
        s2 = _sample_snapshot()
        # Reverse the reference choices order
        s2["reference_choices"] = list(reversed(s2["reference_choices"]))
        # After canonicalization, hashes should still be equal
        self.assertEqual(
            compute_preview_hashes(s1)["preview_hash"],
            compute_preview_hashes(s2)["preview_hash"],
        )


def _sample_snapshot():
    return {
        "_schema": f"resolution_preview/{SNAPSHOT_SCHEMA_VERSION}",
        "resolved_versions": {
            "product": {"selector": "latest", "entry_id": "lib_p1",
                        "version_id": "lib_v1", "content_hash": "abc"},
        },
        "constraint_results": {
            "valid_run_items": [{"d0": "v0"}],
            "skip_reasons": [],
        },
        "prompt_assembly": {
            "final_prompt": "test prompt",
            "layer_hashes": {"baseline": "h1", "policy": "h2", "run_inputs": "h3"},
            "composition_hash": "ch",
        },
        "reference_choices": [
            {"run_item_index": 0, "refs": [{"ref_version_id": "rv1"}]},
            {"run_item_index": 1, "refs": [{"ref_version_id": "rv2"}]},
        ],
        "provider_decisions": {
            "provider": "mock", "model": "mock-v1",
            "params": {}, "capability_version": "1.0",
            "pruning": {"needed": False, "decision_hash": "dh"},
        },
        "credential_evidence": {"binding_revision": 1, "eligible": True},
        "exclusions": [],
        "cost_quote": {
            "payable_max_cost": "0",
            "currency": "USD",
            "simulated_estimate": "1.50",
            "expires_at": "2026-10-01T00:00:00Z",
        },
        "eligibility": {"overall": "eligible", "checks": []},
        "confirmation_decisions": [],
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_resolution_preview.py -v`
Expected: FAIL — module doesn't exist

- [ ] **Step 3: Implement preview.py**

```python
# app/library/preview.py
"""ResolutionPreview: freeze, hash, invalidate.

Spec §4.4: selector-aware version resolution, canonical hashing,
time-bound vs recipe-only hashing, invalidation.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    ResolutionPreviewRecord, new_id, utcnow,
)
from app.platform.tenancy import TenantContext
from app.library.contracts import canonical_hash
from app.library.service import (
    LibraryError, LibraryNotFound,
    _run_with_idempotency, _require_workspace, _audit,
)

SNAPSHOT_SCHEMA_VERSION = "20.1"

# Fields excluded from recipe_input_hash (time-bound)
_TIME_BOUND_KEYS = frozenset({
    "credential_evidence",
    "confirmation_decisions",
})
_TIME_BOUND_NESTED = {
    "cost_quote": frozenset({"expires_at"}),
}


def build_canonical_snapshot(
    resolved_versions: dict,
    constraint_results: dict,
    prompt_assembly: dict,
    reference_choices: list[dict],
    provider_decisions: dict,
    credential_evidence: dict,
    exclusions: list,
    cost_quote: dict,
    eligibility: dict,
    confirmation_decisions: list | None = None,
) -> dict:
    """Assemble the authoritative immutable snapshot aggregate."""
    return {
        "_schema": f"resolution_preview/{SNAPSHOT_SCHEMA_VERSION}",
        "resolved_versions": resolved_versions,
        "constraint_results": constraint_results,
        "prompt_assembly": prompt_assembly,
        "reference_choices": sorted(
            reference_choices, key=lambda r: r.get("run_item_index", 0)
        ),
        "provider_decisions": provider_decisions,
        "credential_evidence": credential_evidence,
        "exclusions": sorted(exclusions, key=str),
        "cost_quote": cost_quote,
        "eligibility": eligibility,
        "confirmation_decisions": confirmation_decisions or [],
    }


def _strip_time_bound(snapshot: dict) -> dict:
    """Remove time-bound fields for recipe_input_hash."""
    result = {}
    for key, value in snapshot.items():
        if key in _TIME_BOUND_KEYS:
            continue
        if key in _TIME_BOUND_NESTED:
            nested = dict(value)
            for nk in _TIME_BOUND_NESTED[key]:
                nested.pop(nk, None)
            result[key] = nested
        else:
            result[key] = value
    return result


def compute_recipe_input_hash(snapshot: dict) -> str:
    """Hash excluding time-bound fields."""
    stripped = _strip_time_bound(snapshot)
    return canonical_hash(stripped)


def compute_preview_hashes(snapshot: dict) -> dict[str, str]:
    """Compute both preview_hash and recipe_input_hash."""
    # Canonicalize: sort reference_choices by run_item_index
    canonical = dict(snapshot)
    if "reference_choices" in canonical:
        canonical["reference_choices"] = sorted(
            canonical["reference_choices"],
            key=lambda r: r.get("run_item_index", 0),
        )
    if "exclusions" in canonical:
        canonical["exclusions"] = sorted(canonical["exclusions"], key=str)
    return {
        "preview_hash": canonical_hash(canonical),
        "recipe_input_hash": compute_recipe_input_hash(canonical),
    }


def persist_preview(
    context: TenantContext,
    generation_draft_id: str,
    draft_revision: int,
    snapshot: dict,
    execution_mode: str,
    valid_until: datetime,
) -> dict:
    """Save a resolved preview to the database."""
    hashes = compute_preview_hashes(snapshot)
    with tenant_session(context) as session:
        _require_workspace(session, context.workspace_id)
        record = ResolutionPreviewRecord(
            id=new_id("rpv"),
            workspace_id=context.workspace_id,
            generation_draft_id=generation_draft_id,
            draft_revision=draft_revision,
            snapshot_json=snapshot,
            snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
            preview_hash=hashes["preview_hash"],
            recipe_input_hash=hashes["recipe_input_hash"],
            execution_mode=execution_mode,
            valid_until=valid_until,
            created_by=context.initiator_id or "",
        )
        session.add(record)
        _audit(session, context, "resolve_preview",
               detail={
                   "preview_hash": hashes["preview_hash"],
                   "generation_draft_id": generation_draft_id,
               })
        session.flush()
        return {
            "id": record.id,
            "preview_hash": hashes["preview_hash"],
            "recipe_input_hash": hashes["recipe_input_hash"],
            "execution_mode": execution_mode,
            "valid_until": valid_until.isoformat(),
        }


def invalidate_previews_for_version(
    context: TenantContext,
    version_id: str,
    reason: str,
    *,
    selector_filter: str | None = None,
) -> int:
    """Invalidate unexpired previews referencing a given version (§4.4 invalidation).

    selector_filter: "latest" only invalidates latest-resolved previews.
    None invalidates all (used for withdrawal).
    """
    now = utcnow()
    with tenant_session(context) as session:
        # Find all non-invalidated, non-expired previews
        stmt = select(ResolutionPreviewRecord).where(
            ResolutionPreviewRecord.workspace_id == context.workspace_id,
            ResolutionPreviewRecord.invalidated_at.is_(None),
            ResolutionPreviewRecord.valid_until > now,
            ResolutionPreviewRecord.submitted_run_id.is_(None),
        )
        previews = session.scalars(stmt).all()
        count = 0
        for preview in previews:
            snap = preview.snapshot_json or {}
            resolved = snap.get("resolved_versions") or {}
            for _key, entry_info in resolved.items():
                if isinstance(entry_info, dict) and entry_info.get("version_id") == version_id:
                    if selector_filter and entry_info.get("selector") != selector_filter:
                        continue
                    preview.invalidated_at = now
                    preview.invalidation_reason = reason[:300]
                    count += 1
                    break
                if isinstance(entry_info, list):
                    for item in entry_info:
                        if isinstance(item, dict) and item.get("version_id") == version_id:
                            if selector_filter and item.get("selector") != selector_filter:
                                continue
                            preview.invalidated_at = now
                            preview.invalidation_reason = reason[:300]
                            count += 1
                            break
        session.flush()
    return count


def check_preview_valid(
    context: TenantContext,
    preview_id: str,
    expected_hash: str,
) -> dict:
    """Submission-time check: preview exists, hash matches, not invalidated, not expired, not consumed."""
    with tenant_session(context) as session:
        record = session.get(ResolutionPreviewRecord, preview_id)
        if record is None or record.workspace_id != context.workspace_id:
            raise LibraryNotFound("Preview 不存在")
        now = utcnow()
        if record.preview_hash != expected_hash:
            raise LibraryError("Preview hash 不匹配")
        if record.invalidated_at is not None:
            raise LibraryError(f"Preview 已失效: {record.invalidation_reason}")
        if record.valid_until < now:
            raise LibraryError("Preview 已过期")
        if record.submitted_run_id is not None:
            raise LibraryError("Preview 已被提交")
        return {
            "id": record.id,
            "snapshot": dict(record.snapshot_json),
            "execution_mode": record.execution_mode,
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_resolution_preview.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add app/library/preview.py tests/test_resolution_preview.py
git commit -m "feat(library): resolution preview with canonical hashing and invalidation (#20)"
```

---

### Task 8: Integration Tests, Cross-Tenant & Role Evidence

**Files:**
- Test: `tests/test_library_integration_20.py`
- Modify: `tests/test_postgres_rls.py` (add new tables to inventory)

**Interfaces:**
- Consumes: All modules from Tasks 1-7
- Produces: Full integration test suite covering acceptance criteria from issue #20

- [ ] **Step 1: Write integration test for full lifecycle**

```python
# tests/test_library_integration_20.py
"""Integration tests for issue #20: templates, prompt policy, data sources, preview.

Cross-tenant, role, idempotency, and transaction evidence.
"""
import unittest
from tests.support.library_fixtures import LibraryFixtures, FUTURE
from app.library.contracts import (
    ENTRY_DIMENSION_TEMPLATE, ENTRY_DIMENSION_SCHEMA,
    ENTRY_PROMPT_POLICY, ENTRY_DATA_SOURCE,
    ENTRY_MANUAL_FACT_SET, ENTRY_TRUTH_RESOLUTION_POLICY,
    ENTRY_PRODUCT, ROLE_OWNER, ROLE_MEMBER,
)
from app.library import service
from app.library.service import LibraryForbidden, LibraryValidationError
from app.library.templates import apply_template
from app.library.prompt_policy import publish_baseline
from app.library.datasources import bind_credential


class TestOwnerOnlyPublish(LibraryFixtures):
    """Member cannot publish Owner-only entry types."""

    def test_member_cannot_publish_prompt_policy(self):
        ctx = self.ctx(role=ROLE_MEMBER)
        entry = service.create_entry(ctx, entry_type=ENTRY_PROMPT_POLICY,
                                     display_name="Test Policy",
                                     content=_policy_content())
        rev = entry["draft"]["revision"]
        service.validate_draft(ctx, entry["id"], expected_revision=rev)
        with self.assertRaises(LibraryForbidden):
            service.publish(ctx, entry["id"], rev)

    def test_owner_can_publish_prompt_policy(self):
        ctx = self.ctx(role=ROLE_OWNER)
        entry = service.create_entry(ctx, entry_type=ENTRY_PROMPT_POLICY,
                                     display_name="Test Policy",
                                     content=_policy_content())
        rev = entry["draft"]["revision"]
        service.validate_draft(ctx, entry["id"], expected_revision=rev)
        version = service.publish(ctx, entry["id"], rev)
        self.assertEqual(version["state"], "effective")

    def test_member_can_publish_dimension_template(self):
        ctx = self.ctx(role=ROLE_MEMBER)
        entry = service.create_entry(ctx, entry_type=ENTRY_DIMENSION_TEMPLATE,
                                     display_name="Test Template",
                                     content=_template_content())
        rev = entry["draft"]["revision"]
        service.validate_draft(ctx, entry["id"], expected_revision=rev)
        version = service.publish(ctx, entry["id"], rev)
        self.assertEqual(version["state"], "effective")


class TestCrossTenantIsolation(LibraryFixtures):
    """Cross-workspace access is blocked for all new entry types."""

    def test_cross_workspace_template_blocked(self):
        ctx_a = self.ctx()
        ctx_b = self.ctx()
        entry = service.create_entry(ctx_a, entry_type=ENTRY_DIMENSION_TEMPLATE,
                                     display_name="Template A",
                                     content=_template_content())
        with self.assertRaises(Exception):
            service.validate_draft(ctx_b, entry["id"], expected_revision=1)


class TestIdempotencyEvidence(LibraryFixtures):
    """Same key + fingerprint replays; same key + different fingerprint rejects."""

    def test_same_key_replays(self):
        ctx = self.ctx()
        r1 = service.create_entry(ctx, entry_type=ENTRY_DIMENSION_TEMPLATE,
                                  display_name="Template",
                                  content=_template_content(),
                                  idempotency_key="idem-tmpl-1",
                                  request_fingerprint="fp1")
        r2 = service.create_entry(ctx, entry_type=ENTRY_DIMENSION_TEMPLATE,
                                  display_name="Template",
                                  content=_template_content(),
                                  idempotency_key="idem-tmpl-1",
                                  request_fingerprint="fp1")
        self.assertEqual(r1["id"], r2["id"])

    def test_different_fingerprint_rejects(self):
        ctx = self.ctx()
        service.create_entry(ctx, entry_type=ENTRY_DIMENSION_TEMPLATE,
                             display_name="Template",
                             content=_template_content(),
                             idempotency_key="idem-tmpl-2",
                             request_fingerprint="fp1")
        from app.library.service import IdempotencyConflict
        with self.assertRaises(IdempotencyConflict):
            service.create_entry(ctx, entry_type=ENTRY_DIMENSION_TEMPLATE,
                                 display_name="Template",
                                 content=_template_content(),
                                 idempotency_key="idem-tmpl-2",
                                 request_fingerprint="fp_different")


class TestBlockingValidationNoPartialVersion(LibraryFixtures):
    """A blocked publish produces no version or dependency rows."""

    def test_blocked_publish_no_version(self):
        ctx = self.ctx()
        entry = service.create_entry(ctx, entry_type=ENTRY_DIMENSION_TEMPLATE,
                                     display_name="Bad Template",
                                     content={"dimensions": []})
        rev = entry["draft"]["revision"]
        with self.assertRaises(LibraryValidationError):
            service.publish(ctx, entry["id"], rev)
        # No version should exist
        versions = service.list_versions(ctx, entry["id"])
        self.assertEqual(len(versions), 0)


def _template_content():
    return {
        "dimensions": [{
            "id": "dim-x", "name": "X",
            "values": [{"id": "val-1", "name": "V1",
                        "positive_fragments": ["v1"], "negative_fragments": []}],
        }],
        "constraints": [],
        "concept_key_mappings": [],
        "combination_cardinality": "one_per_dimension",
    }


def _policy_content():
    return {
        "registered_tokens": [],
        "locked_phrases": [],
        "brand_tone": {"style": "neutral"},
        "allowed_template_entry_ids": [],
        "optional_clauses": [],
    }
```

- [ ] **Step 2: Run tests**

Run: `python -m pytest tests/test_library_integration_20.py -v`
Expected: All PASS

- [ ] **Step 3: Update PostgreSQL RLS test inventory**

In `tests/test_postgres_rls.py`, add the new tables to the protected-table inventory:
- `dimension_concept_keys`
- `datasource_credential_bindings`
- `resolution_previews`

Note: `prompt_baselines` is intentionally not in the RLS inventory — it's platform-scoped, not workspace-scoped.

- [ ] **Step 4: Run full test suite**

Run: `make verify` or `python -m pytest tests/ -v --tb=short`
Expected: All tests PASS including existing tests.

- [ ] **Step 5: Commit**

```bash
git add tests/test_library_integration_20.py tests/test_postgres_rls.py
git commit -m "feat(library): integration tests and acceptance evidence for #20"
```

---

## Post-Implementation Checklist

After all tasks complete:

1. Run `make verify` — all tests pass.
2. Verify migration upgrade and downgrade on both SQLite and PostgreSQL.
3. Open a PR referencing issue #20 with:
   - Migration: new tables, no schema changes to existing tables
   - Test evidence: constraint solver, template apply/reapply, prompt composition, credential binding, preview hashing, cross-tenant isolation, role boundaries
   - Operational impact: 4 new tables, no external service dependencies
   - Remaining risks: Shopify adapter (#9), provider execution (#21), MFA step-up proof (TenantContext extension needed)
