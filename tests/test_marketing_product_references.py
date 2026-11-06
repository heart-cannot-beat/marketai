"""Governed product-photo selection reaches generation, QC and feedback intact."""
import asyncio
import unittest
import hashlib
from datetime import datetime,timezone
from unittest.mock import AsyncMock,patch
from sqlalchemy import select
from app import config
from app.adapters.persistence import models as m
from app.adapters.persistence.database import tenant_session
from app.adapters.blob_store_local import LocalFileBlobStore
from app.generation.models import RunItemRecord,GenerationRecipeSnapshotRecord
from app.generation.paid_execution import execute_run_item
from app.marketing.blob_read import write_generated
from app.marketing.service import digest
from app.marketing.product_references import resolve,assert_current,lineage_current,from_product_set,REFERENCE_BRIEF
from app.marketing.service import get_project
from app.marketing.image_editing import execute_edit
from app.platform.canonical import canonical_hash
from types import SimpleNamespace
from app.marketing.ai_qc import frozen_input,normalize_report
from app.marketing import workflow
from app.providers.adapter import ProviderCallResult
import tests.test_marketing_ai_qc as ai_qc_tests
from tests.test_marketing_workflow import MarketingWorkflowFixtures
from tests.support.library_fixtures import png_bytes


AUTO={'reference_version_ids':[],'source':'','confirms_real_product':False}


class ProductPhotoFixtures(MarketingWorkflowFixtures):
    def photo(self,colour=(40,90,180)):
        if not getattr(self,'blobs',None):
            self.blobs=LocalFileBlobStore(root=config.DATA_DIR/m.new_id('photo-blobs'),public_base_url='http://localhost:9000',signing_secret=b'test-only')
        content=png_bytes(colour=colour);sha=hashlib.sha256(content).hexdigest()
        blob=asyncio.run(write_generated(self.blobs,f'committed/{self.context.workspace_id}/reference_derivative/{sha}.png',content,'image/png'))
        with tenant_session(self.context) as s:
            obj=m.ReferenceObjectRecord(id=m.new_id('refobj'),workspace_id=self.context.workspace_id,content_sha256=sha,
                original_object_key=blob.committed_key,declared_mime='image/png',mime_type='image/png',byte_size=len(content),state='released',
                derivative_object_key=blob.committed_key,derivative_storage_version_id=blob.storage_version_id,derivative_sha256=sha,
                derivative_byte_size=len(content),derivative_mime='image/png',execution_mode='mock')
            entry=m.LibraryEntryRecord(id=m.new_id('lib'),workspace_id=self.context.workspace_id,entry_type='reference_asset',display_name='Authoritative product photo fixture',lifecycle='active')
            s.add_all([obj,entry]);s.flush()
            meta={'object_id':obj.id,'rights':{'state':'granted','valid_to':'2099-12-31','scope':'owned product photo'}}
            version=m.LibraryVersionRecord(id=m.new_id('libver'),workspace_id=self.context.workspace_id,entry_id=entry.id,entry_type='reference_asset',
                version_no=1,state='effective',eligibility='eligible',content_json=meta,content_hash=digest(meta),published_by=self.context.initiator_id)
            s.add(version);s.flush();entry.current_version_id=version.id
            return version.id,content

    def prepare(self,project,selection,provider='mock'):
        return self.client.post(f"/api/v1/marketing/projects/{project['id']}/generation-drafts",headers=self.headers(project['revision']),
            json={'provider':provider,'model':'mock-v1' if provider=='mock' else config.OPENAI_IMAGE_MODEL,'product_references':selection})

    def execute_prepared(self,project,delivery_verified=True):
        adapter=AsyncMock()
        async def dispatch(request):
            from app.providers.base import reference_delivery_metadata
            return ProviderCallResult(png_bytes(colour=(50,60,70)),'fixture-provider',False,
                metadata=reference_delivery_metadata(request.reference_inputs) if delivery_verified else {})
        adapter.execute.side_effect=dispatch
        for draft_id in project['generation_draft_ids']:
            draft=self.client.get(f'/api/v1/generation/drafts/{draft_id}').json()
            preview=self.client.post(f'/api/v1/generation/drafts/{draft_id}/previews',headers=self.headers(draft['revision']))
            self.assertEqual(preview.status_code,201,preview.text);p=preview.json()
            result=self.client.post('/api/v1/generation/runs',headers=self.headers(draft['revision']),json={'preview_id':p['id'],'preview_hash':p['preview_hash']})
            self.assertEqual(result.status_code,202,result.text)
            with tenant_session(self.context) as s:
                item=s.scalar(select(RunItemRecord).where(RunItemRecord.generation_run_id==result.json()['generation_run_id']))
                job=s.scalar(select(m.JobRecord).where(m.JobRecord.resource_id==item.id,m.JobRecord.workspace_id==self.context.workspace_id))
                item_id,job_id=item.id,job.id
            from app.platform.tenancy import system_context
            worker_context=system_context(self.context.workspace_id,initiator_id=self.context.initiator_id,initiator_role="owner")
            asyncio.run(execute_run_item(worker_context,job_id,item_id,self.blobs,adapter=adapter))
        return adapter


class TestMarketingProductReferences(ProductPhotoFixtures):
    def test_same_exact_photo_is_frozen_and_sent_to_generation_qc_and_feedback(self):
        self.paid_setup()
        with patch.object(config,'GENERATION_MAX_COSTS_JSON','{"openai":{"'+config.OPENAI_IMAGE_MODEL+'":{"1K":"0.30","2K":"0.50"}}}'):
            project=self.confirmed();photo,content=self.photo()
            selection={'reference_version_ids':[photo],'source':'Manufacturer original, exact adapter / variant, owned fixture','confirms_real_product':True}
            result=self.prepare(project,selection,'openai');self.assertEqual(result.status_code,201,result.text)
            self.assertEqual(result.json()['product_references'],selection)
            adapter=self.execute_prepared(result.json())
            self.assertEqual(adapter.execute.await_count,5)
            for call in adapter.execute.await_args_list:
                self.assertEqual(call.args[0].reference_inputs,(content,))
            with patch('app.marketing.workflow.utcnow',return_value=datetime(2024,12,31,tzinfo=timezone.utc)):
                row=self.start_round(project,mode='byok')
            with tenant_session(self.context) as s:
                frozen=frozen_input(s,self.context,workflow.get_round(s,self.context,row['id']),row['assets'][0]['asset_version_id'],kind='single')
                self.assertTrue(frozen['product_reference_available'])
                refs=[i for i in frozen['images'] if i['role']=='product_reference']
                self.assertEqual(refs[0]['reference_version_id'],photo)
                self.assertEqual(refs[0]['content_sha256'],hashlib.sha256(content).hexdigest())
                normalized=normalize_report(ai_qc_tests.TestMarketingAIQC.report(frozen),frozen)
                fidelity=next(f for f in normalized['findings'] if f['check_id']=='product_fidelity')
                self.assertEqual(fidelity['status'],'pass')
                av=s.get(m.AssetVersionRecord,row['assets'][0]['asset_version_id'])
                self.assertTrue(av.reference_sources_json['sent_to_provider'])
            first=row['assets'][0]
            row=self.command(row,'reviews',{'asset_version_id':first['asset_version_id'],'quality':'needs_changes',
                'checks':{k:'fail' if k=='product_fidelity' else 'pass' for k in first['required_checks']},'note':'Match the connector to the exact real photo.'},201)
            row=self.command(row,'feedback-drafts',{'asset_version_id':first['asset_version_id'],'provider':'openai','model':config.OPENAI_IMAGE_MODEL},201)
            draft=self.client.get('/api/v1/generation/drafts/'+row['feedback_drafts'][-1]['draft_id']).json()
            self.assertEqual(draft['selectors']['product_references'][0]['reference_version_id'],photo)
            self.assertFalse(row['assets'][0]['ready'])

    def test_unattested_cross_tenant_withdrawn_and_expired_photos_are_blocked(self):
        project=self.confirmed();photo,_=self.photo()
        selection={'reference_version_ids':[photo],'source':'Exact product photo','confirms_real_product':False}
        response=self.prepare(project,selection);self.assertEqual(response.status_code,422,response.text)
        selection['confirms_real_product']=True
        with tenant_session(self.context) as s:
            frozen=resolve(s,self.context,selection)
            version=s.get(m.LibraryVersionRecord,photo);meta=dict(version.content_json);meta['rights']={**meta['rights'],'valid_to':'2020-01-01'};version.content_json=meta
        response=self.prepare(project,selection);self.assertEqual(response.status_code,422,response.text)
        with tenant_session(self.context) as s:
            with self.assertRaises(Exception):assert_current(s,self.context,frozen)
        other=self._make_context()
        with tenant_session(other) as s:
            with self.assertRaises(Exception):resolve(s,other,selection)

    def test_other_reference_lineage_formats_keep_their_existing_gates(self):
        with tenant_session(self.context) as s:
            self.assertTrue(lineage_current(s,self.context,{'references':[{'legacy_reference':'preserved'}]}))

    def test_real_photo_is_never_a_substitute_for_an_ad_comparison(self):
        from app.marketing.ai_qc import normalize_report
        inputs={'required_checks':{'product_fidelity':'x','comparison_faithful':'x'},'understanding':{'facts':[]},
            'product_reference_available':True,'comparison_kind':'specific_factor','images':[{'role':'target'},{'role':'product_reference'}]}
        report={'summary':'Visible product','findings':[{'check_id':key,'status':'pass','confidence':'high','visual_evidence':'Visible detail','rationale':'Matches','suggested_fix':'','fact_ids':[]} for key in inputs['required_checks']]}
        result=normalize_report(report,inputs)
        self.assertEqual(next(f for f in result['findings'] if f['check_id']=='comparison_faithful')['status'],'unknown')

    def test_unreadable_photo_fails_before_provider_dispatch_and_releases_the_reservation(self):
        self.paid_setup()
        with patch.object(config,'GENERATION_MAX_COSTS_JSON','{"openai":{"'+config.OPENAI_IMAGE_MODEL+'":{"1K":"0.30","2K":"0.50"}}}'):
            project=self.confirmed();photo,_=self.photo()
            selection={'reference_version_ids':[photo],'source':'Exact controlled fixture','confirms_real_product':True}
            prepared=self.prepare(project,selection,'openai');self.assertEqual(prepared.status_code,201,prepared.text)
            with patch('app.marketing.blob_read.read_committed',side_effect=OSError('fixture unavailable')):
                with self.assertRaises(OSError):
                    self.execute_prepared(prepared.json())
            from app.entitlements.cost_models import ProviderCallAttemptRecord,SpendReservationRecord
            with tenant_session(self.context) as s:
                self.assertEqual(s.scalar(select(ProviderCallAttemptRecord).where(ProviderCallAttemptRecord.workspace_id==self.context.workspace_id)),None)
                reservations=s.scalars(select(SpendReservationRecord).where(SpendReservationRecord.workspace_id==self.context.workspace_id)).all()
                self.assertEqual(len(reservations),1)
                self.assertEqual(reservations[0].state,'released')

    def test_missing_adapter_delivery_evidence_never_publishes_an_asset_as_reference_backed(self):
        self.paid_setup()
        with patch.object(config,'GENERATION_MAX_COSTS_JSON','{"openai":{"'+config.OPENAI_IMAGE_MODEL+'":{"1K":"0.30","2K":"0.50"}}}'):
            project=self.confirmed();photo,_=self.photo()
            selection={'reference_version_ids':[photo],'source':'Exact controlled fixture','confirms_real_product':True}
            response=self.prepare(project,selection,'openai');self.assertEqual(response.status_code,201,response.text)
            prepared=response.json();prepared['generation_draft_ids']=prepared['generation_draft_ids'][:1]
            adapter=self.execute_prepared(prepared,delivery_verified=False)
            self.assertEqual(adapter.execute.await_count,1)
            from app.generation.models import RunItemAssetVersionBindingRecord
            with tenant_session(self.context) as s:
                item=s.scalar(select(RunItemRecord).where(RunItemRecord.workspace_id==self.context.workspace_id))
                self.assertEqual(item.state,'uncertain')
                self.assertIsNone(s.scalar(select(RunItemAssetVersionBindingRecord).where(RunItemAssetVersionBindingRecord.run_item_id==item.id)))


class TestProductReferenceSetSelection(ProductPhotoFixtures):
    """J-22: verified photos of the exact product model are frozen automatically, by hash."""
    MODEL='Acme Charge 65W'

    def setUp(self):
        super().setUp()
        from app.library.contracts import ENTRY_PRODUCT
        _,hero=self._reference_version(self.context,name='Legacy hero')
        _,group=self._group_version(self.context,hero['id'])
        self.entry_id,_=self._publish(self.context,ENTRY_PRODUCT,'Acme Charge',self._product_content(hero['id'],group['id'],
            model_variants=[{'label':self.MODEL,'specs':{}}]))
        with tenant_session(self.context) as s:
            self.product_id=s.scalar(select(m.ProductRecord.id).where(m.ProductRecord.product_profile_entry_id==self.entry_id))

    def bind(self,version_id,role='detail',status='verified_physical',model=None,note=''):
        path=f'/api/v1/products/{self.product_id}/references'
        response=self.client.post(path,headers={'Idempotency-Key':m.new_id('key'),'If-Match':str(self.client.get(path).json()['revision'])},
            json={'reference_version_id':version_id,'model_variant':self.MODEL if model is None else model,'role':role,'identity_status':status,'note':note})
        self.assertEqual(response.status_code,200,response.text)
        return next(i for i in response.json()['items'] if i['reference_version_id']==version_id)

    def create(self):
        response=self.client.post('/api/v1/marketing/projects?include_languages=true&include_plan_review=true',headers=self.headers(),
            json={'source_text':'Acme Charge 65W 充电器\n65W，多口，面向差旅办公用户。','product_id':self.entry_id})
        self.assertEqual(response.status_code,201,response.text)
        return response.json()

    def item(self,item_id,method,body=None):
        path=f'/api/v1/products/{self.product_id}/references'
        response=self.client.request(method,f'{path}/{item_id}',json=body,
            headers={'Idempotency-Key':m.new_id('key'),'If-Match':str(self.client.get(path).json()['revision'])})
        self.assertEqual(response.status_code,200,response.text)

    def recipes(self):
        with tenant_session(self.context) as s:
            return {r.id:(r.recipe_hash,r.canonical_json) for r in s.scalars(select(GenerationRecipeSnapshotRecord).where(
                GenerationRecipeSnapshotRecord.workspace_id==self.context.workspace_id)).all()}

    def board(self,row):
        response=self.client.get(f"/api/v1/marketing/rounds/{row['id']}?include_board=true")
        self.assertEqual(response.status_code,200,response.text)
        board=response.json()['data']['board']
        return board,[n for a in board['tree']['angles'] for x in a['expressions'] for c in x['combinations'] for n in c['executions']]

    def qc_input(self,row,asset_id):
        with tenant_session(self.context) as s:
            return frozen_input(s,self.context,workflow.get_round(s,self.context,row['id']),asset_id,kind='single')

    def next_round(self,project,row):
        """Close this round and confirm a new plan revision, as a user preparing the next small round would."""
        self.command(row,'close',{'reason':'user_stopped','note':'Reference photos changed; prepare a new round'})
        project=self.read(project)
        response=self.edit(project,'confirm_plan',plan=project['plan'])
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def test_verified_main_and_detail_photos_are_frozen_by_hash_and_reach_generation_qc_and_board(self):
        photos={k:self.photo(c) for k,c in [('d1',(10,20,30)),('unverified',(11,21,31)),('logo',(12,22,32)),('mark',(13,23,33)),
            ('main',(14,24,34)),('d2',(15,25,35)),('d3',(16,26,36))]}
        self.bind(photos['d1'][0],note='Detail of the exact port layout')
        self.bind(photos['unverified'][0],status='unverified')
        self.bind(photos['logo'][0],role='logo')
        self.bind(photos['mark'][0],role='certification_mark')
        self.bind(photos['main'][0],role='primary',note='Studio photo of the exact unit')
        self.bind(photos['d2'][0]);self.bind(photos['d3'][0])
        chosen=['main','d1','d2']  # primary first, then details in set order, at most three
        project=self.confirmed()
        response=self.prepare(project,AUTO);self.assertEqual(response.status_code,201,response.text)
        self.assertEqual(response.json()['product_references']['reference_version_ids'],[photos[k][0] for k in chosen])
        adapter=self.execute_prepared(response.json())
        for call in adapter.execute.await_args_list:
            self.assertEqual(call.args[0].reference_inputs,tuple(photos[k][1] for k in chosen))
            self.assertIn(REFERENCE_BRIEF,call.args[0].prompt)
        recipes=self.recipes();self.assertEqual(len(recipes),5)
        for recipe_hash,recipe in recipes.values():
            refs=recipe['selection']['product_references']
            self.assertEqual([r['content_sha256'] for r in refs],[hashlib.sha256(photos[k][1]).hexdigest() for k in chosen])
            self.assertEqual([r['reference_role'] for r in refs],['primary','detail','detail'])
            self.assertEqual({(r['role'],r['attestation'],r['model_variant'],r['selection_policy']) for r in refs},
                {('product_reference','verified_physical_product',self.MODEL,'product-reference-set-v1')})
            self.assertEqual(refs[0]['source'],'Studio photo of the exact unit')
            self.assertEqual(recipe_hash,canonical_hash(recipe))
            altered={**recipe,'selection':{**recipe['selection'],'product_references':[{**refs[0],'content_sha256':'0'*64},*refs[1:]]}}
            self.assertNotEqual(canonical_hash(altered),recipe_hash)
        row=self.start_round(project)
        target=row['assets'][0]['asset_version_id']
        frozen=self.qc_input(row,target)
        self.assertEqual(frozen['images'][0]['role'],'target');self.assertEqual(frozen['images'][0]['asset_version_id'],target)
        self.assertTrue({i['role'] for i in frozen['images'][1:-3]}<={'comparison'})
        self.assertEqual([(i['role'],i['content_sha256']) for i in frozen['images'][-3:]],
            [('product_reference',hashlib.sha256(photos[k][1]).hexdigest()) for k in chosen])
        self.assertTrue(frozen['product_reference_available'])
        without={**frozen,'images':frozen['images'][:-3]}
        self.assertNotEqual(digest(frozen),digest(without))
        report=ai_qc_tests.TestMarketingAIQC.report(frozen)
        next(f for f in report['findings'] if f['check_id']=='facts_supported')['visual_evidence']=(
            'The verified photo shows a UL 2252 certification label and a 500 A rating.')
        findings={f['check_id']:f for f in normalize_report(report,frozen)['findings']}
        self.assertEqual(findings['product_fidelity']['status'],'pass')  # visible appearance only
        self.assertEqual((findings['facts_supported']['status'],findings['facts_supported']['gate_reason']),('unknown','claim_evidence_missing'))
        board,nodes=self.board(row)
        self.assertEqual(board['product_appearance'],{'status':'reference_backed',
            'reference_version_ids':[photos[k][0] for k in chosen],'pending_combination_ids':[]})
        self.assertEqual({(n['product_appearance']['status'],n['product_appearance']['reason'],tuple(n['product_appearance']['reference_version_ids']))
            for n in nodes},{('reference_backed',None,tuple(photos[k][0] for k in chosen))})

    def test_set_changes_keep_frozen_recipes_and_new_round_without_verified_photos_is_pending(self):
        main=self.bind(self.photo((14,24,34))[0],role='primary')
        detail=self.bind(self.photo((10,20,30))[0])
        project=self.confirmed()
        prepared=self.prepare(project,AUTO).json()
        before={d:self.client.get(f'/api/v1/generation/drafts/{d}').json()['selectors']['product_references'] for d in prepared['generation_draft_ids']}
        self.item(main['item_id'],'DELETE')
        self.item(detail['item_id'],'PATCH',{'identity_status':'unverified'})
        replay=self.prepare(project,AUTO)  # the frozen drafts of this plan revision never move to the edited set
        self.assertEqual(replay.status_code,409,replay.text);self.assertEqual(replay.json()['code'],'generation_drafts_already_prepared')
        for draft_id,refs in before.items():
            self.assertEqual(self.client.get(f'/api/v1/generation/drafts/{draft_id}').json()['selectors']['product_references'],refs)
            self.assertEqual(len(refs),2)
        self.execute_prepared(prepared)  # removed from the set, the photos stay released: old recipes still run as frozen
        old=self.recipes()
        self.assertEqual({tuple(r['reference_version_id'] for r in recipe['selection']['product_references']) for _,recipe in old.values()},
            {(main['reference_version_id'],detail['reference_version_id'])})
        row=self.start_round(project)
        self.assertEqual(self.board(row)[0]['product_appearance']['status'],'reference_backed')

        project=self.next_round(project,row)
        response=self.prepare(project,AUTO);self.assertEqual(response.status_code,201,response.text)
        self.assertEqual(response.json()['product_references'],{'reference_version_ids':[],'source':'','confirms_real_product':False})
        board,nodes=self.board(self.start_round(project))
        self.assertEqual(board['product_appearance']['status'],'pending_verification')
        self.assertEqual({(n['asset_version_id'],n['product_appearance']['reason']) for n in nodes},
            {(None,'no_reference')})  # placeholders show the prepared drafts' missing reference
        adapter=self.execute_prepared(response.json())
        self.assertEqual({call.args[0].reference_inputs for call in adapter.execute.await_args_list},{()})
        self.assertTrue(all(REFERENCE_BRIEF not in call.args[0].prompt for call in adapter.execute.await_args_list))
        self.assertEqual({k:v for k,v in self.recipes().items() if k in old},old)
        row=self.client.get(f"/api/v1/marketing/projects/{project['id']}/workflow").json()['current']
        board,nodes=self.board(row)
        self.assertEqual(board['product_appearance'],{'status':'pending_verification','reference_version_ids':[],
            'pending_combination_ids':[c['id'] for c in project['plan']['combinations']]})
        self.assertTrue(all(n['asset_version_id'] and n['product_appearance']['status']=='pending_verification' for n in nodes))
        frozen=self.qc_input(row,row['assets'][0]['asset_version_id'])
        self.assertFalse(frozen['product_reference_available'])
        self.assertFalse([i for i in frozen['images'] if i['role']=='product_reference'])
        report=ai_qc_tests.TestMarketingAIQC.report(frozen)
        fidelity=next(f for f in normalize_report(report,frozen)['findings'] if f['check_id']=='product_fidelity')
        self.assertEqual((fidelity['status'],fidelity['gate_reason']),('unknown','product_reference_missing'))

    def test_logos_unverified_photos_other_models_and_multi_model_products_are_never_selected(self):
        self.bind(self.photo((12,22,32))[0],role='logo')
        self.bind(self.photo((13,23,33))[0],role='certification_mark')
        self.bind(self.photo((14,24,34))[0],role='primary',status='unverified')
        project=self.create()
        with tenant_session(self.context) as s:
            row=get_project(s,self.context,project['id'])
            self.assertEqual(from_product_set(s,self.context,row),[])
        from app.library.contracts import ENTRY_PRODUCT
        _,hero=self._reference_version(self.context,name='Other hero')
        _,group=self._group_version(self.context,hero['id'])
        self.entry_id,_=self._publish(self.context,ENTRY_PRODUCT,'Acme Charge family',self._product_content(hero['id'],group['id'],
            model_variants=[{'label':self.MODEL,'specs':{}},{'label':'Acme Charge 100W','specs':{}}]))
        with tenant_session(self.context) as s:
            self.product_id=s.scalar(select(m.ProductRecord.id).where(m.ProductRecord.product_profile_entry_id==self.entry_id))
        self.bind(self.photo((15,25,35))[0],role='primary')
        project=self.create()
        with tenant_session(self.context) as s:
            row=get_project(s,self.context,project['id'])
            self.assertEqual(from_product_set(s,self.context,row),[])  # which model this test is about is not known

    def test_cross_tenant_revised_source_and_lapsed_rights_never_supply_appearance_truth(self):
        photo,_=self.photo((14,24,34))
        self.bind(photo,role='primary')
        project=self.confirmed()
        prepared=self.prepare(project,AUTO).json()
        self.assertEqual(prepared['product_references']['reference_version_ids'],[photo])
        other=self._make_context()
        with tenant_session(other) as s:  # SQLite has no RLS here: the selector itself scopes every read to the caller
            self.assertEqual(from_product_set(s,other,SimpleNamespace(id=project['id'])),[])
        self.context,mine=other,self.context
        refused=self.client.post('/api/v1/marketing/projects',headers=self.headers(),json={'source_text':'x','product_id':self.entry_id})
        self.assertGreaterEqual(refused.status_code,400,refused.text)
        self.assertEqual(self.client.get(f'/api/v1/products/{self.product_id}/references').status_code,404)
        self.context=mine
        with tenant_session(self.context) as s:
            version=s.get(m.LibraryVersionRecord,photo);meta=dict(version.content_json);meta['rights']={**meta['rights'],'valid_to':'2020-01-01'};version.content_json=meta
        draft=self.client.get(f"/api/v1/generation/drafts/{prepared['generation_draft_ids'][0]}").json()
        preview=self.client.post(f"/api/v1/generation/drafts/{draft['id']}/previews",headers=self.headers(draft['revision']))
        self.assertNotEqual(preview.status_code,201,preview.text)  # a frozen photo whose rights lapsed is never dispatched
        unavailable=self.create()
        with tenant_session(self.context) as s:
            self.assertEqual(from_product_set(s,self.context,get_project(s,self.context,unavailable['id'])),[])
            version=s.get(m.LibraryVersionRecord,photo);meta['rights']={**meta['rights'],'valid_to':'2099-12-31'};version.content_json=dict(meta)
            self.assertEqual(len(from_product_set(s,self.context,get_project(s,self.context,unavailable['id']))),1)
        revised=self.client.patch(f"/api/v1/marketing/projects/{unavailable['id']}",headers=self.headers(unavailable['revision']),
            json={'command':'revise_source','source_text':'A different charger described from scratch.'})
        self.assertEqual(revised.status_code,200,revised.text)
        with tenant_session(self.context) as s:
            self.assertEqual(from_product_set(s,self.context,get_project(s,self.context,unavailable['id'])),[])

    def test_feedback_revision_and_crop_keep_the_exact_frozen_photos_after_the_set_changes(self):
        main_id,main_content=self.photo((14,24,34))
        main=self.bind(main_id,role='primary')
        detail_id,detail=self.photo((10,20,30))
        self.bind(detail_id)
        project=self.confirmed()
        prepared=self.prepare(project,AUTO).json()
        self.execute_prepared(prepared)
        row=self.start_round(project)
        frozen=next(iter(self.recipes().values()))[1]['selection']['product_references']
        self.item(main['item_id'],'DELETE')
        source=row['assets'][0]
        row=self.command(row,'feedback-drafts',{'asset_version_id':source['asset_version_id'],'provider':'mock','model':'mock-v1',
            'note':'Match the connector to the exact real photo.'},201)
        draft=self.client.get('/api/v1/generation/drafts/'+row['feedback_drafts'][-1]['draft_id']).json()
        self.assertEqual(draft['selectors']['product_references'],frozen)
        self.assertIn(REFERENCE_BRIEF,draft['selectors']['prompt'])
        known=set(self.recipes())
        call=self.execute_prepared({'generation_draft_ids':[draft['id']]}).execute.await_args_list[0].args[0]
        self.assertEqual(call.reference_inputs[1:],(main_content,detail))  # the revised source goes first, in its own role
        selection=next(r for k,(_,r) in self.recipes().items() if k not in known)['selection']
        self.assertEqual((selection['source_image']['role'],selection['source_image']['asset_version_id']),('edit_source',source['asset_version_id']))
        self.assertEqual([(r['role'],r['reference_version_id']) for r in selection['product_references']],
            [('product_reference',main_id),('product_reference',detail_id)])
        row=self.command(row,'image-edits',{'asset_version_id':source['asset_version_id'],'crop':{'x':0,'y':0,'width':.5,'height':1},'rotation':0},202)
        asyncio.run(execute_edit(self.context,row['image_edits'][0]['id'],self.blobs))
        child=self.client.get(f"/api/v1/marketing/rounds/{row['id']}",params={'include_image_edits':True}).json()['data']['image_edits'][0]['result_asset_version_id']
        refs=[i for i in self.qc_input(row,child)['images'] if i['role']=='product_reference']
        self.assertEqual([(r['reference_version_id'],r['content_sha256'],r['attestation']) for r in refs],
            [(r['reference_version_id'],r['content_sha256'],r['attestation']) for r in frozen])
        nodes={n['asset_version_id']:n for n in self.board(row)[1]}
        self.assertEqual(nodes[child]['product_appearance']['status'],'reference_backed')
        self.assertEqual(nodes[child]['product_appearance']['reference_version_ids'],[main_id,detail_id])


class TestBoardProductAppearance(unittest.TestCase):
    def test_lapsed_or_missing_photos_keep_their_combinations_pending_and_placeholders_use_the_prepared_draft(self):
        from tests.test_marketing_board import SCREEN,execution,board
        backed={'status':'reference_backed','reason':None,'reference_version_ids':['r1','r2']}
        lapsed={'status':'pending_verification','reason':'reference_not_current','reference_version_ids':['r3']}
        assets=[{**execution(f'v{n}',f'c{n}',n),'product_appearance':backed} for n in (1,2)]+[
            {**execution('v3','c3',3),'product_appearance':lapsed},execution('v4','c4',4)]
        result=board(SCREEN,assets,prepared_appearance={'c5':backed})
        self.assertEqual(result['product_appearance'],{'status':'partial','reference_version_ids':['r1','r2'],
            'pending_combination_ids':['c3','c4']})
        nodes={n['combination_id']:n['product_appearance'] for a in result['tree']['angles'] for x in a['expressions']
            for c in x['combinations'] for n in c['executions']}
        self.assertEqual(nodes['c4'],{'status':'pending_verification','reason':'no_reference','reference_version_ids':[]})
        self.assertEqual(nodes['c5'],backed)
        self.assertEqual(board(SCREEN,[])['product_appearance']['status'],'pending_verification')
