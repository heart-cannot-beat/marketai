"""Contract fixtures only: no real providers, Meta writes, or customer performance."""
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch
from sqlalchemy import select
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence import models as m
from app.marketing import workflow as w
from app.marketing.evidence_handoff import CreateEvidenceHandoff, create, current_evidence
from app.marketing.service import digest, MarketingError
from app.experiments.winner_eligibility import evaluate_winner_identity, validate_winner_eligibility, WinnerIneligibleError
from app.localization.eligibility import validate_source_for_batch, list_eligible_winners
from app.scale.candidates import list_eligible_candidates
from tests.test_marketing_workflow import MarketingWorkflowFixtures
import tests.test_marketing_followup as followup_tests


class TestMarketingEvidenceHandoff(MarketingWorkflowFixtures):
    def confirmed(self):
        project = self.drafted()
        plan = project['plan']
        plan.update(market='US',placement='feed',landing_page='https://product.example.test/adapter',offer='Exact adapter single unit',media_budget='2000')
        result=self.edit(project,'confirm_plan',plan=plan)
        self.assertEqual(result.status_code,200,result.text)
        return result.json()

    def commercial_versions(self,row):
        with tenant_session(self.context) as s:
            values=[]
            for kind,content in [('product',{'name':row['snapshot']['understanding']['product_name']}),('offer',{})]:
                entry=m.LibraryEntryRecord(id=m.new_id('lib'),workspace_id=self.context.workspace_id,entry_type=kind,
                    display_name=kind,lifecycle='active')
                s.add(entry);s.flush()
                if kind=='offer':
                    content={'name':'Confirmed adapter offer','product_version_id':values[0],'item':{'sku':'adapter','quantity':1},
                        'landing_page_url':row['snapshot']['plan']['landing_page'],'market_scope':['US'],'valid_from':'2024-01-01',
                        'source_market_snapshot':{'market':'US','placement':'feed','country':'US','locale':'en-US','currency':'USD'}}
                version=m.LibraryVersionRecord(id=m.new_id('libver'),workspace_id=self.context.workspace_id,entry_id=entry.id,
                    entry_type=kind,version_no=1,state='effective',eligibility='eligible',content_json=content,content_hash=digest(content),published_by=self.context.initiator_id)
                s.add(version);s.flush();entry.current_version_id=version.id;values.append(version.id)
            return values

    def evaluated_real_contract(self):
        return followup_tests.TestMarketingFollowup.paid_evaluated(self)

    def handoff(self,row,offer_id,**kw):
        body={'evaluation_id':row['evaluation']['id'],'asset_version_id':row['evaluation']['qualified_asset_version_ids'][0],
              'offer_version_id':offer_id,'confirms_tested_context':True,'note':'Isolated fixture: exact product, offer and audience attested. Not real performance.'}
        body.update(kw)
        return self.client.post(f"/api/v1/marketing/rounds/{row['id']}/evidence-handoff",headers=self.headers(row['revision']),json=body)

    def test_exact_asset_handoff_is_explicit_idempotent_localization_eligible_but_not_scale(self):
        row=self.evaluated_real_contract();pv,ov=self.commercial_versions(row)
        path=f"/api/v1/marketing/rounds/{row['id']}/evidence-handoff"
        options=self.client.get(path)
        self.assertEqual(options.status_code,200,options.text)
        self.assertEqual(len(options.json()['data']['assets']),5)
        self.assertTrue(all(not a['scale'] for a in options.json()['data']['assets']))
        failed=self.handoff(row,ov,confirms_tested_context=False)
        self.assertEqual(failed.status_code,422,failed.text)
        response=self.handoff(row,ov);self.assertEqual(response.status_code,201,response.text)
        decision=response.json()['id'];asset=row['evaluation']['qualified_asset_version_ids'][0]
        repeated=self.handoff(row,ov);self.assertEqual(repeated.status_code,201,repeated.text);self.assertEqual(repeated.json()['id'],decision)
        with tenant_session(self.context) as s:
            identity,findings=evaluate_winner_identity(s,self.context,decision_id=decision)
            self.assertEqual(findings,())
            self.assertTrue(validate_source_for_batch(s,self.context,asset,decision,requested_evidence_tier='production').valid)
            self.assertIn(decision,[i.decision_id for i in list_eligible_winners(s,self.context)])
            self.assertNotIn(decision,[i.decision_id for i in list_eligible_candidates(s,self.context)])
            with self.assertRaises(WinnerIneligibleError) as caught:
                validate_winner_eligibility(s,self.context,decision_id=decision)
            self.assertIn('marketing_combination_not_confirmed',[f.code for f in caught.exception.findings])
            self.assertEqual(s.scalar(select(m.JudgementRecord).where(m.JudgementRecord.id==identity.decision.judgement_id)).algorithm_version,'marketing-handoff-v1')
            self.assertFalse(identity.experiment_version.frozen_snapshot_json['marketing_evidence_handoff']['relative_win_claimed'])
            # The native Localization draft freezes this precise new source.
            import tests.test_localization_batch as local_tests
            from app.localization.batch import create_draft, BatchSourceInput, BatchMarketTargetInput
            policy=local_tests._make_market_policy_version(s,'GB')
            draft=create_draft(s,self.context,product_version_id=pv,offer_version_id=ov,truth_data_mode='real',provider_execution_mode='byok',
                sources=[BatchSourceInput(asset_version_id=asset,decision_id=decision)],
                market_targets=[BatchMarketTargetInput(market='GB',market_policy_version_id=policy.id)],
                requested_evidence_tier='production',actor_id=self.context.initiator_id)
            source=s.scalar(select(m.LocalizationBatchSourceRecord).where(m.LocalizationBatchSourceRecord.batch_id==draft.batch_id))
            self.assertEqual(source.decision_id,decision)
            self.assertEqual(source.source_market_snapshot_json['market'],'US')
            self.assertEqual(s.get(m.LocalizationBatchRecord,draft.batch_id).phase,'draft')


    def test_corrected_report_immediately_invalidates_downstream_without_rewriting_history(self):
        row=self.evaluated_real_contract();_,ov=self.commercial_versions(row)
        result=self.handoff(row,ov);self.assertEqual(result.status_code,201,result.text);decision=result.json()['id']
        original=row['evaluation']['id']
        body=self.csv(row,source_mode='customer_file',replaces_batch_id=row['batches'][-1]['id'])
        body['csv_text']=body['csv_text'].replace(',10,10000',',11,10000')
        row=self.command(row,'metrics/preview',body,201)
        row=self.command(row,'metrics/confirm',{'batch_id':row['batches'][-1]['id']})
        with tenant_session(self.context) as s:
            _,findings=evaluate_winner_identity(s,self.context,decision_id=decision)
            self.assertIn('marketing_evidence_stale',[f.code for f in findings])
            self.assertEqual(s.scalar(select(w.Evaluation).where(w.Evaluation.id==original)).id,original)
        options=self.client.get(f"/api/v1/marketing/rounds/{row['id']}/evidence-handoff").json()['data']
        self.assertEqual(options['assets'],[]);self.assertFalse(options['existing'][0]['current'])

    def test_replacing_approval_requires_new_evaluation_before_downstream_reuse(self):
        row=self.evaluated_real_contract();_,ov=self.commercial_versions(row)
        response=self.handoff(row,ov);self.assertEqual(response.status_code,201,response.text)
        decision=response.json()['id'];asset=row['evaluation']['qualified_asset_version_ids'][0]
        from app.generation.review import create_review
        with tenant_session(self.context) as s:
            prior=w.active_quality(s,self.context,asset)
            create_review(s,self.context,asset,'approved','Fixture: new review evidence; preserve prior history',self.context.initiator_id,prior[0].id)
        options=self.client.get(f"/api/v1/marketing/rounds/{row['id']}/evidence-handoff").json()['data']
        self.assertEqual(options['assets'],[])
        self.assertFalse(options['existing'][0]['current'])
        with tenant_session(self.context) as s:
            _,findings=evaluate_winner_identity(s,self.context,decision_id=decision)
            self.assertIn('marketing_evidence_stale',[f.code for f in findings])
            self.assertNotIn(decision,[i.decision_id for i in list_eligible_winners(s,self.context)])
        row=self.command(row,'evaluate')
        self.assertTrue(row['evaluation']['current'])
        renewed=self.handoff(row,ov);self.assertEqual(renewed.status_code,201,renewed.text)
        self.assertNotEqual(renewed.json()['id'],decision)

    def test_context_mismatch_withdrawal_and_tenant_are_not_confirmation_overrides(self):
        row=self.evaluated_real_contract();_,ov=self.commercial_versions(row)
        with tenant_session(self.context) as s:
            offer=s.get(m.LibraryVersionRecord,ov);content=deepcopy(offer.content_json);content['source_market_snapshot']['currency']='EUR';offer.content_json=content
        result=self.handoff(row,ov);self.assertEqual(result.status_code,409,result.text)
        self.assertEqual(result.json()['code'],'handoff_context_mismatch')
        self.context=self._make_context()
        result=self.client.get(f"/api/v1/marketing/rounds/{row['id']}/evidence-handoff")
        self.assertEqual(result.status_code,404,result.text)

    def test_mock_approved_assets_and_ctr_never_qualify(self):
        project,row=self.bound()
        row=self.command(row,'metrics/preview',self.csv(row),201)
        row=self.command(row,'metrics/confirm',{'batch_id':row['batches'][-1]['id']})
        row=self.command(row,'evaluate')
        options=self.client.get(f"/api/v1/marketing/rounds/{row['id']}/evidence-handoff").json()['data']
        self.assertEqual(options['assets'],[])
        with tenant_session(self.context) as s:
            with self.assertRaises(MarketingError):
                current_evidence(s,self.context,row['evaluation']['id'],row['assets'][0]['asset_version_id'])

    def prepare_scale_contract(self, decision_id):
        """Existing truth/reuse pipeline plus a curated sandbox target; no Meta calls."""
        import tests.test_scale_enrollment_candidates as fixtures
        from app.experiments.winner_eligibility import _versioned_hash
        from app.entitlements.admission import PaidOperationAdmission
        from app.entitlements.policy import EntitlementPolicy
        from app.entitlements.quote_engine import CostQuoteEngine
        from app.entitlements.reservation_book import ReservationBook
        from app.scale.delivery_targets import DeliveryTargetRequest, create_delivery_target
        from app.scale.candidates import create_enrollment_candidate
        from app.scale.enrollment import create_draft_enrollment
        from datetime import datetime,timedelta,timezone
        sandbox=replace(self.context,meta_mode='customer_sandbox')
        with tenant_session(self.context) as session:
            identity,findings=evaluate_winner_identity(session,self.context,decision_id=decision_id)
            self.assertEqual(findings,())
            version=identity.experiment_version
            chain={'decision_id':decision_id,'asset_version_id':identity.asset_version.id,'product_id':session.get(m.AssetRecord,identity.asset_version.asset_id).product_id,
                'product_version_id':version.product_version_id,'offer_version_id':version.offer_version_id,
                'market':version.market,'landing_page_evidence_hash':version.landing_page_evidence_hash,
                'attribution_semantics_hash':_versioned_hash('attribution-semantics-v1',{'click_window_days':7,'view_window_days':1})}
        def target_fixture(chain,tag):
            with tenant_session(sandbox) as session:
                upstream=fixtures._make_curated_scenario_context(session,tag)
                now=datetime.now(timezone.utc)
                target=create_delivery_target(session,sandbox,DeliveryTargetRequest(
                    meta_connection_id=upstream['meta_connection_id'],ad_account_id=upstream['ad_account_id'],scenario_scope_key='__no_scenario__',
                    campaign_remote_id='controlled-campaign',adset_remote_id='controlled-adset',ad_remote_id='controlled-ad',
                    ad_creative_id='controlled-creative',creative_asset_version_id=chain['asset_version_id'],creative_binding_evidence_hash='a'*64,
                    market_code='US',placement='feed',attribution_setting=chain['attribution_semantics_hash'],
                    contract_generation_id=upstream['contract_generation_id'],offer_version_id=chain['offer_version_id'],
                    landing_page_version_id=chain['offer_version_id'],source_type='curated_scenario',
                    source_manifest={'schema_version':1,'source_type':'curated_scenario','fixture':'not real delivery'},
                    observed_at=now,expires_at=now+timedelta(days=30),scenario_version_id=upstream['scenario_version_id']))
                return target.id
        admission=PaidOperationAdmission(policy=EntitlementPolicy(),quote_engine=CostQuoteEngine(price_registry=fixtures.PRICE_REGISTRY,price_rule_version='test-v1'),reservation_book=ReservationBook())
        # This marketing source already has an exact approval. The legacy
        # helper's extra synthetic approval would be new, unevaluated evidence.
        with patch.object(fixtures,'CTX',self.context),patch.object(fixtures,'CTX_SANDBOX',sandbox),patch.object(fixtures,'PROD_ID',chain['product_id']),patch.object(fixtures,'_next_market',return_value='US'),patch.object(fixtures,'_build_confirmed_winner',return_value=chain),patch.object(fixtures,'_make_matching_delivery_target',side_effect=target_fixture),patch.object(fixtures,'_seed_review',return_value=None):
            ready=fixtures._build_ready_winner_and_target('marketing-'+decision_id[-8:],admission)
        with tenant_session(sandbox) as session:
            eligible=validate_winner_eligibility(session,sandbox,decision_id=decision_id)
            self.assertEqual(eligible.landing_page_version_id,chain['offer_version_id'])
            choices=list_eligible_candidates(session,sandbox)
            selected=next(item for item in choices if item.decision_id==decision_id)
            self.assertIn(ready['delivery_target_id'],[item['id'] for item in selected.delivery_targets])
            candidate=create_enrollment_candidate(session,sandbox,decision_id=decision_id,delivery_target_id=ready['delivery_target_id'],idempotency_key='marketing-sandbox-candidate')
            enrollment=create_draft_enrollment(session,sandbox,enrollment_candidate_id=candidate.id,idempotency_key='marketing-sandbox-draft')
            self.assertEqual(enrollment.lifecycle,'draft')

    def test_new_execution_and_second_round_enter_scale_discovery_but_stale_support_removes_them(self):
        import asyncio,json
        from datetime import datetime,timezone
        from app import config
        from app.marketing.handoff_worker import execute_export
        self.paid_setup()
        with patch.object(config,"GENERATION_MAX_COSTS_JSON",json.dumps({"openai":{config.OPENAI_IMAGE_MODEL:{"1K":"0.30","2K":"0.50"}}})):
            project=self.confirmed();plan=project['plan']
            previous_evaluation=None
            for ordinal in range(2):
                self.generate(project,provider='openai',colour=(20+ordinal*50,50,70))
                with patch('app.marketing.workflow.utcnow',return_value=datetime(2024,12,31,tzinfo=timezone.utc)):
                    row=self.start_round(project,mode='byok')
                for asset in row['assets']:
                    row=self.command(row,'reviews',{'asset_version_id':asset['asset_version_id'],'quality':'approved',
                        'checks':{k:'pass' for k in asset['required_checks']},'note':'Isolated contract fixture, no real performance'},201)
                row=self.command(row,'handoff',{'asset_version_ids':[a['asset_version_id'] for a in row['assets']]},202)
                asyncio.run(execute_export(self.context,row['handoff']['id'],self.blobs))
                row=self.client.get(f"/api/v1/marketing/projects/{project['id']}/workflow").json()['current']
                for i,entry in enumerate(row['handoff']['entries']):
                    row=self.command(row,'bindings',{'entry_id':entry['entry_id'],'ad_account_id':'123','ad_id':str((ordinal+1)*1000+i),'conditions_match':True},201)
                row=self.command(row,'metrics/preview',self.csv(row,source_mode='customer_file'),201)
                row=self.command(row,'metrics/confirm',{'batch_id':row['batches'][-1]['id']})
                row=self.command(row,'evaluate')
                if ordinal==0:
                    previous_evaluation=row['evaluation']['id']
                    row=self.command(row,'close',{'reason':'asset_only','note':'Only exact ads qualify in this contract fixture'})
                    plan['stage']='confirm_combination';project=self.edit(project,'confirm_plan',plan=plan).json()
            _,offer_id=self.commercial_versions(row)
            result=self.handoff(row,offer_id);self.assertEqual(result.status_code,201,result.text);decision=result.json()['id']
            with tenant_session(self.context) as s:
                items=list_eligible_candidates(s,self.context)
                self.assertIn(decision,[i.decision_id for i in items])
                source_item=next(i for i in items if i.decision_id==decision)
                self.assertEqual(source_item.delivery_targets,())
                self.assertTrue(source_item.delivery_unavailable_reason)
                identity,findings=evaluate_winner_identity(s,self.context,decision_id=decision)
                self.assertEqual(findings,())
                self.assertEqual(identity.experiment_version.frozen_snapshot_json['marketing_evidence_handoff']['scope'],'repeatable_combination')
            self.prepare_scale_contract(decision)
            with tenant_session(self.context) as s:
                identity,_=evaluate_winner_identity(s,self.context,decision_id=decision)
                # A revoked supporting execution invalidates scale, even while the current exact ad still passes.
                prior=s.get(w.Evaluation,previous_evaluation)
                source=next(e for e in prior.input_json['entries'] if e['combination_id']==next(e for e in row['handoff']['entries'] if e['asset_version_id']==identity.asset_version.id)['combination_id'])
                from app.generation.review import create_review
                reviews=w.active_quality(s,self.context,source['asset_version_id'])
                create_review(s,self.context,source['asset_version_id'],'rejected','Fixture revoke',self.context.initiator_id,reviews[0].id)
            with tenant_session(self.context) as s:
                self.assertNotIn(decision,[i.decision_id for i in list_eligible_candidates(s,self.context)])
                with self.assertRaises(WinnerIneligibleError) as err:
                    validate_winner_eligibility(s,self.context,decision_id=decision)
                self.assertIn('marketing_evidence_stale',[f.code for f in err.exception.findings])
            refreshed=self.client.get(f"/api/v1/marketing/rounds/{row['id']}").json()['data']
            self.assertFalse(refreshed['evaluation']['current'])
            refreshed=self.command(refreshed,'evaluate')
            self.assertTrue(refreshed['evaluation']['current'])
            self.assertEqual(refreshed['evaluation']['confirmed_combinations'],[])
