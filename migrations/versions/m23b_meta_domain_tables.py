"""Physical schema for all Meta/Launch domain tables (issue #23, Task 7).

Creates the 19 Meta-domain tables (``app.meta.models``, Tasks 3-4/§3.1-3.14,
§3.22-3.26, §4.3) and 7 Launch-domain tables (``app.launch.models``, Tasks
5-6/§3.15-3.21) as physical PostgreSQL/SQLite tables.

Each table is created from its already-mapped SQLAlchemy ``Table`` object
(``Model.__table__.create(...)``) rather than a hand-transcribed
``op.create_table()`` call. The 26 tables carry composite foreign keys, a
project-wide naming convention that re-prefixes ``CheckConstraint`` names
with the table name (see ``app.adapters.persistence.database.Base``'s
``NAMING_CONVENTION``), and several partial unique ``Index`` objects
declared at module scope in the model files -- transcribing all of that by
hand for 26 tables is exactly the kind of drift ``alembic check`` exists to
catch. Building the table from the mapped object instead guarantees the
physical schema and the ORM metadata can never disagree.

On PostgreSQL this migration additionally:

* Enables Row Level Security (with FORCE) on every one of the 26 tables and
  installs the ``tenant_isolation`` policy using
  ``marketai_meta_context_is_current(workspace_id, meta_mode)`` (the
  HMAC-verified, meta-mode-aware sibling of ``marketai_current_workspace()``
  created in ``m23a_meta_foundation``).
* Grants SELECT/INSERT/UPDATE to ``marketai_domain_api`` and
  ``marketai_domain_worker``, and SELECT to ``marketai_domain_readonly``.
  No role is granted DELETE: every Meta/Launch-domain table is either
  immutable or an append-only/current-state projection over an
  append-only history, matching the project's other domain tables.
* Installs the spec §3.29 deferred constraint trigger: an
  ``internal_validation`` ``MetaConnectionRecord`` may only exist in a
  platform workspace (``workspaces.is_platform_workspace = true``, the flag
  added in ``m23a_meta_foundation``). The trigger is DEFERRABLE INITIALLY
  DEFERRED so a same-transaction workspace flag flip and connection insert
  can still commit together.

Revision ID: m23b_meta_domain_tables
Revises: m23a_meta_foundation
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.adapters.persistence.database import Base
from app.meta.models import (
    EvidenceAttestationRecord,
    EvidencePublicationRecord,
    FakeMetaObjectRecord,
    MetaCallIntentBindingRecord,
    MetaCallIntentRecord,
    MetaCallOutcomeRecord,
    MetaCallReconciliationRecord,
    MetaConnectionRecord,
    MetaDataSourceBindingRecord,
    MetaEvidencePageRecord,
    MetaObservationSyncRunRecord,
    MetaObservationSyncScheduleRecord,
    MetaPageFetchAttemptRecord,
    MetaStatusHistoryRecord,
    ObservationContractGenerationRecord,
    ObservationCoverageLedgerRecord,
    ScaleVerdictTaintRecord,
    ScenarioVersionRecord,
    VerdictSupersessionRecord,
)
from app.launch.models import (
    LaunchCandidateRecord,
    LaunchParityCheckRecord,
    LaunchProvisioningRecord,
    LaunchProvisioningStatusHistoryRecord,
    LaunchReconciliationRecord,
    LaunchSagaStepAttemptRecord,
    MetaLaunchPreviewRecord,
)

revision = "m23b_meta_domain_tables"
down_revision = "m23a_meta_foundation"
branch_labels = None
depends_on = None


# Dependency order: every table here only references a table earlier in this
# tuple, or a table from an earlier migration (workspaces, credential_versions,
# operation_records, launch_confirmations). Downgrade drops in reverse.
_TABLES_IN_DEPENDENCY_ORDER = (
    # --- app.meta.models --------------------------------------------------
    MetaConnectionRecord,
    MetaDataSourceBindingRecord,
    ScenarioVersionRecord,
    FakeMetaObjectRecord,
    MetaCallIntentRecord,
    MetaCallOutcomeRecord,
    MetaCallReconciliationRecord,
    MetaCallIntentBindingRecord,
    ObservationContractGenerationRecord,
    MetaObservationSyncRunRecord,
    MetaEvidencePageRecord,
    MetaPageFetchAttemptRecord,
    EvidencePublicationRecord,
    EvidenceAttestationRecord,
    MetaStatusHistoryRecord,
    ObservationCoverageLedgerRecord,
    MetaObservationSyncScheduleRecord,
    ScaleVerdictTaintRecord,
    VerdictSupersessionRecord,
    # --- app.launch.models --------------------------------------------------
    MetaLaunchPreviewRecord,
    LaunchCandidateRecord,
    LaunchProvisioningRecord,
    LaunchProvisioningStatusHistoryRecord,
    LaunchSagaStepAttemptRecord,
    LaunchParityCheckRecord,
    LaunchReconciliationRecord,
)

#: Picked up by the postgres RLS acceptance sweep -- see
#: ``tests/test_postgres_rls.py``'s ``all_protected_tables()``.
NEW_WORKSPACE_OWNED = tuple(model.__tablename__ for model in _TABLES_IN_DEPENDENCY_ORDER)

_PLATFORM_WORKSPACE_TRIGGER_FUNCTION = (
    "marketai_check_internal_validation_platform_workspace"
)
_PLATFORM_WORKSPACE_TRIGGER = "trg_meta_connection_platform_workspace"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _create_tables() -> None:
    bind = op.get_bind()
    for model in _TABLES_IN_DEPENDENCY_ORDER:
        if model is LaunchProvisioningRecord:
            _historical_launch_provisioning_table().create(
                bind=bind, checkfirst=False
            )
            continue
        model.__table__.create(bind=bind, checkfirst=False)


def _historical_launch_provisioning_table() -> sa.Table:
    """Return the provisioning schema owned by this historical revision.

    ``m23b`` predates the mode-scoped confirmation table introduced by
    ``a79b1c2d3e4f``.  Reusing the live ORM table directly would make a fresh
    migration try to reference that future table before it exists.  Clone
    the metadata, replace only the future FK with the original canary
    confirmation FK, and leave the live runtime metadata untouched.  The
    ``a79`` migration later backfills and replaces this constraint.
    """
    metadata = sa.MetaData(naming_convention=Base.metadata.naming_convention)
    for table in Base.metadata.tables.values():
        table.to_metadata(metadata)
    provisioning = metadata.tables[LaunchProvisioningRecord.__tablename__]
    future_fk = next(
        constraint
        for constraint in provisioning.foreign_key_constraints
        if constraint.name == "fk_lprov_workspace_mode_confirmation"
    )
    provisioning.constraints.remove(future_fk)
    for element in future_fk.elements:
        element.parent.foreign_keys.discard(element)
        provisioning.foreign_keys.discard(element)
    provisioning.append_constraint(
        sa.ForeignKeyConstraint(
            ["confirmation_id"],
            ["launch_confirmations.id"],
            name="fk_lprov_confirmation",
            ondelete="RESTRICT",
        )
    )
    return provisioning


def _drop_tables() -> None:
    for model in reversed(_TABLES_IN_DEPENDENCY_ORDER):
        op.drop_table(model.__tablename__)


def _install_rls_and_grants() -> None:
    if not _is_postgres():
        # SQLite: the repository layer is the only enforcement available,
        # same as every other tenant-owned table in this project.
        return

    for model in _TABLES_IN_DEPENDENCY_ORDER:
        table = model.__tablename__
        # FORCE applies the policy to the table owner too -- the runtime
        # roles own nothing, but this is belt and braces against a
        # migration-role mistake reading across tenants or Meta modes.
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"')
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON "{table}"
            USING (marketai_meta_context_is_current(workspace_id, meta_mode))
            WITH CHECK (marketai_meta_context_is_current(workspace_id, meta_mode))
            """
        )
        op.execute(
            f'GRANT SELECT, INSERT, UPDATE ON TABLE "{table}" '
            "TO marketai_domain_api, marketai_domain_worker"
        )
        op.execute(f'GRANT SELECT ON TABLE "{table}" TO marketai_domain_readonly')


def _uninstall_rls_and_grants() -> None:
    if not _is_postgres():
        return

    for model in reversed(_TABLES_IN_DEPENDENCY_ORDER):
        table = model.__tablename__
        op.execute(
            f'REVOKE ALL ON TABLE "{table}" FROM '
            "marketai_domain_api, marketai_domain_worker, marketai_domain_readonly"
        )
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')


def _install_platform_workspace_trigger() -> None:
    """Spec §3.29 #2: an internal_validation connection needs a platform workspace.

    A deferred constraint trigger, not a CHECK, because the rule spans two
    tables (``meta_connection_records`` and ``workspaces``). SECURITY DEFINER
    (like ``marketai_meta_context_is_current`` in ``m23a_meta_foundation``)
    so the lookup is not itself filtered by RLS on ``workspaces``.
    """
    if not _is_postgres():
        return

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {_PLATFORM_WORKSPACE_TRIGGER_FUNCTION}()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF NEW.meta_mode = 'internal_validation' THEN
                IF NOT EXISTS (
                    SELECT 1 FROM public.workspaces
                    WHERE id = NEW.workspace_id AND is_platform_workspace = true
                ) THEN
                    RAISE EXCEPTION
                        'internal_validation connections require a platform workspace';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {_PLATFORM_WORKSPACE_TRIGGER_FUNCTION}() FROM PUBLIC"
    )
    op.execute(
        f'DROP TRIGGER IF EXISTS {_PLATFORM_WORKSPACE_TRIGGER} '
        'ON meta_connection_records'
    )
    op.execute(
        f"""
        CREATE CONSTRAINT TRIGGER {_PLATFORM_WORKSPACE_TRIGGER}
        AFTER INSERT OR UPDATE ON meta_connection_records
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION {_PLATFORM_WORKSPACE_TRIGGER_FUNCTION}()
        """
    )


def _uninstall_platform_workspace_trigger() -> None:
    if not _is_postgres():
        return
    op.execute(
        f'DROP TRIGGER IF EXISTS {_PLATFORM_WORKSPACE_TRIGGER} '
        'ON meta_connection_records'
    )
    op.execute(f"DROP FUNCTION IF EXISTS {_PLATFORM_WORKSPACE_TRIGGER_FUNCTION}()")


def upgrade() -> None:
    _create_tables()
    _install_rls_and_grants()
    _install_platform_workspace_trigger()


def downgrade() -> None:
    _uninstall_platform_workspace_trigger()
    _uninstall_rls_and_grants()
    _drop_tables()
