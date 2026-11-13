"""Legacy Operations are settled under the real Worker role (#22).

``tests/test_legacy_operations.py``'s fixtures, on an isolated migrated
PostgreSQL: seeded as the schema owner, the epoch changed as the owner (the
control plane's write), the Job rejected by ``marketai_claim_next_job_v2``
running as ``marketai_domain_worker``, and the reconciler run as that same
Worker role under FORCE RLS.

Every case ends by closing whatever its Workspace left open
(``marketai_health_stats_v1`` counts open Jobs and Operations in every
Workspace, and later suites share the database). Rows are not deleted:
``operation_state_events`` refuses DELETE and references them.
"""
from __future__ import annotations

import contextlib
import os
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, select, text, update
from sqlalchemy.orm import sessionmaker

from app import legacy_operations
from app.adapters.persistence.models import (
    AuditEventRecord,
    JobRecord,
    OperationRecord,
    OperationStateEventRecord,
    WorkspaceRecord,
    utcnow,
)
from app.entitlements.usage_models import UsageEventRecord
from app.platform import jobs
from app.platform.operations import OperationState

URLS = [os.getenv(key) for key in (
    "DOMAIN_OWNER_DATABASE_URL", "DOMAIN_API_DATABASE_URL", "DOMAIN_WORKER_DATABASE_URL",
)]
EPOCH_STALE = "authorization_epoch_stale"


def _fold_fixtures():
    from tests import test_legacy_operations as fold

    return (
        fold.TestScaleExportFold, fold.TestReferenceGateFold, fold.TestExportPackageFold,
        fold.TestConnectionCheckFold, fold.TestManualFactImportFold, fold.TestFinalizeUploadFold,
        fold.TestLaunchProvisioningFold, fold.TestLaunchRecoveryFold,
        fold.TestPlanningCallFold, fold.TestResolveCallFold, fold.TestAIQCCallFold,
        fold.TestImageEditFold, fold.TestHandoffFold,
        # #146: under controlled_mock the fold counts the Meta Operation as Mock.
        fold.TestControlledMockScaleExportFold, fold.TestControlledMockLaunchProvisioningFold,
        fold.TestControlledMockLaunchRecoveryFold,
    )


@unittest.skipUnless(
    all(URLS) and os.getenv("MARKETAI_TENANT_CONTEXT_SECRET"),
    "requires isolated PostgreSQL runtime roles",
)
class LegacyOperationFoldOnPostgresTests(unittest.TestCase):
    def setUp(self):
        self.owner, self.api, self.worker = [create_engine(url) for url in URLS]
        for engine in (self.owner, self.api, self.worker):
            self.addCleanup(engine.dispose)
        self.sessions = [
            sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
            for engine in (self.owner, self.api, self.worker)
        ]

    # -- roles ---------------------------------------------------------

    def _as(self, index: int):
        stack = contextlib.ExitStack()
        stack.enter_context(patch("app.adapters.persistence.database.SessionLocal", self.sessions[index]))
        stack.enter_context(patch("app.platform.jobs.engine", (self.owner, self.api, self.worker)[index]))
        return stack

    def _as_owner(self):
        return self._as(0)

    def _as_worker(self):
        return self._as(2)

    # -- the steps -----------------------------------------------------

    def _seed(self, fixture_cls):
        fixture = fixture_cls()
        with self._as_owner():
            if hasattr(fixture_cls, "setUpClass") and hasattr(fixture_cls, "_build_ready_batch"):
                fixture_cls.setUpClass()
            fixture.setUp()
            fixture.seed()
        self.addCleanup(self._retire_workspace, fixture.workspace_id())
        self.addCleanup(fixture.doCleanups)
        return fixture

    def _change_epoch(self, workspace_id: str) -> int:
        with self.owner.begin() as connection:
            return connection.execute(
                update(WorkspaceRecord).where(WorkspaceRecord.id == workspace_id)
                .values(authorization_epoch=WorkspaceRecord.authorization_epoch + 1)
                .returning(WorkspaceRecord.authorization_epoch)
            ).scalar_one()

    def _dispatch_as_worker(self):
        # The claim function rejects every stale Job before it looks for
        # work; a lane no Job is in makes it do only that.
        with self._as_worker():
            self.assertIsNone(jobs.claim_next("ci-only-legacy-fold", job_types=("ci-only.no-such-job",)))

    def _fold_as_worker(self) -> None:
        with self._as_worker():
            legacy_operations.reconcile_legacy_operations()

    def _retire_workspace(self, workspace_id: str) -> None:
        with self.owner.begin() as connection:
            connection.execute(
                update(JobRecord).where(
                    JobRecord.workspace_id == workspace_id,
                    JobRecord.state.in_(("queued", "retry_wait", "leased", "running", "uncertain")),
                ).values(state="cancelled", finished_at=utcnow(), lease_owner=None,
                         lease_expires_at=None, heartbeat_at=None)
            )
            connection.execute(
                update(OperationRecord).where(
                    OperationRecord.workspace_id == workspace_id,
                    OperationRecord.state.notin_(("succeeded", "failed", "partially_succeeded", "cancelled")),
                ).values(state="cancelled", updated_at=utcnow())
            )

    # -- reads, as the owner -------------------------------------------

    def _row(self, model, row_id):
        with self.sessions[0]() as session:
            row = session.get(model, row_id)
            session.expunge(row)
            return row

    def _transitions(self, operation_id: str) -> list[tuple[str | None, str]]:
        with self.sessions[0]() as session:
            return [
                (row.from_state, row.to_state)
                for row in session.scalars(
                    select(OperationStateEventRecord)
                    .where(OperationStateEventRecord.operation_id == operation_id)
                    .order_by(OperationStateEventRecord.created_at, OperationStateEventRecord.id)
                )
            ]

    def _resource(self, fixture):
        with self._as_owner():
            return fixture.resource()

    # -- cases ---------------------------------------------------------

    def test_an_epoch_change_settles_each_legacy_command_as_the_worker(self):
        for fixture_cls in _fold_fixtures():
            with self.subTest(command=fixture_cls.__name__):
                fixture = self._seed(fixture_cls)
                self.assertEqual(self._resource(fixture), fixture.RESOURCE_BEFORE)
                fixture.adopt_epoch(self._change_epoch(fixture.workspace_id()))
                self._dispatch_as_worker()
                job = self._row(JobRecord, fixture.job_id)
                self.assertEqual((job.state, job.error_code), ("failed", EPOCH_STALE))
                self.assertEqual(self._row(OperationRecord, fixture.operation_id).state, "queued")

                self._fold_as_worker()
                self._fold_as_worker()

                self.assertEqual(self._row(OperationRecord, fixture.operation_id).state, "failed")
                self.assertEqual(self._transitions(fixture.operation_id), [(None, "queued"), ("queued", "failed")])
                self.assertEqual(self._resource(fixture), fixture.RESOURCE_AFTER)
                with self.sessions[0]() as session:
                    audits = session.scalars(
                        select(AuditEventRecord.executor).where(
                            AuditEventRecord.event_type == "operation.legacy_reconciled",
                            AuditEventRecord.resource_id == fixture.operation_id,
                        )
                    ).all()
                self.assertEqual(audits, ["system:worker"])
                # Usage (#8 S2, #146): counted once, as the Worker, in the
                # bucket of the command's mode.
                with self.sessions[0]() as session:
                    usage = sorted(session.execute(
                        select(UsageEventRecord.metric_key, UsageEventRecord.execution_mode).where(
                            UsageEventRecord.source_resource_type == "operation",
                            UsageEventRecord.source_resource_id == fixture.operation_id,
                        )
                    ).all())
                self.assertEqual([tuple(row) for row in usage], [
                    ("operations", fixture.USAGE_MODE), ("processing_seconds", fixture.USAGE_MODE),
                ])

    def test_a_generation_run_is_settled_by_its_projection_as_the_worker(self):
        from tests import test_legacy_operations as fold

        fixture = self._seed(fold.TestGenerationRunFold)
        fixture.adopt_epoch(self._change_epoch(fixture.workspace_id()))
        self._dispatch_as_worker()
        self._fold_as_worker()
        self._fold_as_worker()
        self.assertEqual(self._resource(fixture), ("failed", ["failed"]))
        self.assertEqual(
            self._transitions(fixture.operation_id),
            [(None, "queued"), ("queued", "running"), ("running", "failed")],
        )

    def test_an_epoch_change_in_workspace_b_folds_nothing_in_workspace_a(self):
        from tests import test_legacy_operations as fold

        a = self._seed(fold.TestReferenceGateFold)
        b = self._seed(fold.TestReferenceGateFold)
        b.adopt_epoch(self._change_epoch(b.workspace_id()))
        self._dispatch_as_worker()
        self._fold_as_worker()

        self.assertEqual(self._row(JobRecord, a.job_id).state, "queued")
        self.assertEqual(self._transitions(a.operation_id), [(None, "queued")])
        self.assertEqual(self._resource(a), a.RESOURCE_BEFORE)
        self.assertEqual(self._row(OperationRecord, b.operation_id).state, "failed")

    def test_the_admitted_operation_reconciler_now_folds_as_the_worker(self):
        """Before #22 this sweep listed nothing on PostgreSQL: an aggregated
        Operation whose only Job the claim rejected stayed ``admitted``."""
        from app.adapters.persistence.database import tenant_session
        from app.platform.operations import create_operation
        from tests.support.library_fixtures import LibraryFixtures

        helper = LibraryFixtures()
        with self._as_owner():
            ctx = helper._make_context(workspace_id=f"ws_fold_v1_{os.urandom(8).hex()}")
            with tenant_session(ctx) as session:
                operation = create_operation(
                    session, command_type="products.intake", initial_state=OperationState.ADMITTED,
                    admission_contract_version=1,
                )
                job = jobs.enqueue_in_session(
                    session, context=ctx, job_type="products.intake", payload={},
                    idempotency_key=f"ci-only-v1-fold:{operation.id}",
                )
                job.operation_id = operation.id
                operation_id, job_id = operation.id, job.id
        self.addCleanup(self._retire_workspace, ctx.workspace_id)
        self._change_epoch(ctx.workspace_id)
        self._dispatch_as_worker()
        self.assertEqual(self._row(JobRecord, job_id).error_code, EPOCH_STALE)
        self.assertEqual(self._row(OperationRecord, operation_id).state, "admitted")

        with self._as_worker():
            jobs.reconcile_admitted_operations()
            jobs.reconcile_admitted_operations()

        self.assertEqual(self._transitions(operation_id), [(None, "admitted"), ("admitted", "failed")])

    def test_only_the_worker_may_list_candidates_and_only_ids_come_back(self):
        """Each function is EXECUTE-able by the Worker alone, runs as the
        owner with an empty search_path, and returns ids and the epoch."""
        functions = {
            "marketai_legacy_operation_candidates_v1(text[], integer)":
                "SELECT * FROM public.marketai_legacy_operation_candidates_v1(ARRAY['scale_export'], 1)",
            "marketai_admitted_operation_candidates_v1(integer)":
                "SELECT * FROM public.marketai_admitted_operation_candidates_v1(1)",
        }
        with self.owner.connect() as connection:
            for signature in functions:
                with self.subTest(signature=signature):
                    definer, config, volatility, columns = connection.execute(text(
                        "SELECT p.prosecdef, p.proconfig, p.provolatile, "
                        "       pg_get_function_result(p.oid) "
                        "  FROM pg_proc p WHERE p.oid = CAST(:signature AS regprocedure)"
                    ), {"signature": f"public.{signature}"}).one()
                    self.assertTrue(definer)
                    self.assertEqual(config, ['search_path=""'])
                    self.assertEqual(volatility, "s", "STABLE: it reads, never writes")
                    self.assertRegex(
                        columns,
                        r"^TABLE\(workspace_id character varying, authorization_epoch integer, "
                        r"(job_id|operation_id) character varying\)$",
                    )
                    for role in ("marketai_domain_api", "marketai_domain_readonly", "PUBLIC"):
                        allowed = connection.scalar(text(
                            "SELECT has_function_privilege(CAST(:role AS text), "
                            "CAST(:signature AS regprocedure), 'EXECUTE')"
                            if role != "PUBLIC" else
                            "SELECT bool_or(acl.privilege_type = 'EXECUTE') "
                            "  FROM pg_proc p, aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) acl "
                            " WHERE p.oid = CAST(:signature AS regprocedure) AND acl.grantee = 0"
                        ), {"role": role, "signature": f"public.{signature}"})
                        self.assertFalse(allowed, f"{role} may not execute {signature}")
                    self.assertTrue(connection.scalar(text(
                        "SELECT has_function_privilege('marketai_domain_worker', "
                        "CAST(:signature AS regprocedure), 'EXECUTE')"
                    ), {"signature": f"public.{signature}"}))
        engines = [self.api]
        if os.getenv("DOMAIN_READONLY_DATABASE_URL"):
            engines.append(create_engine(os.environ["DOMAIN_READONLY_DATABASE_URL"]))
            self.addCleanup(engines[-1].dispose)
        for engine in engines:
            for query in functions.values():
                with self.subTest(role=engine.url.username, query=query):
                    with engine.connect() as connection, self.assertRaisesRegex(Exception, "permission denied"):
                        connection.execute(text(query)).all()

    def test_a_forged_payload_naming_another_workspaces_operation_settles_nothing(self):
        """Workspace A's rejected Job names B's Operation: under FORCE RLS
        the Worker, bound to A, cannot see it, and B is untouched."""
        from tests import test_legacy_operations as fold

        a = self._seed(fold.TestConnectionCheckFold)
        b = self._seed(fold.TestConnectionCheckFold)
        with self.owner.begin() as connection:
            connection.execute(
                update(JobRecord).where(JobRecord.id == a.job_id)
                .values(payload_json={"operation_id": b.operation_id, "data_source_entry_id": "ds_forged"})
            )
        a.adopt_epoch(self._change_epoch(a.workspace_id()))
        self._dispatch_as_worker()
        self._fold_as_worker()

        self.assertEqual(self._transitions(b.operation_id), [(None, "queued")])
        self.assertEqual(self._transitions(a.operation_id), [(None, "queued")], "its correlation is gone")
        with self.owner.connect() as connection:
            self.assertEqual(
                connection.scalar(text(
                    "SELECT count(*) FROM audit_events WHERE event_type = 'operation.legacy_reconciled' "
                    "AND workspace_id IN (:a, :b)"
                ), {"a": a.workspace_id(), "b": b.workspace_id()}),
                0,
            )


if __name__ == "__main__":
    unittest.main()
