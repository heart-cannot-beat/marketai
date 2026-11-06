"""Real HTTP, images, costs, durable QC and human confirmation; external vision only is faked."""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from sqlalchemy import select
from tests.test_marketing_workflow import MarketingWorkflowFixtures
from app import config
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import JobRecord
from app.entitlements.cost_models import SpendReservationRecord
from app.marketing.ai_qc_models import MarketingAIQCRecord
from app.marketing.ai_qc_worker import execute_ai_qc


class MarketingAIQCFixtures(MarketingWorkflowFixtures):
    def setup_round(self, paid=False, count=None):
        """`count` generates only the first images; the other drafts wait in self.pending_drafts."""
        if paid:
            self.paid_setup()
            self.env = patch.dict('os.environ', {'MARKETAI_MARKETING_QC_PROVIDER':'openai',
                'MARKETAI_MARKETING_QC_MODEL':'vision-test', 'MARKETAI_MARKETING_QC_MAX_COST_USD':'0.10'})
            self.env.start(); self.addCleanup(self.env.stop)
            prices = patch.object(config, 'GENERATION_MAX_COSTS_JSON', '{"openai":{"' + config.OPENAI_IMAGE_MODEL + '":{"1K":"0.30","2K":"0.50"}}}')
            prices.start(); self.addCleanup(prices.stop)
        project = self.confirmed()
        self.pending_drafts = self.generate(project, provider='openai' if paid else 'mock', count=count)
        with patch('app.marketing.workflow.utcnow', return_value=datetime(2024,12,31,tzinfo=timezone.utc)):
            row = self.start_round(project, mode='byok' if paid else 'mock')
        return project, row

    def quote(self, row, kind=None):
        response = self.client.get(f"/api/v1/marketing/rounds/{row['id']}/ai-qc/quote", params={'kind': kind} if kind else None)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def run_kind(self, row, kind, complete=None):
        quote = self.quote(row, kind)
        self.assertTrue(quote['available'], quote)
        row = self.command(row, 'ai-qc', {'quote_hash': quote['quote_hash'], 'kind': kind}, 202)
        return row, self.execute_checks(row, complete)

    def run_both(self, row, complete=None):
        """Single-image checks first, then the paired checks once every comparison object exists."""
        row, single = self.run_kind(row, 'single', complete)
        row = self.client.get(f"/api/v1/marketing/rounds/{row['id']}").json()['data']
        _, paired = self.run_kind(row, 'paired', complete)
        return single + paired

    @staticmethod
    def report(inputs, status='pass'):
        """A faithful answer; a transcribing (single v3) call renders the confirmed copy exactly."""
        from app.marketing.ai_qc_contracts import TRANSCRIBED_POLICIES
        return {**({'rendered_text':{f:inputs['combination'][f] for f in ('headline','body_copy')}}
                   if inputs.get('policy_version') in TRANSCRIBED_POLICIES else {}),
            'summary':'画面主张与文案清楚；请确认产品真实外观。', 'findings':[
            {'check_id':key,'status':status,'confidence':'high','visual_evidence':'观察到画面中的产品和主标题，配套文案一致。',
             'rationale':'与冻结的主张、表达和事实 f1 对照。','suggested_fix':'','fact_ids':['f1'] if key=='facts_supported' else []}
            for key in inputs['required_checks']]}

    def execute_checks(self, row, complete=None):
        with tenant_session(self.context) as session:
            calls = session.scalars(select(MarketingAIQCRecord).where(MarketingAIQCRecord.round_id==row['id'], MarketingAIQCRecord.workspace_id==self.context.workspace_id, MarketingAIQCRecord.state=='queued')).all()
            ids = [call.id for call in calls]
            for call in calls:
                session.get(JobRecord, call.job_id).state='running'
        for call_id in ids:
            asyncio.run(execute_ai_qc(self.context, call_id, self.blobs, **({'complete':complete} if complete else {})))
        return ids


class TestMarketingAIQC(MarketingAIQCFixtures):
    def test_workflow_language_read_projection_does_not_rewrite_frozen_plan(self):
        from copy import deepcopy
        from app.marketing.workflow_models import MarketingRoundRecord
        project, row = self.setup_round()
        with tenant_session(self.context) as session:
            original = deepcopy(session.get(MarketingRoundRecord, row['id']).snapshot_json)
        legacy = self.client.get(f"/api/v1/marketing/rounds/{row['id']}").json()['data']
        opted = self.client.get(f"/api/v1/marketing/rounds/{row['id']}?include_languages=true").json()['data']
        self.assertNotIn('ad_language', legacy['snapshot']['plan'])
        self.assertTrue(all('image_brief' not in combo for combo in legacy['snapshot']['plan']['combinations']))
        self.assertEqual(opted['snapshot']['plan']['ad_language'], 'en-US')
        self.assertEqual(legacy['snapshot']['plan_hash'], opted['snapshot']['plan_hash'])
        with tenant_session(self.context) as session:
            self.assertEqual(session.get(MarketingRoundRecord, row['id']).snapshot_json, original)

    def test_language_failure_is_frozen_and_blocks_confirmation(self):
        project, row = self.setup_round(paid=True)
        async def wrong_language(provider, model, key, inputs, images):
            if inputs['kind'] == 'paired':
                self.assertNotIn('language_correct', inputs['required_checks'])
                return self.report(inputs)
            self.assertEqual(inputs['ad_language'], 'en-US')
            self.assertIn('language_correct', inputs['required_checks'])
            report = self.report(inputs)
            language = next(f for f in report['findings'] if f['check_id'] == 'language_correct')
            language.update(status='fail', visual_evidence='Visible headline contains 中文 while en-US is planned.',
                rationale='The image mixes Chinese and English.', suggested_fix='Render all copy in English.')
            return report
        ids = self.run_both(row, AsyncMock(side_effect=wrong_language))
        current = self.client.get(f"/api/v1/marketing/projects/{project['id']}/workflow").json()['current']
        for asset in current['assets']:
            finding = next(f for f in asset['ai_qc']['result']['findings'] if f['check_id'] == 'language_correct')
            self.assertEqual(finding['status'], 'fail')
            self.assertFalse(asset['ready'])
        rejected = self.client.post(f"/api/v1/marketing/rounds/{row['id']}/ai-qc/confirm",
            headers=self.headers(current['revision']), json={'ai_qc_ids': ids,
                'resolved_checks': {'product_fidelity': 'pass'}, 'note': 'Confirmed product identity only.'})
        self.assertEqual(rejected.status_code, 422, rejected.text)
        self.assertEqual(rejected.json()['code'], 'semantic_qc_unresolved')
        with tenant_session(self.context) as session:
            old_inputs = [(call.id, call.input_hash, __import__('copy').deepcopy(call.input_json)) for call in session.scalars(
                select(MarketingAIQCRecord).where(MarketingAIQCRecord.round_id == row['id']))]
        self.client.get(f"/api/v1/marketing/projects/{project['id']}/workflow")
        with tenant_session(self.context) as session:
            self.assertEqual([(call.id, call.input_hash, call.input_json) for call in session.scalars(
                select(MarketingAIQCRecord).where(MarketingAIQCRecord.round_id == row['id']))], old_inputs)

    def test_unknown_qc_can_be_closed_by_worker_without_changing_provider_certainty(self):
        from app.marketing.recovery import execute_resolution
        from app.entitlements.cost_models import ProviderCallAttemptRecord
        project, row = self.setup_round(paid=True)
        row = self.command(row, 'ai-qc', {'quote_hash': self.quote(row)['quote_hash']}, 202)
        provider = AsyncMock(side_effect=TimeoutError())
        ids = self.execute_checks(row, provider)
        self.context = self.owner_ctx(self.context)
        for call_id in ids:
            response = self.client.post(f'/api/v1/marketing/calls/{call_id}/resolve', headers=self.headers(), json={
                'kind': 'ai_qc', 'maximum_cost': '0.10', 'acknowledge_estimated_cost': True,
                'note': 'Reviewed unknown provider outcome; retain the maximum estimate.'})
            self.assertEqual(response.status_code, 202, response.text)
            execute_resolution(self.context, response.json()['operation_id'])
            asyncio.run(execute_ai_qc(self.context, call_id, self.blobs, complete=provider))
        self.assertEqual(provider.await_count, 5)
        with tenant_session(self.context) as session:
            for call_id in ids:
                call = session.get(MarketingAIQCRecord, call_id)
                self.assertEqual(call.state, 'failed')
                attempt = session.scalar(select(ProviderCallAttemptRecord).where(ProviderCallAttemptRecord.job_id == call.job_id))
                self.assertEqual(attempt.dispatch_state, 'uncertain')
        self.assertTrue(self.quote(row)['available'])
        current = self.client.get(f"/api/v1/marketing/projects/{project['id']}/workflow").json()['current']
        self.assertTrue(all(not a['ready'] for a in current['assets']))

    def test_paid_images_ai_suggestions_bulk_human_confirmation_and_export(self):
        project,row=self.setup_round(paid=True)
        for kind in ('single','paired'):
            quote=self.quote(row,kind)
            self.assertTrue(quote['available'])
            self.assertEqual((quote['kind'],quote['quantity'],quote['waiting']),(kind,5,[]))
            self.assertEqual(Decimal(quote['maximum_cost']),Decimal('0.50'))
        async def vision(provider, model, key, inputs, images):
            self.assertEqual(provider,'openai'); self.assertEqual(model,'vision-test')
            self.assertTrue(key)
            roles=[i['role'] for i in inputs['images']]
            if inputs['kind']=='single':
                self.assertNotIn('comparison',roles)
            else:
                self.assertEqual(roles[0],'target'); self.assertGreater(roles.count('comparison'),0)
                self.assertNotIn('product_reference',roles)
            self.assertTrue(images[0]['bytes'].startswith(b'\xff\xd8'))
            self.assertNotIn('blob', __import__('app.marketing.ai_qc_provider',fromlist=['public_input']).public_input(inputs)['images'][0])
            return self.report(inputs)
        adapter=AsyncMock(side_effect=vision)
        ids=self.run_both(row,adapter)
        self.assertEqual(adapter.await_count,10)
        row=self.client.get(f"/api/v1/marketing/projects/{project['id']}/workflow?include_qc_kinds=true").json()['current']
        self.assertTrue(all(not a['ready'] for a in row['assets']))  # no automatic approval
        for asset in row['assets']:
            check=asset['ai_qc']; self.assertEqual((check['kind'],check['state']),('single','succeeded'))
            finding=next(f for f in check['result']['findings'] if f['check_id']=='product_fidelity')
            self.assertEqual(finding['status'],'unknown')
            self.assertEqual(finding['model_status'],'pass')
            self.assertEqual(check['result']['findings'][0]['check_id'],'file_valid')
            self.assertEqual((asset['paired_ai_qc']['kind'],asset['paired_ai_qc']['state']),('paired','succeeded'))
        response=self.client.post(f"/api/v1/marketing/rounds/{row['id']}/ai-qc/confirm",headers=self.headers(row['revision']),json={'ai_qc_ids':ids})
        self.assertEqual(response.status_code,422,response.text)
        row=self.command(row,'ai-qc/confirm',{'ai_qc_ids':ids,'resolved_checks':{'product_fidelity':'pass'},'note':'已集中核对这些图片中的产品外观与实物一致。'})
        self.assertTrue(all(a['ready'] for a in row['assets']))
        self.assertTrue(all(a['quality']['source']=='ai_assisted_human_review' for a in row['assets']))
        self.assertTrue(all('qc_kinds' not in a['quality'] for a in row['assets']), 'command responses keep the pre-split shape')
        row=self.command(row,'handoff',{'asset_version_ids':[a['asset_version_id'] for a in row['assets']]},202)
        self.assertEqual(row['handoff']['state'],'queued')
        with tenant_session(self.context) as session:
            jobs=[c.job_id for c in session.scalars(select(MarketingAIQCRecord).where(MarketingAIQCRecord.round_id==row['id'])).all()]
            reservations=session.scalars(select(SpendReservationRecord).where(SpendReservationRecord.job_id.in_(jobs))).all()
            self.assertEqual(len(reservations),10)
            self.assertTrue(all(r.state=='settled' for r in reservations))

    def test_same_intent_does_not_duplicate_calls_and_tenant_scope_holds(self):
        project,row=self.setup_round()
        quote=self.quote(row)
        path=f"/api/v1/marketing/rounds/{row['id']}/ai-qc"
        headers=self.headers(row['revision'])
        first=self.client.post(path,headers=headers,json={'quote_hash':quote['quote_hash']})
        replay=self.client.post(path,headers=headers,json={'quote_hash':quote['quote_hash']})
        self.assertEqual(first.status_code,202,first.text); self.assertEqual(first.json(),replay.json())
        with tenant_session(self.context) as session:
            self.assertEqual(len(session.scalars(select(MarketingAIQCRecord).where(MarketingAIQCRecord.round_id==row['id'])).all()),5)
        self.context=self._make_context()
        self.assertEqual(self.client.get(path+'/quote').status_code,404)

    def test_timeout_is_uncertain_keeps_exposure_and_never_retries(self):
        project,row=self.setup_round(paid=True)
        row=self.command(row,'ai-qc',{'quote_hash':self.quote(row)['quote_hash']},202)
        adapter=AsyncMock(side_effect=TimeoutError('provider response missing'))
        ids=self.execute_checks(row,adapter)
        for call_id in ids:
            asyncio.run(execute_ai_qc(self.context,call_id,self.blobs,complete=adapter))
        self.assertEqual(adapter.await_count,5)
        with tenant_session(self.context) as session:
            calls=session.scalars(select(MarketingAIQCRecord).where(MarketingAIQCRecord.round_id==row['id'])).all()
            for call in calls:
                self.assertEqual(call.state,'uncertain')
                reservation=session.scalar(select(SpendReservationRecord).where(SpendReservationRecord.job_id==call.job_id))
                self.assertEqual(reservation.state,'uncertain'); self.assertGreater(reservation.outstanding_budget_amount,0)
        self.assertEqual(self.quote(row)['unavailable_reason'],'qc_in_progress')

    def test_invalid_output_is_not_an_approval_and_explicit_retry_has_new_cost_intent(self):
        project,row=self.setup_round(paid=True)
        row=self.command(row,'ai-qc',{'quote_hash':self.quote(row)['quote_hash']},202)
        ids=self.execute_checks(row,AsyncMock(return_value={'summary':'Everything passes','findings':[]}))
        row=self.client.get(f"/api/v1/marketing/projects/{project['id']}/workflow").json()['current']
        self.assertTrue(all(a['ai_qc']['state']=='failed' and not a['ready'] for a in row['assets']))
        quote=self.quote(row); self.assertTrue(quote['available'])
        row=self.command(row,'ai-qc',{'quote_hash':quote['quote_hash']},202)
        with tenant_session(self.context) as session:
            calls=session.scalars(select(MarketingAIQCRecord).where(MarketingAIQCRecord.round_id==row['id'])).all()
            self.assertEqual(len(calls),10)
            self.assertEqual({c.retry_of_id for c in calls if c.retry_of_id},set(ids))

    def test_mock_is_explicit_and_does_not_call_real_vision_or_auto_pass(self):
        project,row=self.setup_round()
        quote=self.quote(row);self.assertEqual(quote['maximum_cost'],'0')
        row=self.command(row,'ai-qc',{'quote_hash':quote['quote_hash']},202)
        adapter=AsyncMock()
        self.execute_checks(row,adapter);adapter.assert_not_awaited()
        row=self.client.get(f"/api/v1/marketing/projects/{project['id']}/workflow").json()['current']
        for asset in row['assets']:
            self.assertEqual(asset['ai_qc']['result']['source'],'controlled_mock')
            file_check,*findings=asset['ai_qc']['result']['findings']
            self.assertEqual((file_check['check_id'],file_check['status']),('file_valid','pass'))
            self.assertTrue(all(f['status']=='unknown' for f in findings))
            self.assertFalse(asset['ready'])

    def test_ai_failures_cannot_be_bulk_ignored_and_overrides_need_a_reason(self):
        project,row=self.setup_round(paid=True)
        async def failed(provider,model,key,inputs,images):
            report=self.report(inputs)
            for f in report['findings']:
                if f['check_id']=='proposition_clear': f['status']='fail'
            return report
        ids=self.run_both(row,AsyncMock(side_effect=failed))
        row=self.client.get(f"/api/v1/marketing/rounds/{row['id']}").json()['data']
        response=self.client.post(f"/api/v1/marketing/rounds/{row['id']}/ai-qc/confirm",headers=self.headers(row['revision']),json={'ai_qc_ids':ids,'resolved_checks':{'proposition_clear':'pass','product_fidelity':'pass'},'note':'Attempt to ignore the failed proposition'})
        self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(response.json()['code'],'qc_failure_requires_review')
        row=self.client.get(f"/api/v1/marketing/projects/{project['id']}/workflow?include_qc_kinds=true").json()['current']
        asset=row['assets'][0];ai=asset['ai_qc'];paired=asset['paired_ai_qc']
        body={'asset_version_id':asset['asset_version_id'],'quality':'approved','checks':{key:'pass' for key in asset['required_checks']},'note':'Reviewed suggestions','ai_qc_id':ai['id'],'paired_ai_qc_id':paired['id'],'compared_asset_version_ids':paired['compared_asset_version_ids']}
        response=self.client.post(f"/api/v1/marketing/rounds/{row['id']}/reviews",headers=self.headers(row['revision']),json=body)
        self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(response.json()['code'],'qc_override_note_required')
        row=self.command(row,'reviews',{**body,'override_note':'I inspected the exact image: the proposition is present and the real product matches.'},201)
        self.assertTrue(row['assets'][0]['ready'])
        self.assertEqual(row['assets'][0]['quality']['overrides']['proposition_clear'],{'ai':'fail','human':'pass'})

    def test_restart_after_committed_dispatch_never_sends_a_second_request(self):
        project,row=self.setup_round(paid=True)
        row=self.command(row,'ai-qc',{'quote_hash':self.quote(row)['quote_hash']},202)
        async def interrupted(provider,model,key,inputs,images):
            raise KeyboardInterrupt('worker stopped after dispatch')
        with tenant_session(self.context) as session:
            call=session.scalars(select(MarketingAIQCRecord).where(MarketingAIQCRecord.round_id==row['id'])).first()
            call_id=call.id
            session.get(JobRecord,call.job_id).state='running'
        with self.assertRaises(KeyboardInterrupt):
            asyncio.run(execute_ai_qc(self.context,call_id,self.blobs,complete=interrupted))
        adapter=AsyncMock()
        asyncio.run(execute_ai_qc(self.context,call_id,self.blobs,complete=adapter))
        adapter.assert_not_awaited()
        with tenant_session(self.context) as session:
            self.assertEqual(session.get(MarketingAIQCRecord,call_id).state,'uncertain')

    def test_missing_image_releases_reservation_without_calling_the_model(self):
        project,row=self.setup_round(paid=True)
        row=self.command(row,'ai-qc',{'quote_hash':self.quote(row)['quote_hash']},202)
        with tenant_session(self.context) as session:
            call=session.scalars(select(MarketingAIQCRecord).where(MarketingAIQCRecord.round_id==row['id'])).first()
            call_id,job_id=call.id,call.job_id
            session.get(JobRecord,job_id).state='running'
        broken=AsyncMock();broken.read_committed.side_effect=OSError('storage unavailable');adapter=AsyncMock()
        asyncio.run(execute_ai_qc(self.context,call_id,broken,complete=adapter));adapter.assert_not_awaited()
        with tenant_session(self.context) as session:
            call=session.get(MarketingAIQCRecord,call_id)
            self.assertEqual(call.error_code,'qc_image_unavailable')
            reservation=session.scalar(select(SpendReservationRecord).where(SpendReservationRecord.job_id==job_id))
            self.assertEqual(reservation.state,'released')

    def test_multimodal_adapters_send_actual_inline_images_and_structured_schema(self):
        import base64,json,httpx
        from app.marketing.ai_qc_provider import visual_completion, prepare_image
        from tests.support.library_fixtures import png_bytes
        from app.marketing.ai_qc import frozen_input
        import hashlib
        project,row=self.setup_round()
        with tenant_session(self.context) as session:
            from app.marketing.workflow import get_round
            inputs=frozen_input(session,self.context,get_round(session,self.context,row['id']),row['assets'][0]['asset_version_id'],kind='single')
        content=png_bytes();prepared=prepare_image(content,hashlib.sha256(content).hexdigest())
        images=[prepared for _ in inputs['images']]
        original=httpx.AsyncClient
        for provider in ('openai','gemini'):
            requests=[]
            def response(request):
                body=json.loads(request.content);requests.append(body)
                if provider=='openai':
                    parts=body['input'][1]['content'];inline=[p for p in parts if p['type']=='input_image']
                    self.assertEqual(len(inline),len(images));self.assertTrue(body['text']['format']['strict']);self.assertFalse(body['store'])
                    self.assertEqual(base64.b64decode(inline[0]['image_url'].split(',',1)[1]),prepared['bytes'])
                    self.assertEqual(body['service_tier'],'default')
                    return httpx.Response(200,headers={'x-request-id':'real-provider-receipt'},json={'status':'completed','usage':{'input_tokens':1200,'output_tokens':600,'credential':'must-not-survive'},'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(self.report(inputs))}]}]})
                parts=body['contents'][0]['parts'];inline=[p['inlineData'] for p in parts if 'inlineData' in p]
                self.assertEqual(len(inline),len(images));self.assertIn('responseJsonSchema',body['generationConfig'])
                self.assertEqual(base64.b64decode(inline[0]['data']),prepared['bytes'])
                return httpx.Response(200,json={'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':json.dumps(self.report(inputs))}]}}]})
            with patch('app.marketing.ai_qc_provider.httpx.AsyncClient',side_effect=lambda **kwargs:original(transport=httpx.MockTransport(response),**kwargs)):
                result=asyncio.run(visual_completion(provider,'vision-test',b'tenant-only-key',inputs,images))
            self.assertEqual(len(requests),1);self.assertIn('summary',json.loads(result))
            if provider=='openai':
                self.assertEqual(result.usage,{'input_tokens':1200,'output_tokens':600})
                self.assertEqual(result.request_id,'real-provider-receipt')

    def test_missing_reference_never_erases_an_observed_product_failure(self):
        from app.marketing.ai_qc import frozen_input,normalize_report
        from app.marketing.workflow import get_round
        project,row=self.setup_round()
        with tenant_session(self.context) as session:
            inputs=frozen_input(session,self.context,get_round(session,self.context,row['id']),row['assets'][0]['asset_version_id'],kind='single')
        report=self.report(inputs)
        next(f for f in report['findings'] if f['check_id']=='product_fidelity').update(status='fail',rationale='Visible product use contradicts a confirmed fact.')
        normalized=normalize_report(report,inputs)
        finding=next(f for f in normalized['findings'] if f['check_id']=='product_fidelity')
        self.assertEqual(finding['status'],'fail')
        self.assertEqual(normalized['quality_suggestion'],'needs_changes')

    def test_product_name_cannot_support_certification_or_electrical_ratings(self):
        from app.marketing.ai_qc import normalize_report, effective_report
        from app.marketing.workflow import get_round
        from types import SimpleNamespace
        import copy
        project,row=self.setup_round()
        from app.marketing.ai_qc import frozen_input
        with tenant_session(self.context) as session:
            inputs=frozen_input(session,self.context,get_round(session,self.context,row['id']),row['assets'][0]['asset_version_id'],kind='single')
        report=self.report(inputs)
        check=next(f for f in report['findings'] if f['check_id']=='facts_supported')
        check['visual_evidence']='Visible UL 2252 certification, 500 A / 1000 V ratings; the cited fact only identifies the product.'
        normalized=normalize_report(report,inputs)
        finding=next(f for f in normalized['findings'] if f['check_id']=='facts_supported')
        self.assertEqual(finding['status'],'unknown')
        self.assertEqual(finding['model_status'],'pass')
        self.assertEqual(finding['gate_reason'],'claim_evidence_missing')
        self.assertEqual(len(finding['unsupported_claims']),3)
        # Reading a historical receipt tightens the gate without a paid resend
        # or overwriting what the model originally said.
        stored={**report,'source':'ai_visual_suggestion'}
        original=copy.deepcopy(stored)
        call=SimpleNamespace(result_json=stored,input_json=inputs,kind='single')
        self.assertEqual(next(f for f in effective_report(call)['findings'] if f['check_id']=='facts_supported')['status'],'unknown')
        self.assertEqual(stored,original)
        inputs['understanding']['facts'][0].update(statement='UL 2252; 500 A / 1000 V',source_quote='UL 2252; 500 A / 1000 V')
        normalized=normalize_report(report,inputs)
        self.assertEqual(next(f for f in normalized['findings'] if f['check_id']=='facts_supported')['status'],'pass')
        inputs['understanding']['facts'][0].update(statement='UL 22520; 1500 A / 11000 V',source_quote='UL 22520; 1500 A / 11000 V')
        self.assertEqual(next(f for f in normalize_report(report,inputs)['findings'] if f['check_id']=='facts_supported')['status'],'unknown')

    def test_human_correction_survives_old_ai_pass_and_is_not_billed_again(self):
        project,row=self.setup_round(paid=True)
        self.run_both(row,AsyncMock(side_effect=lambda p,m,k,i,images:self.report(i)))
        row=self.client.get(f"/api/v1/marketing/projects/{project['id']}/workflow?include_qc_kinds=true").json()['current']
        target=row['assets'][0];call=target['ai_qc'];paired=target['paired_ai_qc']
        row=self.command(row,'reviews',{'asset_version_id':target['asset_version_id'],'quality':'needs_changes',
            'checks':{key:'fail' if key=='product_fidelity' else 'pass' for key in target['required_checks']},
            'note':'Connector does not match the product photo.','ai_qc_id':call['id'],'paired_ai_qc_id':paired['id'],
            'compared_asset_version_ids':paired['compared_asset_version_ids'],'override_note':'Visible port geometry differs from the authoritative photo.'},201)
        response=self.client.post(f"/api/v1/marketing/rounds/{row['id']}/ai-qc/confirm",headers=self.headers(row['revision']),json={'ai_qc_ids':[call['id'],paired['id']],'resolved_checks':{'product_fidelity':'pass'},'note':'bulk confirmation'})
        self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(response.json()['code'],'human_changes_require_review')
        with patch('app.marketing.ai_qc_provider.system_prompt',return_value='New quality policy'):
            quote=self.quote(row,'single')
        self.assertEqual(quote['quantity'],len(row['assets'])-1)
        self.assertFalse(next(a for a in row['assets'] if a['asset_version_id']==target['asset_version_id'])['ready'])

    def test_correction_request_removes_a_comparison_object_until_it_is_revised(self):
        from app.marketing import ai_qc,workflow
        from app.marketing.service import MarketingError
        project,row=self.setup_round()
        target=row['assets'][0]['asset_version_id']
        with tenant_session(self.context) as session:
            before=ai_qc.frozen_input(session,self.context,workflow.get_round(session,self.context,row['id']),target,kind='paired')
        compared=[i['asset_version_id'] for i in before['images'] if i['role']=='comparison']
        self.assertEqual(len(compared),len(before['comparison_scope']['combination_ids']))
        peer=next(a for a in row['assets'] if a['asset_version_id']==compared[0])
        row=self.command(row,'reviews',{'asset_version_id':peer['asset_version_id'],'quality':'needs_changes','checks':{key:'fail' for key in peer['required_checks']},'note':'Bad execution, use another candidate for comparison.'},201)
        with tenant_session(self.context) as session:
            with self.assertRaises(MarketingError) as waiting:
                ai_qc.frozen_input(session,self.context,workflow.get_round(session,self.context,row['id']),target,kind='paired')
        self.assertEqual(waiting.exception.code,'paired_qc_waiting')
        quote=self.quote(row,'paired')
        self.assertIn({'asset_version_id':target,'awaiting_combination_ids':[peer['combination_id']]},quote['waiting'])
