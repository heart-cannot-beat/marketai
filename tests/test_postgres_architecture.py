"""Run against an isolated migrated PostgreSQL with the real API/Worker roles."""
import asyncio
import contextlib
import os
import time
import unittest
import uuid
from datetime import timedelta
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, event, text, update
from sqlalchemy.orm import sessionmaker

from tests import test_marketing_architecture as architecture_fixtures
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import WorkspaceRecord, JobRecord, OperationRecord, utcnow
from app.platform import jobs
from app.platform.tenancy import TenantContext
from tests.support.job_policies import probe_job_types

URLS = [os.getenv(key) for key in ('DOMAIN_OWNER_DATABASE_URL', 'DOMAIN_API_DATABASE_URL', 'DOMAIN_WORKER_DATABASE_URL')]


@unittest.skipUnless(all(URLS) and os.getenv('MARKETAI_TENANT_CONTEXT_SECRET'), 'requires isolated PostgreSQL runtime roles')
class PostgresArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.owner, self.api, self.worker = [create_engine(url) for url in URLS]
        for engine in (self.owner, self.api, self.worker):
            self.addCleanup(engine.dispose)
        self.sessions = [sessionmaker(bind=e, expire_on_commit=False, autoflush=False) for e in (self.owner, self.api, self.worker)]
        self.workspaces = []
        self.addCleanup(self.retire_jobs)

    def retire_jobs(self):
        # Other runtime tests deliberately exercise the global dispatcher.
        # Their queue must not inherit this test's simulated pending work.
        with self.owner.begin() as connection:
            connection.execute(update(JobRecord).where(JobRecord.workspace_id.in_(self.workspaces),
                JobRecord.state.in_(('queued', 'retry_wait', 'leased', 'running'))).values(
                    state='cancelled', finished_at=utcnow(), lease_owner=None, lease_expires_at=None, heartbeat_at=None))

    def test_marketing_history_pages_with_real_api_role_and_index(self):
        fixture = architecture_fixtures.TestMarketingArchitecture()
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[0]):
            fixture.setUp()
            fixture.context = fixture.owner_ctx(fixture._make_context(workspace_id='ws_history_' + uuid.uuid4().hex))
        self.workspaces.append(fixture.context.workspace_id)
        self.addCleanup(fixture.doCleanups)
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[1]):
            created = [fixture.create()['id'] for _ in range(5)]
            first = fixture.client.get('/api/v1/marketing/projects', params={'limit': 2}).json()
            second = fixture.client.get('/api/v1/marketing/projects', params={'limit': 2, 'cursor': first['next_cursor']}).json()
            third = fixture.client.get('/api/v1/marketing/projects', params={'limit': 2, 'cursor': second['next_cursor']}).json()
            self.assertEqual(set(created), {p['id'] for page in (first, second, third) for p in page['items']})
            self.assertIsNone(third['next_cursor'])
            searched = fixture.client.get('/api/v1/marketing/projects', params={'q': 'Alpha'}).json()
            self.assertEqual(len(searched['items']), 5)
            self.assertEqual(fixture.client.get('/api/v1/marketing/projects', params={'q': '%_'}).json()['items'], [])
        with self.owner.connect() as connection:
            self.assertIsNotNone(connection.scalar(text("SELECT indexname FROM pg_indexes WHERE indexname='ix_marketing_projects_history'")))

    def test_filtered_claims_are_atomic_and_transaction_local(self):
        suffix = uuid.uuid4().hex
        ctx = TenantContext(workspace_id='ws_arch_' + suffix, role='owner', initiator_id='owner')
        self.workspaces.append(ctx.workspace_id)
        with self.sessions[0]() as session:
            session.add(WorkspaceRecord(id=ctx.workspace_id, name='Isolated scheduler test'))
            session.commit()
        slow, fast = 'arch.slow.' + suffix, 'arch.fast.' + suffix
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[0]):
            with probe_job_types(slow, fast), tenant_session(ctx) as session:
                for kind in (slow, fast):
                    jobs.enqueue_in_session(session, context=ctx, job_type=kind, payload={}, idempotency_key=kind)
        with patch('app.platform.jobs.engine', self.worker):
            fast_job = jobs.claim_next('fast-owner', job_types=(fast,))
            self.assertEqual(fast_job.type, fast)
            with ThreadPoolExecutor(max_workers=2) as pool:
                claimed = list(pool.map(lambda n: jobs.claim_next(f'slow-{n}', job_types=(slow,)), range(2)))
            self.assertEqual(len([job for job in claimed if job]), 1)
            self.assertEqual(next(job for job in claimed if job).context.workspace_id, ctx.workspace_id)
        with self.worker.connect() as connection:
            self.assertIn(connection.scalar(text("SELECT current_setting('marketai.job_types', true)")), (None, ''))

    def test_recovery_crosses_api_then_worker_with_existing_minimum_privileges(self):
        # Reuse the HTTP fixture but bind every phase to its real runtime role.
        fixture = architecture_fixtures.TestMarketingArchitecture()
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[0]):
            fixture.setUp()
            fixture.context = fixture.owner_ctx(fixture._make_context(workspace_id='ws_arch_live_' + uuid.uuid4().hex))
            self.workspaces.append(fixture.context.workspace_id)
            fixture.paid_setup()
        self.addCleanup(fixture.doCleanups)
        env = {'MARKETAI_PLANNING_PROVIDER': 'openai', 'MARKETAI_PLANNING_MODEL': 'text-test',
            'MARKETAI_PLANNING_MAX_COST_USD': '0.10'}
        with patch.dict(os.environ, env), patch('app.adapters.persistence.database.SessionLocal', self.sessions[1]):
            project = fixture.create()
            cap = fixture.client.get('/api/v1/marketing/capabilities').json()['paid']
            call = fixture.plan_call(project, mode='byok', quote_hash=cap['quote_hash'])
        provider = AsyncMock(side_effect=TimeoutError())
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[2]):
            fixture.execute(call, provider)
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[1]):
            result = fixture.client.post(f"/api/v1/marketing/calls/{call['id']}/resolve", headers=fixture.headers(),
                json={'kind': 'planning', 'maximum_cost': '0.10', 'acknowledge_estimated_cost': True,
                    'note': 'Owner reviewed the missing provider response.'})
            self.assertEqual(result.status_code, 202, result.text)
        from app.worker_runtime import WorkerRuntime
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[2]), \
             patch('app.platform.jobs.engine', self.worker):
            runtime = WorkerRuntime(owner='recovery-postgres')
            self.assertTrue(asyncio.run(runtime.run_once(job_types=('marketing.resolve_call',))))
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[1]):
            self.assertEqual(fixture.read(project)['latest_call']['state'], 'failed')
            edited = fixture.edit(fixture.read(project), 'revise_source', source_text='Updated product input')
            self.assertEqual(edited.status_code, 200, edited.text)
            with patch.dict(os.environ, env):
                capability = fixture.client.get('/api/v1/marketing/capabilities').json()['paid']
                self.assertTrue(capability['available'])
                fixture.plan_call(edited.json(), mode='byok', quote_hash=capability['quote_hash'])
        provider.assert_awaited_once()
        with self.api.connect() as connection:
            # The recovery endpoint did not require broader API write grants.
            self.assertFalse(connection.scalar(text("SELECT has_table_privilege(current_user, 'spend_reservations', 'UPDATE')")))
            self.assertFalse(connection.scalar(text("SELECT has_table_privilege(current_user, 'marketing_ai_qc_calls', 'UPDATE')")))
            # Production already exposes tenant-scoped ledger reporting to the API.
            # Preserve that existing read grant while adding no mutation rights.
            self.assertTrue(connection.scalar(text("SELECT has_table_privilege(current_user, 'provider_cost_ledger', 'SELECT')")))
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[1]):
            with tenant_session(fixture.context) as session:
                self.assertEqual(session.scalar(text('SELECT marketai_settled_cost_total(:w, NULL)'),
                    {'w': fixture.context.workspace_id}), Decimal('0.10'))
        from sqlalchemy.exc import ProgrammingError
        with self.api.connect() as connection:
            with self.assertRaises(ProgrammingError):
                connection.scalar(text('SELECT marketai_settled_cost_total(:w, NULL)'), {'w': fixture.context.workspace_id})
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[1]):
            with self.assertRaises(ProgrammingError), tenant_session(fixture.context) as session:
                session.scalar(text('SELECT marketai_settled_cost_total(:w, NULL)'), {'w': 'other_workspace'})

    def test_brand_kit_versions_are_insert_only_for_runtime_roles(self):
        # Plans and recipes freeze exact versions; no runtime role may rewrite or remove one.
        for engine, insert in ((self.api, True), (self.worker, False)):
            with engine.connect() as connection:
                for privilege, expected in (("SELECT", True), ("INSERT", insert), ("UPDATE", False), ("DELETE", False)):
                    self.assertEqual(connection.scalar(text(
                        f"SELECT has_table_privilege(current_user, 'product_brand_kits', '{privilege}')")), expected, privilege)
        with self.owner.connect() as connection:
            self.assertTrue(connection.scalar(text(
                "SELECT relforcerowsecurity FROM pg_class WHERE relname = 'product_brand_kits'")))

    def test_workflow_sql_excludes_other_projects_with_api_role(self):
        fixture = architecture_fixtures.TestMarketingArchitecture()
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[0]):
            fixture.setUp()
            fixture.context = fixture.owner_ctx(fixture._make_context(workspace_id='ws_arch_read_' + uuid.uuid4().hex))
            self.workspaces.append(fixture.context.workspace_id)
            project = fixture.confirmed()
            fixture.generate(project)
            fixture.start_round(project)
            other = fixture.confirmed()
            fixture.generate(other)
        self.addCleanup(fixture.doCleanups)
        transferred = []
        def after(conn, cursor, statement, parameters, context, executemany):
            if 'FROM asset_versions JOIN' in statement:
                transferred.append(cursor.rowcount)
        event.listen(self.api, 'after_cursor_execute', after)
        try:
            with patch('app.adapters.persistence.database.SessionLocal', self.sessions[1]):
                response = fixture.client.get(f"/api/v1/marketing/projects/{project['id']}/workflow")
        finally:
            event.remove(self.api, 'after_cursor_execute', after)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()['current']['assets']), 5)
        self.assertEqual(transferred, [5])  # Ten exist; only this project's five cross SQL.

    def test_a_check_the_worker_commits_inside_a_workflow_read_keeps_polling(self):
        """READ COMMITTED gives each statement a new snapshot; the Worker's commit lands inside the API's read (#307)."""
        from app.worker_runtime import WorkerRuntime
        fixture = architecture_fixtures.TestPollingReadWindow()
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[0]):
            fixture.setUp()
            fixture.context = fixture.owner_ctx(fixture._make_context(workspace_id='ws_poll_window_' + uuid.uuid4().hex))
            self.workspaces.append(fixture.context.workspace_id)
            project, row = fixture.setup_round()
        self.addCleanup(fixture.doCleanups)

        def check(kind):
            with patch('app.adapters.persistence.database.SessionLocal', self.sessions[1]):
                current = fixture.client.get(f"/api/v1/marketing/rounds/{row['id']}").json()['data']
                quote = fixture.quote(current, kind)
                fixture.command(current, 'ai-qc', {'quote_hash': quote['quote_hash'], 'kind': kind}, 202)

        def work():
            with patch('app.adapters.persistence.database.SessionLocal', self.sessions[2]), \
                 patch('app.platform.jobs.engine', self.worker):
                runtime = WorkerRuntime(owner='poll-window-postgres')
                runtime.blob_store = fixture.blobs
                self.assertTrue(asyncio.run(runtime.run_once(job_types=('marketing.ai_qc',))))
        check('single')
        for _ in range(5):
            work()
        check('paired')
        for _ in range(4):
            work()
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[1]):
            with fixture.finishing_during_read(work):
                result = fixture.client.get(fixture.path(project)).json()
            running = [a for a in result['current']['assets'] if a['paired_ai_qc']['state'] in ('queued', 'running')]
            self.assertEqual(len(running), 1)
            self.assertEqual(result['poll_after_ms'], 3000)
            result = fixture.client.get(fixture.path(project)).json()
            self.assertEqual({a['paired_ai_qc']['state'] for a in result['current']['assets']}, {'succeeded'})
            self.assertEqual(result['poll_after_ms'], 0)

    def test_crop_then_rotate_keeps_truth_evidence_with_real_api_and_worker_roles(self):
        from app.adapters.persistence.models import AssetVersionRecord
        from app.worker_runtime import WorkerRuntime
        fixture = architecture_fixtures.TestMarketingArchitecture()
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[0]):
            fixture.setUp()
            fixture.context = fixture.owner_ctx(fixture._make_context(workspace_id='ws_image_edit_' + uuid.uuid4().hex))
            self.workspaces.append(fixture.context.workspace_id)
            project = fixture.confirmed()
            fixture.generate(project)
            row = fixture.start_round(project)
        self.addCleanup(fixture.doCleanups)
        source_id = row['assets'][0]['asset_version_id']
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[1]):
            row = fixture.command(row, 'image-edits', {'asset_version_id': source_id,
                'crop': {'x': 0, 'y': 0, 'width': .5, 'height': 1}, 'rotation': 90}, 202)
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[2]), \
             patch('app.platform.jobs.engine', self.worker):
            runtime = WorkerRuntime(owner='image-edit-postgres')
            runtime.blob_store = fixture.blobs
            self.assertTrue(asyncio.run(runtime.run_once(job_types=('marketing.image_edit',))))
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[1]):
            response = fixture.client.get(f"/api/v1/marketing/rounds/{row['id']}?include_image_edits=true")
            self.assertEqual(response.status_code, 200, response.text)
            row = response.json()['data']
            edit = row['image_edits'][0]
            self.assertEqual(edit['state'], 'succeeded', edit)
            child = next(a for a in row['assets'] if a['asset_version_id'] == edit['result_asset_version_id'])
            self.assertEqual(child['parent_version_id'], source_id)
            self.assertIsNone(child['quality'])
            self.assertFalse(child['ready'])
            with tenant_session(fixture.context) as session:
                original = session.get(AssetVersionRecord, source_id)
                result = session.get(AssetVersionRecord, child['asset_version_id'])
                self.assertIsNotNone(original.truth_dependency_evidence_hash)
                for field in ('truth_dependency_expected', 'truth_dependency_policy_version',
                              'truth_dependency_evidence_json', 'truth_dependency_evidence_hash'):
                    self.assertEqual(getattr(result, field), getattr(original, field), field)
            workflow = fixture.client.get(f"/api/v1/marketing/projects/{project['id']}/workflow?include_polling=true&include_image_edits=true")
            self.assertEqual(workflow.status_code, 200, workflow.text)
            self.assertEqual(workflow.json()['poll_after_ms'], 0)
        # Imported legacy images have no truth stamp. Its JSON column must
        # remain SQL NULL, rather than turning into a partial JSON-null stamp.
        import hashlib
        from sqlalchemy import null
        from app.adapters.persistence.models import new_id
        from app.marketing.blob_read import write_generated
        from tests.support.library_fixtures import png_bytes
        content = png_bytes(colour=(70, 80, 90))
        legacy_id = new_id('av')
        blob = asyncio.run(write_generated(fixture.blobs,
            f'committed/{fixture.context.workspace_id}/{legacy_id}.png', content, 'image/png'))
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[0]):
            with tenant_session(fixture.context) as session:
                source = session.get(AssetVersionRecord, source_id)
                session.add(AssetVersionRecord(id=legacy_id, workspace_id=source.workspace_id,
                    asset_id=source.asset_id, kind='uploaded', origin_kind='upload', execution_mode='mock',
                    aspect_ratio='4:3', object_key=blob.committed_key, committed_object_key=blob.committed_key,
                    committed_storage_version_id=blob.storage_version_id,
                    content_sha256=hashlib.sha256(content).hexdigest(), byte_size=len(content), mime_type='image/png',
                    truth_dependency_evidence_json=null()))
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[1]):
            row = fixture.command(row, 'creatives/reuse', {'asset_version_id': legacy_id,
                'combination_id': row['snapshot']['plan']['combinations'][0]['id']}, 201)
            row = fixture.command(row, 'image-edits', {'asset_version_id': legacy_id, 'rotation': 90}, 202)
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[2]), \
             patch('app.platform.jobs.engine', self.worker):
            self.assertTrue(asyncio.run(runtime.run_once(job_types=('marketing.image_edit',))))
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[1]):
            row = fixture.client.get(f"/api/v1/marketing/rounds/{row['id']}?include_image_edits=true").json()['data']
            edit = next(e for e in row['image_edits'] if e['source_asset_version_id'] == legacy_id)
            self.assertEqual(edit['state'], 'succeeded', edit)
            with tenant_session(fixture.context) as session:
                version = session.get(AssetVersionRecord, edit['result_asset_version_id'])
                self.assertEqual(version.parent_version_id, legacy_id)
                for field in ('truth_dependency_expected', 'truth_dependency_policy_version',
                              'truth_dependency_evidence_json', 'truth_dependency_evidence_hash'):
                    self.assertIsNone(getattr(version, field), field)

    def test_supplier_tools_use_the_real_api_worker_roles_and_fresh_review(self):
        from tests.test_supplier_image_tools import TestSupplierImageTools, picture
        from app.generation.image_tools import delivery_metadata
        from app.providers.base import reference_delivery_metadata
        from app.providers.adapter import ProviderCallResult
        from app.worker_runtime import WorkerRuntime
        fixture = TestSupplierImageTools()
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[0]):
            fixture.setUp()
            fixture.context = fixture.owner_ctx(fixture._make_context(workspace_id='ws_supplier_tools_' + uuid.uuid4().hex))
            self.workspaces.append(fixture.context.workspace_id)
            project, row = fixture.setup_round(paid=True)
        self.addCleanup(fixture.doCleanups)
        source = row['assets'][0]['asset_version_id']
        provider = AsyncMock()
        async def output(request):
            return ProviderCallResult(picture(transparent=request.edit_operation == 'remove_background'), 'fixture-only', False,
                {**reference_delivery_metadata(request.reference_inputs), **delivery_metadata(request.edit_operation, request.edit_mask)})
        provider.execute.side_effect = output
        count = 5
        for kind, parameters in (('upscale', {}), ('remove_background', {}), ('erase', {}),
                ('erase', {'region': None, 'strokes': [{'width': .1, 'points': [{'x': .2, 'y': .3}, {'x': .6, 'y': .7}]}]})):
            with patch('app.adapters.persistence.database.SessionLocal', self.sessions[1]):
                row, draft, preview = fixture.prepare(row, kind, **parameters)
                fixture.submit(draft, preview)
            count += 1
            with patch('app.adapters.persistence.database.SessionLocal', self.sessions[2]), \
                 patch('app.platform.jobs.engine', self.worker), \
                 patch('app.generation.paid_execution.get_provider', return_value=provider):
                runtime = WorkerRuntime(owner='supplier-tools-postgres')
                runtime.blob_store = fixture.blobs
                for _ in range(20):
                    if not asyncio.run(runtime.run_once(job_types=('generation.run_item',))):
                        break
            with patch('app.adapters.persistence.database.SessionLocal', self.sessions[1]):
                row = fixture.refresh(row)
                self.assertEqual(len(row['assets']), count)
                child = next(a for a in reversed(row['assets']) if a['parent_version_id'] == source and a['quality'] is None)
                self.assertFalse(child['ready'])
                self.assertEqual(child['edit_operation'], kind)
                self.assertTrue(child['asset_id'] and child['created_at'])
                row = fixture.command(row, 'reviews', {'asset_version_id':child['asset_version_id'], 'quality':'approved',
                    'checks':{k:'pass' for k in child['required_checks']}, 'note':'Real role fixture review'}, 201)
                self.assertTrue(next(a for a in row['assets'] if a['asset_version_id'] == child['asset_version_id'])['ready'])
        self.assertEqual(provider.execute.await_count, 4)

    def test_generation_reconciliation_accepts_real_item_ids_without_resending(self):
        from tests.test_generation_run_operations import _RunOperationsBase
        from app.generation.run_commands import reconcile_run
        from app.generation.run_queries import get_run
        from app.adapters.persistence.models import OperationRecord
        from sqlalchemy import func, select
        fixture = _RunOperationsBase()
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[0]):
            fixture.setUp()
            with tenant_session(fixture.ctx) as session:
                run_id, _ = fixture._submit(session, variants=3)
                targets = [item.id for item in fixture._items(session, run_id)]
                before = session.scalar(select(func.count()).select_from(JobRecord).where(JobRecord.workspace_id == fixture.ctx.workspace_id))
        self.workspaces.append(fixture.ctx.workspace_id)
        self.addCleanup(fixture.doCleanups)
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[1]):
            with tenant_session(fixture.ctx) as session:
                run = get_run(session, fixture.ctx, run_id)
                revision = run.revision
                result = reconcile_run(session, fixture.ctx, run_id=run_id, expected_revision=revision,
                    uncertain_targets=targets, operator_note='Reviewed existing result; do not redispatch.', idempotency_key='reconcile-real-identifiers')
                self.assertEqual(session.get(OperationRecord, result['operation_id']).state, 'succeeded')
                replay = reconcile_run(session, fixture.ctx, run_id=run_id, expected_revision=revision,
                    uncertain_targets=targets, operator_note='Reviewed existing result; do not redispatch.', idempotency_key='reconcile-real-identifiers')
                self.assertEqual(replay, result)
                self.assertEqual(get_run(session, fixture.ctx, run_id).revision, revision + 1)
                after = session.scalar(select(func.count()).select_from(JobRecord).where(JobRecord.workspace_id == fixture.ctx.workspace_id))
                self.assertEqual(before, after)

    def test_a_connection_check_runs_as_the_worker_and_reads_back_as_the_api(self):
        """The API enqueues and replays, the Worker reads the version and the bound credential state and appends the evidence, the API reads it back (#383)."""
        from tests.test_library_connection_check_worker import ConnectionCheckFixtures
        from tests.test_library_datasource_api import _provision_credential_version
        from app.library import connection_checks
        from app.library.contracts import ROLE_OWNER
        from app.library.datasources import bind_credential
        from app.worker_runtime import WorkerRuntime
        fixture = ConnectionCheckFixtures()
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[0]):
            fixture.setUp()
            fixture.ctx = fixture._make_context(workspace_id='ws_conncheck_' + uuid.uuid4().hex, role=ROLE_OWNER)
            self.workspaces.append(fixture.ctx.workspace_id)
            with tenant_session(fixture.ctx) as session:
                cv_id = _provision_credential_version(fixture.ctx, session)
        self.addCleanup(fixture.doCleanups)
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[1]):
            entry_id = fixture.data_source('mock', 'mock')
            bind_credential(fixture.ctx, entry_id, cv_id)
            first = fixture.check(entry_id, 'ci-only-conncheck-postgres', mode='real')
            replay = fixture.check(entry_id, 'ci-only-conncheck-postgres')
            self.assertEqual((first.status_code, replay.status_code), (202, 202), replay.text)
            self.assertEqual(replay.json()['operation_id'], first.json()['operation_id'])
            [job] = fixture.jobs_for(entry_id)
            self.assertTrue(job.safe_to_retry_after_crash)
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[2]), \
             patch('app.platform.jobs.engine', self.worker):
            runtime = WorkerRuntime(owner='conncheck-postgres')
            self.assertTrue(asyncio.run(runtime.run_once(job_types=('library.connection_check',))))
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[1]):
            operation = fixture.operation(first.json()['operation_id'])
            [event] = fixture.evidence()
            self.assertEqual((operation['state'], operation['result']),
                ('succeeded', {'resource_type': connection_checks.RESULT_RESOURCE_TYPE, 'resource_id': event.id}))
            self.assertEqual((event.resource_type, event.resource_id), ('data_source', entry_id))
            details = event.details_json
            self.assertEqual((details['reason_code'], details['execution_mode'], details['requested_mode']),
                ('controlled_mock_source', 'mock', 'real'))
            self.assertEqual((details['bound_version_id'], details['bound_version_state']), (cv_id, 'active'))
            read = fixture.client.get(f'/api/v1/library/entries/{entry_id}/connection-checks/{event.id}')
            self.assertEqual(read.status_code, 200, read.text)
            self.assertEqual((read.json()['reason_code'], read.json()['credential_state']), ('controlled_mock_source', 'active'))

    def test_a_manual_fact_import_survives_a_crash_as_the_worker_and_reads_back_as_the_api(self):
        """The API enqueues, replays and refuses a reused upload; a crashed run's expired lease is requeued by the claim, not left uncertain; the Worker writes the draft and settles the Operation under its row lock; the API reads both (#397)."""
        from datetime import timedelta
        from tests.test_library_manual_fact_import_worker import JOB_TYPE, RESULT_TYPE, ManualFactImportFixtures
        from app.library.contracts import ROLE_OWNER
        from app.platform.operations import OperationState, transition_operation
        from app.worker_runtime import WorkerRuntime
        fixture = ManualFactImportFixtures()
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[0]):
            fixture.setUp()
            fixture.ctx = fixture._make_context(workspace_id='ws_factimport_' + uuid.uuid4().hex, role=ROLE_OWNER)
            fixture.act_as(fixture.ctx)
            self.workspaces.append(fixture.ctx.workspace_id)
        self.addCleanup(fixture.doCleanups)
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[1]):
            data_source = fixture.data_source()
            upload = fixture.upload(data_source)
            first = fixture.import_(data_source, upload, 'ci-only-factimport-postgres')
            replay = fixture.import_(data_source, upload, 'ci-only-factimport-postgres')
            self.assertEqual((first.status_code, replay.status_code), (202, 202), replay.text)
            self.assertEqual(replay.json()['operation_id'], first.json()['operation_id'])
            fixture.assert_problem(fixture.import_(data_source, upload), 409, 'upload_intent_consumed')
            [job] = fixture.jobs()
            self.assertTrue(job.safe_to_retry_after_crash)
        operation_id = first.json()['operation_id']
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[2]), \
             patch('app.platform.jobs.engine', self.worker):
            # A Worker took the Job, started the Operation and died.
            claimed = jobs.claim_next('factimport-crashed', job_types=(JOB_TYPE,))
            self.assertEqual(claimed.id, job.id)
            jobs.mark_running(claimed.context, claimed.id, 'factimport-crashed')
            with tenant_session(claimed.context) as session:
                transition_operation(session, operation_id, OperationState.RUNNING, reason=None, actor=claimed.context.executor)
        with self.owner.begin() as connection:
            connection.execute(update(JobRecord).where(JobRecord.id == job.id)
                .values(lease_expires_at=utcnow() - timedelta(seconds=1)))
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[2]), \
             patch('app.platform.jobs.engine', self.worker):
            runtime = WorkerRuntime(owner='factimport-postgres')
            runtime.blob_store = fixture.store
            self.assertTrue(asyncio.run(runtime.run_once(job_types=(JOB_TYPE,))))
        with self.owner.connect() as connection:
            self.assertEqual(connection.scalar(text(
                "SELECT count(*) FROM audit_events WHERE resource_id = :job AND event_type = 'job.requeued'"),
                {'job': job.id}), 1)
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[1]):
            operation = fixture.operation(operation_id)
            [fact_set] = fixture.fact_sets()
            self.assertEqual((operation['state'], operation['result']),
                ('succeeded', {'resource_type': RESULT_TYPE, 'resource_id': fact_set}))
            [job] = fixture.jobs()
            self.assertEqual(job.state, 'succeeded')

    # -- #22: Jobs with no external call come back from a crash ------------------
    #
    # The fixtures are tests.test_job_crash_recovery's: seeded as the schema
    # owner, every Worker phase run as marketai_domain_worker, recovery done by
    # marketai_claim_next_job_v2 rather than the SQLite sweep.

    def _as_worker(self):
        stack = contextlib.ExitStack()
        stack.enter_context(patch('app.adapters.persistence.database.SessionLocal', self.sessions[2]))
        stack.enter_context(patch('app.platform.jobs.engine', self.worker))
        return stack

    def _expire_lease(self, job_id):
        with self.owner.begin() as connection:
            connection.execute(update(JobRecord).where(JobRecord.id == job_id)
                .values(lease_expires_at=utcnow() - timedelta(seconds=1)))

    def _quarantine_others(self, workspace_id):
        # As the owner: the Worker role's untenanted session sees no Job at all.
        with self.owner.begin() as connection:
            open_states = JobRecord.state.in_(('queued', 'retry_wait'))
            connection.execute(update(JobRecord).where(JobRecord.workspace_id != workspace_id, open_states)
                .values(next_attempt_at=utcnow() + timedelta(days=1)))
            connection.execute(update(JobRecord).where(JobRecord.workspace_id == workspace_id, open_states)
                .values(next_attempt_at=None))

    def _retire_crash_workspace(self, workspace_id):
        # marketai_health_stats_v1 counts open and uncertain Jobs and stale
        # open Operations in every Workspace, and later suites share this
        # database. The paid control leaves its Jobs uncertain on purpose and
        # the fixtures leave their Operations open, so close both. The rows
        # stay: operation_state_events and the Localization tables refuse
        # DELETE and reference them, and each Workspace is this test's own.
        with self.owner.begin() as connection:
            connection.execute(update(JobRecord).where(JobRecord.workspace_id == workspace_id,
                JobRecord.state.in_(('queued', 'retry_wait', 'leased', 'running', 'uncertain'))).values(
                    state='cancelled', finished_at=utcnow(), lease_owner=None, lease_expires_at=None, heartbeat_at=None))
            connection.execute(update(OperationRecord).where(OperationRecord.workspace_id == workspace_id,
                OperationRecord.state.notin_(('succeeded', 'failed', 'partially_succeeded', 'cancelled'))).values(
                    state='cancelled', updated_at=utcnow()))

    @staticmethod
    def _claim_recovers_expired_leases():
        # The claim function retires every expired lease before it looks for
        # work; a lane no Job is in makes it do only that.
        assert jobs.claim_next('ci-only-recovery-probe', job_types=('ci-only.no-such-job',)) is None

    def _crash_fixture(self, fixture_cls):
        fixture = fixture_cls()
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[0]):
            if hasattr(fixture_cls, '_build_ready_batch'):
                fixture_cls.setUpClass()
            fixture.setUp()
            fixture.seed()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(self._retire_crash_workspace, fixture.workspace_id())
        self.workspaces.append(fixture.workspace_id())
        fixture.expire_lease = lambda: self._expire_lease(fixture.job_id)
        fixture.recover = self._claim_recovers_expired_leases
        self._quarantine_others(fixture.workspace_id())
        return fixture

    def _wait_until_blocked(self, thread):
        """Return once the second Worker waits on a row lock (or has finished)."""
        deadline = time.monotonic() + 15
        while thread.is_alive() and time.monotonic() < deadline:
            # pg_locks, not pg_stat_activity: another role's wait events are
            # hidden from a non-superuser, its ungranted locks are not. A row
            # lock is waited for as the holder's transaction id, which names
            # no database.
            with self.owner.connect() as connection:
                if connection.scalar(text("SELECT count(*) FROM pg_locks WHERE NOT granted")):
                    self.second_worker_blocked = True
                    return
            time.sleep(0.05)

    def _requeue_fixtures(self):
        from tests import test_job_crash_recovery as crash
        return (crash.ReferenceGateFixture, crash.ExportPackageFixture, crash.DeterministicQcFixture,
                crash.FinalizeItemFixture, crash.FoldBatchFixture)

    def test_a_crashed_job_with_no_external_call_is_requeued_by_the_claim_and_settles_once(self):
        """mark_running, then the Worker dies: the claim requeues the Job (lease_expired, not paid_call_outcome_unknown) and the rerun settles the work once; a Worker that dies after the work committed leaves a Job the rerun only closes (#22)."""
        for fixture_cls in self._requeue_fixtures():
            with self.subTest(job_type=fixture_cls.JOB_TYPE, crash='after mark_running'):
                fixture = self._crash_fixture(fixture_cls)
                with self._as_worker():
                    fixture.crash_after_mark_running()
                    fixture.recover()
                job = fixture.job_row()
                self.assertEqual((job.state, job.error_code), ('queued', 'lease_expired'))
                self.assertNotIn('job.uncertain', fixture.job_events())
                self.assertIn('job.requeued', fixture.job_events())
                with self._as_worker():
                    self.assertEqual(fixture.drain(), 1)
                self.assertEqual(fixture.job_row().state, 'succeeded')
                fixture.assert_settled_once(fixture.observe())
            with self.subTest(job_type=fixture_cls.JOB_TYPE, crash='after the work committed'):
                fixture = self._crash_fixture(fixture_cls)
                with self._as_worker():
                    fixture.crash_before_closing_the_job()
                    fixture.recover()
                settled = fixture.observe()
                fixture.assert_settled_once(settled)
                self.assertEqual(fixture.job_row().state, 'queued')
                with self._as_worker():
                    self.assertEqual(fixture.drain(), 1)
                self.assertEqual(fixture.job_row().state, 'succeeded')
                self.assertEqual(fixture.observe(), settled)

    def test_two_workers_on_one_requeued_job_write_once_under_the_row_lock(self):
        """The first Worker holds the row lock past its lease; the second takes the Job over, waits on the lock and then finds the work done (#22)."""
        from tests.test_job_crash_recovery import LEASE_LOST
        for fixture_cls in self._requeue_fixtures():
            with self.subTest(job_type=fixture_cls.JOB_TYPE):
                fixture = self._crash_fixture(fixture_cls)
                self.second_worker_blocked = False
                with self._as_worker():
                    first, second = fixture.run_two_workers(wait_for_second=self._wait_until_blocked)
                self.assertIsNone(second)
                self.assertIsInstance(first, RuntimeError)
                self.assertIn(LEASE_LOST, str(first))
                if fixture_cls.JOB_TYPE != 'library.reference_gate':
                    # The gate reads its bytes outside any transaction; every
                    # other handler holds its resource's lock at that point.
                    self.assertTrue(self.second_worker_blocked, 'the second Worker waited on the lock')
                self.assertEqual(fixture.job_row().state, 'succeeded')
                fixture.assert_settled_once(fixture.observe())

    def test_a_crashed_scale_export_is_requeued_by_the_claim(self):
        """Only the recovery half: the frozen Job context carries no meta_mode, which scale_exports' policy requires, so the Worker role cannot run the export itself yet (#22)."""
        from tests.test_job_crash_recovery import ScaleExportFixture
        fixture = self._crash_fixture(ScaleExportFixture)
        with self._as_worker():
            claimed = jobs.claim_next('scale-crashed', job_types=('scale_export',))
            self.assertEqual(claimed.id, fixture.job_id)
            jobs.mark_running(claimed.context, claimed.id, 'scale-crashed')
            fixture.expire_lease()
            fixture.recover()
        job = fixture.job_row()
        self.assertTrue(job.safe_to_retry_after_crash)
        self.assertEqual((job.state, job.error_code), ('queued', 'lease_expired'))
        self.assertNotIn('job.uncertain', fixture.job_events())

    def test_a_crashed_paid_job_is_still_left_uncertain_by_the_claim(self):
        from tests.test_job_crash_recovery import PaidJobFixture
        fixture = PaidJobFixture()
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[0]):
            fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(self._retire_crash_workspace, fixture.ctx.workspace_id)
        self.workspaces.append(fixture.ctx.workspace_id)
        for job_type in PaidJobFixture.PAID_TYPES:
            with self.subTest(job_type=job_type):
                self._quarantine_others(fixture.ctx.workspace_id)
                with self._as_worker():
                    job_id = fixture.crash(job_type, expire_lease=self._expire_lease)
                    self._claim_recovers_expired_leases()
                with patch('app.adapters.persistence.database.SessionLocal', self.sessions[0]):
                    fixture.assert_uncertain(job_id)

    def test_a_reclaimed_job_keeps_its_workspace_and_cannot_reach_another(self):
        """Workspace A's expired Job is reclaimed with A's frozen context and writes only A's object; a forged payload naming B's object finds nothing under FORCE RLS (#22)."""
        from tests.test_job_crash_recovery import ReferenceGateFixture
        from app.platform.operations import create_operation
        with self.owner.connect() as connection:
            for table in ('jobs', 'reference_objects', 'operation_records', 'export_packages',
                          'scale_exports', 'localization_items', 'localization_batches'):
                self.assertEqual(connection.execute(text(
                    'SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = :t'),
                    {'t': table}).one(), (True, True), table)
        a = self._crash_fixture(ReferenceGateFixture)
        b = self._crash_fixture(ReferenceGateFixture)
        self._quarantine_others(a.workspace_id())
        with self._as_worker():
            claimed = jobs.claim_next('crashed-in-a', job_types=('library.reference_gate',))
            self.assertEqual(claimed.id, a.job_id)
            jobs.mark_running(claimed.context, claimed.id, 'crashed-in-a')
            a.expire_lease()
            a.recover()
            reclaimed = jobs.claim_next('reclaims-a', job_types=('library.reference_gate',))
        self.assertEqual(reclaimed.id, a.job_id)
        self.assertEqual(
            (reclaimed.context.workspace_id, reclaimed.context.initiator_id,
             reclaimed.context.authorization_epoch, reclaimed.context.executor),
            (a.ctx.workspace_id, a.ctx.initiator_id, a.ctx.authorization_epoch, 'system:worker'))
        with self._as_worker():
            asyncio.run(a.runtime('reclaims-a')._library_reference_gate(reclaimed))
        self.assertEqual(a.observe()['object'], 'released')
        b_before = b.observe()
        self.assertEqual((b_before['object'], b_before['operation']), ('quarantined', 'queued'))
        # Only a forged payload could name B's object from A's Workspace.
        with patch('app.adapters.persistence.database.SessionLocal', self.sessions[0]):
            with tenant_session(a.ctx) as session:
                forged_operation = create_operation(session, command_type='reference_gate').id
                forged = jobs.enqueue_in_session(
                    session, context=a.ctx, job_type='library.reference_gate',
                    payload={'object_id': b.object_id, 'operation_id': forged_operation},
                    idempotency_key='reference_gate:ci-only-forged', resource_type='reference_object',
                    resource_id=b.object_id,
                ).id
        with self._as_worker():
            self.assertEqual(a.drain(), 1)
        a.job_id = forged
        job = a.job_row()
        # The fence records the gate's failure family; the detail says why.
        self.assertEqual((job.state, job.error_code), ('failed', 'reference_gate_rejected'))
        self.assertIn('参考对象不存在', job.error_detail)
        self.assertEqual(b.observe(), b_before, "B's object, Operation and audit are untouched")
        with self.owner.connect() as connection:
            self.assertEqual(connection.scalar(text(
                'SELECT state FROM reference_objects WHERE id = :id'), {'id': b.object_id}), 'quarantined')
