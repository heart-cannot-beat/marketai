"""PostgreSQL evidence for the API/Worker runtime cut-over."""
from __future__ import annotations

import os
import unittest
import uuid
from contextlib import contextmanager


OWNER_URL = os.environ.get("DOMAIN_OWNER_DATABASE_URL")
API_URL = os.environ.get("DOMAIN_API_DATABASE_URL")
WORKER_URL = os.environ.get("DOMAIN_WORKER_DATABASE_URL")
CONTEXT_SECRET = os.environ.get("MARKETAI_TENANT_CONTEXT_SECRET")
CONFIGURED = all((OWNER_URL, API_URL, WORKER_URL, CONTEXT_SECRET))


@unittest.skipUnless(
    CONFIGURED,
    "set DOMAIN_OWNER_DATABASE_URL, DOMAIN_API_DATABASE_URL and "
    "DOMAIN_WORKER_DATABASE_URL against a migrated domain database",
)
class PostgresRuntimeWiringTests(unittest.TestCase):
    def test_signed_initial_snapshot_bootstraps_empty_workspace_once(self):
        import asyncio
        import hashlib
        import hmac
        import json
        from concurrent.futures import ThreadPoolExecutor
        from datetime import datetime, timedelta, timezone
        from sqlalchemy.orm import sessionmaker
        from app.entitlements.projection import EntitlementProjectionModule

        workspace = "ws_initial_" + uuid.uuid4().hex
        snapshot = "snapshot_" + uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        key = b"isolated-provisioning-regression-only"
        header = {"schema_version": 2, "signature_key_id": "test", "signature_algorithm": "hmac-sha256"}
        payload = {"snapshot_id": snapshot, "workspace_id": workspace, "workspace_name": "Empty workspace",
            "authorization_epoch": 0, "entitlement_version": 1, "lease_sequence": 1, "projection_version": 1,
            "lifecycle": "provisioning", "suspended": False,
            "entitlement_snapshot": {"plan_version_id": "isolated-test-plan", "source_refs": [],
                "period_start": now.isoformat(), "period_end": (now + timedelta(days=30)).isoformat(),
                "period_anchor": now.isoformat(), "resolved_entitlements": {"controlled_mock": True},
                "restriction_summary": [], "semantic_hash": "initial-test", "issued_at": now.isoformat(),
                "valid_until": (now + timedelta(hours=1)).isoformat(), "audit_event_id": "isolated-test"}}
        content = {"protected_header": header, "payload": payload}
        signature = hmac.new(key, json.dumps(content, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()
        raw = json.dumps({**content, "signature": signature}).encode()
        module = EntitlementProjectionModule(sessionmaker(bind=self.api), {"test": key})
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: asyncio.run(module.ingest(raw, delivery_kind="push")).value, range(2)))
        self.assertEqual(sorted(results), ["applied", "duplicate"])
        with self.owner.connect() as connection:
            self.assertEqual(connection.scalar(self.text("SELECT lifecycle FROM workspaces WHERE id=:w"), {"w": workspace}), "provisioning")
            self.assertEqual(connection.scalar(self.text("SELECT count(*) FROM entitlement_snapshot_inbox WHERE workspace_id=:w"), {"w": workspace}), 1)
            # The contract's Beta guardrail (#126), not the 0 limits that refused every paid call.
            self.assertEqual(connection.scalar(self.text("SELECT per_operation_limit FROM spend_guardrails WHERE workspace_id=:w"), {"w": workspace}), 5)
            self.assertEqual(connection.scalar(self.text("SELECT count(*) FROM products WHERE workspace_id=:w"), {"w": workspace}), 0)

    @classmethod
    def setUpClass(cls) -> None:
        from sqlalchemy import create_engine, text

        cls.text = staticmethod(text)
        cls.workspace = f"ws_dispatch_{uuid.uuid4().hex[:8]}"
        cls.job_id = f"job_{uuid.uuid4().hex}"
        cls.owner = create_engine(OWNER_URL, future=True)
        cls.api = create_engine(API_URL, future=True)
        cls.worker = create_engine(WORKER_URL, future=True)
        with cls.owner.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO workspaces "
                    "(id, name, default_currency, authorization_epoch, lifecycle, budget_currency, created_at) "
                    "VALUES (:id, :id, 'USD', 17, 'active', 'USD', now())"
                ),
                {"id": cls.workspace},
            )

    @classmethod
    def tearDownClass(cls) -> None:
        with cls.owner.begin() as connection:
            connection.execute(
                cls.text(
                    "ALTER TABLE audit_events DISABLE TRIGGER audit_events_reject_mutation"
                )
            )
            connection.execute(
                cls.text("DELETE FROM audit_events WHERE workspace_id = :workspace"),
                {"workspace": cls.workspace},
            )
            connection.execute(
                cls.text(
                    "ALTER TABLE audit_events ENABLE TRIGGER audit_events_reject_mutation"
                )
            )
            connection.execute(
                cls.text("DELETE FROM jobs WHERE workspace_id = :workspace"),
                {"workspace": cls.workspace},
            )
            connection.execute(
                cls.text("DELETE FROM workspaces WHERE id = :workspace"),
                {"workspace": cls.workspace},
            )
        cls.worker.dispose()
        cls.api.dispose()
        cls.owner.dispose()

    @staticmethod
    def _scope(connection, workspace: str, epoch: int) -> None:
        from app.platform.tenancy import TenantContext, tenant_context_signature

        context = TenantContext(
            workspace_id=workspace,
            role="owner",
            initiator_id="runtime-test",
            authorization_epoch=epoch,
        )
        connection.execute(
            PostgresRuntimeWiringTests.text(
                "SELECT set_config('marketai.workspace_id', :workspace, true)"
            ),
            {"workspace": workspace},
        )
        connection.execute(
            PostgresRuntimeWiringTests.text(
                "SELECT set_config('marketai.authorization_epoch', :epoch, true)"
            ),
            {"epoch": str(epoch)},
        )
        connection.execute(
            PostgresRuntimeWiringTests.text(
                "SELECT set_config('marketai.context_signature', :signature, true)"
            ),
            {"signature": tenant_context_signature(context, CONTEXT_SECRET)},
        )

    def test_projection_repair_can_create_a_missing_workspace(self) -> None:
        workspace = f"ws_projection_{uuid.uuid4().hex[:8]}"
        try:
            with self.api.begin() as connection:
                applied = connection.scalar(
                    self.text(
                        "SELECT marketai_apply_workspace_projection("
                        ":id, :name, 3, 5, false, 7, :currency)"
                    ),
                    {"id": workspace, "name": "Projection repair", "currency": "USD"},
                )
            self.assertTrue(applied)

            with self.owner.connect() as connection:
                row = connection.execute(
                    self.text(
                        "SELECT name, default_currency, lifecycle, budget_currency, "
                        "authorization_epoch, entitlement_version, projection_version "
                        "FROM workspaces WHERE id = :id"
                    ),
                    {"id": workspace},
                ).one()
            self.assertEqual(
                tuple(row),
                ("Projection repair", "USD", "active", "USD", 3, 5, 7),
            )

            with self.api.begin() as connection:
                applied = connection.scalar(
                    self.text(
                        "SELECT marketai_apply_workspace_projection("
                        ":id, :name, 4, 6, true, 8, :currency)"
                    ),
                    {"id": workspace, "name": "Projection updated", "currency": "EUR"},
                )
            self.assertTrue(applied)

            with self.owner.connect() as connection:
                updated = connection.execute(
                    self.text(
                        "SELECT name, default_currency, lifecycle, budget_currency "
                        "FROM workspaces WHERE id = :id"
                    ),
                    {"id": workspace},
                ).one()
            self.assertEqual(
                tuple(updated),
                ("Projection updated", "EUR", "suspended", "EUR"),
            )
        finally:
            with self.owner.begin() as connection:
                connection.execute(
                    self.text("DELETE FROM workspaces WHERE id = :id"),
                    {"id": workspace},
                )

    def test_worker_claim_restores_the_frozen_request_identity(self) -> None:
        with self.api.begin() as connection:
            self._scope(connection, self.workspace, 17)
            connection.execute(
                self.text(
                    "INSERT INTO jobs (id, workspace_id, initiator_id, initiator_role,"
                    " request_id, trace_id, authorization_epoch, type, resource_type,"
                    " resource_id, payload_version, payload_json, idempotency_key, state,"
                    " safe_to_retry_after_crash, attempt, max_attempts, error_code,"
                    " error_detail, created_at) VALUES (:id, :workspace, 'user_17',"
                    " 'member', 'req_17', 'trace_17', 17, 'test_runtime_context', '', '',"
                    " 1, '{}', :key, 'queued', true, 0, 3, '', '', now())"
                ),
                {
                    "id": self.job_id,
                    "workspace": self.workspace,
                    "key": f"runtime-context:{self.job_id}",
                },
            )

        with self.worker.begin() as connection:
            claimed = connection.execute(
                self.text("SELECT * FROM marketai_claim_next_job_v2('runtime-test', 60)")
            ).mappings().one()

        self.assertEqual(claimed["job_id"], self.job_id)
        self.assertEqual(claimed["workspace_id"], self.workspace)
        self.assertEqual(claimed["initiator_id"], "user_17")
        self.assertEqual(claimed["initiator_role"], "member")
        self.assertEqual(claimed["request_id"], "req_17")
        self.assertEqual(claimed["trace_id"], "trace_17")
        self.assertEqual(claimed["authorization_epoch"], 17)
        with self.owner.connect() as connection:
            leased = connection.execute(
                self.text(
                    "SELECT initiator_id, initiator_role, executor FROM audit_events "
                    "WHERE event_type = 'job.leased' AND resource_id = :id"
                ),
                {"id": self.job_id},
            ).one()
        self.assertEqual(tuple(leased), ("user_17", "member", "system:worker"))

        with self.worker.begin() as connection:
            self._scope(connection, self.workspace, 17)
            changed = connection.execute(
                self.text(
                    "UPDATE jobs SET state = 'running' WHERE id = :id AND lease_owner = 'runtime-test'"
                ),
                {"id": self.job_id},
            )
            self.assertEqual(changed.rowcount, 1)

    def test_expired_launch_job_marks_queued_operation_uncertain_atomically(self) -> None:
        operation_id = f"op_launch_crash_{uuid.uuid4().hex}"
        job_id = f"job_launch_crash_{uuid.uuid4().hex}"
        with self.owner.begin() as connection:
            connection.execute(
                self.text(
                    "INSERT INTO operation_records "
                    "(id, workspace_id, initiator_id, command_type, state, "
                    "created_at, updated_at) VALUES "
                    "(:id, :workspace, 'launch-user', 'launch.execute', "
                    "'queued', now(), now())"
                ),
                {"id": operation_id, "workspace": self.workspace},
            )
            connection.execute(
                self.text(
                    "INSERT INTO jobs "
                    "(id, workspace_id, initiator_id, initiator_role, request_id, "
                    "trace_id, authorization_epoch, type, resource_type, resource_id, "
                    "payload_version, payload_json, idempotency_key, state, "
                    "safe_to_retry_after_crash, attempt, max_attempts, error_code, "
                    "error_detail, lease_owner, lease_expires_at, operation_id, created_at) "
                    "VALUES (:id, :workspace, 'launch-user', 'owner', 'req-launch', "
                    "'trace-launch', 17, 'launch_provisioning', 'launch_provisioning', "
                    ":operation, 1, json_build_object("
                    "'operation_id', CAST(:operation_payload AS varchar(64))), "
                    ":key, 'running', false, 1, 3, '', '', 'dead-worker', "
                    "now() - interval '1 minute', :operation, now())"
                ),
                {
                    "id": job_id,
                    "workspace": self.workspace,
                    "operation": operation_id,
                    "operation_payload": operation_id,
                    "key": f"launch-crash:{job_id}",
                },
            )

        try:
            with self.worker.begin() as connection:
                connection.execute(
                    self.text(
                        "SELECT * FROM marketai_claim_next_job_v2('launch-reaper', 60)"
                    )
                ).mappings().first()

            with self.owner.connect() as connection:
                job = connection.execute(
                    self.text(
                        "SELECT state, error_code, lease_owner, lease_expires_at "
                        "FROM jobs WHERE id = :id"
                    ),
                    {"id": job_id},
                ).one()
                operation_state = connection.scalar(
                    self.text("SELECT state FROM operation_records WHERE id = :id"),
                    {"id": operation_id},
                )
                events = connection.execute(
                    self.text(
                        "SELECT from_state, to_state FROM operation_state_events "
                        "WHERE operation_id = :id ORDER BY created_at, id"
                    ),
                    {"id": operation_id},
                ).all()
                audit_count = connection.scalar(
                    self.text(
                        "SELECT count(*) FROM audit_events "
                        "WHERE event_type = 'job.uncertain' AND resource_id = :id"
                    ),
                    {"id": job_id},
                )
            self.assertEqual(tuple(job), ("uncertain", "paid_call_outcome_unknown", None, None))
            self.assertEqual(operation_state, "uncertain")
            # Both transitions share the dispatcher's one ``checked_at``
            # timestamp and their append-only IDs are hashes, so PostgreSQL
            # promises no causal order for ``ORDER BY created_at, id``. The
            # complete transition set plus the final state is the invariant.
            self.assertCountEqual(
                [tuple(event) for event in events],
                [("queued", "running"), ("running", "uncertain")],
            )
            self.assertEqual(audit_count, 1)
        finally:
            with self.owner.begin() as connection:
                connection.execute(
                    self.text(
                        "ALTER TABLE audit_events DISABLE TRIGGER audit_events_reject_mutation"
                    )
                )
                connection.execute(
                    self.text("DELETE FROM audit_events WHERE resource_id = :id"),
                    {"id": job_id},
                )
                connection.execute(
                    self.text(
                        "ALTER TABLE audit_events ENABLE TRIGGER audit_events_reject_mutation"
                    )
                )
                connection.execute(
                    self.text("DELETE FROM jobs WHERE id = :id"), {"id": job_id}
                )
                connection.execute(
                    self.text(
                        "ALTER TABLE operation_state_events DISABLE TRIGGER "
                        "operation_state_events_reject_mutation"
                    )
                )
                connection.execute(
                    self.text("DELETE FROM operation_records WHERE id = :id"),
                    {"id": operation_id},
                )
                connection.execute(
                    self.text(
                        "ALTER TABLE operation_state_events ENABLE TRIGGER "
                        "operation_state_events_reject_mutation"
                    )
                )

    def test_worker_dispatch_rejects_a_stale_authorization_epoch(self) -> None:
        stale_job = f"job_stale_{uuid.uuid4().hex}"
        with self.api.begin() as connection:
            self._scope(connection, self.workspace, 17)
            connection.execute(
                self.text(
                    "INSERT INTO jobs (id, workspace_id, initiator_id, initiator_role,"
                    " request_id, trace_id, authorization_epoch, type, resource_type,"
                    " resource_id, payload_version, payload_json, idempotency_key, state,"
                    " safe_to_retry_after_crash, attempt, max_attempts, error_code,"
                    " error_detail, created_at) VALUES (:id, :workspace, 'revoked_user',"
                    " 'member', 'req_stale', 'trace_stale', 17, 'test_stale_context', '', '',"
                    " 1, '{}', :key, 'queued', true, 0, 3, '', '', now())"
                ),
                {
                    "id": stale_job,
                    "workspace": self.workspace,
                    "key": f"stale-context:{stale_job}",
                },
            )

        try:
            with self.owner.begin() as connection:
                connection.execute(
                    self.text(
                        "UPDATE workspaces SET authorization_epoch = 18 WHERE id = :workspace"
                    ),
                    {"workspace": self.workspace},
                )
            with self.worker.begin() as connection:
                claimed = connection.execute(
                    self.text("SELECT * FROM marketai_claim_next_job_v2('stale-test', 60)")
                ).mappings().first()
            self.assertIsNone(claimed)
            with self.owner.connect() as connection:
                state, code = connection.execute(
                    self.text("SELECT state, error_code FROM jobs WHERE id = :id"),
                    {"id": stale_job},
                ).one()
            self.assertEqual(state, "failed")
            self.assertEqual(code, "authorization_epoch_stale")
            with self.owner.connect() as connection:
                rejected = connection.execute(
                    self.text(
                        "SELECT initiator_role, executor FROM audit_events "
                        "WHERE event_type = 'job.authorization_rejected' "
                        "AND resource_id = :id"
                    ),
                    {"id": stale_job},
                ).one()
            self.assertEqual(tuple(rejected), ("member", "system:worker"))
        finally:
            with self.owner.begin() as connection:
                connection.execute(
                    self.text(
                        "UPDATE workspaces SET authorization_epoch = 17 WHERE id = :workspace"
                    ),
                    {"workspace": self.workspace},
                )

    def test_expired_job_cannot_recover_from_another_workspace_asset(self) -> None:
        other = f"ws_recovery_{uuid.uuid4().hex[:8]}"
        product = f"prod_recovery_{uuid.uuid4().hex[:8]}"
        plan = f"plan_recovery_{uuid.uuid4().hex[:8]}"
        run = f"run_recovery_{uuid.uuid4().hex[:8]}"
        asset = f"asset_recovery_{uuid.uuid4().hex[:8]}"
        job = f"job_recovery_{uuid.uuid4().hex}"
        with self.owner.begin() as connection:
            connection.execute(
                self.text(
                    "INSERT INTO workspaces "
                    "(id, name, default_currency, authorization_epoch, lifecycle, budget_currency, created_at) "
                    "VALUES (:id, :id, 'USD', 0, 'active', 'USD', now())"
                ),
                {"id": other},
            )
            connection.execute(
                self.text(
                    "INSERT INTO products (id, workspace_id, name, description, selling_points,"
                    " audience, brand_notes, ref_images, economics, revision, created_at, updated_at)"
                    " VALUES (:id, :workspace, 'Other', '', '[]', '', '', '[]', '{}', 1, now(), now())"
                ),
                {"id": product, "workspace": other},
            )
            connection.execute(
                self.text(
                    "INSERT INTO plans (id, workspace_id, product_id, mode, cap, total_full,"
                    " snapshot_hash, created_at) VALUES (:id, :workspace, :product, 'full',"
                    " 0, 1, :hash, now())"
                ),
                {"id": plan, "workspace": other, "product": product, "hash": "e" * 64},
            )
            connection.execute(
                self.text(
                    "INSERT INTO generation_runs (id, workspace_id, product_id, plan_id, provider,"
                    " model, aspect_ratio, image_size, variants, use_ref, status, total, done,"
                    " failed, est_cost, currency, revision, created_at, updated_at) VALUES"
                    " (:id, :workspace, :product, :plan, 'mock', 'mock', '1:1', '1K', 1,"
                    " false, 'done', 1, 1, 0, 0, 'USD', 1, now(), now())"
                ),
                {"id": run, "workspace": other, "product": product, "plan": plan},
            )
            connection.execute(
                self.text(
                    "INSERT INTO assets (id, workspace_id, product_id, run_id, combo_id, combo_code,"
                    " tags_json, filename, provider, model, prompt, fingerprint, status, error_code,"
                    " error_detail, created_at, updated_at) VALUES (:id, :workspace, :product, :run,"
                    " 'C01', 'A1', '{}', 'other.png', 'mock', 'mock', '', :fingerprint, 'ok', '', '',"
                    " now(), now())"
                ),
                {
                    "id": asset,
                    "workspace": other,
                    "product": product,
                    "run": run,
                    "fingerprint": "f" * 64,
                },
            )

        try:
            with self.api.begin() as connection:
                self._scope(connection, self.workspace, 17)
                connection.execute(
                    self.text(
                        "INSERT INTO jobs (id, workspace_id, initiator_id, initiator_role, request_id,"
                        " trace_id, authorization_epoch, type, resource_type, resource_id, payload_version,"
                        " payload_json, idempotency_key, state, safe_to_retry_after_crash, attempt,"
                        " max_attempts, lease_owner, lease_expires_at, error_code, error_detail, created_at)"
                        " VALUES (:id, :workspace, 'user_17', 'owner', '', '', 17, 'generate_asset', '', '',"
                        " 1, json_build_object('asset_id', CAST(:asset AS text)), :key, 'running', false, 1, 3,"
                        " 'crashed', now() - interval '1 second', '', '', now())"
                    ),
                    {
                        "id": job,
                        "workspace": self.workspace,
                        "asset": asset,
                        "key": f"cross-tenant-recovery:{job}",
                    },
                )

            with self.worker.begin() as connection:
                connection.execute(
                    self.text("SELECT * FROM marketai_claim_next_job_v2('recovery-test', 60)")
                ).mappings().first()

            with self.owner.connect() as connection:
                state, code = connection.execute(
                    self.text("SELECT state, error_code FROM jobs WHERE id = :id"),
                    {"id": job},
                ).one()
            self.assertEqual((state, code), ("uncertain", "paid_call_outcome_unknown"))
        finally:
            with self.owner.begin() as connection:
                connection.execute(self.text("DELETE FROM assets WHERE id = :id"), {"id": asset})
                connection.execute(self.text("DELETE FROM generation_runs WHERE id = :id"), {"id": run})
                connection.execute(self.text("DELETE FROM plans WHERE id = :id"), {"id": plan})
                connection.execute(self.text("DELETE FROM products WHERE id = :id"), {"id": product})
                connection.execute(self.text("DELETE FROM workspaces WHERE id = :id"), {"id": other})

    def test_repository_and_worker_flow_use_their_real_roles(self) -> None:
        import asyncio
        import shutil

        from sqlalchemy.orm import sessionmaker

        from app import store
        from app.adapters.persistence import database
        from app.pipeline import manager
        from app.platform import jobs
        from app.platform.tenancy import TenantContext
        from app.schemas import (
            AngleSchema,
            Combo,
            Dimension,
            DimensionValue,
            GenRequest,
            Plan,
            Product,
        )
        from app.worker_runtime import WorkerRuntime

        @contextmanager
        def runtime_database(target):
            previous_engine = database.engine
            previous_sessions = database.SessionLocal
            previous_jobs_engine = jobs.engine
            database.engine = target
            database.SessionLocal = sessionmaker(
                bind=target, expire_on_commit=False, autoflush=False, future=True
            )
            jobs.engine = target
            try:
                yield
            finally:
                jobs.engine = previous_jobs_engine
                database.SessionLocal = previous_sessions
                database.engine = previous_engine

        suffix = uuid.uuid4().hex[:10]
        product_id = f"prod_runtime_{suffix}"
        context = TenantContext(
            workspace_id=self.workspace,
            role="member",
            initiator_id="user_runtime",
            request_id=f"req_{suffix}",
            trace_id=f"trace_{suffix}",
            authorization_epoch=17,
        )
        product = Product(id=product_id, name="Runtime role flow")
        dimension = Dimension(
            id=f"dim_{suffix}",
            label="Hook",
            values=[
                DimensionValue(
                    id=f"value_{suffix}",
                    label="Role cut-over",
                    prompt_fragment="runtime role cut-over",
                )
            ],
        )
        angles = AngleSchema(product_id=product_id, dimensions=[dimension])
        plan = Plan(
            id=f"plan_{suffix}",
            product_id=product_id,
            mode="full",
            cap=0,
            total_full=1,
            combos=[
                Combo(
                    id="C01",
                    code="A1",
                    values={dimension.id: dimension.values[0].id},
                    tags={dimension.label: dimension.values[0].label},
                )
            ],
        )
        self.addCleanup(shutil.rmtree, store.product_dir(product_id), True)

        with runtime_database(self.api):
            store.save_product(context, product)
            store.save_angles(context, angles)
            store.save_plan(context, plan)
            run = manager.start(
                context,
                product,
                angles,
                plan,
                GenRequest(
                    product_id=product_id,
                    provider="mock",
                    variants=1,
                    use_ref=False,
                ),
            )

        with runtime_database(self.worker):
            self.assertTrue(asyncio.run(WorkerRuntime(owner=f"runtime-{suffix}").run_once()))

        with runtime_database(self.api):
            persisted = store.get_run(context, product_id, run.id)
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.status, "done")
        self.assertEqual(persisted.done, 1)

        with self.owner.connect() as connection:
            created = connection.execute(
                self.text(
                    "SELECT initiator_id, initiator_role, executor, request_id, trace_id "
                    "FROM audit_events WHERE event_type = 'generation_run.created' "
                    "AND resource_id = :run_id"
                ),
                {"run_id": run.id},
            ).one()
            completed = connection.execute(
                self.text(
                    "SELECT initiator_id, initiator_role, executor, request_id, trace_id "
                    "FROM audit_events WHERE event_type = 'asset.completed' "
                    "AND resource_id = :asset_id"
                ),
                {"asset_id": run.assets[0].id},
            ).one()
        self.assertEqual(
            tuple(created),
            ("user_runtime", "member", "user", context.request_id, context.trace_id),
        )
        self.assertEqual(
            tuple(completed),
            (
                "user_runtime",
                "member",
                "system:worker",
                context.request_id,
                context.trace_id,
            ),
        )

    def test_localization_slow_provider_heartbeats_against_competing_claim(
        self,
    ) -> None:
        """A slow localization call keeps its lease and is never dispatched twice."""
        import asyncio
        from unittest.mock import patch

        from sqlalchemy.orm import sessionmaker

        from app import config
        from app.adapters.persistence import database
        from app.platform import jobs
        from app.worker_runtime import WorkerRuntime

        @contextmanager
        def runtime_database(target):
            previous_engine = database.engine
            previous_sessions = database.SessionLocal
            previous_jobs_engine = jobs.engine
            database.engine = target
            database.SessionLocal = sessionmaker(
                bind=target, expire_on_commit=False, autoflush=False, future=True
            )
            jobs.engine = target
            try:
                yield
            finally:
                jobs.engine = previous_jobs_engine
                database.SessionLocal = previous_sessions
                database.engine = previous_engine

        suffix = uuid.uuid4().hex
        job_id = f"job_localization_heartbeat_{suffix}"
        worker_a = f"localization-heartbeat-a-{suffix}"
        worker_b = f"localization-heartbeat-b-{suffix}"
        with self.api.begin() as connection:
            self._scope(connection, self.workspace, 17)
            connection.execute(
                self.text(
                    "INSERT INTO jobs (id, workspace_id, initiator_id, initiator_role, "
                    "request_id, trace_id, authorization_epoch, type, resource_type, "
                    "resource_id, payload_version, payload_json, idempotency_key, state, "
                    "safe_to_retry_after_crash, attempt, max_attempts, error_code, "
                    "error_detail, created_at) VALUES (:id, :workspace, 'user-heartbeat', "
                    "'member', :request_id, :trace_id, 17, 'localization.edit_attempt', "
                    "'localization_item', :resource_id, 1, "
                    "json_build_object('batch_id', 'batch-heartbeat', "
                    "'attempt_number', 1), :key, 'queued', true, 0, 3, '', '', "
                    "now() - interval '30 days')"
                ),
                {
                    "id": job_id,
                    "workspace": self.workspace,
                    "request_id": f"req_{suffix}",
                    "trace_id": f"trace_{suffix}",
                    "resource_id": f"item_{suffix}",
                    "key": f"localization-heartbeat:{suffix}",
                },
            )

        provider_calls = 0
        contender_job_id: str | None = None

        async def exercise() -> None:
            nonlocal provider_calls, contender_job_id
            with runtime_database(self.worker), patch.object(
                config, "JOB_LEASE_SECONDS", 1
            ):
                claimed = jobs.claim_next(worker_a, lease_seconds=1)
                self.assertIsNotNone(claimed)
                assert claimed is not None
                self.assertEqual(claimed.id, job_id)
                runtime = WorkerRuntime(
                    owner=worker_a, localization_coordinator=object()
                )

                async def slow_provider(_claimed) -> None:
                    nonlocal provider_calls
                    provider_calls += 1
                    await asyncio.sleep(1.6)

                with patch.object(
                    runtime, "_dispatch_localization_job", side_effect=slow_provider
                ):
                    running = asyncio.create_task(runtime._localization_job(claimed))
                    await asyncio.sleep(1.2)
                    contender = jobs.claim_next(worker_b, lease_seconds=1)
                    contender_job_id = contender.id if contender is not None else None
                    await running

        asyncio.run(exercise())

        self.assertEqual(provider_calls, 1)
        self.assertNotEqual(contender_job_id, job_id)
        with self.owner.connect() as connection:
            state, attempt, lease_owner, lease_expires_at = connection.execute(
                self.text(
                    "SELECT state, attempt, lease_owner, lease_expires_at "
                    "FROM jobs WHERE id = :id"
                ),
                {"id": job_id},
            ).one()
        self.assertEqual((state, attempt), ("succeeded", 1))
        self.assertIsNone(lease_owner)
        self.assertIsNone(lease_expires_at)

    def test_scheduled_work_has_a_system_initiator_without_fabricating_a_human_role(
        self,
    ) -> None:
        from unittest.mock import patch

        from app.platform import jobs

        suffix = uuid.uuid4().hex[:10]
        account_id = f"mact_schedule_{suffix}"
        schedule_id = f"msched_{suffix}"
        with self.owner.begin() as connection:
            connection.execute(
                self.text(
                    "INSERT INTO meta_accounts (id, workspace_id, ad_account_id, name, "
                    "currency, timezone_name, api_version, created_at, updated_at) "
                    "VALUES (:id, :workspace, :account, '', 'USD', 'UTC', 'v23.0', "
                    "now(), now())"
                ),
                {
                    "id": account_id,
                    "workspace": self.workspace,
                    "account": f"act_{suffix}",
                },
            )
            connection.execute(
                self.text(
                    "INSERT INTO meta_sync_schedules (id, workspace_id, account_id, "
                    "enabled, timezone_name, local_hour, lookback_days, next_run_at, "
                    "created_at, updated_at) VALUES (:id, :workspace, :account, true, "
                    "'UTC', 3, 3, now() - interval '1 minute', now(), now())"
                ),
                {
                    "id": schedule_id,
                    "workspace": self.workspace,
                    "account": account_id,
                },
            )

        with patch.object(jobs, "engine", self.worker):
            claimed = jobs.claim_next(f"schedule-worker-{suffix}", lease_seconds=60)

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.context.role, "system")
        self.assertIsNone(claimed.context.initiator_id)
        self.assertEqual(claimed.context.audit_initiator_role, "system")

    def test_worker_dispatch_permissions_are_explicit(self) -> None:
        with self.api.connect() as connection:
            self.assertTrue(
                connection.scalar(
                    self.text(
                        "SELECT has_table_privilege(current_user, "
                        "'public.audit_events', 'INSERT')"
                    )
                )
            )
        with self.worker.connect() as connection:
            for table, privilege in (
                ("meta_sync_schedules", "SELECT"),
                ("meta_sync_schedules", "UPDATE"),
                ("meta_sync_runs", "INSERT"),
                ("meta_ingestions", "INSERT"),
                ("jobs", "INSERT"),
            ):
                allowed = connection.scalar(
                    self.text(
                        "SELECT has_table_privilege(current_user, :table, :privilege)"
                    ),
                    {"table": f"public.{table}", "privilege": privilege},
                )
                self.assertTrue(allowed, f"Worker lacks {privilege} on {table}")
            self.assertTrue(
                connection.scalar(
                    self.text(
                        "SELECT has_table_privilege(current_user, "
                        "'public.audit_events', 'INSERT')"
                    )
                )
            )

    def test_api_privileges_match_the_request_statement_inventory(self) -> None:
        with self.api.connect() as connection:
            # Operational history stays read-only-role territory, with one
            # exception: the API serves the Workspace audit trail itself
            # (``GET /api/v1/audit``, spec §8.8), so it reads ``audit_events``
            # under tenant RLS and still cannot rewrite it (append-only
            # trigger; no UPDATE/DELETE grant).
            for table in (
                "import_batches",
                "launch_audit_log",
                "experiment_state_events",
            ):
                with self.subTest(table=table, privilege="SELECT"):
                    self.assertFalse(
                        connection.scalar(
                            self.text(
                                "SELECT has_table_privilege(current_user, :table, 'SELECT')"
                            ),
                            {"table": f"public.{table}"},
                        )
                    )
            with self.subTest(table="audit_events", privilege="SELECT"):
                self.assertTrue(
                    connection.scalar(
                        self.text(
                            "SELECT has_table_privilege(current_user, "
                            "'public.audit_events', 'SELECT')"
                        )
                    )
                )

            for table in (
                "assets",
                "cost_ledger",
                "meta_accounts",
                "meta_sync_runs",
                "meta_ingestions",
                "launch_previews",
                "launch_operations",
            ):
                with self.subTest(table=table, privilege="UPDATE"):
                    self.assertFalse(
                        connection.scalar(
                            self.text(
                                "SELECT has_table_privilege(current_user, :table, 'UPDATE')"
                            ),
                            {"table": f"public.{table}"},
                        )
                    )

            for table, privilege in (
                ("products", "UPDATE"),
                ("generation_runs", "UPDATE"),
                ("jobs", "UPDATE"),
                ("meta_sync_schedules", "UPDATE"),
                ("experiments", "UPDATE"),
                ("launch_confirmations", "UPDATE"),
            ):
                with self.subTest(table=table, privilege=privilege):
                    self.assertTrue(
                        connection.scalar(
                            self.text(
                                "SELECT has_table_privilege(current_user, :table, :privilege)"
                            ),
                            {"table": f"public.{table}", "privilege": privilege},
                        )
                    )

    def test_audit_events_are_append_only_for_runtime_roles(self) -> None:
        from sqlalchemy.exc import DBAPIError, ProgrammingError

        audit_id = f"audit_{uuid.uuid4().hex}"
        with self.api.begin() as connection:
            self._scope(connection, self.workspace, 17)
            connection.execute(
                self.text(
                    "INSERT INTO audit_events "
                    "(id, workspace_id, initiator_id, initiator_role, executor, "
                    "request_id, trace_id, authorization_epoch, event_type, "
                    "resource_type, resource_id, details_json, created_at) VALUES "
                    "(:id, :workspace, 'user_audit', 'member', 'user', 'req_audit', "
                    "'trace_audit', 17, 'audit.probe', 'probe', 'probe_1', '{}', now())"
                ),
                {"id": audit_id, "workspace": self.workspace},
            )
            for statement in (
                "UPDATE audit_events SET event_type = 'rewritten' WHERE id = :id",
                "DELETE FROM audit_events WHERE id = :id",
            ):
                with self.assertRaises(ProgrammingError):
                    with connection.begin_nested():
                        connection.execute(self.text(statement), {"id": audit_id})

        with self.owner.begin() as connection:
            self.assertEqual(
                connection.scalar(
                    self.text("SELECT count(*) FROM audit_events WHERE id = :id"),
                    {"id": audit_id},
                ),
                1,
            )
            for statement in (
                "UPDATE audit_events SET event_type = 'owner-rewrite' WHERE id = :id",
                "DELETE FROM audit_events WHERE id = :id",
            ):
                with self.assertRaises(DBAPIError):
                    with connection.begin_nested():
                        connection.execute(self.text(statement), {"id": audit_id})

    def test_import_idempotency_is_scoped_to_one_workspace(self) -> None:
        """The same source tree may be imported independently by two tenants."""
        from sqlalchemy.exc import IntegrityError

        other_workspace = f"ws_import_{uuid.uuid4().hex[:8]}"
        source_hash = uuid.uuid4().hex
        with self.owner.begin() as connection:
            connection.execute(
                self.text(
                    "INSERT INTO workspaces "
                    "(id, name, default_currency, authorization_epoch, lifecycle, budget_currency, created_at) "
                    "VALUES (:id, :id, 'USD', 0, 'active', 'USD', now())"
                ),
                {"id": other_workspace},
            )
            for workspace, suffix in (
                (self.workspace, "first"),
                (other_workspace, "second"),
            ):
                connection.execute(
                    self.text(
                        "INSERT INTO import_batches "
                        "(id, workspace_id, source_root, source_hash, status, "
                        "report_json, created_at) VALUES "
                        "(:id, :workspace, '/same/source', :source_hash, "
                        "'completed', CAST('{}' AS json), now())"
                    ),
                    {
                        "id": f"imp_{suffix}_{source_hash}",
                        "workspace": workspace,
                        "source_hash": source_hash,
                    },
                )

            with self.assertRaises(IntegrityError):
                with connection.begin_nested():
                    connection.execute(
                        self.text(
                            "INSERT INTO import_batches "
                            "(id, workspace_id, source_root, source_hash, status, "
                            "report_json, created_at) VALUES "
                            "(:id, :workspace, '/same/source', :source_hash, "
                            "'completed', CAST('{}' AS json), now())"
                        ),
                        {
                            "id": f"imp_duplicate_{source_hash}",
                            "workspace": self.workspace,
                            "source_hash": source_hash,
                        },
                    )

            connection.execute(
                self.text("DELETE FROM import_batches WHERE source_hash = :source_hash"),
                {"source_hash": source_hash},
            )
            connection.execute(
                self.text("DELETE FROM workspaces WHERE id = :workspace"),
                {"workspace": other_workspace},
            )

    def test_runtime_roles_are_not_interchangeable(self) -> None:
        with self.api.connect() as connection:
            self.assertEqual(connection.scalar(self.text("SELECT current_user")), "marketai_domain_api")
        with self.worker.connect() as connection:
            self.assertEqual(
                connection.scalar(self.text("SELECT current_user")),
                "marketai_domain_worker",
            )

        from sqlalchemy.exc import ProgrammingError

        with self.worker.begin() as connection:
            self._scope(connection, self.workspace, 17)
            with self.assertRaises(ProgrammingError):
                with connection.begin_nested():
                    connection.execute(
                        self.text(
                            "INSERT INTO products (id, workspace_id, name, description,"
                            " selling_points, audience, brand_notes, ref_images, economics,"
                            " revision, created_at, updated_at) VALUES ('forbidden', :workspace,"
                            " 'forbidden', '', '[]', '', '', '[]', '{}', 1, now(), now())"
                        ),
                        {"workspace": self.workspace},
                    )

    def test_process_startup_checks_the_role_it_really_connected_as(self) -> None:
        import subprocess
        import sys

        command = [
            sys.executable,
            "-c",
            "import sys; from app.adapters.persistence.database import "
            "assert_runtime_role; assert_runtime_role(sys.argv[1])",
        ]
        for url, expected in (
            (API_URL, "marketai_domain_api"),
            (WORKER_URL, "marketai_domain_worker"),
        ):
            environment = dict(os.environ)
            environment["MARKETAI_DATABASE_URL"] = url
            accepted = subprocess.run(
                [*command, expected],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)

            rejected = subprocess.run(
                [
                    *command,
                    "marketai_domain_worker"
                    if expected == "marketai_domain_api"
                    else "marketai_domain_api",
                ],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("database role mismatch", rejected.stderr)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
