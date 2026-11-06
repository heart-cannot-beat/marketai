"""PostgreSQL/RLS integration gate for the Scale domain (Issue #25, Task 18).

Spec: docs/architecture/saas-ui-fastapi-integration-contract.md, ADRs 0002-0004
Task: .superpowers/sdd/2026-09-08-scale-deterministic-evidence-advice/task-18-brief.md §2

Every gate the task brief lists for this file --

* cross-workspace read fails via RLS
* cross-mode read fails via RLS
* composite FK violations rejected at the database level
* the partial unique index enforces binding reservation
* immutability triggers reject UPDATE on sealed snapshots
* immutability triggers reject DELETE on evaluation records

-- is already fully implemented by
``tests/test_scale_migration.py::ScalePostgresConstraintTests`` (Task 6),
one test method per bullet, marked ``@pytest.mark.postgres`` and gated on
the same ``DOMAIN_OWNER_DATABASE_URL`` / ``DOMAIN_API_DATABASE_URL`` /
``MARKETAI_TENANT_CONTEXT_SECRET`` triple ``tests/test_postgres_rls.py``
and ``tests/test_localization_rls.py`` use (not the
``MARKETAI_PG_TEST_URL``/psycopg sketch in this task's own brief, which
does not match any convention actually used elsewhere in this
repository). Re-deriving those ~280 lines under this filename would be
pure duplication, so this module instead:

1. re-exports that class so pytest also collects and runs it under this
   filename, matching the task brief's and the acceptance-gate
   traceability table's expectation that ``test_scale_domain_pg.py``
   exists and carries this coverage;
2. adds the two gates that class does *not* yet cover: a cross-workspace
   *write* rejected by RLS (only cross-workspace *read* was tested), and
   composite-FK enforcement exercised through the real least-privilege
   API role under RLS (Task 6's composite-FK tests all run as the
   ``owner`` role on SQLite -- see ``ScaleCompositeForeignKeyTests`` in
   that same module -- which is dialect-portable but never proves the
   FK still holds for a *scoped* connection with a live RLS policy
   active on the same statement).

Skips entirely (both this file and ``ScalePostgresConstraintTests``) when
the environment does not provide a real PostgreSQL cluster provisioned
per ``deploy/postgres/domain-tenant-roles.sql`` and migrated to head, so
``make verify`` stays clean on a SQLite-only checkout; CI supplies the
cluster.
"""
from __future__ import annotations

import unittest
import uuid

import pytest

# Re-export: pytest collects this class under this filename too.
from tests.test_scale_migration import (  # noqa: F401
    API_URL,
    CONFIGURED,
    CONTEXT_SECRET,
    INSERT_CONTRACT_SET,
    INSERT_ENROLLMENT,
    INSERT_EPISODE,
    INSERT_WORKSPACE,
    OWNER_URL,
    SKIP_REASON,
    ScalePostgresConstraintTests,
)


@pytest.mark.postgres
@unittest.skipUnless(CONFIGURED, SKIP_REASON)
class TestCrossWorkspaceWriteRejectedByRLS(unittest.TestCase):
    """The read-side of this guarantee is
    ``ScalePostgresConstraintTests.test_cross_workspace_read_blocked_by_rls``;
    this is its write-side complement -- an INSERT whose own row data
    claims a *different* Workspace than the session's RLS scope must be
    rejected by the policy's ``WITH CHECK`` clause, not merely hidden
    from later reads."""

    @classmethod
    def setUpClass(cls) -> None:
        from sqlalchemy import create_engine, text

        cls.create_engine = staticmethod(create_engine)
        cls.text = staticmethod(text)
        cls.owner = create_engine(OWNER_URL, future=True)
        cls.workspace = f"ws_scale_pg_{uuid.uuid4().hex[:8]}"
        cls.other_workspace = f"ws_scale_pg_other_{uuid.uuid4().hex[:8]}"
        with cls.owner.begin() as conn:
            conn.execute(cls.text(INSERT_WORKSPACE), {"id": cls.workspace})
            conn.execute(cls.text(INSERT_WORKSPACE), {"id": cls.other_workspace})

    @classmethod
    def tearDownClass(cls) -> None:
        with cls.owner.begin() as conn:
            for ws in (cls.workspace, cls.other_workspace):
                conn.execute(
                    cls.text("DELETE FROM scale_metric_semantic_contract_sets WHERE workspace_id = :ws"),
                    {"ws": ws},
                )
                conn.execute(cls.text("DELETE FROM workspaces WHERE id = :ws"), {"ws": ws})
        cls.owner.dispose()

    def _scoped_connection(self, workspace: str, meta_mode: str | None):
        from app.platform.tenancy import TenantContext, tenant_context_signature

        engine = self.create_engine(API_URL, future=True)
        self.addCleanup(engine.dispose)
        conn = engine.connect()
        self.addCleanup(conn.close)
        transaction = conn.begin()
        self.addCleanup(lambda: transaction.rollback() if transaction.is_active else None)

        context = TenantContext(
            workspace_id=workspace, role="owner", initiator_id="scale-pg-test",
            authorization_epoch=0, meta_mode=meta_mode,
        )
        conn.execute(self.text("SELECT set_config('marketai.workspace_id', :w, true)"), {"w": workspace})
        conn.execute(self.text("SELECT set_config('marketai.authorization_epoch', '0', true)"))
        conn.execute(self.text("SELECT set_config('marketai.meta_mode', :m, true)"), {"m": meta_mode or ""})
        conn.execute(
            self.text("SELECT set_config('marketai.context_signature', :s, true)"),
            {"s": tenant_context_signature(context, CONTEXT_SECRET)},
        )
        return conn

    def test_insert_claiming_a_different_workspace_is_rejected(self) -> None:
        from sqlalchemy.exc import DBAPIError

        conn = self._scoped_connection(self.workspace, "customer_sandbox")
        contract_set_id = f"smcs_{uuid.uuid4().hex[:8]}"

        with self.assertRaises(DBAPIError):
            conn.execute(
                self.text(INSERT_CONTRACT_SET),
                {
                    "id": contract_set_id,
                    # The connection's own RLS scope is `self.workspace`;
                    # this row's data claims `self.other_workspace`.
                    "workspace_id": self.other_workspace,
                    "meta_mode": "customer_sandbox",
                },
            )


@pytest.mark.postgres
@unittest.skipUnless(CONFIGURED, SKIP_REASON)
class TestCompositeFKUnderRLSScope(unittest.TestCase):
    """``ScaleCompositeForeignKeyTests`` (Task 6, dialect-portable) proves
    the composite ``(workspace_id, meta_mode, enrollment_id)`` foreign key
    on ``scale_episodes`` rejects a cross-Workspace parent using the
    ``owner`` engine directly. This proves the same constraint still
    fires for the real least-privilege API role with a live RLS policy
    active on the same INSERT -- the FK and the RLS policy are
    independent defenses and neither should be able to mask the other's
    absence."""

    @classmethod
    def setUpClass(cls) -> None:
        from sqlalchemy import create_engine, text

        cls.create_engine = staticmethod(create_engine)
        cls.text = staticmethod(text)
        cls.owner = create_engine(OWNER_URL, future=True)
        cls.workspace = f"ws_scale_pgfk_{uuid.uuid4().hex[:8]}"
        cls.foreign_workspace = f"ws_scale_pgfk_other_{uuid.uuid4().hex[:8]}"
        with cls.owner.begin() as conn:
            conn.execute(cls.text(INSERT_WORKSPACE), {"id": cls.workspace})
            conn.execute(cls.text(INSERT_WORKSPACE), {"id": cls.foreign_workspace})
            cls.foreign_enrollment_id = f"senr_{uuid.uuid4().hex[:8]}"
            conn.execute(
                cls.text(INSERT_ENROLLMENT),
                {
                    "id": cls.foreign_enrollment_id,
                    "workspace_id": cls.foreign_workspace,
                    "meta_mode": "customer_sandbox",
                    "meta_connection_id": "mcon_fk",
                    "ad_account_id": "act_fk",
                    "remote_binding_key": "mcon_fk/act_fk/camp_1",
                    "lifecycle": "active",
                },
            )
            cls.contract_set_id = f"smcs_{uuid.uuid4().hex[:8]}"
            conn.execute(
                cls.text(INSERT_CONTRACT_SET),
                {"id": cls.contract_set_id, "workspace_id": cls.workspace, "meta_mode": "customer_sandbox"},
            )

    @classmethod
    def tearDownClass(cls) -> None:
        with cls.owner.begin() as conn:
            for ws in (cls.workspace, cls.foreign_workspace):
                conn.execute(cls.text("DELETE FROM scale_episodes WHERE workspace_id = :ws"), {"ws": ws})
                conn.execute(cls.text("DELETE FROM scale_enrollments WHERE workspace_id = :ws"), {"ws": ws})
                conn.execute(
                    cls.text("DELETE FROM scale_metric_semantic_contract_sets WHERE workspace_id = :ws"),
                    {"ws": ws},
                )
                conn.execute(cls.text("DELETE FROM workspaces WHERE id = :ws"), {"ws": ws})
        cls.owner.dispose()

    def _scoped_connection(self, workspace: str, meta_mode: str | None):
        from app.platform.tenancy import TenantContext, tenant_context_signature

        engine = self.create_engine(API_URL, future=True)
        self.addCleanup(engine.dispose)
        conn = engine.connect()
        self.addCleanup(conn.close)
        transaction = conn.begin()
        self.addCleanup(lambda: transaction.rollback() if transaction.is_active else None)

        context = TenantContext(
            workspace_id=workspace, role="owner", initiator_id="scale-pg-fk-test",
            authorization_epoch=0, meta_mode=meta_mode,
        )
        conn.execute(self.text("SELECT set_config('marketai.workspace_id', :w, true)"), {"w": workspace})
        conn.execute(self.text("SELECT set_config('marketai.authorization_epoch', '0', true)"))
        conn.execute(self.text("SELECT set_config('marketai.meta_mode', :m, true)"), {"m": meta_mode or ""})
        conn.execute(
            self.text("SELECT set_config('marketai.context_signature', :s, true)"),
            {"s": tenant_context_signature(context, CONTEXT_SECRET)},
        )
        return conn

    def test_episode_referencing_a_foreign_workspace_enrollment_is_rejected(self) -> None:
        from sqlalchemy.exc import DBAPIError

        conn = self._scoped_connection(self.workspace, "customer_sandbox")
        episode_id = f"sep_{uuid.uuid4().hex[:8]}"

        with self.assertRaises(DBAPIError):
            conn.execute(
                self.text(INSERT_EPISODE),
                {
                    "id": episode_id,
                    "workspace_id": self.workspace,
                    "meta_mode": "customer_sandbox",
                    # Real Enrollment id -- just owned by a different
                    # Workspace than this Episode row (and this
                    # connection's own RLS scope) declares.
                    "enrollment_id": self.foreign_enrollment_id,
                    "meta_connection_id": "mcon_fk",
                    "ad_account_id": "act_fk",
                    "contract_set_id": self.contract_set_id,
                    "hash1": "e" * 64,
                    "hash2": "c" * 64,
                    "hash3": "a" * 64,
                },
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
