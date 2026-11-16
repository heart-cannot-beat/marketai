"""Ads-first images (ADR 0018, #191): one ad per combination of the ideas, with nothing confirmed first."""
import asyncio

from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import JobRecord, ProductRecord
from app.generation.models import GenerationDraftRecord, RunItemRecord
from app.generation.paid_execution import execute_run_item
from app.generation.selection import SelectionError, check_frozen_selection
from app.library import service as library
from app.library.contracts import ENTRY_PRODUCT
from app.marketing import ideas, service
from app.marketing.ideas_contracts import IdeasProjectDetail
from app.marketing.models import MarketingProjectRecord
from tests.test_marketing_ideas_planning import SOURCE, IdeasPlanningFixtures
from tests.test_marketing_workflow import MarketingWorkflowFixtures

INTENT_KEYS = {"schema_version", "flow", "project_id", "plan_revision", "ideas_hash", "combination", "proposition",
               "expression", "understanding", "cited_fact_ids"}


class IdeasGenerationFixtures(IdeasPlanningFixtures, MarketingWorkflowFixtures):
    def prepare(self, project, provider="mock", revision=None, key=None, **extra):
        return self.client.post(f"/api/v1/marketing/ideas/{project['id']}/generation-drafts",
            headers=self.headers(revision or project["revision"], key),
            json={"provider": provider, "model": "mock-v1", **extra})

    def prepared(self, project, **kwargs):
        response = self.prepare(project, **kwargs)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def preview(self, draft_id):
        draft = self.client.get(f"/api/v1/generation/drafts/{draft_id}").json()
        return draft, self.client.post(f"/api/v1/generation/drafts/{draft_id}/previews", headers=self.headers(draft["revision"]))

    def previewed(self, draft_id):
        draft, response = self.preview(draft_id)
        self.assertEqual(response.status_code, 201, response.text)
        return draft, response.json()

    def submit(self, draft, preview):
        return self.client.post("/api/v1/generation/runs", headers=self.headers(draft["revision"]),
                                json={"preview_id": preview["id"], "preview_hash": preview["preview_hash"]})

    def submitted(self, draft_id):
        """Preview and submit one draft; the run's ``(item_id, job_id)`` pairs, not executed yet."""
        draft, preview = self.previewed(draft_id)
        run = self.submit(draft, preview)
        self.assertEqual(run.status_code, 202, run.text)
        with tenant_session(self.context) as session:
            return [(item.id, session.scalar(select(JobRecord.id).where(JobRecord.workspace_id == self.context.workspace_id,
                                                                        JobRecord.resource_id == item.id)))
                    for item in session.scalars(select(RunItemRecord).where(
                        RunItemRecord.workspace_id == self.context.workspace_id,
                        RunItemRecord.generation_run_id == run.json()["generation_run_id"])).all()]

    def execute_items(self, items):
        from unittest.mock import AsyncMock
        from app import config
        from app.adapters.blob_store_local import LocalFileBlobStore
        from app.adapters.persistence.models import new_id
        from app.providers.adapter import ProviderCallResult
        from tests.support.library_fixtures import png_bytes
        if not getattr(self, "blobs", None):
            self.blobs = LocalFileBlobStore(root=config.DATA_DIR / new_id("blobs"), public_base_url="http://localhost:9000",
                                            signing_secret=b"test-only")
        adapter = AsyncMock()
        adapter.execute.return_value = ProviderCallResult(png_bytes(colour=(12, 34, 56)), "external-request", False)
        for item_id, job_id in items:
            asyncio.run(execute_run_item(self.context, job_id, item_id, self.blobs, adapter=adapter))

    def item_states(self, items):
        with tenant_session(self.context) as session:
            return [(session.get(RunItemRecord, item_id).state, session.get(RunItemRecord, item_id).error_summary)
                    for item_id, _ in items]

    def draft_intent(self, draft_id):
        with tenant_session(self.context) as session:
            return session.get(GenerationDraftRecord, draft_id).selectors_json["marketing_intent"]

    def recipe_intents(self, project_id):
        with tenant_session(self.context) as session:
            row = session.get(MarketingProjectRecord, project_id)
            return [ideas._intent(recipe) for _, recipe in ideas.project_ads(session, self.context, row)]

    def rewrite_ideas(self, project_id, change):
        """Stand in for a re-drafting of the ideas that changes one combination's content."""
        with tenant_session(self.context) as session:
            row = session.get(MarketingProjectRecord, project_id)
            doc = {**row.ideas_json, "combinations": [dict(c) for c in row.ideas_json["combinations"]]}
            change(doc)
            row.ideas_json = doc


class IdeasGenerationTests(IdeasGenerationFixtures):
    def test_prepare_preview_and_submit_need_no_confirmation(self):
        project = self.with_ideas()
        self.assertEqual(project["state"], "ideas_ready")
        self.assertTrue(all(f["confirmation"] == "unconfirmed" for f in project["understanding"]["facts"]))
        prepared = self.prepared(project)
        self.assertEqual((prepared["project_id"], prepared["revision"]), (project["id"], project["revision"]))
        self.assertEqual([d["combination_id"] for d in prepared["drafts"]], [c["id"] for c in project["ideas"]["combinations"]])
        self.run_drafts([d["draft_id"] for d in prepared["drafts"]])
        current = self.read_ideas(project)
        IdeasProjectDetail.model_validate(current)
        self.assertEqual(sorted(ad["combination_id"] for ad in current["ads"]),
                         sorted(c["id"] for c in project["ideas"]["combinations"]))
        self.assertTrue(all(ad["status"] == "draft" and not ad["kept"] for ad in current["ads"]))
        self.assertEqual(current["pending"], [])
        # Nothing was confirmed along the way: the facts stay unchecked and no test plan appears.
        self.assertEqual((current["state"], current["understanding"]), ("ideas_ready", project["understanding"]))
        with tenant_session(self.context) as session:
            self.assertIsNone(session.get(MarketingProjectRecord, project["id"]).plan_json)

    def test_the_recipe_records_the_understanding_and_the_cited_facts(self):
        project = self.with_ideas()
        prepared = self.prepared(project)
        first = prepared["drafts"][0]
        draft_intent = self.draft_intent(first["draft_id"])
        self.assertTrue(INTENT_KEYS <= set(draft_intent), set(draft_intent))
        self.run_drafts([first["draft_id"]])
        [intent] = self.recipe_intents(project["id"])
        self.assertTrue(INTENT_KEYS <= set(intent), set(intent))
        ideas_doc = project["ideas"]
        combination = next(c for c in ideas_doc["combinations"] if c["id"] == first["combination_id"])
        angle = next(a for a in ideas_doc["angles"] if a["id"] == combination["angle_id"])
        expression = next(e for e in angle["expressions"] if e["id"] == combination["expression_id"])
        with tenant_session(self.context) as session:
            stored = session.get(MarketingProjectRecord, project["id"]).ideas_json
        self.assertEqual((intent["schema_version"], intent["flow"], intent["project_id"], intent["plan_revision"]),
                         (2, "ads_first", project["id"], project["revision"]))
        self.assertEqual(intent["ideas_hash"], service.digest(stored))
        self.assertEqual((intent["combination"]["id"], intent["combination"]["angle_id"], intent["combination"]["expression_id"]),
                         (combination["id"], angle["id"], expression["id"]))
        self.assertEqual(intent["proposition"], angle["proposition"])
        self.assertEqual(intent["expression"]["id"], expression["id"])
        # Every fact carries its checking status as it was when the image was made: all unchecked here.
        self.assertEqual([(f["id"], f["confirmation"]) for f in intent["understanding"]["facts"]],
                         [(f["id"], "unconfirmed") for f in project["understanding"]["facts"]])
        cited = set(angle["fact_ids"]) | set(expression["evidence_basis"]["fact_ids"])
        self.assertTrue(cited)
        self.assertEqual(intent["cited_fact_ids"], [f["id"] for f in project["understanding"]["facts"] if f["id"] in cited])
        self.assertEqual(intent["ad_language"], "en-US")

    def test_cited_facts_include_those_behind_a_cited_inference(self):
        project = self.with_ideas()
        facts = [f["id"] for f in project["understanding"]["facts"]]
        inference = project["understanding"]["inferences"][0]
        self.assertTrue(inference["fact_ids"])

        def cite_inference_only(doc):
            for angle in doc["angles"]:
                angle["fact_ids"], angle["inference_ids"] = [], [inference["id"]]
                for expression in angle["expressions"]:
                    expression["evidence_basis"] = {"fact_ids": [], "gap": "Only the inference supports it."}
        self.rewrite_ideas(project["id"], cite_inference_only)
        with tenant_session(self.context) as session:
            row = session.get(MarketingProjectRecord, project["id"])
            intent = ideas.ideas_intent(row, row.ideas_json["combinations"][0]["id"])
        self.assertEqual(intent["cited_fact_ids"], [f for f in facts if f in inference["fact_ids"]])

    def test_images_still_generating_are_pending(self):
        project = self.with_ideas()
        prepared = self.prepared(project)
        first, second = prepared["drafts"][:2]
        items = self.submitted(first["draft_id"])
        current = self.read_ideas(project)
        self.assertEqual(current["ads"], [])
        self.assertEqual(current["pending"], [{"combination_id": first["combination_id"], "state": "queued"}])
        failing = self.submitted(second["draft_id"])
        with tenant_session(self.context) as session:
            session.get(RunItemRecord, failing[0][0]).state = "failed"
        self.execute_items(items)
        current = self.read_ideas(project)
        self.assertEqual([ad["combination_id"] for ad in current["ads"]], [first["combination_id"]])
        self.assertEqual(current["pending"], [{"combination_id": second["combination_id"], "state": "failed"}])
        # A later attempt at the same ad replaces the failed one.
        retry = self.submitted(second["draft_id"])
        self.assertEqual(self.read_ideas(project)["pending"], [{"combination_id": second["combination_id"], "state": "queued"}])
        self.execute_items(retry)
        current = self.read_ideas(project)
        self.assertEqual(sorted(ad["combination_id"] for ad in current["ads"]),
                         sorted((first["combination_id"], second["combination_id"])))
        self.assertEqual(current["pending"], [])

    def test_an_image_under_check_or_stopped_by_it_is_an_ad_not_pending(self):
        project = self.with_ideas()
        prepared = self.prepared(project)
        items = self.submitted(prepared["drafts"][0]["draft_id"])
        self.execute_items(items)
        for state in ("qc_pending", "failed"):  # the image exists: being checked, or blocked by the check
            with tenant_session(self.context) as session:
                session.get(RunItemRecord, items[0][0]).state = state
            current = self.read_ideas(project)
            self.assertEqual([ad["combination_id"] for ad in current["ads"]], [prepared["drafts"][0]["combination_id"]])
            self.assertEqual(current["pending"], [], state)

    def test_attempts_at_replaced_ideas_are_not_pending(self):
        project = self.with_ideas()
        prepared = self.prepared(project)
        first, second = prepared["drafts"][:2]
        self.submitted(first["draft_id"])
        failing = self.submitted(second["draft_id"])
        with tenant_session(self.context) as session:
            session.get(RunItemRecord, failing[0][0]).state = "failed"

        def new_headlines(doc):
            for combination in doc["combinations"][:2]:
                combination["headline"] = "Charge every night, then forget it"
        self.rewrite_ideas(project["id"], new_headlines)
        # The new ideas reuse the combination ids, but nothing was made for them yet.
        self.assertEqual(self.read_ideas(project)["pending"], [])

    def test_keeping_works_on_a_generated_ad(self):
        project = self.with_ideas()
        prepared = self.prepared(project)
        self.run_drafts([prepared["drafts"][0]["draft_id"]])
        [ad] = self.read_ideas(project)["ads"]
        response = self.client.put(f"/api/v1/marketing/ideas/{project['id']}/ads/{ad['asset_version_id']}/kept",
                                   json={"kept": True}, headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([(a["asset_version_id"], a["kept"]) for a in response.json()["ads"]], [(ad["asset_version_id"], True)])
        [summary] = [item for item in self.client.get("/api/v1/marketing/ideas").json()["items"] if item["id"] == project["id"]]
        self.assertEqual((summary["ad_count"], summary["kept_count"]), (1, 1))

    def test_preparing_again_returns_the_same_drafts(self):
        project = self.with_ideas()
        first = self.prepared(project)
        again = self.prepared(project)
        self.assertEqual(again, first)
        conflict = self.prepare(project, aspect_ratio="1:1")
        self.assertEqual((conflict.status_code, conflict.json()["code"]), (409, "ideas_drafts_already_prepared"))
        with tenant_session(self.context) as session:
            self.assertEqual(len(session.scalars(select(GenerationDraftRecord).where(
                GenerationDraftRecord.workspace_id == self.context.workspace_id)).all()), len(first["drafts"]))

    def test_photos_checked_after_preparing_do_not_block_preparing_again(self):
        """Only the settings the client chose can conflict; the product's photos are the application's."""
        from unittest.mock import patch
        project = self.with_ideas()
        first = self.prepared(project)
        checked = [{"reference_version_id": "rv_new", "source": "product_set", "content_sha256": "0" * 64}]
        with patch("app.marketing.product_references.from_product_set", return_value=checked):
            self.assertEqual(self.prepared(project), first)

    def test_the_same_key_replays_the_prepared_drafts(self):
        project = self.with_ideas()
        first = self.prepared(project, key="prepare-ideas-once")
        self.assertEqual(self.prepared(project, key="prepare-ideas-once"), first)

    def test_one_ad_per_way_unless_more_are_asked_for(self):
        project = self.with_ideas()
        drafts = self.prepared(project, variants=2)["drafts"]
        with tenant_session(self.context) as session:
            self.assertEqual({session.get(GenerationDraftRecord, d["draft_id"]).format_json["variants"] for d in drafts}, {2})
        other = self.with_ideas()
        with tenant_session(self.context) as session:
            self.assertEqual({session.get(GenerationDraftRecord, d["draft_id"]).format_json["variants"]
                              for d in self.prepared(other)["drafts"]}, {1})

    def test_ideas_are_needed_first(self):
        project = self.understood_ideas()
        response = self.prepare(project)
        self.assertEqual((response.status_code, response.json()["code"]), (409, "ideas_not_ready"))

    def test_a_drafting_call_in_progress_blocks_preparing(self):
        project = self.with_ideas()
        self.drafted_call(project, "ideas")
        response = self.prepare(self.read_ideas(project))
        self.assertEqual((response.status_code, response.json()["code"]), (409, "planning_in_progress"))

    def test_a_stale_revision_is_refused(self):
        project = self.with_ideas()
        response = self.prepare(project, revision=project["revision"] - 1)
        self.assertEqual((response.status_code, response.json()["code"]), (409, "stale_marketing_revision"))
        missing = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/generation-drafts", headers={"Idempotency-Key": "k-no-if-match"},
                                   json={"provider": "mock", "model": "mock-v1"})
        self.assertEqual(missing.status_code, 428)

    def test_an_unknown_model_is_refused(self):
        project = self.with_ideas()
        response = self.prepare(project, model="not-a-model")
        self.assertEqual((response.status_code, response.json()["code"]), (422, "model_unavailable"))


class IdeasContentCheckTests(IdeasGenerationFixtures):
    """`ads_first` drafts follow the ideas' content, not the project revision (#191)."""

    def test_an_unchanged_combination_survives_a_new_revision(self):
        project = self.with_ideas()
        draft_id = self.prepared(project)["drafts"][0]["draft_id"]
        # Drafting the ideas again with the same content moves the revision on.
        self.execute(self.drafted_call(project, "ideas"))
        current = self.read_ideas(project)
        self.assertGreater(current["revision"], project["revision"])
        self.run_drafts([draft_id])
        [intent] = self.recipe_intents(project["id"])
        self.assertEqual(intent["plan_revision"], current["revision"])

    def test_a_changed_combination_refuses_its_old_draft(self):
        project = self.with_ideas()
        draft = self.prepared(project)["drafts"][0]

        def new_headline(doc):
            doc["combinations"][0]["headline"] = "Charge every night, then forget it"
        self.rewrite_ideas(project["id"], new_headline)
        _, response = self.preview(draft["draft_id"])
        self.assertEqual((response.status_code, response.json()["code"]), (422, "ideas_changed"))

    def test_other_combinations_may_change(self):
        project = self.with_ideas()
        drafts = self.prepared(project)["drafts"]

        def new_headline(doc):
            doc["combinations"][-1]["headline"] = "Charge every night, then forget it"
        self.rewrite_ideas(project["id"], new_headline)
        self.run_drafts([drafts[0]["draft_id"]])
        self.assertEqual(len(self.read_ideas(project)["ads"]), 1)

    def test_drafting_the_understanding_again_refuses_old_drafts(self):
        project = self.with_ideas()
        draft = self.prepared(project)["drafts"][0]
        self.execute(self.drafted_call(project, "understanding"))
        _, response = self.preview(draft["draft_id"])
        self.assertEqual((response.status_code, response.json()["code"]), (422, "ideas_changed"))

    def test_a_change_after_the_preview_refuses_the_submission(self):
        project = self.with_ideas()
        draft_id = self.prepared(project)["drafts"][0]["draft_id"]
        draft, preview = self.previewed(draft_id)

        def new_headline(doc):
            doc["combinations"][0]["headline"] = "Charge every night, then forget it"
        self.rewrite_ideas(project["id"], new_headline)
        response = self.submit(draft, preview)
        self.assertEqual((response.status_code, response.json()["code"]), (422, "ideas_changed"))

    def test_a_change_after_the_submission_stops_the_image(self):
        project = self.with_ideas()
        items = self.submitted(self.prepared(project)["drafts"][0]["draft_id"])

        def new_headline(doc):
            doc["combinations"][0]["headline"] = "Charge every night, then forget it"
        self.rewrite_ideas(project["id"], new_headline)
        self.execute_items(items)
        self.assertEqual(self.item_states(items), [("failed", "ideas_changed")])
        self.assertEqual(self.read_ideas(project)["ads"], [])

    def test_a_frozen_intent_finds_only_a_project_of_its_own_flow(self):
        ideas_project = self.with_ideas()
        plan_project = self.create()
        with tenant_session(self.context) as session:
            # A plan-first intent naming an ads-first project is not found, as before.
            with self.assertRaises(service.MarketingError):
                check_frozen_selection(session, self.context, {"execution_mode": "mock", "selection": {
                    "marketing_intent": {"schema_version": 1, "project_id": ideas_project["id"], "plan_revision": ideas_project["revision"]}}})
            # An ads-first intent naming a plan-first project is not found either.
            with self.assertRaises(SelectionError) as caught:
                check_frozen_selection(session, self.context, {"execution_mode": "mock", "selection": {
                    "marketing_intent": {"schema_version": 2, "flow": "ads_first", "project_id": plan_project["id"],
                                         "combination": {"id": "c1_1"}}}})
            self.assertEqual(caught.exception.code, "ideas_unavailable")


class PlanFirstGenerationTests(IdeasGenerationFixtures):
    def test_a_plan_first_project_without_a_confirmed_plan_still_cannot_generate(self):
        project = self.drafted()
        self.assertNotEqual(project["state"], "plan_confirmed")
        response = self.client.post(f"/api/v1/marketing/projects/{project['id']}/generation-drafts",
            headers=self.headers(project["revision"]), json={"provider": "mock", "model": "mock-v1"})
        self.assertEqual((response.status_code, response.json()["code"]), (409, "plan_not_confirmed"))

    def test_each_flow_prepares_only_its_own_projects(self):
        plan_project = self.drafted()
        self.assertEqual(self.prepare(plan_project).status_code, 404)
        ideas_project = self.with_ideas()
        response = self.client.post(f"/api/v1/marketing/projects/{ideas_project['id']}/generation-drafts",
            headers=self.headers(ideas_project["revision"]), json={"provider": "mock", "model": "mock-v1"})
        self.assertEqual((response.status_code, response.json()["code"]), (404, "marketing_project_not_found"))

    def test_a_confirmed_plan_still_generates_through_its_round_free_path(self):
        project = self.confirmed()
        self.generate(project)
        response = self.client.get(f"/api/v1/marketing/projects/{project['id']}")
        self.assertEqual(response.status_code, 200, response.text)


class IdeasGenerationTenantTests(IdeasGenerationFixtures):
    def test_another_workspace_can_neither_prepare_nor_read_nor_keep(self):
        project = self.with_ideas()
        prepared = self.prepared(project)
        self.run_drafts([prepared["drafts"][0]["draft_id"]])
        [ad] = self.read_ideas(project)["ads"]
        draft_id = prepared["drafts"][1]["draft_id"]
        mine = self.context
        self.context = self._make_context()
        try:
            refused = self.prepare(project)
            self.assertEqual((refused.status_code, refused.json()["code"]), (404, "marketing_project_not_found"))
            self.assertEqual(self.client.get(f"/api/v1/marketing/ideas/{project['id']}").status_code, 404)
            self.assertEqual(self.client.get(f"/api/v1/generation/drafts/{draft_id}").status_code, 404)
            preview = self.client.post(f"/api/v1/generation/drafts/{draft_id}/previews", headers=self.headers(1))
            self.assertEqual(preview.status_code, 404, preview.text)
            keep = self.client.put(f"/api/v1/marketing/ideas/{project['id']}/ads/{ad['asset_version_id']}/kept",
                                   json={"kept": True}, headers=self.headers())
            self.assertEqual(keep.status_code, 404)
            self.assertEqual([i["id"] for i in self.client.get("/api/v1/marketing/ideas").json()["items"]], [])
        finally:
            self.context = mine
        current = self.read_ideas(project)
        self.assertEqual([(a["asset_version_id"], a["kept"]) for a in current["ads"]], [(ad["asset_version_id"], False)])
        with tenant_session(self.context) as session:
            self.assertEqual(len(session.scalars(select(GenerationDraftRecord).where(
                GenerationDraftRecord.workspace_id == self.context.workspace_id)).all()), len(prepared["drafts"]))

    def test_another_workspace_sees_no_ads_or_pending_of_this_project(self):
        project = self.with_ideas()
        prepared = self.prepared(project)
        self.run_drafts([prepared["drafts"][0]["draft_id"]])
        self.submitted(prepared["drafts"][1]["draft_id"])
        other = self._make_context()
        with tenant_session(self.context) as session:
            row = session.get(MarketingProjectRecord, project["id"])
            session.expunge(row)
        with tenant_session(other) as session:
            self.assertEqual(ideas.project_ads(session, other, row), [])
            self.assertEqual(ideas.pending_ads(session, other, row), [])

    def test_a_frozen_ads_first_intent_is_checked_in_its_own_workspace(self):
        project = self.with_ideas()
        other = self._make_context()
        with tenant_session(self.context) as session:
            row = session.get(MarketingProjectRecord, project["id"])
            intent = ideas.ideas_intent(row, row.ideas_json["combinations"][0]["id"])
        with tenant_session(other) as session:
            with self.assertRaises(SelectionError) as caught:
                ideas.check_intent(session, other, intent)
            self.assertEqual(caught.exception.code, "ideas_unavailable")


class DraftProductTests(IdeasGenerationFixtures):
    """Open question of #191: a product that is only an unpublished Library draft."""

    def test_an_unpublished_product_draft_prepares_previews_and_generates(self):
        entry = library.create_entry(self.context, entry_type=ENTRY_PRODUCT, display_name="Acme Charge Home", content={})
        with tenant_session(self.context) as session:
            bridged = session.scalar(select(ProductRecord.id).where(ProductRecord.product_profile_entry_id == entry["id"]))
        response = self.client.post("/api/v1/marketing/ideas", headers=self.headers(), json={
            "product_id": entry["id"], "source_text": SOURCE, "market": "United States", "ad_language": "en-US"})
        self.assertEqual(response.status_code, 201, response.text)
        project = response.json()
        self.assertEqual(project["product_id"], bridged)
        self.execute(self.drafted_call(project, "understanding"))
        self.execute(self.drafted_call(self.read_ideas(project), "ideas"))
        project = self.read_ideas(project)
        draft_id = self.prepared(project)["drafts"][0]["draft_id"]
        with tenant_session(self.context) as session:
            self.assertEqual(session.get(GenerationDraftRecord, draft_id).product_id, bridged)
        self.run_drafts([draft_id])
        self.assertEqual(len(self.read_ideas(project)["ads"]), 1)
        with tenant_session(self.context) as session:
            row = session.get(MarketingProjectRecord, project["id"])
            [(_, recipe)] = ideas.project_ads(session, self.context, row)
            product = recipe.canonical_json["selection"]["product"]
        # No published version exists: the image is made from the bridged product row, not a Library version.
        self.assertEqual((product["id"], product["version_id"], product["content"]["name"]), (bridged, None, "Acme Charge Home"))
