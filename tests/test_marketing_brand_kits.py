"""Versioned brand baselines and explicit style variation (#228, spec §6.1, ADR 0013)."""
import asyncio
import hashlib
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from app import config
from app.adapters.blob_store_local import LocalFileBlobStore
from app.adapters.persistence import models as m
from app.adapters.persistence.database import tenant_session
from app.generation.models import RunItemRecord
from app.generation.paid_execution import execute_run_item
from app.library.contracts import ENTRY_PRODUCT
from app.marketing import workflow
from app.marketing.ai_qc import frozen_input, normalize_report
from app.marketing.ai_qc_provider import system_prompt
from app.marketing.blob_read import write_generated
from app.marketing.evaluation import evaluate_snapshot
from app.marketing.explanation import frozen_inputs
from app.marketing.models import ProductBrandKitRecord
from app.marketing.service import digest
from app.providers.adapter import ProviderCallResult
import tests.test_marketing_ai_qc as ai_qc_tests
from tests.test_marketing_evaluation import fixture as evaluation_fixture
from tests.test_marketing_workflow import MarketingWorkflowFixtures
from tests.support.library_fixtures import png_bytes

PATH = "/api/v1/marketing/products/{}/brand-kit"
KIT = {"fonts": {"primary": "Inter", "secondary": "Source Serif"}, "palette": ["#0a3d62", "#F5F6FA"],
       "readability_rules": {"min_text_height_ratio": 0.04, "min_contrast_ratio": 4.5, "safe_area_ratio": 0.06},
       "forbidden": ["Unverified UL certification", "Competitor logos"]}


class BrandKitFixtures(MarketingWorkflowFixtures):
    def setUp(self):
        super().setUp()
        self.member = self.context
        self.owner = self.owner_ctx(self.member)
        self.blobs = LocalFileBlobStore(root=config.DATA_DIR / m.new_id("brand-blobs"), public_base_url="http://localhost:9000",
                                        signing_secret=b"test-only")
        _, original = self._reference_version(self.member)
        _, group = self._group_version(self.member, original["id"])
        self.entry_id, _ = self._publish(self.member, ENTRY_PRODUCT, "Acme Charge adapter", self._product_content(original["id"], group["id"]))
        with tenant_session(self.member) as session:
            self.product_id = session.scalar(select(m.ProductRecord.id).where(m.ProductRecord.product_profile_entry_id == self.entry_id))
        self.logo_version, self.logo_bytes = self.released_image((200, 30, 30))
        self.logo_item = self.add_reference(self.logo_version, "logo")

    def released_image(self, colour):
        content = png_bytes(colour=colour)
        sha = hashlib.sha256(content).hexdigest()
        blob = asyncio.run(write_generated(self.blobs, f"committed/{self.member.workspace_id}/reference_derivative/{sha}.png", content, "image/png"))
        with tenant_session(self.member) as s:
            obj = m.ReferenceObjectRecord(id=m.new_id("refobj"), workspace_id=self.member.workspace_id, content_sha256=sha,
                original_object_key=blob.committed_key, declared_mime="image/png", mime_type="image/png", byte_size=len(content),
                state="released", derivative_object_key=blob.committed_key, derivative_storage_version_id=blob.storage_version_id,
                derivative_sha256=sha, derivative_byte_size=len(content), derivative_mime="image/png", execution_mode="mock")
            entry = m.LibraryEntryRecord(id=m.new_id("lib"), workspace_id=self.member.workspace_id, entry_type="reference_asset",
                                         display_name="Brand logo fixture", lifecycle="active")
            s.add_all([obj, entry])
            s.flush()
            meta = {"object_id": obj.id, "rights": {"state": "granted", "valid_to": "2099-12-31", "scope": "own brand logo"}}
            version = m.LibraryVersionRecord(id=m.new_id("libver"), workspace_id=self.member.workspace_id, entry_id=entry.id,
                entry_type="reference_asset", version_no=1, state="effective", eligibility="eligible", content_json=meta,
                content_hash=digest(meta), published_by=self.member.initiator_id)
            s.add(version)
            s.flush()
            entry.current_version_id = version.id
            return version.id, content

    def add_reference(self, version_id, role):
        path = f"/api/v1/products/{self.product_id}/references"
        revision = self.client.get(path).json()["revision"]
        added = self.client.post(path, headers={"Idempotency-Key": m.new_id("key"), "If-Match": str(revision)},
                                 json={"reference_version_id": version_id, "role": role})
        self.assertEqual(added.status_code, 200, added.text)
        return next(i["item_id"] for i in added.json()["items"] if i["reference_version_id"] == version_id)

    def save_kit(self, body, revision=None, *, as_ctx=None, key=None):
        self.context = as_ctx or self.owner
        try:
            if revision is None:
                revision = self.client.get(PATH.format(self.product_id)).json()["revision"]
            return self.client.put(PATH.format(self.product_id), json=body,
                                   headers={"Idempotency-Key": key or m.new_id("key"), "If-Match": f'"{revision}"'})
        finally:
            self.context = self.member

    def kit_with_logo(self, placement="top_right"):
        return {**KIT, "logo": {"item_id": self.logo_item, "placement": placement}}

    def ref(self, view):
        current = view["current"]
        return {k: current[k] for k in ("id", "product_id", "version", "content_hash")}

    def confirmed_with_brand(self, ref=None, **changes):
        project = self.drafted()
        plan = {**project["plan"], **changes}
        if ref is not None:
            plan["brand_kit"] = ref
        response = self.edit(project, "confirm_plan", plan=plan)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def prepare(self, project, provider="mock"):
        response = self.client.post(f"/api/v1/marketing/projects/{project['id']}/generation-drafts?include_brand_style=true",
            headers=self.headers(project["revision"]), json={"provider": provider, "model": "mock-v1"})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def drafts(self, project):
        return [self.client.get(f"/api/v1/generation/drafts/{d}").json() for d in project["generation_draft_ids"]]

    def run_prepared(self, project):
        adapter = AsyncMock()
        adapter.execute.return_value = ProviderCallResult(png_bytes(colour=(50, 60, 70)), "fixture-provider", False)
        for draft in self.drafts(project):
            preview = self.client.post(f"/api/v1/generation/drafts/{draft['id']}/previews", headers=self.headers(draft["revision"]))
            self.assertEqual(preview.status_code, 201, preview.text)
            run = self.client.post("/api/v1/generation/runs", headers=self.headers(draft["revision"]),
                                   json={"preview_id": preview.json()["id"], "preview_hash": preview.json()["preview_hash"]})
            self.assertEqual(run.status_code, 202, run.text)
            with tenant_session(self.context) as s:
                item = s.scalar(select(RunItemRecord).where(RunItemRecord.generation_run_id == run.json()["generation_run_id"]))
                job = s.scalar(select(m.JobRecord).where(m.JobRecord.resource_id == item.id, m.JobRecord.workspace_id == self.context.workspace_id))
                item_id, job_id = item.id, job.id
            asyncio.run(execute_run_item(self.context, job_id, item_id, self.blobs, adapter=adapter))
        return adapter


class TestBrandKitVersions(BrandKitFixtures):
    def test_owner_appends_immutable_versions_with_revision_and_idempotency(self):
        empty = self.client.get(PATH.format(self.product_id))
        self.assertEqual(empty.status_code, 200, empty.text)
        self.assertEqual(empty.json(), {"product_id": self.product_id, "revision": 0, "current": None})
        first = self.save_kit(self.kit_with_logo(), 0, key="brand-intent")
        self.assertEqual(first.status_code, 200, first.text)
        v1 = first.json()["current"]
        self.assertEqual((v1["version"], v1["created_by"]), (1, self.owner.initiator_id))
        self.assertEqual(v1["content"]["palette"], ["#0A3D62", "#F5F6FA"])
        self.assertEqual(v1["content"]["logo"]["reference_version_id"], self.logo_version)
        self.assertEqual(v1["content"]["logo"]["content_sha256"], hashlib.sha256(self.logo_bytes).hexdigest())
        self.assertTrue(v1["logo_available"])
        self.assertEqual(self.save_kit(self.kit_with_logo(), 0, key="brand-intent").json(), first.json())
        reused = self.save_kit({**KIT, "palette": ["#000000"]}, 0, key="brand-intent")
        self.assertEqual(reused.status_code, 409, reused.text)
        self.assertEqual(self.save_kit(self.kit_with_logo(), 1).json()["current"]["version"], 1)  # unchanged: no new version
        stale = self.save_kit({**KIT, "palette": ["#000000"]}, 0)
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "stale_brand_kit_revision"))
        second = self.save_kit({**KIT, "palette": ["#000000"]}, 1).json()["current"]
        self.assertEqual(second["version"], 2)
        with tenant_session(self.member) as s:
            rows = s.scalars(select(ProductBrandKitRecord).where(ProductBrandKitRecord.product_id == self.product_id)
                             .order_by(ProductBrandKitRecord.version)).all()
            self.assertEqual([r.content_hash for r in rows], [v1["content_hash"], second["content_hash"]])
            self.assertEqual(rows[0].content_json, v1["content"])
            events = s.scalars(select(m.AuditEventRecord).where(m.AuditEventRecord.event_type == "marketing.brand_kit_saved",
                m.AuditEventRecord.workspace_id == self.member.workspace_id)).all()
            self.assertEqual(len(events), 2)
        listed = self.client.get("/api/v1/marketing/brand-kits").json()["items"]
        self.assertEqual([(i["product_id"], i["current"]["version"]) for i in listed], [(self.product_id, 2)])

    def test_validation_and_logo_must_be_the_products_released_logo_item(self):
        self.assertEqual(self.save_kit({**KIT, "palette": ["#111111"] * 2}, 0).status_code, 422)
        self.assertEqual(self.save_kit({**KIT, "palette": [f"#00000{i}" for i in range(6)]}, 0).status_code, 422)
        self.assertEqual(self.save_kit({**KIT, "palette": ["red"]}, 0).status_code, 422)
        detail_version, _ = self.released_image((10, 200, 10))
        detail_item = self.add_reference(detail_version, "detail")
        for item_id in ("guessed-item", detail_item):
            refused = self.save_kit({**KIT, "logo": {"item_id": item_id}}, 0)
            self.assertEqual(refused.json()["code"], "brand_logo_invalid", refused.text)
        with tenant_session(self.member) as s:
            version = s.get(m.LibraryVersionRecord, self.logo_version)
            version.content_json = {**version.content_json, "rights": {**version.content_json["rights"], "state": "revoked"}}
        refused = self.save_kit(self.kit_with_logo(), 0)
        self.assertEqual((refused.status_code, refused.json()["code"]), (409, "brand_logo_unavailable"))

    def test_member_reads_but_only_owner_writes_and_other_tenants_see_nothing(self):
        saved = self.save_kit(KIT, 0)
        self.assertEqual(saved.status_code, 200, saved.text)
        member = self.save_kit({**KIT, "palette": ["#000000"]}, 1, as_ctx=self.member)
        self.assertEqual((member.status_code, member.json()["code"]), (403, "owner_required"))
        self.assertEqual(self.client.get(PATH.format(self.product_id)).json()["revision"], 1)
        self.context = replace(self.member, role="system", executor="system:worker", identity_kind=None)
        self.assertEqual(self.client.get(PATH.format(self.product_id)).json()["code"], "brand_kit_member_required")
        self.assertEqual(self.client.get("/api/v1/marketing/brand-kits").status_code, 403)
        self.context = self.member
        other = self._make_context()
        other_owner = self.owner_ctx(other)
        self.context = other
        missing = self.client.get(PATH.format(self.product_id))
        self.assertEqual((missing.status_code, missing.json()["code"]), (404, "brand_kit_product_not_found"))
        self.assertEqual(self.client.get("/api/v1/marketing/brand-kits").json()["items"], [])
        refused = self.save_kit(KIT, 0, as_ctx=other_owner)
        self.assertEqual(refused.status_code, 404, refused.text)
        self.context = self.member
        with tenant_session(self.member) as s:
            self.assertEqual(len(s.scalars(select(ProductBrandKitRecord).where(
                ProductBrandKitRecord.workspace_id.in_([self.member.workspace_id, other.workspace_id]))).all()), 1)

    def test_a_plan_cannot_reference_another_workspaces_or_a_changed_brand_version(self):
        ref = self.ref(self.save_kit(KIT, 0).json())
        other = self._make_context()
        self.context = other
        project = self.drafted()
        refused = self.edit(project, "save_plan", plan={**project["plan"], "brand_kit": ref})
        self.assertEqual((refused.status_code, refused.json()["code"]), (404, "brand_kit_not_found"))
        self.context = self.member
        project = self.drafted()
        changed = self.edit(project, "save_plan", plan={**project["plan"], "brand_kit": {**ref, "content_hash": "0" * 64}})
        self.assertEqual((changed.status_code, changed.json()["code"]), (409, "brand_kit_changed"))


class TestBrandBaselineInPlansAndGeneration(BrandKitFixtures):
    def test_brief_freezes_the_exact_version_and_held_style_is_shared_by_every_group(self):
        ref = self.ref(self.save_kit(self.kit_with_logo(), 0).json())
        project = self.confirmed_with_brand(ref)
        self.assertEqual(project["plan"]["brand_kit"], ref)
        self.assertEqual(project["brand_kit"]["version"], 1)
        prepared = self.prepare(project)
        drafts = self.drafts(prepared)
        prompts = [d["selectors"]["prompt"] for d in drafts]
        for prompt in prompts:
            self.assertIn("Brand baseline", prompt)
            self.assertIn("#0A3D62, #F5F6FA", prompt)
            self.assertIn("brand logo from the last reference image in the top-right corner", prompt)
            self.assertIn("Never show or claim: Unverified UL certification; Competitor logos.", prompt)
            self.assertIn("Inter-style", prompt)
            self.assertLessEqual(len(prompt), 4000)
        styles = {line for p in prompts for line in p.splitlines() if line.startswith("Visual style")}
        self.assertEqual(len(styles), 1)
        self.assertTrue(next(iter(styles)).startswith("Visual style (held constant across every group): "))
        groups = {g["id"] for g in project["plan"]["groups"]}
        self.assertGreaterEqual(len(groups), 2)
        for draft in drafts:
            self.assertEqual(draft["selectors"]["marketing_intent"]["brand_kit"], ref)
            self.assertEqual(draft["selectors"]["brand_logo"]["reference_version_id"], self.logo_version)
            self.assertEqual(draft["selectors"]["brand_logo"]["role"], "brand_logo")
        # The product's newer kit never rewrites the confirmed plan, its drafts or its view.
        self.save_kit({**KIT, "palette": ["#FF0000"]}, 1)
        reread = self.read(project)
        self.assertEqual(reread["plan"]["brand_kit"], ref)
        self.assertEqual(reread["brand_kit"]["content"]["palette"], ["#0A3D62", "#F5F6FA"])
        self.assertEqual([d["selectors"]["prompt"] for d in self.drafts(prepared)], prompts)
        adapter = self.run_prepared(prepared)
        for call in adapter.execute.await_args_list:
            self.assertEqual(call.args[0].reference_inputs[-1], self.logo_bytes)
        row = self.start_round(prepared)
        self.assertEqual(row["snapshot"]["plan"].get("brand_kit"), None)  # legacy projection by default
        opted = self.client.get(f"/api/v1/marketing/rounds/{row['id']}?include_brand_style=true").json()["data"]
        self.assertEqual(opted["snapshot"]["plan"]["brand_kit"], ref)
        with tenant_session(self.context) as s:
            frozen = frozen_input(s, self.context, workflow.get_round(s, self.context, row["id"]), row["assets"][0]["asset_version_id"], kind="single")
        self.assertIn("brand_style", frozen["required_checks"])
        self.assertIn("text_readable", frozen["required_checks"])
        self.assertEqual(frozen["brand_kit"]["ref"], ref)
        self.assertEqual(frozen["brand_kit"]["palette"], ["#0A3D62", "#F5F6FA"])
        self.assertEqual(frozen["style_variation"], "held")
        self.assertTrue(frozen["brand_logo_available"])
        logo = [i for i in frozen["images"] if i["role"] == "brand_logo"]
        self.assertEqual(logo[0]["content_sha256"], hashlib.sha256(self.logo_bytes).hexdigest())
        self.assertIn("Check brand_style against brand_kit", system_prompt(frozen))

    def test_logo_withdrawn_after_preparation_is_not_sent(self):
        ref = self.ref(self.save_kit(self.kit_with_logo(), 0).json())
        prepared = self.prepare(self.confirmed_with_brand(ref))
        with tenant_session(self.member) as s:
            version = s.get(m.LibraryVersionRecord, self.logo_version)
            version.content_json = {**version.content_json, "rights": {**version.content_json["rights"], "state": "revoked"}}
        draft = self.drafts(prepared)[0]
        preview = self.client.post(f"/api/v1/generation/drafts/{draft['id']}/previews", headers=self.headers(draft["revision"]))
        self.assertEqual(preview.status_code, 422, preview.text)
        self.assertEqual(preview.json()["code"], "brand_logo_unavailable")

    def test_tested_style_is_written_per_group_and_must_be_declared(self):
        project = self.drafted()
        plan = deepcopy(project["plan"])
        plan["style_variation"] = "tested"
        refused = self.edit(project, "confirm_plan", plan=plan)
        self.assertEqual(refused.status_code, 422, refused.text)
        self.assertEqual(refused.json()["code"], "style_variation_undeclared")
        self.assertIn("body.plan.changed", [f["field"] for f in refused.json()["field_errors"]])
        plan["held_constant"] = [h for h in plan["held_constant"] if h != "Visual style"]
        plan["changed"] = [*plan["changed"], "Visual style"]
        refused = self.edit(project, "confirm_plan", plan=plan)
        self.assertEqual(refused.json()["code"], "group_style_required")
        plan["groups"][0]["style"] = "Bright lifestyle photo, warm daylight"
        plan["groups"][1]["style"] = "Dark studio product shot, crisp rim light"
        confirmed = self.edit(project, "confirm_plan", plan=plan)
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        prompts = {d["selectors"]["marketing_intent"]["combination"]["id"]: d["selectors"]["prompt"] for d in self.drafts(self.prepare(confirmed.json()))}
        for group in plan["groups"]:
            for combination_id in group["combination_ids"]:
                self.assertIn(f"Visual style for this group (the tested change): {group['style']}", prompts[combination_id])

    def test_held_style_and_specific_factor_rules(self):
        project = self.drafted()
        plan = deepcopy(project["plan"])
        missing = {**plan, "held_constant": [h for h in plan["held_constant"] if h != "Visual style"]}
        self.assertEqual(self.edit(project, "confirm_plan", plan=missing).json()["code"], "style_not_held")
        styled = deepcopy(plan)
        styled["groups"][0]["style"] = "A different look"
        self.assertEqual(self.edit(project, "confirm_plan", plan=styled).json()["code"], "group_style_not_held")
        self.assertEqual(self.edit(project, "confirm_plan", plan={**plan, "style_notes": " "}).json()["code"], "style_notes_required")
        chinese = self.edit(project, "confirm_plan", plan={**plan, "style_notes": "明亮的生活方式摄影"})
        self.assertEqual(chinese.json()["code"], "image_brief_language_mismatch")
        factor = {**plan, "comparison_kind": "specific_factor", "conclusion_scope": "specific_factor", "changed": ["Headline wording"],
                  "style_variation": "tested"}
        refused = self.edit(project, "confirm_plan", plan=factor)
        self.assertEqual(refused.json()["code"], "style_variation_conflicts_factor", refused.text)
        # Saving a draft keeps an incomplete style choice editable; only confirmation enforces it.
        self.assertEqual(self.edit(project, "save_plan", plan={**plan, "style_variation": "tested"}).status_code, 200)

    def test_a_client_without_style_fields_keeps_the_stored_choice(self):
        project = self.drafted()
        plan = deepcopy(project["plan"])
        plan.update(style_variation="tested", changed=[*plan["changed"], "Visual style"],
                    held_constant=[h for h in plan["held_constant"] if h != "Visual style"])
        for index, group in enumerate(plan["groups"]):
            group["style"] = f"Style {index}"
        saved = self.edit(project, "save_plan", plan=plan).json()
        legacy = self.client.get(f"/api/v1/marketing/projects/{project['id']}?include_languages=true&include_plan_review=true").json()
        self.assertNotIn("style_variation", legacy["plan"])
        self.assertTrue(all("style" not in g for g in legacy["plan"]["groups"]))
        self.assertNotIn("brand_style_support", legacy)
        response = self.client.patch(f"/api/v1/marketing/projects/{project['id']}?include_languages=true&include_plan_review=true",
            headers=self.headers(saved["revision"]), json={"command": "confirm_plan", "plan": legacy["plan"]})
        self.assertEqual(response.status_code, 200, response.text)
        stored = self.read(project)["plan"]
        self.assertEqual(stored["style_variation"], "tested")
        self.assertEqual([g["style"] for g in stored["groups"]], ["Style 0", "Style 1"])

    def test_redraft_keeps_the_plans_exact_brand_version(self):
        ref = self.ref(self.save_kit(KIT, 0).json())
        project = self.drafted()
        saved = self.edit(project, "save_plan", plan={**project["plan"], "brand_kit": ref}).json()
        self.save_kit({**KIT, "palette": ["#FF0000"]}, 1)
        call = self.plan_call(saved, "plan")
        with tenant_session(self.context) as s:
            from app.marketing.models import PlanningCallRecord
            inputs = s.get(PlanningCallRecord, call["id"]).input_json
        self.assertEqual(inputs["brand_kit"]["ref"], ref)
        self.assertEqual(inputs["brand_kit"]["palette"], ["#0A3D62", "#F5F6FA"])
        self.execute(call)
        self.assertEqual(self.read(project)["plan"]["brand_kit"], ref)


    def test_first_draft_offers_the_chosen_library_products_current_kit(self):
        ref = self.ref(self.save_kit(KIT, 0).json())
        response = self.client.post("/api/v1/marketing/projects?include_languages=true&include_plan_review=true&include_brand_style=true",
            headers=self.headers(), json={"source_text": "Acme Charge adapter", "product_id": self.entry_id})
        self.assertEqual(response.status_code, 201, response.text)
        project = response.json()
        self.execute(self.plan_call(project))
        project = self.read(project)
        understanding = project["understanding"]
        for fact in understanding["facts"]:
            fact["confirmation"] = "confirmed"
        project = self.edit(project, "confirm_understanding", understanding=understanding).json()
        self.execute(self.plan_call(project, "plan"))
        drafted = self.read(project)
        self.assertEqual(drafted["plan"]["brand_kit"], ref)
        self.assertEqual(drafted["brand_kit"]["content"]["fonts"]["primary"], "Inter")
        # A baseline the user removed stays removed on the next draft.
        cleared = self.edit(drafted, "save_plan", plan={**drafted["plan"], "brand_kit": None}).json()
        self.execute(self.plan_call(cleared, "plan"))
        self.assertIsNone(self.read(project)["plan"]["brand_kit"])


class TestBrandStyleQualityAndInterpretation(BrandKitFixtures):
    def test_qc_fails_an_old_image_without_the_logo_and_keeps_failure(self):
        ref = self.ref(self.save_kit(self.kit_with_logo(), 0).json())
        prepared = self.prepare(self.confirmed_with_brand(ref))
        self.run_prepared(prepared)
        row = self.start_round(prepared)
        with tenant_session(self.context) as s:
            frozen = frozen_input(s, self.context, workflow.get_round(s, self.context, row["id"]), row["assets"][0]["asset_version_id"], kind="single")
        report = ai_qc_tests.TestMarketingAIQC.report(frozen)
        for finding in report["findings"]:
            if finding["check_id"] == "brand_style":
                finding.update(status="fail", visual_evidence="No brand logo is visible anywhere in the image.",
                               rationale="The baseline requires the logo top right.", suggested_fix="Add the supplied logo.")
        normalized = normalize_report(report, frozen)
        self.assertEqual(next(f for f in normalized["findings"] if f["check_id"] == "brand_style")["status"], "fail")
        self.assertEqual(normalized["quality_suggestion"], "needs_changes")
        # Without the logo original the check cannot pass on the logo.
        passing = normalize_report(ai_qc_tests.TestMarketingAIQC.report(frozen), {**frozen, "brand_logo_available": False})
        self.assertEqual(next(f for f in passing["findings"] if f["check_id"] == "brand_style")["gate_reason"], "brand_logo_missing")

    def test_rounds_frozen_before_the_baseline_keep_their_checks(self):
        combo = {"checks": ["Product looks accurate"], "image_brief": {"proposition": "x"}}
        self.assertNotIn("brand_style", workflow.required_checks(combo, {"groups": []}))
        self.assertIn("brand_style", workflow.required_checks(combo, {"style_variation": "held"}))
        self.assertIn("text_readable", workflow.required_checks(combo, {"style_variation": "held"}))

    def test_evaluation_and_explanation_separate_tested_style_from_held_deviation(self):
        inputs = evaluation_fixture()
        plan = inputs["snapshot"]["plan"]
        tested = deepcopy(inputs)
        tested["snapshot"]["plan"] = {**plan, "style_variation": "tested",
            "groups": [{**g, "style": f"Style {i}"} for i, g in enumerate(plan["groups"])]}
        result = evaluate_snapshot(tested)
        self.assertTrue(any("actively tested change" in item for item in result["limitations"]))
        evidence = self.explanation(tested, result)
        style = next(item for item in evidence["catalogue"] if item["id"] == "style")
        self.assertEqual(list(style["interpretations"]), ["compare_scope"])
        held = deepcopy(inputs)
        held["snapshot"]["plan"] = {**plan, "style_variation": "held"}
        held["style_deviations"] = ["v0"]
        result = evaluate_snapshot(held)
        self.assertTrue(any("meant to be held constant" in item for item in result["limitations"]))
        evidence = self.explanation(held, result)
        style = next(item for item in evidence["catalogue"] if item["id"] == "style")
        self.assertEqual(list(style["interpretations"]), ["investigate"])
        self.assertIn("meant to be held constant", " ".join(evidence["limitations"]))
        inputs["snapshot"]["plan"] = {k: v for k, v in plan.items() if k not in ("style_variation", "style_notes", "brand_kit")}
        legacy = evaluate_snapshot(inputs)
        self.assertFalse(any("Visual style" in item for item in legacy["limitations"]))
        self.assertFalse(any(item["id"] == "style" for item in self.explanation(inputs, legacy)["catalogue"]))

    def explanation(self, inputs, result):
        result = {**result, "big_round_spend": "100", "big_round_budget": "1000", "budget_exhausted": False, "round_limit_reached": False}
        return frozen_inputs(SimpleNamespace(id="round", snapshot_json=inputs["snapshot"]),
            SimpleNamespace(id="evaluation", input_hash="frozen", result_json=result, input_json=inputs))
