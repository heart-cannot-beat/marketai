"""The Beta default ScalePolicyVersion on a released PostgreSQL cluster (#127).

``make db-release`` ran the roles script, Alembic and the roles script again;
nothing else wrote ``scale_policy_versions``:

* the row the migration published equals the constant column for column;
* the API and the Worker, under their own roles, select it for every mode it
  permits and nothing for ``controlled_mock``;
* the API, Worker and read-only roles hold exactly the privileges on
  ``scale_policy_versions`` that ``deploy/postgres/domain-tenant-roles.sql``
  grants (the migration grants nothing);
* running the migration again adds no row; downgrade keeps a version a run
  names, and fails rather than trust an RLS-filtered answer when the role
  running it does not bypass RLS.

``tests/test_scale_live_evaluation_pg.py`` evaluates under this Policy end
to end. Run with ``DOMAIN_OWNER_DATABASE_URL``, ``DOMAIN_API_DATABASE_URL``,
``DOMAIN_WORKER_DATABASE_URL`` and ``DOMAIN_READONLY_DATABASE_URL``.
Fictional brand only: Acme Charge.
"""
from __future__ import annotations

import importlib
import os
import unittest
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.adapters.persistence.models import WorkspaceRecord
from app.platform.runtime_role import API_ROLE, WORKER_ROLE
from app.scale.live_trigger import eligible_live_policy
from app.scale.models import ScaleEvaluationRunRecord, ScalePolicyVersionRecord
from app.scale.platform_policy import BETA_DEFAULT_POLICY_V1
from tests.support.grant_matrix import load_grant_matrix

URLS = [os.getenv(key) for key in (
    "DOMAIN_OWNER_DATABASE_URL", "DOMAIN_API_DATABASE_URL", "DOMAIN_WORKER_DATABASE_URL",
    "DOMAIN_READONLY_DATABASE_URL",
)]
OWNER, API, WORKER, READONLY = range(4)
READONLY_ROLE = "marketai_domain_readonly"
TABLE = "scale_policy_versions"
MIGRATION = importlib.import_module("migrations.versions.zzg7d8e9f0a1_publish_beta_default_scale_policy")
MODES = ("customer_sandbox", "internal_validation", "controlled_mock")
PERMITTED = {"customer_sandbox", "internal_validation"}  # #127 P1-B


@unittest.skipUnless(all(URLS), "requires isolated PostgreSQL runtime roles")
class PublishedBetaDefaultPolicyPostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engines = [create_engine(url) for url in URLS]
        for engine in self.engines:
            self.addCleanup(engine.dispose)

    def _count(self) -> int:
        with self.engines[OWNER].connect() as connection:
            return connection.execute(select(func.count()).select_from(ScalePolicyVersionRecord.__table__)
                                      .where(ScalePolicyVersionRecord.id == BETA_DEFAULT_POLICY_V1.id)).scalar_one()

    def test_the_released_row_is_the_constant_column_for_column(self):
        with self.engines[OWNER].connect() as connection:
            [row] = connection.execute(select(ScalePolicyVersionRecord.__table__)
                                       .where(ScalePolicyVersionRecord.id == BETA_DEFAULT_POLICY_V1.id)).all()
        self.assertEqual(dict(row._mapping), BETA_DEFAULT_POLICY_V1.row())

    def test_the_api_and_the_worker_select_it_for_each_permitted_mode_only(self):
        for name, index in (("API", API), ("Worker", WORKER)):
            with self.subTest(name), Session(self.engines[index]) as session:
                selected = {mode: eligible_live_policy(session, mode) for mode in MODES}
                self.assertEqual({mode: policy.id for mode, policy in selected.items() if policy is not None},
                                 {mode: BETA_DEFAULT_POLICY_V1.id for mode in PERMITTED})

    def test_runtime_grants_on_the_policy_table_are_the_role_files(self):
        matrix = load_grant_matrix()
        with self.engines[OWNER].connect() as connection:
            for role in (API_ROLE, WORKER_ROLE, READONLY_ROLE):
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                    with self.subTest(role=role, privilege=privilege):
                        held = connection.execute(text("SELECT has_table_privilege(:role, :table, :privilege)"),
                                                  {"role": role, "table": TABLE, "privilege": privilege}).scalar_one()
                        self.assertEqual(held, matrix.allows(role, TABLE, privilege))
        # What the role file grants today: the API publishes and updates
        # versions, the Worker and read-only roles only read them, nobody deletes.
        self.assertEqual([matrix.allows(API_ROLE, TABLE, p) for p in ("SELECT", "INSERT", "UPDATE", "DELETE")],
                         [True, True, True, False])
        self.assertEqual([matrix.allows(WORKER_ROLE, TABLE, p) for p in ("SELECT", "INSERT", "UPDATE", "DELETE")],
                         [True, False, False, False])

    def test_running_the_migration_again_adds_no_row(self):
        self.assertEqual(self._count(), 1)
        with self.engines[OWNER].begin() as connection, Operations.context(MigrationContext.configure(connection)):
            MIGRATION.upgrade()
            MIGRATION.upgrade()
        self.assertEqual(self._count(), 1)

    def test_downgrade_keeps_a_version_a_run_names(self):
        workspace_id = f"ws_policy_pg_{uuid.uuid4().hex[:10]}"
        with self.engines[OWNER].connect() as connection:
            transaction = connection.begin()
            try:
                session = Session(bind=connection)
                session.add(WorkspaceRecord(id=workspace_id, name=f"Acme Charge {workspace_id[-4:]}"))
                session.flush()
                from tests.support.scale_evaluation import seed_evaluable_run

                seeded = seed_evaluable_run(session, workspace_id)
                session.execute(update(ScaleEvaluationRunRecord)
                                .where(ScaleEvaluationRunRecord.id == seeded.run_id)
                                .values(policy_version_id=BETA_DEFAULT_POLICY_V1.id))
                session.flush()
                with Operations.context(MigrationContext.configure(connection)):
                    MIGRATION.downgrade()
                kept = connection.execute(select(ScalePolicyVersionRecord.is_published)
                                          .where(ScalePolicyVersionRecord.id == BETA_DEFAULT_POLICY_V1.id)).scalar()
                self.assertTrue(kept, "a run names it: kept, still published")
            finally:
                transaction.rollback()  # the seeded rows and the downgrade, together
        self.assertEqual(self._count(), 1)

    def test_downgrade_refuses_a_row_level_security_filtered_answer(self):
        with self.engines[WORKER].connect() as connection:
            with self.assertRaises(DBAPIError) as raised, connection.begin(), \
                    Operations.context(MigrationContext.configure(connection)):
                MIGRATION.downgrade()
        self.assertIn("row-level security", str(raised.exception))
        self.assertEqual(self._count(), 1)


if __name__ == "__main__":
    unittest.main()
