"""Language separation through persisted planning and generated image previews."""
from copy import deepcopy
from unittest.mock import AsyncMock, patch
import json

from sqlalchemy import select
from app.adapters.persistence.database import tenant_session, platform_session
from app.adapters.persistence.models import WorkspaceRecord
from app.marketing.models import MarketingProjectRecord, MarketingRevisionRecord, PlanningCallRecord
from app.marketing.planner import example_result, system_prompt
from app.marketing.languages import script_matches
from app.marketing.plan_review import legacy_review_projection, legacy_style_projection, plan_document
from app.marketing.workflow_models import MarketingRoundRecord
from tests.test_marketing_planning import MarketingPlanningFixtures
from tests.test_marketing_workflow import MarketingWorkflowFixtures


class TestMarketingLanguages(MarketingPlanningFixtures):
    def test_chinese_strategy_english_ads_and_image_brief_survive_mock_and_preview(self):
        project = self.understood()
        response = self.client.post(f"/api/v1/marketing/projects/{project['id']}/planning",
            headers=self.headers(project['revision']), json={"kind": "plan", "execution_mode": "mock",
                "ad_language": "en-US", "strategy_language": "zh-CN"})
        self.assertEqual(response.status_code, 202, response.text)
        self.execute(response.json())
        project = self.read(project)
        plan = project['plan']
        self.assertEqual(plan['ad_language'], 'en-US')
        self.assertEqual(plan['strategy_language'], 'zh-CN')
        self.assertIn('清楚', plan['angles'][0]['proposition'])
        for combo in plan['combinations']:
            self.assertTrue(script_matches(combo['headline'], 'en-US'))
            self.assertTrue(script_matches(combo['body_copy'], 'en-US'))
            self.assertTrue(script_matches(combo['visual_brief'], 'en-US'))
        project = self.edit(project, 'confirm_plan', plan=plan).json()
        prepared = self.client.post(f"/api/v1/marketing/projects/{project['id']}/generation-drafts",
            headers=self.headers(project['revision']), json={'provider': 'mock', 'model': 'mock-v1'})
        self.assertEqual(prepared.status_code, 201, prepared.text)
        preview = self.client.post(f"/api/v1/generation/drafts/{prepared.json()['generation_draft_ids'][0]}/previews", headers=self.headers(1))
        self.assertEqual(preview.status_code, 201, preview.text)
        prompt = preview.json()['prompt']
        self.assertFalse(__import__('re').search(r'[\u3400-\u9fff]', prompt))
        for heading in ('Proposition:', 'Expression:', 'Scene:', 'Elements:', 'Hold constant:', 'Checks:'):
            self.assertIn(heading, prompt)
        self.assertIn('Render the headline text exactly as: "Explore the product" in en-US.', prompt)

    def test_language_projection_is_opt_in_and_new_writes_still_require_language(self):
        project = self.drafted()
        legacy = self.client.get(f"/api/v1/marketing/projects/{project['id']}")
        self.assertEqual(legacy.status_code, 200, legacy.text)
        self.assertNotIn('language_support', legacy.json())
        for key in ('ad_language', 'strategy_language', 'language_confirmation_required'):
            self.assertNotIn(key, legacy.json()['plan'])
        self.assertTrue(all('image_brief' not in combo for combo in legacy.json()['plan']['combinations']))
        self.assertEqual(project['language_support'], 'v1')
        self.assertIn('ad_language', project['plan'])
        rejected = self.edit(project, 'confirm_plan', plan=legacy.json()['plan'])
        self.assertEqual(rejected.status_code, 422, rejected.text)
        self.assertEqual(self.read(project)['revision'], project['revision'])

    def test_exact_copy_escaping_is_preserved_in_image_brief(self):
        project = self.drafted()
        plan = project['plan']
        plan['combinations'][0].update(headline='Explore "real" use\nToday', body_copy='Use "as described"\nRead the guide.')
        response = self.edit(project, 'confirm_plan', plan=plan)
        self.assertEqual(response.status_code, 200, response.text)
        project = response.json()
        prepared = self.client.post(f"/api/v1/marketing/projects/{project['id']}/generation-drafts",
            headers=self.headers(project['revision']), json={'provider': 'mock', 'model': 'mock-v1'})
        self.assertEqual(prepared.status_code, 201, prepared.text)
        preview = self.client.post(f"/api/v1/generation/drafts/{prepared.json()['generation_draft_ids'][0]}/previews", headers=self.headers(1)).json()
        for key, label in [('headline', 'headline'), ('body_copy', 'body')]:
            expected = json.dumps(plan['combinations'][0][key], ensure_ascii=False)
            self.assertIn(f'Render the {label} text exactly as: {expected} in en-US.', preview['prompt'])

    def test_copy_mixed_script_and_non_english_image_instructions_rejected(self):
        project = self.drafted()
        plan = deepcopy(project['plan'])
        plan['combinations'][0]['headline'] = 'Explore 产品'
        bad = self.edit(project, 'confirm_plan', plan=plan)
        self.assertEqual(bad.status_code, 422, bad.text)
        self.assertEqual(bad.json()['code'], 'copy_language_mismatch')
        plan = deepcopy(project['plan'])
        plan['combinations'][0]['image_brief']['expression'] = '中文画面'
        bad = self.edit(project, 'confirm_plan', plan=plan)
        self.assertEqual(bad.status_code, 422, bad.text)
        self.assertEqual(bad.json()['code'], 'image_brief_language_mismatch')
        self.assertEqual(self.read(project)['revision'], project['revision'])

    def test_legacy_projection_is_explicit_and_preserves_all_stored_history(self):
        project = self.drafted()
        with tenant_session(self.context) as session:
            row = session.get(MarketingProjectRecord, project['id'])
            legacy = deepcopy(row.plan_json)
            for key in ('ad_language', 'strategy_language', 'language_confirmation_required'):
                legacy.pop(key, None)
            for combo in legacy['combinations']:
                combo.pop('image_brief', None)
            legacy['market'] = '北美'
            row.plan_json, row.state = legacy, 'plan_confirmed'
            revisions = [(r.id, deepcopy(r.content_json), r.content_hash) for r in session.scalars(select(MarketingRevisionRecord).where(MarketingRevisionRecord.project_id == row.id))]
        opened = self.read(project)
        self.assertEqual(opened['plan']['ad_language'], 'en-US')
        self.assertTrue(opened['plan']['language_confirmation_required'])
        self.assertIn('ad_language:', opened['understanding']['unknowns'][-1])
        blocked = self.client.post(f"/api/v1/marketing/projects/{project['id']}/generation-drafts",
            headers=self.headers(project['revision']), json={'provider': 'mock', 'model': 'mock-v1'})
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertEqual(blocked.json()['code'], 'ad_language_confirmation_required')
        with tenant_session(self.context) as session:
            self.assertEqual(session.get(MarketingProjectRecord, project['id']).plan_json, legacy)
            self.assertEqual([(r.id, r.content_json, r.content_hash) for r in session.scalars(select(MarketingRevisionRecord).where(MarketingRevisionRecord.project_id == project['id']))], revisions)
        # Manual completion is an explicit new revision; no AI call is needed.
        edited = deepcopy(opened['plan'])
        for combo, original in zip(edited['combinations'], project['plan']['combinations']):
            combo['image_brief'] = original['image_brief']
        confirmed = self.edit(opened, 'confirm_plan', plan=edited)
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertFalse(confirmed.json()['plan']['language_confirmation_required'])
        self.assertGreater(confirmed.json()['revision'], project['revision'])

    def test_legacy_frozen_preview_cannot_admit_new_generation(self):
        from app.generation.selection import check_frozen_selection, SelectionError
        project = self.drafted()
        response = self.edit(project, 'confirm_plan', plan=project['plan'])
        self.assertEqual(response.status_code, 200, response.text)
        project = response.json()
        legacy_recipe = {'selection': {'marketing_intent': {'project_id': project['id'], 'plan_revision': project['revision']}}}
        with tenant_session(self.context) as session:
            with self.assertRaises(SelectionError) as error:
                check_frozen_selection(session, self.context, legacy_recipe)
            self.assertEqual(error.exception.code, 'ad_language_confirmation_required')

    def test_language_commands_do_not_cross_tenants_or_survive_suspension(self):
        project = self.drafted()
        original = self.context
        self.context = self._make_context()
        denied = self.edit(project, 'confirm_plan', plan=project['plan'])
        self.assertEqual(denied.status_code, 404, denied.text)
        denied = self.client.post(f"/api/v1/marketing/projects/{project['id']}/planning",
            headers=self.headers(project['revision']), json={'kind': 'plan', 'execution_mode': 'mock', 'ad_language': 'zh-CN'})
        self.assertEqual(denied.status_code, 404, denied.text)
        self.context = original
        with platform_session() as session:
            session.get(WorkspaceRecord, original.workspace_id).lifecycle = 'suspended'
        denied = self.edit(project, 'confirm_plan', plan=project['plan'])
        self.assertEqual(denied.status_code, 403, denied.text)

    def test_legacy_paid_plan_request_is_rejected_before_cost_or_revision_changes(self):
        self.paid_setup()
        project = self.understood()
        with tenant_session(self.context) as session:
            existing = list(session.scalars(select(PlanningCallRecord.id).where(PlanningCallRecord.project_id == project['id'])))
        with patch.dict('os.environ', {'MARKETAI_PLANNING_PROVIDER': 'openai', 'MARKETAI_PLANNING_MODEL': 'text-test', 'MARKETAI_PLANNING_MAX_COST_USD': '0.10'}):
            cap = self.client.get('/api/v1/marketing/capabilities').json()['paid']
            denied = self.client.post(f"/api/v1/marketing/projects/{project['id']}/planning",
                headers=self.headers(project['revision']), json={'kind': 'plan', 'execution_mode': 'byok', 'quote_hash': cap['quote_hash']})
        self.assertEqual(denied.status_code, 422, denied.text)
        self.assertEqual(denied.json()['code'], 'ad_language_confirmation_required')
        self.assertEqual(self.read(project)['revision'], project['revision'])
        with tenant_session(self.context) as session:
            self.assertEqual(list(session.scalars(select(PlanningCallRecord.id).where(PlanningCallRecord.project_id == project['id']))), existing)

    def test_controlled_openai_adapter_keeps_requested_language_and_rejects_changed_output(self):
        # Exercises the real BYOK dispatch fence, with a controlled completion.
        self.paid_setup()
        project = self.understood()
        with patch.dict('os.environ', {'MARKETAI_PLANNING_PROVIDER': 'openai', 'MARKETAI_PLANNING_MODEL': 'text-test', 'MARKETAI_PLANNING_MAX_COST_USD': '0.10'}):
            cap = self.client.get('/api/v1/marketing/capabilities').json()['paid']
            call = self.plan_call(project, 'plan', mode='byok', quote_hash=cap['quote_hash'])
        output = example_result('plan', {'understanding': project['understanding'], 'ad_language': 'en-US', 'strategy_language': 'en-US'})
        complete = AsyncMock(return_value=json.dumps(output))
        self.execute(call, complete)
        self.assertEqual(self.read(project)['latest_call']['state'], 'succeeded')
        self.assertIn('headline and body_copy MUST use en-US', complete.await_args.args[3])
        self.assertEqual(json.loads(complete.await_args.args[4])['ad_language'], 'en-US')
        current = self.read(project)
        with patch.dict('os.environ', {'MARKETAI_PLANNING_PROVIDER': 'openai', 'MARKETAI_PLANNING_MODEL': 'text-test', 'MARKETAI_PLANNING_MAX_COST_USD': '0.10'}):
            cap = self.client.get('/api/v1/marketing/capabilities').json()['paid']
            second = self.plan_call(current, 'plan', mode='byok', quote_hash=cap['quote_hash'])
        output['ad_language'] = 'zh-CN'
        self.execute(second, AsyncMock(return_value=json.dumps(output)))
        rejected = self.read(project)
        self.assertEqual(rejected['latest_call']['state'], 'failed')
        self.assertEqual(rejected['plan']['ad_language'], 'en-US')
        with tenant_session(self.context) as session:
            failed = session.get(PlanningCallRecord, second['id'])
            self.assertEqual(failed.output_json['validation_errors'][0]['type'], 'planning_language_mismatch')



LANGUAGE_KEYS = ('ad_language', 'strategy_language', 'language_confirmation_required')
# What the test page asks for when it reads the project (`MarketingWorkspace`; `gateway.getMarketingProject`
# reads competitor review with plan review).
PROJECT_READ = ('include_recovery=true&include_languages=true&include_plan_review=true&include_brand_style=true'
    '&include_execution_plan=true&include_competitor_review=true')
# What it asked for when it read a round (`MarketingExecution`) before #367: the frozen plan without review and style.
WORKFLOW_READ_BEFORE_367 = ('include_languages=true&include_image_edits=true&include_version_history=true'
    '&include_board=true&include_execution_plan=true&include_qc_kinds=true&include_board_thumbnails=true'
    '&include_text_language=true&include_qc_evidence=true&include_polling=true')
# What it asks for when it reads a round (`MarketingExecution`, #367). The workflow route has no competitor review.
WORKFLOW_READ = WORKFLOW_READ_BEFORE_367 + '&include_plan_review=true&include_brand_style=true'


class PageReads:
    """The project and its rounds read with exactly the test page's parameters."""

    def read(self, project):
        response = self.client.get(f"/api/v1/marketing/projects/{project['id']}?{PROJECT_READ}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def workflow(self, project, query=WORKFLOW_READ):
        response = self.client.get(f"/api/v1/marketing/projects/{project['id']}/workflow?{query}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


class TestLegacyPlanRounds(PageReads, MarketingWorkflowFixtures):
    """#310: a round frozen from a plan that predates the advertising language (J-02) and the expression details
    (#222). The page follows such a round; these pin the server semantics it reads, without changing any."""

    def legacy_round(self):
        project = self.confirmed()
        with tenant_session(self.context) as session:
            row = session.get(MarketingProjectRecord, project['id'])
            legacy = legacy_style_projection(legacy_review_projection(plan_document(row.plan_json)))
            for key in LANGUAGE_KEYS:
                legacy.pop(key, None)
            for combo in legacy['combinations']:
                combo.pop('image_brief', None)
            legacy['market'] = '北美'
            row.plan_json = legacy
        return project, self.start_round(project)

    def close(self, row):
        return self.command(row, 'close', {'reason': 'insufficient_evidence', 'note': 'Plan predates the advertising language'})

    def test_round_frozen_from_a_plan_without_language_keeps_the_language_unconfirmed(self):
        project, row = self.legacy_round()
        opened = self.read(project)
        self.assertEqual(opened['state'], 'plan_confirmed')
        self.assertEqual(opened['plan']['ad_language'], 'en-US')
        self.assertTrue(opened['plan']['language_confirmation_required'])
        current = self.workflow(project)['current']
        self.assertEqual(current['id'], row['id'])
        # The page treats the plan as this round's when the revisions match.
        self.assertEqual(current['plan_revision'], opened['revision'])
        # The round froze the plan without a language. The page compares the serialized plans, so without the
        # projected language keys the project's plan must equal the frozen one key for key, in order.
        frozen = current['snapshot']['plan']
        self.assertFalse(set(LANGUAGE_KEYS) & set(frozen))
        unprojected = {key: value for key, value in opened['plan'].items() if key not in LANGUAGE_KEYS}
        self.assertEqual(json.dumps(unprojected, ensure_ascii=False), json.dumps(frozen, ensure_ascii=False))
        blocked = self.client.post(f"/api/v1/marketing/projects/{project['id']}/generation-drafts",
            headers=self.headers(opened['revision']), json={'provider': 'mock', 'model': 'mock-v1'})
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertEqual(blocked.json()['code'], 'ad_language_confirmation_required')
        self.assertEqual(self.read(project)['revision'], opened['revision'])

    def test_next_plan_takes_the_chosen_language_without_rewriting_the_frozen_round(self):
        project, row = self.legacy_round()
        self.close(row)
        with tenant_session(self.context) as session:
            snapshot = deepcopy(session.get(MarketingRoundRecord, row['id']).snapshot_json)
            revisions = [(r.id, deepcopy(r.content_json), r.content_hash) for r in session.scalars(
                select(MarketingRevisionRecord).where(MarketingRevisionRecord.project_id == project['id']))]
        opened = self.read(project)
        self.assertTrue(opened['plan']['language_confirmation_required'])
        # The language arrives with the request; the old plan itself is not completed first.
        response = self.client.post(f"/api/v1/marketing/projects/{project['id']}/planning",
            headers=self.headers(opened['revision']), json={'kind': 'plan', 'execution_mode': 'mock',
                'ad_language': 'zh-CN', 'strategy_language': 'zh-CN', 'direction': 'screen_angles'})
        self.assertEqual(response.status_code, 202, response.text)
        self.execute(response.json())
        drafted = self.read(project)
        self.assertEqual(drafted['latest_call']['state'], 'succeeded')
        self.assertEqual(drafted['plan']['ad_language'], 'zh-CN')
        self.assertFalse(drafted['plan']['language_confirmation_required'])
        with tenant_session(self.context) as session:
            frozen = session.get(MarketingRoundRecord, row['id'])
            self.assertEqual(frozen.snapshot_json, snapshot)
            self.assertEqual(frozen.snapshot_json['plan_hash'], row['snapshot']['plan_hash'])
            kept = {r.id: (r.content_json, r.content_hash) for r in session.scalars(
                select(MarketingRevisionRecord).where(MarketingRevisionRecord.project_id == project['id']))}
        for revision_id, content, content_hash in revisions:
            self.assertEqual(kept[revision_id], (content, content_hash))

    def test_another_workspace_can_neither_read_the_round_nor_plan_the_next(self):
        project, row = self.legacy_round()
        self.close(row)
        opened = self.read(project)
        original = self.context
        self.context = self._make_context()
        denied = self.client.get(f"/api/v1/marketing/projects/{project['id']}/workflow?{WORKFLOW_READ}")
        self.assertEqual(denied.status_code, 404, denied.text)
        denied = self.client.post(f"/api/v1/marketing/projects/{project['id']}/planning",
            headers=self.headers(opened['revision']), json={'kind': 'plan', 'execution_mode': 'mock',
                'ad_language': 'zh-CN', 'strategy_language': 'zh-CN', 'direction': 'screen_angles'})
        self.assertEqual(denied.status_code, 404, denied.text)
        self.context = original
        self.assertEqual(self.read(project), opened)
        self.assertEqual(self.workflow(project)['current']['state'], 'closed')


class TestNewPlanRounds(PageReads, MarketingWorkflowFixtures):
    """#367: a plan drafted since #222 / #228 keeps its review, expression details and style. The page offers the
    next plan when its project read equals the ended round's frozen plan; these pin that both reads, with the page's
    parameters, return that plan alike, without changing any server behavior."""

    def ended_round(self):
        project = self.confirmed()
        row = self.start_round(project)
        self.command(row, 'close', {'reason': 'insufficient_evidence', 'note': 'Controlled Mock only'})
        return project, row

    def test_the_project_and_its_ended_round_read_the_same_plan_key_for_key(self):
        project, row = self.ended_round()
        opened = self.read(project)
        current = self.workflow(project)['current']
        self.assertEqual((current['id'], current['state']), (row['id'], 'closed'))
        self.assertEqual(current['plan_revision'], opened['revision'])
        frozen = current['snapshot']['plan']
        # A new plan: the round read now returns what it dropped before the page asked for it.
        for key in ('plan_review', 'guiding_questions', 'style_variation', 'style_notes', 'brand_kit'):
            self.assertIn(key, frozen)
        self.assertTrue(all(key in expression for angle in frozen['angles'] for expression in angle['expressions']
                            for key in ('how_to_tell', 'visual_mechanism', 'evidence_basis')))
        self.assertTrue(all('style' in group for group in frozen['groups']))
        # The page compares the serialized plans: key for key, in order.
        self.assertEqual(json.dumps(opened['plan'], ensure_ascii=False), json.dumps(frozen, ensure_ascii=False))
        before = self.workflow(project, WORKFLOW_READ_BEFORE_367)['current']['snapshot']['plan']
        self.assertNotEqual(json.dumps(opened['plan'], ensure_ascii=False), json.dumps(before, ensure_ascii=False))
        self.assertFalse({'plan_review', 'guiding_questions', 'style_variation'} & set(before))

    def test_another_workspace_cannot_read_the_round_with_the_page_parameters(self):
        project, _ = self.ended_round()
        opened, workflow = self.read(project), self.workflow(project)
        original = self.context
        self.context = self._make_context()
        denied = self.client.get(f"/api/v1/marketing/projects/{project['id']}/workflow?{WORKFLOW_READ}")
        self.assertEqual(denied.status_code, 404, denied.text)
        denied = self.client.get(f"/api/v1/marketing/projects/{project['id']}?{PROJECT_READ}")
        self.assertEqual(denied.status_code, 404, denied.text)
        self.context = original
        self.assertEqual(self.read(project), opened)
        self.assertEqual(self.workflow(project), workflow)


def test_script_checks_cover_mixed_copy_and_preserve_brand_identifiers():
    assert script_matches('Explore the ACME 65W charger.', 'en-US')
    assert not script_matches('Explore the 产品.', 'en-US')
    assert not script_matches('Привет', 'en-US')
    assert script_matches('了解产品与适用条件 ACME', 'zh-CN')
    assert not script_matches('English only', 'zh-CN')


def test_legacy_qc_does_not_gain_language_check_or_new_system_prompt():
    from app.marketing.workflow import required_checks
    from app.marketing.ai_qc_provider import system_prompt
    assert 'language_correct' not in required_checks({'checks': ['Product looks accurate']}, {})
    old_input = {'required_checks': {'product_fidelity': 'Check product'}, 'images': []}
    assert 'language_correct against' not in system_prompt(old_input)
    new_input = {**old_input, 'ad_language': 'en-US', 'required_checks': {'language_correct': 'Check text'}}
    assert 'language_correct against ad_language' in system_prompt(new_input)


def test_legacy_frozen_plan_projection_does_not_rewrite_snapshot_or_hash():
    from app.marketing.contracts import FrozenTestPlan
    from app.marketing.service import digest
    value = example_result('plan', {'understanding': {'audience': 'Audience', 'facts': []}})
    for key in ('ad_language', 'strategy_language', 'language_confirmation_required'):
        value.pop(key, None)
    for combo in value['combinations']:
        combo.pop('image_brief', None)
    before = deepcopy(value)
    original_hash = digest(value)
    projected = FrozenTestPlan.model_validate(value).model_dump(mode='json', exclude_unset=True)
    assert value == before
    assert projected == before
    assert digest(projected) == original_hash


def test_legacy_market_inference_is_a_candidate_even_with_region_details():
    from app.marketing.languages import plan_projection
    assert plan_projection({'market': '北美市场'})['ad_language'] == 'en-US'
    candidate = plan_projection({'market': '中国大陆'})
    assert candidate['ad_language'] == 'zh-CN'
    assert candidate['language_confirmation_required'] is True
