"""PostgreSQL evidence: Row Level Security on Localization tables (#24,
task 19, spec §3I).

Localization's tenant isolation is covered generically by
``tests/test_postgres_rls.py`` (every table in
``NEW_WORKSPACE_OWNED``, including the Localization ones, is checked there
for ``FORCE ROW LEVEL SECURITY`` and a ``tenant_isolation`` policy;
``PLATFORM_TABLES`` is separately classified as deliberately global -- see
its ``CURRENT_AGGREGATE_TABLES["localization_truth_bounded_qc"]``). This
module gives Localization-specific, row-level evidence on top of that: two
real Workspaces' real
``truth_packages``/``localization_batches`` rows, read back through the
actual ``marketai_domain_api`` role, prove the isolation the generic
schema-level check can only infer.

Requires a PostgreSQL cluster provisioned with
``deploy/postgres/control-plane-domain-roles.sql`` and
``deploy/postgres/domain-tenant-roles.sql``, migrated to head -- the exact
same requirement as ``tests/test_postgres_rls.py``, whose connection/
signing pattern this module reuses (``marketai_context_is_current``
requires not just a matching ``marketai.workspace_id`` but a valid HMAC
``marketai.context_signature`` -- see
``migrations/versions/e9f0a1b2c3d4_authenticate_tenant_context.py`` --
so every scoped connection here signs its context with
``app.platform.tenancy.tenant_context_signature`` exactly as
``tests/test_postgres_rls.py`` does). Skips entirely when the connection
strings are absent so ``make verify`` still runs on a clean checkout; PG
CI supplies them.
"""
from __future__ import annotations

import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone

OWNER_URL = os.environ.get("DOMAIN_OWNER_DATABASE_URL")
API_URL = os.environ.get("DOMAIN_API_DATABASE_URL")
SUPERUSER_URL = os.environ.get("DOMAIN_SUPERUSER_DATABASE_URL")
CONTEXT_SECRET = os.environ.get("MARKETAI_TENANT_CONTEXT_SECRET")

CONFIGURED = all((OWNER_URL, API_URL, CONTEXT_SECRET))

SKIP_REASON = (
    "set DOMAIN_OWNER_DATABASE_URL, DOMAIN_API_DATABASE_URL and "
    "MARKETAI_TENANT_CONTEXT_SECRET against a cluster provisioned with "
    "deploy/postgres/domain-tenant-roles.sql and migrated to head "
    "(matches tests/test_postgres_rls.py's requirements)"
)

SUPERUSER_SKIP_REASON = (
    "also set DOMAIN_SUPERUSER_DATABASE_URL: only a superuser may change "
    "marketai_domain_owner's BYPASSRLS attribute"
)


@unittest.skipUnless(CONFIGURED, SKIP_REASON)
class TestLocalizationRLS(unittest.TestCase):
    """Truth/Batch tenant isolation the application cannot forget, because
    it is not the enforcer -- ``marketai_context_is_current`` (the USING/
    WITH CHECK predicate every Localization table's ``tenant_isolation``
    policy shares) is."""

    @classmethod
    def setUpClass(cls) -> None:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        from app.adapters.persistence.models import (
            LibraryEntryRecord,
            LibraryVersionRecord,
            LocalizationBatchRecord,
            TruthPackageRecord,
            WorkspaceRecord,
            new_id,
        )

        cls._create_engine = staticmethod(create_engine)
        cls.owner = create_engine(OWNER_URL, future=True)
        cls.alpha = f"ws_loc_alpha_{uuid.uuid4().hex[:8]}"
        cls.beta = f"ws_loc_beta_{uuid.uuid4().hex[:8]}"

        now = datetime.now(timezone.utc)

        def _make_versions(session: Session, ws_id: str) -> list[str]:
            ids = []
            for kind in ("product", "offer", "truth_resolution_policy"):
                entry_id = new_id("le")
                session.add(LibraryEntryRecord(
                    id=entry_id, workspace_id=ws_id, entry_type=kind,
                    display_name=f"{kind}-{ws_id}", lifecycle="active",
                ))
                session.flush()
                version_id = new_id("lv")
                session.add(LibraryVersionRecord(
                    id=version_id, workspace_id=ws_id, entry_id=entry_id, entry_type=kind,
                    version_no=1, content_json={}, content_hash=f"hash-{version_id}",
                    state="effective", published_by="rls-test",
                ))
                session.flush()
                ids.append(version_id)
            return ids

        def _make_truth_package(session: Session, ws_id: str, versions: list[str]) -> str:
            tp_id = new_id("tp")
            session.add(TruthPackageRecord(
                id=tp_id, workspace_id=ws_id,
                product_version_id=versions[0], offer_version_id=versions[1],
                truth_resolution_policy_version_id=versions[2],
                truth_data_mode="real", canonical_hash=f"hash-{tp_id}".ljust(64, "0")[:64],
                status="full", valid_until=now + timedelta(days=1), created_at=now,
            ))
            session.flush()
            return tp_id

        def _make_batch(session: Session, ws_id: str, versions: list[str]) -> str:
            batch_id = new_id("lb")
            session.add(LocalizationBatchRecord(
                id=batch_id, workspace_id=ws_id,
                product_version_id=versions[0], offer_version_id=versions[1],
                source_market_snapshot_json={"market": "US", "placement": "feed", "locale": "en-US", "country": "US", "currency": "USD"}, truth_package_id=None,
                truth_data_mode="real", provider_execution_mode="mock", evidence_tier=None,
                phase="draft", preparation_stage=None, outcome="none", truth_freshness=None,
                frozen_recipe_json=None, max_attempts=3, revision=1, frozen_at=None,
                created_at=now, updated_at=now,
            ))
            session.flush()
            return batch_id

        with Session(cls.owner, future=True) as s:
            s.begin()
            for ws_id in (cls.alpha, cls.beta):
                if s.get(WorkspaceRecord, ws_id) is None:
                    s.add(WorkspaceRecord(id=ws_id, name=ws_id))
            s.flush()

            alpha_versions = _make_versions(s, cls.alpha)
            beta_versions = _make_versions(s, cls.beta)
            cls.alpha_truth_package_id = _make_truth_package(s, cls.alpha, alpha_versions)
            cls.beta_truth_package_id = _make_truth_package(s, cls.beta, beta_versions)
            cls.alpha_batch_id = _make_batch(s, cls.alpha, alpha_versions)
            cls.beta_batch_id = _make_batch(s, cls.beta, beta_versions)
            s.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        from sqlalchemy import create_engine, text

        # TruthPackage is deliberately append-only even for the schema
        # owner.  A configured test superuser may disable user triggers for
        # this transaction-only fixture cleanup; otherwise keep the uniquely
        # named rows instead of weakening production enforcement or failing
        # an otherwise successful runtime-role test during teardown.
        if SUPERUSER_URL:
            cleanup_engine = create_engine(SUPERUSER_URL, future=True)
            try:
                with cleanup_engine.begin() as conn:
                    conn.execute(text("SET LOCAL session_replication_role = replica"))
                    for ws_id in (cls.alpha, cls.beta):
                        conn.execute(text(
                            "DELETE FROM localization_batches WHERE workspace_id = :w"
                        ), {"w": ws_id})
                        conn.execute(text(
                            "DELETE FROM truth_packages WHERE workspace_id = :w"
                        ), {"w": ws_id})
                        conn.execute(text(
                            "DELETE FROM library_versions WHERE workspace_id = :w"
                        ), {"w": ws_id})
                        conn.execute(text(
                            "DELETE FROM library_entries WHERE workspace_id = :w"
                        ), {"w": ws_id})
                        conn.execute(text(
                            "DELETE FROM workspaces WHERE id = :w"
                        ), {"w": ws_id})
            finally:
                cleanup_engine.dispose()
        cls.owner.dispose()

    # -- connection helpers -------------------------------------------

    def engine(self, url: str, **kwargs):
        engine = self._create_engine(url, future=True, **kwargs)
        self.addCleanup(engine.dispose)
        return engine

    def scoped_connection(self, engine, workspace: str | None):
        """A connection inside a transaction carrying the tenant setting,
        matching `tests/test_postgres_rls.py`'s helper of the same name."""
        conn = engine.connect()
        self.addCleanup(conn.close)
        transaction = conn.begin()
        self.addCleanup(lambda: transaction.rollback() if transaction.is_active else None)

        if workspace is not None:
            self.set_scope(conn, workspace)

        return conn

    def set_scope(self, connection, workspace: str, epoch: int = 0) -> None:
        from sqlalchemy import text

        from app.platform.tenancy import TenantContext, tenant_context_signature

        context = TenantContext(
            workspace_id=workspace, role="owner", initiator_id="rls-test",
            authorization_epoch=epoch,
        )
        connection.execute(
            text("SELECT set_config('marketai.workspace_id', :w, true)"), {"w": workspace},
        )
        connection.execute(
            text("SELECT set_config('marketai.authorization_epoch', :e, true)"), {"e": str(epoch)},
        )
        connection.execute(
            text("SELECT set_config('marketai.context_signature', :s, true)"),
            {"s": tenant_context_signature(context, CONTEXT_SECRET)},
        )

    # ------------------------------------------------------------- tests --

    def test_release_role_matrix_preserves_localization_runtime_privileges(self) -> None:
        """The post-migration release convergence must not revoke #24 grants."""
        from sqlalchemy import text

        from migrations.versions.u1a2b3c4d5e6_localization_truth_bounded_qc import (
            NEW_WORKSPACE_OWNED,
            PLATFORM_TABLES,
        )

        mutable = {
            "localization_batches",
            "localization_items",
            "localization_semantic_claims",
            "localization_preparation_provider_calls",
            "market_asset_bindings",
        }
        role_specific_mutable = {
            "marketai_domain_api": {"localized_copy_versions"},
            "marketai_domain_worker": {"localization_attempts"},
        }
        immutable = set(NEW_WORKSPACE_OWNED) - mutable - set().union(
            *role_specific_mutable.values()
        )

        def has_privilege(connection, role: str, table: str, privilege: str) -> bool:
            return bool(connection.scalar(text(
                "SELECT has_table_privilege(:role, :table, :privilege)"
            ), {"role": role, "table": table, "privilege": privilege}))

        with self.owner.begin() as connection:
            for table in PLATFORM_TABLES:
                with self.subTest(table=table, category="platform"):
                    self.assertTrue(has_privilege(
                        connection, "marketai_domain_api", table, "SELECT"
                    ))
                    # The global market directory is migration/admin-owned.
                    # Request handling may resolve it but cannot create a new
                    # platform market. Version/event rows retain the API
                    # command grants required by their explicit endpoints.
                    self.assertEqual(
                        has_privilege(
                            connection, "marketai_domain_api", table, "INSERT"
                        ),
                        table != "market_policies",
                    )
                    self.assertTrue(has_privilege(
                        connection, "marketai_domain_worker", table, "SELECT"
                    ))
                    self.assertTrue(has_privilege(
                        connection, "marketai_domain_readonly", table, "SELECT"
                    ))
                    self.assertFalse(has_privilege(
                        connection, "marketai_domain_worker", table, "INSERT"
                    ))
                    for role in (
                        "marketai_domain_api",
                        "marketai_domain_worker",
                        "marketai_domain_readonly",
                    ):
                        self.assertFalse(has_privilege(
                            connection, role, table, "UPDATE"
                        ))

            for table in immutable:
                with self.subTest(table=table, category="immutable"):
                    for role in ("marketai_domain_api", "marketai_domain_worker"):
                        self.assertTrue(has_privilege(
                            connection, role, table, "SELECT"
                        ))
                        self.assertTrue(has_privilege(
                            connection, role, table, "INSERT"
                        ))
                        self.assertFalse(has_privilege(
                            connection, role, table, "UPDATE"
                        ))
                    self.assertTrue(has_privilege(
                        connection, "marketai_domain_readonly", table, "SELECT"
                    ))
                    for privilege in ("INSERT", "UPDATE"):
                        self.assertFalse(has_privilege(
                            connection, "marketai_domain_readonly", table, privilege
                        ))

            for table in mutable:
                with self.subTest(table=table, category="mutable"):
                    for role in ("marketai_domain_api", "marketai_domain_worker"):
                        for privilege in ("SELECT", "INSERT", "UPDATE"):
                            self.assertTrue(has_privilege(
                                connection, role, table, privilege
                            ))
                    self.assertTrue(has_privilege(
                        connection, "marketai_domain_readonly", table, "SELECT"
                    ))
                    for privilege in ("INSERT", "UPDATE"):
                        self.assertFalse(has_privilege(
                            connection, "marketai_domain_readonly", table, privilege
                        ))

            for mutable_role, tables in role_specific_mutable.items():
                other_role = (
                    "marketai_domain_worker"
                    if mutable_role == "marketai_domain_api"
                    else "marketai_domain_api"
                )
                for table in tables:
                    for privilege in ("SELECT", "INSERT", "UPDATE"):
                        self.assertTrue(has_privilege(
                            connection, mutable_role, table, privilege
                        ))
                    for role in (other_role, "marketai_domain_readonly"):
                        self.assertTrue(has_privilege(
                            connection, role, table, "SELECT"
                        ))
                        self.assertFalse(has_privilege(
                            connection, role, table, "UPDATE"
                        ))
                    self.assertTrue(has_privilege(
                        connection, other_role, table, "INSERT"
                    ))
                    self.assertFalse(has_privilege(
                        connection,
                        "marketai_domain_readonly",
                        table,
                        "INSERT",
                    ))

            for table in ("provider_call_attempts", "provider_cost_ledger"):
                self.assertTrue(has_privilege(
                    connection, "marketai_domain_api", table, "SELECT"
                ))

            for table in (
                "provider_attempt_events",
                "provider_attempt_projections",
            ):
                for privilege in ("SELECT", "INSERT"):
                    self.assertTrue(has_privilege(
                        connection, "marketai_domain_worker", table, privilege
                    ))
            self.assertFalse(has_privilege(
                connection,
                "marketai_domain_worker",
                "provider_attempt_events",
                "UPDATE",
            ))
            self.assertTrue(has_privilege(
                connection,
                "marketai_domain_worker",
                "provider_attempt_projections",
                "UPDATE",
            ))

            # Localization re-validates Winner eligibility in the Worker
            # immediately before every provider dispatch.
            for table in (
                "experiments",
                "experiment_decisions",
                "quality_reviews",
            ):
                self.assertTrue(has_privilege(
                    connection, "marketai_domain_worker", table, "SELECT"
                ))

            for table in (*PLATFORM_TABLES, *NEW_WORKSPACE_OWNED):
                for role in (
                    "marketai_domain_api",
                    "marketai_domain_worker",
                    "marketai_domain_readonly",
                ):
                    self.assertFalse(has_privilege(
                        connection, role, table, "DELETE"
                    ))

    def test_api_can_read_but_cannot_create_global_market_policy(self) -> None:
        """Request handling resolves the catalog; only owner/admin seeds it."""
        from sqlalchemy import text
        from sqlalchemy.exc import DBAPIError, ProgrammingError

        with self.engine(API_URL).connect() as connection:
            transaction = connection.begin()
            self.assertIsNotNone(
                connection.scalar(text("SELECT count(*) FROM market_policies"))
            )
            with self.assertRaises((DBAPIError, ProgrammingError)):
                connection.execute(
                    text(
                        "INSERT INTO market_policies "
                        "(id, market_id, locale, country, currency, created_at) "
                        "VALUES (:id, :market, 'en-US', 'US', 'USD', now())"
                    ),
                    {
                        "id": f"mp_api_forbidden_{uuid.uuid4().hex}",
                        "market": f"X{uuid.uuid4().hex[:7].upper()}",
                    },
                )
            transaction.rollback()

    def test_tenant_a_cannot_read_tenant_b_truth(self) -> None:
        from sqlalchemy import text

        conn = self.scoped_connection(self.engine(API_URL), self.alpha)
        rows = [r[0] for r in conn.execute(text("SELECT id FROM truth_packages"))]

        self.assertEqual(rows, [self.alpha_truth_package_id])
        self.assertNotIn(self.beta_truth_package_id, rows)

        # And symmetrically: the exact known other-tenant row, not merely
        # an empty table, returns nothing -- guessing a real id doesn't help.
        found = conn.execute(
            text("SELECT count(*) FROM truth_packages WHERE id = :id"),
            {"id": self.beta_truth_package_id},
        ).scalar()
        self.assertEqual(found, 0)

    def test_tenant_a_cannot_read_tenant_b_batch(self) -> None:
        from sqlalchemy import text

        conn = self.scoped_connection(self.engine(API_URL), self.alpha)
        rows = [r[0] for r in conn.execute(text("SELECT id FROM localization_batches"))]

        self.assertEqual(rows, [self.alpha_batch_id])
        self.assertNotIn(self.beta_batch_id, rows)

        found = conn.execute(
            text("SELECT count(*) FROM localization_batches WHERE id = :id"),
            {"id": self.beta_batch_id},
        ).scalar()
        self.assertEqual(found, 0)

    @unittest.skipUnless(SUPERUSER_URL, SUPERUSER_SKIP_REASON)
    def test_force_rls_applies_to_superuser(self) -> None:
        """A true PostgreSQL superuser (or any role with the ``BYPASSRLS``
        attribute) bypasses row security regardless of ``FORCE ROW LEVEL
        SECURITY`` -- that is documented PostgreSQL behaviour, not a gap
        this schema can close. What ``FORCE ROW LEVEL SECURITY`` actually
        buys, and what is testable here, is that the table's *owner* --
        ``marketai_domain_owner``, which this schema deliberately grants
        ``BYPASSRLS`` as the migration-running bridge role (see
        ``deploy/postgres/domain-tenant-roles.sql``) -- would otherwise be
        exempt from every policy by ownership alone. Stripping just the
        ``BYPASSRLS`` attribute (with a real superuser connection) and
        reading as that same owner role proves ``FORCE ROW LEVEL SECURITY``
        is what stands between "owns the table" and "sees every tenant":
        with the attribute gone, an unscoped read returns nothing even
        though the connection is still, in every other respect, the owner
        of `truth_packages`."""
        from sqlalchemy import text

        superuser = self.engine(SUPERUSER_URL, poolclass=None)
        with superuser.connect() as conn:
            conn.execution_options(isolation_level="AUTOCOMMIT").execute(
                text("ALTER ROLE marketai_domain_owner NOBYPASSRLS")
            )

        def _restore() -> None:
            with superuser.connect() as conn:
                conn.execution_options(isolation_level="AUTOCOMMIT").execute(
                    text("ALTER ROLE marketai_domain_owner BYPASSRLS")
                )

        self.addCleanup(_restore)

        owner_conn = self.scoped_connection(self.engine(OWNER_URL), None)
        unscoped = owner_conn.execute(text("SELECT count(*) FROM truth_packages")).scalar()
        self.assertEqual(
            unscoped, 0,
            "marketai_domain_owner without BYPASSRLS must see nothing "
            "unscoped -- FORCE ROW LEVEL SECURITY is the only thing "
            "stopping ownership itself from being an exemption",
        )

        # Scoped to a real Workspace, the same de-privileged owner sees
        # exactly that Workspace's row -- FORCE applies the policy, it
        # does not simply block the owner outright.
        self.set_scope(owner_conn, self.alpha)
        scoped = [
            r[0] for r in owner_conn.execute(text("SELECT id FROM truth_packages"))
        ]
        self.assertEqual(scoped, [self.alpha_truth_package_id])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
