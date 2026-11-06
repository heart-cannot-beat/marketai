"""HTTP regressions for typed responses, reporting context and project history."""
from datetime import datetime, timezone
import asyncio
import copy
import io
import json
import zipfile

from pydantic import ValidationError
from sqlalchemy import update

from tests.test_marketing_workflow import MarketingWorkflowFixtures
from app.adapters.persistence.database import tenant_session
from app.marketing.models import MarketingProjectRecord
from app.marketing.workflow_contracts import WorkflowEnvelope
from app.marketing.workflow_models import MarketingHandoffRecord, MarketingEvaluationRecord
from app.platform.blob_store import CommittedBlobRef
from app.marketing.evaluation import combination_key
from app.marketing.service import digest


class TestMarketingIntegrationContracts(MarketingWorkflowFixtures):
    currency = 'USD'
    window = '7d_click_1d_view'

    def test_old_callers_keep_exact_response_keys_while_new_clients_opt_in(self):
        legacy = self.client.get('/api/v1/marketing/capabilities').json()
        self.assertEqual(set(legacy), {'paid', 'mock_available'})
        extended = self.client.get('/api/v1/marketing/capabilities', params={'include_reporting': True}).json()
        self.assertIn('HKD', extended['media_currencies'])
        project = self.create()
        call = self.plan_call(project)
        self.assertNotIn('maximum_cost', call)
        self.assertNotIn('maximum_cost', self.read(project)['latest_call'])
        projected = self.client.get(f"/api/v1/marketing/projects/{project['id']}", params={'include_recovery': True}).json()
        self.assertEqual(projected['latest_call']['maximum_cost'], '0')
        self.assertEqual(extended['attribution_min_lag_days']['7d_click_1d_view'], 8)
        self.assertEqual(set(self.client.get('/api/v1/marketing/projects').json()), {'items'})
        self.assertEqual(self.client.get('/api/v1/marketing/projects', params={'limit': 50}).json()['next_cursor'], None)

    def confirmed(self):
        project = self.drafted()
        project['plan']['currency'] = self.currency
        response = self.edit(project, 'confirm_plan', plan=project['plan'])
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def start_round(self, project, mode='mock'):
        response = self.client.post(f"/api/v1/marketing/projects/{project['id']}/rounds", headers=self.headers(project['revision']), json={
            'start_date': '2025-01-01', 'end_date': '2025-01-07', 'account_timezone': 'Asia/Hong_Kong',
            'execution_mode': mode, **({'attribution_window': self.window} if self.window != '7d_click_1d_view' else {}),
            'protocol': {'min_days': 7, 'attribution_lag_days': 8}})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()['data']

    def csv(self, row, **overrides):
        return {**super().csv(row), 'currency': self.currency, 'attribution_window': self.window,
            'account_timezone': row['timezone'], **overrides}

    def test_history_reaches_beyond_fifty_and_cursor_is_scoped_and_stable(self):
        ids = [self.create()['id'] for _ in range(55)]
        # Tied timestamps must still have deterministic, non-overlapping pages.
        with tenant_session(self.context) as session:
            session.execute(update(MarketingProjectRecord).where(MarketingProjectRecord.workspace_id == self.context.workspace_id)
                .values(created_at=datetime(2025, 1, 1, tzinfo=timezone.utc)))
        found, cursor = [], None
        while True:
            response = self.client.get('/api/v1/marketing/projects', params={'limit': 20, **({'cursor': cursor} if cursor else {})})
            self.assertEqual(response.status_code, 200, response.text)
            page = response.json()
            found.extend(item['id'] for item in page['items'])
            self.assertIn('next_cursor', page)
            cursor = page['next_cursor']
            if cursor is None:
                break
        self.assertEqual(len(found), 55)
        self.assertEqual(set(found), set(ids))
        first = self.client.get('/api/v1/marketing/projects', params={'limit': 20}).json()
        self.assertEqual(self.client.get('/api/v1/marketing/projects', params={'cursor': first['next_cursor'], 'q': 'different'}).status_code, 400)
        self.context = self._make_context()
        self.assertEqual(self.client.get('/api/v1/marketing/projects', params={'cursor': first['next_cursor']}).status_code, 400)

    def test_search_finds_older_product_and_rejects_bad_cursor(self):
        old = self.create()
        self.client.patch(f"/api/v1/marketing/projects/{old['id']}", headers=self.headers(old['revision']),
            json={'command': 'revise_source', 'source_text': 'Findable older HK charger'})
        for _ in range(51):
            self.create()
        response = self.client.get('/api/v1/marketing/projects', params={'q': 'older HK'})
        self.assertEqual([item['id'] for item in response.json()['items']], [old['id']])
        self.assertEqual(self.client.get('/api/v1/marketing/projects', params={'cursor': 'not-a-cursor'}).status_code, 400)

    def test_non_usd_reporting_retains_units_through_export_and_evaluation(self):
        self.currency, self.window = 'HKD', '1d_click'
        project, row = self.bound()
        self.assertEqual(row['snapshot']['currency'], 'HKD')
        self.assertEqual(row['snapshot']['attribution_window'], '1d_click')
        with tenant_session(self.context) as session:
            handoff = session.get(MarketingHandoffRecord, row['handoff']['id'])
            ref = CommittedBlobRef(**handoff.blob_json)
        with zipfile.ZipFile(io.BytesIO(asyncio.run(self.blobs.read_committed(ref, 1000000)))) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            self.assertEqual(manifest['currency'], 'HKD')
            self.assertEqual(manifest['attribution_window'], '1d_click')
            instructions = archive.read('README.txt').decode()
            self.assertIn('HKD', instructions)
            self.assertIn('1-day click', instructions)
        csv = self.csv(row)
        csv['csv_text'] = csv['csv_text'].replace('spend,', 'Amount spent (HKD),')
        row = self.command(row, 'metrics/preview', csv, 201)
        self.assertEqual(row['batches'][-1]['issues'], [])
        row = self.command(row, 'metrics/confirm', {'batch_id': row['batches'][-1]['id']})
        row = self.command(row, 'evaluate')
        self.assertEqual(row['evaluation']['currency'], 'HKD')
        self.assertEqual(row['evaluation']['attribution_window'], '1d_click')
        self.assertEqual(row['evaluation']['round_spend'], '455')
        # Historical result documents remain immutable; context is hydrated only
        # in the read projection, preserving explanation/evidence hashes.
        with tenant_session(self.context) as session:
            saved = session.get(MarketingEvaluationRecord, row['evaluation']['id'])
            legacy = {k: v for k, v in saved.result_json.items() if k not in ('currency', 'attribution_window', 'account_timezone')}
            saved.result_json = legacy
        reread = self.client.get(f"/api/v1/marketing/projects/{project['id']}/workflow").json()['current']
        self.assertEqual(reread['evaluation']['currency'], 'HKD')
        with tenant_session(self.context) as session:
            self.assertEqual(session.get(MarketingEvaluationRecord, row['evaluation']['id']).result_json, legacy)
        wrong = self.csv(row, currency='USD')
        row = self.command(row, 'metrics/preview', wrong, 201)
        self.assertTrue(any(issue['code'] == 'reporting_context_mismatch' for issue in row['batches'][-1]['issues']))

    def test_csv_rejects_mixed_units_even_with_explicit_column_mapping(self):
        self.currency = 'HKD'
        _, row = self.bound()
        cases = [
            ({'csv_text': self.csv(row)['csv_text'].replace('spend,', 'Amount spent (USD),'),
              'field_mapping': {'spend': 'Amount spent (USD)'}}, 'column_currency_mismatch'),
            ({'csv_text': '\n'.join(line + (',Currency' if i == 0 else ',USD')
                for i, line in enumerate(self.csv(row)['csv_text'].strip().splitlines()))}, 'row_currency_mismatch'),
            ({'csv_text': '\n'.join(line + (',attribution_window' if i == 0 else ',1d_click')
                for i, line in enumerate(self.csv(row)['csv_text'].strip().splitlines()))}, 'row_attribution_mismatch'),
        ]
        for changes, code in cases:
            with self.subTest(code=code):
                row = self.command(row, 'metrics/preview', self.csv(row, **changes), 201)
                self.assertIn(code, [issue['code'] for issue in row['batches'][-1]['issues']])
                rejected = self.command(row, 'metrics/confirm', {'batch_id': row['batches'][-1]['id']}, 409)
                self.assertIsNotNone(rejected)

    def test_frozen_currency_and_attribution_maturity_are_enforced(self):
        project = self.confirmed()
        body = {'start_date': '2025-01-01', 'end_date': '2025-01-07', 'execution_mode': 'mock',
                'protocol': {'attribution_lag_days': 2}}
        response = self.client.post(f"/api/v1/marketing/projects/{project['id']}/rounds", headers=self.headers(project['revision']), json=body)
        self.assertEqual(response.status_code, 422)
        body['attribution_window'] = '1d_click'
        response = self.client.post(f"/api/v1/marketing/projects/{project['id']}/rounds", headers=self.headers(project['revision']), json=body)
        self.assertEqual(response.status_code, 201, response.text)
        row = response.json()['data']
        self.command(row, 'close', {'reason': 'user_stopped', 'note': 'Stop before delivery'})
        plan = copy.deepcopy(project['plan'])
        plan['currency'] = 'HKD'
        response = self.edit(project, 'save_plan', plan=plan)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()['code'], 'big_round_currency_changed')

    def test_default_evidence_key_is_stable_and_other_windows_are_isolated(self):
        project = self.confirmed()
        snapshot = self.start_round(project)['snapshot']
        plan = snapshot['plan']
        combo = plan['combinations'][0]
        angle = next(a for a in plan['angles'] if a['id'] == combo['angle_id'])
        expression = next(e for e in angle['expressions'] if e['id'] == combo['expression_id'])
        historical = digest({'product_understanding_hash': digest(snapshot['understanding']),
            'combination': {k: v for k, v in combo.items() if k not in ('id', 'angle_id', 'expression_id')},
            'proposition': angle['proposition'], 'expression': {k: v for k, v in expression.items() if k != 'id'},
            'context': {k: plan[k] for k in ('audience', 'market', 'placement', 'offer', 'landing_page', 'currency')},
            'metric': plan['rules']['business_metric'], 'threshold': plan['rules']['threshold']})
        self.assertEqual(combination_key(snapshot, {'combination_id': combo['id']}), historical)
        snapshot['attribution_window'] = '1d_click'
        self.assertNotEqual(combination_key(snapshot, {'combination_id': combo['id']}), historical)

    def test_workflow_schema_rejects_missing_or_mistyped_nested_fields(self):
        project = self.confirmed()
        self.start_round(project)
        data = self.client.get(f"/api/v1/marketing/projects/{project['id']}/workflow").json()
        WorkflowEnvelope.model_validate(data)
        data['current']['assets'] = [{'asset_version_id': 'missing-required-details'}]
        with self.assertRaises(ValidationError):
            WorkflowEnvelope.model_validate(data)

    def test_legacy_workflow_shape_survives_rolling_deployment(self):
        project = self.create()
        path = f"/api/v1/marketing/projects/{project['id']}/workflow"
        legacy = self.client.get(path)
        self.assertEqual(legacy.status_code, 200, legacy.text)
        self.assertEqual(set(legacy.json()), {'project_id', 'rounds', 'current', 'next_action'})
        extended = self.client.get(path, params={'include_polling': True})
        self.assertEqual(extended.status_code, 200, extended.text)
        self.assertEqual(extended.json()['poll_after_ms'], 0)
