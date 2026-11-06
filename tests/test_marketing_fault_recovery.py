"""Isolated marketing fault probes. All media/model returns are local fixtures."""
import asyncio
from dataclasses import asdict
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from tests.test_marketing_workflow import MarketingWorkflowFixtures
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import OperationStateEventRecord, new_id
from app.marketing.blob_read import write_generated
from app.marketing.handoff_worker import execute_export
from app.marketing.workflow import active_batches, get_round
from app.marketing.workflow_models import MarketingHandoffRecord, MarketingMetricBatchRecord
from app.platform.operations import get_operation


class ProcessStopped(BaseException):
    """Controlled abrupt termination, outside the Worker's exception handler."""


class TestMarketingFaultRecovery(MarketingWorkflowFixtures):
    def current(self, row):
        response = self.client.get(f"/api/v1/marketing/rounds/{row['id']}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['data']

    def handoff_record(self, row):
        with tenant_session(self.context) as session:
            handoff = session.get(MarketingHandoffRecord, row['handoff']['id'])
            return handoff.state, handoff.operation_id, handoff.blob_json, handoff.manifest_json

    def assert_one_completion(self, operation_id):
        with tenant_session(self.context) as session:
            self.assertEqual(get_operation(session, operation_id).state, 'succeeded')
            events = session.scalars(select(OperationStateEventRecord).where(
                OperationStateEventRecord.operation_id == operation_id)).all()
            self.assertEqual(sum(e.to_state == 'succeeded' for e in events), 1)

    def test_export_commit_acknowledgement_loss_reuses_the_exact_blob_on_explicit_retry(self):
        _, row = self.queued_handoff()
        captured = []

        async def lost_ack(*args):
            captured.append(await write_generated(*args))
            raise OSError('controlled: storage committed, acknowledgement lost')

        with patch('app.marketing.handoff_worker.write_generated', side_effect=lost_ack):
            with self.assertRaises(OSError):
                asyncio.run(execute_export(self.context, row['handoff']['id'], self.blobs))
        state, failed_op, blob, manifest = self.handoff_record(row)
        self.assertEqual(state, 'failed')
        self.assertIsNone(blob)
        row = self.command(self.current(row), 'handoff/retry', status=202)
        asyncio.run(execute_export(self.context, row['handoff']['id'], self.blobs))
        state, op, blob, after = self.handoff_record(row)
        self.assertEqual(state, 'ready')
        self.assertNotEqual(op, failed_op)
        self.assertEqual(blob, asdict(captured[0]))
        self.assertEqual(after, manifest)
        self.assert_one_completion(op)

    def test_export_process_stop_after_blob_commit_recovers_same_operation_and_version(self):
        _, row = self.queued_handoff()
        captured = []

        async def stopped(*args):
            captured.append(await write_generated(*args))
            raise ProcessStopped()

        with patch('app.marketing.handoff_worker.write_generated', side_effect=stopped):
            with self.assertRaises(ProcessStopped):
                asyncio.run(execute_export(self.context, row['handoff']['id'], self.blobs))
        state, op, blob, manifest = self.handoff_record(row)
        self.assertEqual(state, 'queued')
        self.assertIsNone(blob)
        asyncio.run(execute_export(self.context, row['handoff']['id'], self.blobs))
        self.assertEqual(self.handoff_record(row), ('ready', op, asdict(captured[0]), manifest))
        self.assert_one_completion(op)

    def test_duplicate_export_delivery_does_not_reread_or_rewrite_a_ready_package(self):
        _, row = self.ready_handoff()
        before = self.handoff_record(row)
        store = AsyncMock()
        asyncio.run(execute_export(self.context, row['handoff']['id'], store))
        store.open_committed.assert_not_called()
        store.commit_generated_artifact.assert_not_awaited()
        self.assertEqual(self.handoff_record(row), before)
        self.assert_one_completion(before[1])

    def test_truncated_stream_fails_before_zip_commit_and_can_recover(self):
        _, row = self.queued_handoff()
        store = AsyncMock(wraps=self.blobs)

        async def truncated(ref):
            yield b'partial'

        store.open_committed = truncated
        with self.assertRaisesRegex(ValueError, 'blob_integrity_mismatch'):
            asyncio.run(execute_export(self.context, row['handoff']['id'], store))
        store.commit_generated_artifact.assert_not_awaited()
        self.assertEqual(self.handoff_record(row)[0], 'failed')
        self.assertIsNone(self.handoff_record(row)[2])
        row = self.command(self.current(row), 'handoff/retry', status=202)
        asyncio.run(execute_export(self.context, row['handoff']['id'], self.blobs))
        self.assertEqual(self.handoff_record(row)[0], 'ready')

    def test_same_size_corrupt_stream_is_rejected_without_publishing(self):
        _, row = self.queued_handoff()
        store = AsyncMock(wraps=self.blobs)

        async def corrupt(ref):
            content = await self.blobs.read_committed(ref, 20 * 1024 * 1024)
            yield bytes([content[0] ^ 1]) + content[1:]

        store.open_committed = corrupt
        with self.assertRaisesRegex(ValueError, 'blob_integrity_mismatch'):
            asyncio.run(execute_export(self.context, row['handoff']['id'], store))
        store.commit_generated_artifact.assert_not_awaited()
        self.assertEqual(self.handoff_record(row)[0], 'failed')
        self.assertIsNone(self.handoff_record(row)[2])

    def test_simultaneous_export_failure_cannot_undo_a_completed_package(self):
        _, row = self.queued_handoff()
        reached, release = None, None

        async def run_both():
            nonlocal reached, release
            reached, release = asyncio.Event(), asyncio.Event()

            async def failing_read(ref):
                reached.set()
                await release.wait()
                raise OSError('controlled late failure')
                yield  # streaming seam

            store = AsyncMock(wraps=self.blobs)
            store.open_committed = failing_read
            older = asyncio.create_task(execute_export(self.context, row['handoff']['id'], store))
            await reached.wait()
            await execute_export(self.context, row['handoff']['id'], self.blobs)
            release.set()
            with self.assertRaises(OSError):
                await older

        asyncio.run(run_both())
        state, op, blob, _ = self.handoff_record(row)
        self.assertEqual(state, 'ready')
        self.assertIsNotNone(blob)
        self.assert_one_completion(op)

    def test_lost_csv_confirmation_response_replays_without_duplicate_evidence(self):
        _, row = self.bound()
        row = self.command(row, 'metrics/preview', self.csv(row), 201)
        batch_id = row['batches'][-1]['id']
        path = f"/api/v1/marketing/rounds/{row['id']}/metrics/confirm"
        headers = self.headers(row['revision'], new_id('lost_ack'))
        body = {'batch_id': batch_id}
        first = self.client.post(path, headers=headers, json=body)
        replay = self.client.post(path, headers=headers, json=body)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json(), replay.json())
        row = self.command(replay.json()['data'], 'evaluate')
        self.assertEqual(row['evaluation']['round_spend'], '455')
        with tenant_session(self.context) as session:
            batches = active_batches(session, self.context, get_round(session, self.context, row['id']))
            self.assertEqual([b.id for b in batches], [batch_id])

    def test_competing_clean_replacement_previews_are_revalidated_at_confirmation(self):
        _, row = self.bound()
        source = self.csv(row)
        row = self.command(row, 'metrics/preview', source, 201)
        original = row['batches'][-1]['id']
        row = self.command(row, 'metrics/confirm', {'batch_id': original})
        ids = []
        for delta in (1, 2):
            lines = source['csv_text'].splitlines()
            cells = lines[1].split(','); cells[2] = str(int(cells[2]) + delta)
            lines[1] = ','.join(cells)
            row = self.command(row, 'metrics/preview', {**source, 'csv_text': '\n'.join(lines)+'\n', 'replaces_batch_id': original}, 201)
            self.assertEqual(row['batches'][-1]['issues'], [])
            ids.append(row['batches'][-1]['id'])
        row = self.command(row, 'metrics/confirm', {'batch_id': ids[0]})
        before_revision = row['revision']
        error = self.command(row, 'metrics/confirm', {'batch_id': ids[1]}, status=409)
        self.assertEqual(error['code'], 'metrics_preflight_failed')
        self.assertEqual(self.current(row)['revision'], before_revision)
        row = self.command(row, 'evaluate')
        self.assertEqual(row['evaluation']['round_spend'], '456')
        with tenant_session(self.context) as session:
            batches = active_batches(session, self.context, get_round(session, self.context, row['id']))
            self.assertEqual([b.id for b in batches], [ids[0]])
            self.assertEqual(session.get(MarketingMetricBatchRecord, ids[1]).state, 'preview')

    def test_invalid_csv_variants_never_partially_confirm_or_affect_spend(self):
        _, row = self.bound()
        source = self.csv(row)
        text = source['csv_text']
        variants = {
            'nan': text.replace(',10,10000,', ',NaN,10000,'),
            'negative': text.replace(',10,10000,', ',-1,10000,'),
            'infinity': text.replace(',10,10000,', ',Infinity,10000,'),
            'formula': text.replace(',10,10000,', ',=1+1,10000,'),
            'fractional_impressions': text.replace(',10000,100,', ',10000.5,100,'),
            'overflow': text.replace(',10,10000,', ',1000000001,10000,'),
            'over_precision': text.replace(',10,10000,', ',0.0000001,10000,'),
            'malformed_row': text + '2025-01-01,1000,10\n',
        }
        for label, csv_text in variants.items():
            with self.subTest(fault=label):
                row = self.command(row, 'metrics/preview', {**source, 'csv_text': csv_text}, 201)
                self.assertTrue(row['batches'][-1]['issues'])
                error = self.command(row, 'metrics/confirm', {'batch_id': row['batches'][-1]['id']}, status=409)
                self.assertEqual(error['code'], 'metrics_preflight_failed')
                with tenant_session(self.context) as session:
                    self.assertEqual(active_batches(session, self.context, get_round(session, self.context, row['id'])), [])
        row = self.command(row, 'evaluate')
        self.assertEqual(row['evaluation']['round_spend'], '0')
        self.assertTrue(all(result['business_status'] == 'insufficient' for result in row['evaluation']['results']))
        self.assertEqual(row['evaluation']['qualified_asset_version_ids'], [])
