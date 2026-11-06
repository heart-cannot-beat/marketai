"""Regression evidence for recovery, isolated worker lanes and bounded reads."""
import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from sqlalchemy import event, select

from tests.support.library_fixtures import png_bytes
from tests.test_marketing_ai_qc import MarketingAIQCFixtures
from tests.test_marketing_workflow import MarketingWorkflowFixtures
from app.adapters.persistence.database import engine, tenant_session
from app.adapters.persistence.models import GenerationRunRecord, JobRecord
from app.entitlements.cost_models import SpendReservationRecord, ProviderCostLedgerEntryRecord
from app.generation.models import RunItemRecord
from app.generation.paid_execution import execute_run_item
from app.marketing import workflow
from app.marketing.ai_qc_models import MarketingAIQCRecord
from app.marketing.ai_qc_worker import execute_ai_qc
from app.marketing.models import PlanningCallRecord
from app.providers.adapter import ProviderCallResult


class TestMarketingArchitecture(MarketingWorkflowFixtures):
    def uncertain_plan(self):
        self.paid_setup()
        project = self.create()
        with patch.dict('os.environ', {'MARKETAI_PLANNING_PROVIDER': 'openai',
            'MARKETAI_PLANNING_MODEL': 'text-test', 'MARKETAI_PLANNING_MAX_COST_USD': '0.10'}):
            cap = self.client.get('/api/v1/marketing/capabilities').json()['paid']
            call = self.plan_call(project, mode='byok', quote_hash=cap['quote_hash'])
        provider = AsyncMock(side_effect=TimeoutError())
        self.execute(call, provider)
        return project, call, provider

    def resolve(self, call, **fields):
        response = self.client.post(f"/api/v1/marketing/calls/{call['id']}/resolve", headers=self.headers(),
            json={'kind': 'planning', 'maximum_cost': '0.10', 'acknowledge_estimated_cost': True,
                  'note': 'Reviewed missing response; close without result and retain maximum estimated cost.', **fields})
        if response.status_code == 202:
            from app.marketing.recovery import execute_resolution
            execute_resolution(self.context, response.json()['operation_id'])
        return response

    def test_owner_can_close_unknown_result_without_resending_or_erasing_cost(self):
        project, call, provider = self.uncertain_plan()
        self.context = self.owner_ctx(self.context)
        result = self.resolve(call)
        self.assertEqual(result.status_code, 202, result.text)
        self.assertEqual(self.read(project)['latest_call']['state'], 'failed')
        # Repeated reconciliation is harmless, including after browser refresh.
        self.assertEqual(self.resolve(call).status_code, 202)
        self.execute(call, provider)
        self.assertEqual(provider.await_count, 1)
        with tenant_session(self.context) as session:
            row = session.get(PlanningCallRecord, call['id'])
            reservation = session.scalar(select(SpendReservationRecord).where(SpendReservationRecord.job_id == row.job_id))
            self.assertEqual(reservation.state, 'settled')
            self.assertEqual(reservation.outstanding_budget_amount, Decimal('0'))
            entries = session.scalars(select(ProviderCostLedgerEntryRecord).where(
                ProviderCostLedgerEntryRecord.spend_reservation_id == reservation.id,
                ProviderCostLedgerEntryRecord.entry_type == 'estimated_settlement')).all()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].budget_amount, Decimal('0.10'))
        response = self.edit(self.read(project), 'revise_source', source_text='Updated source after reconciliation')
        self.assertEqual(response.status_code, 200, response.text)

    def test_recovery_requires_owner_confirmation_and_correct_cost(self):
        _, call, _ = self.uncertain_plan()
        self.assertEqual(self.resolve(call).status_code, 403)
        self.context = self.owner_ctx(self.context)
        self.assertEqual(self.resolve(call, acknowledge_estimated_cost=False).status_code, 422)
        self.assertEqual(self.resolve(call, maximum_cost='0').status_code, 409)
        original = self.context
        self.context = self.owner_ctx(self._make_context())
        self.assertEqual(self.resolve(call).status_code, 404)
        self.context = original
        self.assertEqual(self.resolve(call).status_code, 202)

    def test_workflow_read_has_bounded_query_count_and_filters_project_in_sql(self):
        project = self.confirmed()
        self.generate(project)
        self.start_round(project)
        statements = []
        def count(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith('SELECT'):
                statements.append(statement)
        event.listen(engine, 'before_cursor_execute', count)
        try:
            result = self.client.get(f"/api/v1/marketing/projects/{project['id']}/workflow?include_polling=true")
        finally:
            event.remove(engine, 'before_cursor_execute', count)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(len(result.json()['current']['assets']), 5)
        self.assertLessEqual(len(statements), 18, f'{len(statements)} reads for five assets')
        scans = [sql for sql in statements if 'asset_versions JOIN' in sql]
        self.assertTrue(scans)
        self.assertTrue(all('JSON_EXTRACT' in sql or 'marketing_intent' in sql for sql in scans))
        self.assertEqual(result.json()['poll_after_ms'], 0)

    def test_polling_tracks_background_activity_and_keeps_quality_gates(self):
        from app.marketing.ai_qc_models import MarketingAIQCRecord
        from app.marketing.ai_qc_worker import execute_ai_qc
        project = self.confirmed()
        self.generate(project)
        row = self.start_round(project)
        path = f"/api/v1/marketing/projects/{project['id']}/workflow?include_polling=true"
        self.assertEqual(self.client.get(path).json()['poll_after_ms'], 0)
        quote = self.client.get(f"/api/v1/marketing/rounds/{row['id']}/ai-qc/quote").json()
        self.command(row, 'ai-qc', {'quote_hash': quote['quote_hash']}, 202)
        self.assertEqual(self.client.get(path).json()['poll_after_ms'], 3000)
        with tenant_session(self.context) as session:
            calls = session.scalars(select(MarketingAIQCRecord).where(MarketingAIQCRecord.round_id == row['id'])).all()
            ids = [call.id for call in calls]
            from app.adapters.persistence.models import JobRecord
            for call in calls:
                session.get(JobRecord, call.job_id).state = 'running'
        for call_id in ids:
            asyncio.run(execute_ai_qc(self.context, call_id, self.blobs))
        queries = []
        def count(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith('SELECT'):
                queries.append(statement)
        event.listen(engine, 'before_cursor_execute', count)
        try:
            result = self.client.get(path).json()
        finally:
            event.remove(engine, 'before_cursor_execute', count)
        self.assertLessEqual(len(queries), 18, f'{len(queries)} reads after visual QC')
        self.assertEqual(result['poll_after_ms'], 0)
        self.assertTrue(all(a['ai_qc']['current'] and not a['ready'] for a in result['current']['assets']))
        with tenant_session(self.context) as session:
            runs = session.scalars(select(GenerationRunRecord).where(GenerationRunRecord.workspace_id == self.context.workspace_id)).all()
            runs[0].status = 'running'
        self.assertEqual(self.client.get(path).json()['poll_after_ms'], 3000)

    def test_resolution_request_replay_uses_one_durable_operation(self):
        _, call, _ = self.uncertain_plan()
        self.context = self.owner_ctx(self.context)
        path = f"/api/v1/marketing/calls/{call['id']}/resolve"
        headers = self.headers()
        body = {'kind': 'planning', 'maximum_cost': '0.10', 'acknowledge_estimated_cost': True, 'note': 'Owner reviewed the missing response.'}
        first = self.client.post(path, headers=headers, json=body)
        second = self.client.post(path, headers=headers, json=body)
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(first.json(), second.json())
        changed = self.client.post(path, headers=headers, json={**body, 'note': 'Different intent'})
        self.assertEqual(changed.status_code, 409)

    def test_exhausted_reconciliation_is_visible_and_retains_original_exposure(self):
        from app.adapters.persistence.models import JobRecord, OperationRecord
        from app.worker_runtime import WorkerRuntime
        from app.platform.jobs import ClaimedJob, _frozen_system_context
        project, call, provider = self.uncertain_plan()
        self.context = self.owner_ctx(self.context)
        response = self.client.post(f"/api/v1/marketing/calls/{call['id']}/resolve", headers=self.headers(),
            json={'kind': 'planning', 'maximum_cost': '0.10', 'acknowledge_estimated_cost': True, 'note': 'Owner reconciliation note'})
        self.assertEqual(response.status_code, 202, response.text)
        operation_id = response.json()['operation_id']
        with tenant_session(self.context) as session:
            job = session.scalar(select(JobRecord).where(JobRecord.idempotency_key == operation_id))
            job.max_attempts = 1
            job.attempt, job.state, job.lease_owner = 1, 'leased', 'recovery-failure'
            claimed = ClaimedJob(job.id, job.type, job.payload_json, 1, 1, job.lease_owner, _frozen_system_context(job))
        # Queue selection has separate concurrency coverage. Bind this failure
        # injection to its own leased job so other fixtures' queues cannot win.
        with patch('app.marketing.recovery.execute_resolution', side_effect=RuntimeError('unavailable')), \
             patch('app.worker_runtime.jobs.claim_next', return_value=claimed):
            asyncio.run(WorkerRuntime(owner='recovery-failure').run_once(job_types=('marketing.resolve_call',)))
        with tenant_session(self.context) as session:
            self.assertEqual(session.get(OperationRecord, operation_id).state, 'failed')
            original = session.get(PlanningCallRecord, call['id'])
            self.assertEqual(original.state, 'uncertain')
            reservation = session.scalar(select(SpendReservationRecord).where(SpendReservationRecord.job_id == original.job_id))
            self.assertEqual(reservation.outstanding_budget_amount, Decimal('0.10'))
        provider.assert_awaited_once()

    def test_running_call_cannot_be_abandoned(self):
        project = self.create()
        call = self.plan_call(project)
        self.context = self.owner_ctx(self.context)
        self.assertEqual(self.resolve(call, maximum_cost='0').status_code, 409)


class TestPollingReadWindow(MarketingAIQCFixtures):
    """A workflow read never reports idle while the round it returns still shows work running (#307).

    Each statement reads the latest commit (READ COMMITTED on PostgreSQL, autocommit reads on SQLite), so a
    Worker that finishes between the round projection and the busy check would leave a stale frame with no
    further poll. These reads commit that Worker inside the window.
    """

    def path(self, project):
        return f"/api/v1/marketing/projects/{project['id']}/workflow?include_polling=true&include_qc_kinds=true&include_board=true"

    def finishing_during_read(self, finish):
        original = workflow.round_dict

        def read(*args, **kwargs):
            result = original(*args, **kwargs)
            finish()
            return result
        return patch('app.marketing.workflow.round_dict', side_effect=read)

    def test_a_paired_check_finishing_during_the_read_keeps_polling(self):
        project, row = self.setup_round()
        self.run_kind(row, 'single')
        row = self.client.get(f"/api/v1/marketing/rounds/{row['id']}").json()['data']
        quote = self.quote(row, 'paired')
        self.assertTrue(quote['available'], quote)
        self.command(row, 'ai-qc', {'quote_hash': quote['quote_hash'], 'kind': 'paired'}, 202)
        with tenant_session(self.context) as session:
            calls = session.scalars(select(MarketingAIQCRecord).where(MarketingAIQCRecord.workspace_id == self.context.workspace_id,
                MarketingAIQCRecord.round_id == row['id'], MarketingAIQCRecord.state == 'queued')
                .order_by(MarketingAIQCRecord.sequence)).all()
            ids = [call.id for call in calls]
            for call in calls:
                session.get(JobRecord, call.job_id).state = 'running'
        self.assertEqual(len(ids), 5)
        # The QC lane runs one check at a time: the last one is still running when the others are done.
        for call_id in ids[:-1]:
            asyncio.run(execute_ai_qc(self.context, call_id, self.blobs))
        with self.finishing_during_read(lambda: asyncio.run(execute_ai_qc(self.context, ids[-1], self.blobs))):
            result = self.client.get(self.path(project)).json()
        running = [a for a in result['current']['assets'] if a['paired_ai_qc']['state'] in ('queued', 'running')]
        self.assertEqual(len(running), 1)
        self.assertEqual(result['poll_after_ms'], 3000)
        result = self.client.get(self.path(project)).json()
        self.assertEqual({a['paired_ai_qc']['state'] for a in result['current']['assets']}, {'succeeded'})
        self.assertEqual(result['poll_after_ms'], 0)

    def test_a_generation_finishing_during_the_read_keeps_polling(self):
        project, _ = self.setup_round(count=4)
        draft = self.client.get(f"/api/v1/generation/drafts/{self.pending_drafts[0]}").json()
        preview = self.client.post(f"/api/v1/generation/drafts/{draft['id']}/previews", headers=self.headers(draft['revision']))
        self.assertEqual(preview.status_code, 201, preview.text)
        run = self.client.post('/api/v1/generation/runs', headers=self.headers(draft['revision']),
            json={'preview_id': preview.json()['id'], 'preview_hash': preview.json()['preview_hash']})
        self.assertEqual(run.status_code, 202, run.text)
        with tenant_session(self.context) as session:
            items = [(item.id, session.scalar(select(JobRecord.id).where(JobRecord.workspace_id == self.context.workspace_id,
                JobRecord.resource_id == item.id))) for item in session.scalars(select(RunItemRecord).where(
                RunItemRecord.workspace_id == self.context.workspace_id, RunItemRecord.generation_run_id == run.json()['generation_run_id'])).all()]
        adapter = AsyncMock()
        adapter.execute.return_value = ProviderCallResult(png_bytes(colour=(12, 34, 56)), 'external-request', False)

        def finish():
            for item_id, job_id in items:
                asyncio.run(execute_run_item(self.context, job_id, item_id, self.blobs, adapter=adapter))
        with self.finishing_during_read(finish):
            result = self.client.get(self.path(project)).json()
        stages = [x['stage'] for a in result['current']['board']['tree']['angles'] for e in a['expressions']
                  for c in e['combinations'] for x in c['executions']]
        self.assertEqual(stages.count('generating'), 1)
        self.assertEqual(result['poll_after_ms'], 3000)
        result = self.client.get(self.path(project)).json()
        self.assertEqual(len(result['current']['assets']), 5)
        self.assertEqual(result['poll_after_ms'], 0)


def test_worker_export_is_not_blocked_by_slow_generation():
    from app.worker_runtime import WorkerRuntime
    from types import SimpleNamespace

    async def scenario():
        runtime = WorkerRuntime(owner='lane-test')
        generated = asyncio.Event()
        release = asyncio.Event()
        exported = asyncio.Event()
        pending = [SimpleNamespace(type='generation.run_item'), SimpleNamespace(type='marketing.export')]
        def claim(owner, lease_seconds=None, *, job_types=None, exclude_types=None):
            for job in pending:
                if (job_types is None or job.type in job_types) and job.type not in (exclude_types or ()):
                    pending.remove(job)
                    return job
            return None
        async def generate(job):
            generated.set()
            await release.wait()
        async def export(job):
            exported.set()
        with patch('app.worker_runtime.jobs.claim_next', side_effect=claim), \
             patch.object(runtime, '_generation_run_item', side_effect=generate), \
             patch.object(runtime, '_marketing_export', side_effect=export):
            worker = asyncio.create_task(runtime.run_forever())
            try:
                await asyncio.wait_for(generated.wait(), 1)
                await asyncio.wait_for(exported.wait(), 0.2)
            finally:
                runtime.stopping = True
                release.set()
                await asyncio.wait_for(worker, 2)
    asyncio.run(scenario())
