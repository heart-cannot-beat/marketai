"""Next-round decisions resolve historical identities without spending past limits."""
from sqlalchemy import select
from app.adapters.persistence.database import tenant_session
from app.marketing.models import PlanningCallRecord
from tests.test_marketing_workflow import MarketingWorkflowFixtures


class TestMarketingProgression(MarketingWorkflowFixtures):
    def next_call(self, project, **extra):
        return self.client.post(f"/api/v1/marketing/projects/{project['id']}/planning",
            headers=self.headers(project['revision']), json={"kind": "plan", "execution_mode": "mock", **extra})

    def test_next_plan_receives_exact_candidates_and_scoped_delivery_evidence(self):
        project = self.confirmed()
        # Keep enough of the original big-round budget for a second round.
        plan = project['plan'];plan['media_budget'] = '2000'
        project = self.edit(project, 'confirm_plan', plan=plan).json()
        self.generate(project)
        row = self.start_round(project)
        for asset in row['assets']:
            row = self.command(row, 'reviews', {'asset_version_id': asset['asset_version_id'], 'quality': 'approved',
                'checks': {k:'pass' for k in asset['required_checks']}, 'note':'controlled review'}, 201)
        row = self.command(row,'handoff',{'asset_version_ids':[a['asset_version_id'] for a in row['assets']]},202)
        import asyncio
        from app.marketing.handoff_worker import execute_export
        asyncio.run(execute_export(self.context,row['handoff']['id'],self.blobs))
        row = self.client.get(f"/api/v1/marketing/rounds/{row['id']}").json()['data']
        for i,e in enumerate(row['handoff']['entries']):
            row=self.command(row,'bindings',{'entry_id':e['entry_id'],'ad_account_id':'act_123','ad_id':str(1000+i),'conditions_match':True},201)
        row=self.command(row,'metrics/preview',self.csv(row),201)
        row=self.command(row,'metrics/confirm',{'batch_id':row['batches'][-1]['id']})
        row=self.command(row,'close',{'reason':'insufficient_evidence','note':'Only controlled evidence; explore expressions next'})
        response=self.next_call(self.read(project),direction='match_expressions',decision_question='Which expression clarifies this claim?')
        self.assertEqual(response.status_code,202,response.text)
        with tenant_session(self.context) as session:
            inputs=session.get(PlanningCallRecord,response.json()['id']).input_json
        history=inputs['prior_rounds'][0]
        self.assertEqual(history['round_id'],row['id'])
        self.assertEqual(history['stage'],'screen_angles')
        self.assertTrue(history['same_understanding'])
        self.assertEqual(history['execution_mode'],'mock')
        self.assertEqual(history['result']['evidence_status'],'simulated')
        self.assertEqual(len(history['candidates']),5)
        self.assertEqual(history['candidates'][0]['combination'],plan['combinations'][0])
        self.assertEqual(history['candidates'][0]['expression']['name'],plan['angles'][0]['expressions'][0]['name'])
        self.assertFalse(any(e['qualified'] for c in history['candidates'] for e in c['executions']))
        self.assertEqual(inputs['big_round_limits']['remaining_budget'],'1545')
        self.execute(response.json())
        updated=self.read(project)
        self.assertEqual(updated['plan']['stage'],'match_expressions')
        self.assertEqual(updated['plan']['media_budget'],'2000')
        changed = dict(updated['plan'], media_budget='3000')
        rejected = self.edit(updated, 'confirm_plan', plan=changed)
        self.assertEqual(rejected.status_code, 422, rejected.text)
        self.assertEqual(rejected.json()['code'], 'big_round_limits_frozen')
        self.assertEqual(updated['plan']['question'],'Which expression clarifies this claim?')
        self.assertEqual(self.client.get(f"/api/v1/marketing/rounds/{row['id']}?include_languages=true&include_plan_review=true&include_brand_style=true&include_execution_plan=true").json()['data']['snapshot']['plan'],plan)

    def test_active_round_and_exhausted_round_count_block_paid_intent_admission(self):
        project=self.confirmed();row=self.start_round(project)
        self.assertEqual(self.next_call(self.read(project)).json()['code'],'round_still_open')
        self.command(row,'close',{'reason':'insufficient_evidence','note':'No actual delivery'})
        project=self.read(project)
        # Original limit remains immutable, so use three closed rounds.
        for _ in range(2):
            project=self.edit(project,'confirm_plan',plan=project['plan']).json()
            row=self.start_round(project)
            self.command(row,'close',{'reason':'insufficient_evidence','note':'No actual delivery'})
        with tenant_session(self.context) as session:
            before=len(session.scalars(select(PlanningCallRecord)).all())
        projection = self.client.get(f"/api/v1/marketing/projects/{project['id']}/workflow").json()
        self.assertFalse(projection['current']['continuation']['can_plan'])
        self.assertEqual(projection['current']['continuation']['remaining_rounds'], 0)
        self.assertEqual(projection['next_action'], 'summarize_and_end')
        response=self.next_call(self.read(project))
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(response.json()['code'],'big_round_limit')
        with tenant_session(self.context) as session:
            self.assertEqual(len(session.scalars(select(PlanningCallRecord)).all()),before)

    def test_element_optimization_requires_a_specific_decision_question(self):
        response=self.next_call(self.understood(),direction='optimize_elements')
        self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(response.json()['code'],'decision_question_required')
