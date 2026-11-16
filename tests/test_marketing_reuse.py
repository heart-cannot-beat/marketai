"""Historical files retain their origin while acquiring a new review intent."""
from unittest.mock import patch
from sqlalchemy import select
from tests.test_marketing_workflow import MarketingWorkflowFixtures
from app import config
from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import (AssetRecord, AssetVersionRecord, AuditEventRecord, ProductRecord,
    WorkspaceRecord, new_id)
from app.entitlements.cost_models import ProviderCallAttemptRecord, SpendReservationRecord
from app.generation.models import GenerationRecipeSnapshotRecord
from app.library.contracts import ENTRY_PRODUCT
from app.main import app
from app.marketing import workflow
from app.marketing.workflow_models import (MarketingCreativeReuseRecord, MarketingFeedbackDraftRecord,
    MarketingImageEditRecord)
from app.platform.http_identity import request_tenant_context


class TestMarketingReuse(MarketingWorkflowFixtures):
    def test_historical_committed_pointer_is_read_with_content_hash(self):
        import asyncio
        from app.generation.paid_execution import asset_blob_ref
        project, row, assets = self.historical()
        with tenant_session(self.context) as session:
            version = session.get(AssetVersionRecord, assets[0]['asset_version_id'])
            ref = asset_blob_ref(session, self.context, version.id)
            version.committed_object_key = ref.committed_key
            version.committed_storage_version_id = ref.storage_version_id
            version.provider_attempt_id = None
            session.flush()
            direct = asset_blob_ref(session, self.context, version.id)
            self.assertTrue(direct.checksum_sha256_b64)
        self.assertEqual(len(asyncio.run(self.blobs.read_committed(direct, 20000000))), direct.size_bytes)
        row = self.command(row, 'creatives/reuse', {'asset_version_id': assets[0]['asset_version_id'],
            'combination_id': assets[0]['combination_id']}, 201)
        self.assertEqual(len(row['assets']),1)

    def historical(self):
        project = self.confirmed()
        self.generate(project)
        row = self.start_round(project)
        assets = row['assets']
        with tenant_session(self.context) as session:
            for recipe in session.scalars(select(GenerationRecipeSnapshotRecord).where(
                    GenerationRecipeSnapshotRecord.workspace_id == self.context.workspace_id)).all():
                data = dict(recipe.canonical_json)
                data['selection'] = {k: v for k, v in data['selection'].items() if k != 'marketing_intent'}
                recipe.canonical_json = data
        return project, row, assets

    def test_reuse_keeps_original_and_requires_fresh_semantic_review(self):
        project, row, assets = self.historical()
        with tenant_session(self.context) as session:
            original = session.get(AssetVersionRecord, assets[0]['asset_version_id'])
            identity = (original.producing_run_item_id, original.origin_kind, original.execution_mode, original.content_sha256)
        row = self.command(row, 'creatives/reuse', {'asset_version_id': assets[0]['asset_version_id'],
            'combination_id': assets[0]['combination_id']}, 201)
        self.assertEqual(len(row['assets']), 1)
        self.assertFalse(row['assets'][0]['ready'])
        row = self.command(row, 'creatives/reuse', {'asset_version_id': assets[0]['asset_version_id'],
            'combination_id': assets[0]['combination_id']}, 201)
        with tenant_session(self.context) as session:
            original = session.get(AssetVersionRecord, assets[0]['asset_version_id'])
            self.assertEqual(identity, (original.producing_run_item_id, original.origin_kind, original.execution_mode, original.content_sha256))
            self.assertEqual(len(session.scalars(select(MarketingCreativeReuseRecord).where(
                MarketingCreativeReuseRecord.workspace_id == self.context.workspace_id)).all()), 1)
        response = self.client.post(f"/api/v1/marketing/rounds/{row['id']}/handoff", headers=self.headers(row['revision']),
            json={'asset_version_ids': [assets[0]['asset_version_id']]})
        self.assertEqual(response.status_code, 422)

    def test_reuse_rejects_other_tenants_modes_and_remapping(self):
        _, row, assets = self.historical()
        body = {'asset_version_id': assets[0]['asset_version_id'], 'combination_id': assets[0]['combination_id']}
        with tenant_session(self.context) as session:
            session.get(AssetVersionRecord, body['asset_version_id']).execution_mode = 'byok'
        response = self.client.post(f"/api/v1/marketing/rounds/{row['id']}/creatives/reuse", headers=self.headers(row['revision']), json=body)
        self.assertEqual(response.json()['code'], 'asset_mode_mismatch')
        with tenant_session(self.context) as session:
            session.get(AssetVersionRecord, body['asset_version_id']).execution_mode = 'mock'
        row = self.command(row, 'creatives/reuse', body, 201)
        response = self.client.post(f"/api/v1/marketing/rounds/{row['id']}/creatives/reuse", headers=self.headers(row['revision']),
            json={**body, 'combination_id': assets[1]['combination_id']})
        self.assertEqual(response.json()['code'], 'reuse_already_mapped')
        self.context = self._make_context()
        self.assertEqual(self.client.post(f"/api/v1/marketing/rounds/{row['id']}/creatives/reuse", headers=self.headers(row['revision']), json=body).status_code,404)


class TestReuseWithinTheProductCard(MarketingWorkflowFixtures):
    """#7 plan A: the tests of one product card share creatives; anything else stays refused."""
    MISMATCH = "只能复用同一产品下的素材。"

    def setUp(self):
        super().setUp()
        self.revise = False
        self.card = self.publish_card("Alpha")

    def publish_card(self, name):
        _, hero = self._reference_version(self.context, name=f"{name} hero")
        _, group = self._group_version(self.context, hero["id"])
        card, _ = self._publish(self.context, ENTRY_PRODUCT, name, self._product_content(hero["id"], group["id"], name=name))
        return card

    def create(self):
        # A test starts from the current card; `card=None` pastes its own text, `revise` rewrites the source first.
        response = self.client.post("/api/v1/marketing/projects?include_languages=true&include_plan_review=true&include_brand_style=true&include_execution_plan=true",
            headers=self.headers(), json={"source_text": "Alpha 充电器\n65W，多口，面向差旅办公用户。",
                                          **({"product_id": self.card} if self.card else {})})
        self.assertEqual(response.status_code, 201, response.text)
        project = response.json()
        if self.revise:
            response = self.edit(project, "revise_source", source_text="Alpha 充电器\n65W，多口，面向差旅办公用户。")
            self.assertEqual(response.status_code, 200, response.text)
            project = response.json()
        return project

    def started(self, *, card=..., revise=False):
        """A test with its own generated creatives in its first round."""
        saved = (self.card, self.revise)
        self.card = self.card if card is ... else card
        self.revise = revise
        try:
            project = self.confirmed()
            self.generate(project)
            return project, self.start_round(project)
        finally:
            self.card, self.revise = saved

    def next_round(self, project, row):
        self.command(row, "close", {"reason": "user_stopped", "note": "Prepare the next small round"})
        project = self.read(project)
        response = self.edit(project, "confirm_plan", plan=project["plan"])
        self.assertEqual(response.status_code, 200, response.text)
        return self.start_round(response.json())

    def reuse(self, row, body, key=None):
        return self.client.post(f"/api/v1/marketing/rounds/{row['id']}/creatives/reuse", headers=self.headers(row["revision"], key), json=body)

    def round(self, row):
        response = self.client.get(f"/api/v1/marketing/rounds/{row['id']}?include_image_edits=true&include_reuse_lineage=true")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    def candidates(self, row, **params):
        return self.client.get(f"/api/v1/marketing/rounds/{row['id']}/reuse-candidates", params=params)

    def reuses(self):
        with tenant_session(self.context) as session:
            return session.scalars(select(MarketingCreativeReuseRecord).where(
                MarketingCreativeReuseRecord.workspace_id == self.context.workspace_id)).all()

    def card_product(self):
        listed = self.client.get("/api/v1/products").json()["items"]
        return next(item["product_id"] for item in listed if item["entry_id"] == self.card)

    def foreign_version(self):
        """A committed creative of a test in another Workspace."""
        mine, card = self.context, self.card
        self.context = self._make_context()
        self.card = self.publish_card("Alpha")
        try:
            return self.started()[1]["assets"][0]["asset_version_id"]
        finally:
            self.context, self.card = mine, card

    def test_another_test_of_the_same_card_reuses_its_creative_and_records_where_it_came_from(self):
        first, first_round = self.started()
        second, second_round = self.started()
        # Each confirmed understanding anchors its own test; the product list folds both into the one card (J-32).
        self.assertNotEqual(first["product_id"], second["product_id"])
        listed = self.client.get("/api/v1/products").json()["items"]
        self.assertEqual(len(listed), 1)
        source = first_round["assets"][0]
        with tenant_session(self.context) as session:
            original = session.get(AssetVersionRecord, source["asset_version_id"])
            identity = (original.producing_run_item_id, original.origin_kind, original.execution_mode, original.content_sha256)
        body = {"asset_version_id": source["asset_version_id"], "combination_id": second_round["assets"][0]["combination_id"]}
        key = new_id("key")
        response = self.reuse(second_round, body, key)
        self.assertEqual(response.status_code, 201, response.text)
        # The same Idempotency-Key replays the same answer and adds no second link.
        replay = self.reuse(second_round, body, key)
        self.assertEqual((replay.status_code, replay.json()), (201, response.json()))
        [link] = self.reuses()
        self.assertEqual({k: link.source_json[k] for k in ("relation", "source_project_id", "card_product_id", "facts_relation", "product_id")},
                         {"relation": "other_test", "source_project_id": first["id"], "card_product_id": listed[0]["product_id"],
                          "facts_relation": "same", "product_id": first["product_id"]})
        with tenant_session(self.context) as session:
            original = session.get(AssetVersionRecord, source["asset_version_id"])
            self.assertEqual(identity, (original.producing_run_item_id, original.origin_kind, original.execution_mode, original.content_sha256))
            event = session.scalars(select(AuditEventRecord).where(AuditEventRecord.workspace_id == self.context.workspace_id,
                AuditEventRecord.event_type == "marketing.creative_reused")).one()
        self.assertEqual((event.details_json["relation"], event.details_json["source_project_id"]), ("other_test", first["id"]))
        # It is reviewed again here, and it is not revised or cropped in this test.
        current = self.round(second_round)
        reused = next(a for a in current["assets"] if a["asset_version_id"] == source["asset_version_id"])
        self.assertEqual((reused["ready"], reused["editable"]), (False, False))
        self.assertTrue(all(a["editable"] for a in current["assets"] if a["asset_version_id"] != source["asset_version_id"]))
        # Readers from before #7 never see the new field, with or without image edits.
        for query in ("", "?include_image_edits=true"):
            legacy = self.client.get(f"/api/v1/marketing/rounds/{second_round['id']}{query}").json()["data"]
            self.assertTrue(all("editable" not in a for a in legacy["assets"]))

    def test_facts_that_differ_between_the_two_tests_are_flagged_but_not_blocked(self):
        first, first_round = self.started()
        _, second_round = self.started()
        with tenant_session(self.context) as session:
            session.get(ProductRecord, first["product_id"]).selling_points = ["Charges two laptops at once"]
        item = next(i for i in self.candidates(second_round).json()["items"] if i["relation"] == "other_test")
        self.assertEqual(item["facts_relation"], "differs")
        self.command(second_round, "creatives/reuse", {"asset_version_id": item["asset_version_id"],
            "combination_id": second_round["assets"][0]["combination_id"]}, 201)
        self.assertEqual(self.reuses()[0].source_json["facts_relation"], "differs")

    def test_candidates_say_whether_a_creative_is_this_tests_the_cards_or_another_tests(self):
        first, first_round = self.started()
        second, second_round = self.started()
        row = self.next_round(second, second_round)
        # A creative generated on the product card itself (e.g. from the product page).
        card_version = first_round["assets"][1]["asset_version_id"]
        with tenant_session(self.context) as session:
            asset_id = session.get(AssetVersionRecord, card_version).asset_id
            session.get(AssetRecord, asset_id).product_id = self.card_product()
        response = self.candidates(row, limit=50)
        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()["items"]
        relation = {i["asset_version_id"]: i["relation"] for i in items}
        mine = {a["asset_version_id"] for a in second_round["assets"]}
        theirs = {a["asset_version_id"] for a in first_round["assets"]} - {card_version}
        self.assertEqual(relation, {**{v: "this_test" for v in mine}, **{v: "other_test" for v in theirs}, card_version: "product_card"})
        other = next(i for i in items if i["relation"] == "other_test")
        self.assertEqual((other["source_project_id"], other["source_test_name"], other["facts_relation"]),
                         (first["id"], first["understanding"]["product_name"], "same"))
        self.assertTrue(all("source_project_id" not in i for i in items if i["relation"] != "other_test"))
        self.assertEqual([i["created_at"] for i in items], sorted((i["created_at"] for i in items), reverse=True))
        # A creative already in this round is no longer offered; pages follow the cursor.
        row = self.command(row, "creatives/reuse", {"asset_version_id": card_version,
            "combination_id": row["snapshot"]["plan"]["combinations"][0]["id"]}, 201)
        first_page = self.candidates(row, limit=3).json()
        rest = self.candidates(row, limit=50, cursor=first_page["next_cursor"]).json()
        paged = [i["asset_version_id"] for i in first_page["items"] + rest["items"]]
        self.assertEqual(sorted(paged), sorted(set(relation) - {card_version}))
        self.assertIsNone(rest["next_cursor"])
        refused = self.candidates(row, cursor="not-a-cursor")
        self.assertEqual((refused.status_code, refused.json()["code"]), (400, "invalid_cursor"))
        # Another execution mode is never offered.
        with tenant_session(self.context) as session:
            for version_id in theirs:
                session.get(AssetVersionRecord, version_id).execution_mode = "byok"
        self.assertFalse(any(i["relation"] == "other_test" for i in self.candidates(row, limit=50).json()["items"]))

    def test_a_pasted_a_revised_or_another_cards_test_still_cannot_share_creatives(self):
        _, card_round = self.started()
        _, pasted_round = self.started(card=None)
        _, revised_round = self.started(revise=True)
        _, other_card_round = self.started(card=self.publish_card("Beta"))
        rounds = [card_round, pasted_round, revised_round, other_card_round]
        for target in rounds:
            for source in rounds:
                if source is target:
                    continue
                with self.subTest(target=rounds.index(target), source=rounds.index(source)):
                    refused = self.reuse(target, {"asset_version_id": source["assets"][0]["asset_version_id"],
                                                  "combination_id": target["assets"][0]["combination_id"]})
                    self.assertEqual(refused.status_code, 422, refused.text)
                    self.assertEqual((refused.json()["code"], refused.json()["detail"]), ("asset_product_mismatch", self.MISMATCH))
            offered = {i["asset_version_id"] for i in self.candidates(target, limit=50).json()["items"]}
            self.assertFalse(offered & {a["asset_version_id"] for r in rounds for a in r["assets"]})
        self.assertEqual(self.reuses(), [])

    def test_a_reused_creative_is_not_revised_or_cropped_here_and_nothing_is_reserved(self):
        _, first_round = self.started()
        _, second_round = self.started()
        source = first_round["assets"][0]["asset_version_id"]
        row = self.command(second_round, "creatives/reuse", {"asset_version_id": source,
            "combination_id": second_round["assets"][0]["combination_id"]}, 201)
        with tenant_session(self.context) as session:
            before = [len(session.scalars(select(model).where(model.workspace_id == self.context.workspace_id)).all())
                      for model in (SpendReservationRecord, ProviderCallAttemptRecord, MarketingImageEditRecord, MarketingFeedbackDraftRecord)]
        for suffix, body in (("feedback-drafts", {"asset_version_id": source, "provider": "mock", "model": "mock-v1", "note": "Make the product larger"}),
                             ("image-edits", {"asset_version_id": source, "rotation": 90})):
            with self.subTest(suffix):
                refused = self.command(row, suffix, body, 422)
                self.assertEqual(refused["code"], "reused_creative_not_editable")
        with tenant_session(self.context) as session:
            after = [len(session.scalars(select(model).where(model.workspace_id == self.context.workspace_id)).all())
                     for model in (SpendReservationRecord, ProviderCallAttemptRecord, MarketingImageEditRecord, MarketingFeedbackDraftRecord)]
        self.assertEqual(after, before)
        # The test's own creatives keep both tools.
        own = second_round["assets"][0]["asset_version_id"]
        self.command(row, "image-edits", {"asset_version_id": own, "rotation": 90}, 202)

    def test_a_review_in_the_reusing_test_applies_to_the_version_in_the_original_test_too(self):
        _, first_round = self.started()
        _, second_round = self.started()
        source = first_round["assets"][0]
        review = {"asset_version_id": source["asset_version_id"], "checks": {k: "pass" for k in source["required_checks"]},
                  "note": "Checked product, proposition, copy and comparison"}
        first_round = self.command(first_round, "reviews", {**review, "quality": "approved"}, 201)
        self.assertTrue(next(a for a in first_round["assets"] if a["asset_version_id"] == source["asset_version_id"])["ready"])
        row = self.command(second_round, "creatives/reuse", {"asset_version_id": source["asset_version_id"],
            "combination_id": second_round["assets"][0]["combination_id"]}, 201)
        reused = next(a for a in row["assets"] if a["asset_version_id"] == source["asset_version_id"])
        self.command(row, "reviews", {**review, "checks": {k: "pass" for k in reused["required_checks"]},
                                      "quality": "rejected", "note": "Claims do not match this test's facts"}, 201)
        # Quality reviews belong to the version (decision 3): the original test's approval no longer stands.
        original = self.round(first_round)
        self.assertFalse(next(a for a in original["assets"] if a["asset_version_id"] == source["asset_version_id"])["ready"])

    def test_reuse_and_candidates_need_this_workspace_a_delegated_identity_and_current_authorization(self):
        foreign = self.foreign_version()
        _, first_round = self.started()
        _, row = self.started()
        source = first_round["assets"][0]
        body = {"asset_version_id": source["asset_version_id"], "combination_id": row["assets"][0]["combination_id"]}
        self.assertNotIn(foreign, {i["asset_version_id"] for i in self.candidates(row, limit=50).json()["items"]})
        mine = self.context
        self.context = self._make_context()
        for response in (self.reuse(row, body), self.candidates(row)):
            self.assertEqual((response.status_code, response.json()["code"]), (404, "round_not_found"), response.text)
        self.context = mine
        response = self.reuse(row, {**body, "asset_version_id": foreign})
        self.assertEqual((response.status_code, response.json()["code"]), (404, "asset_not_found"), response.text)
        app.dependency_overrides.pop(request_tenant_context)
        try:
            with patch.object(config, "LOCAL_IDENTITY_ENABLED", False):
                responses = (self.reuse(row, body), self.candidates(row))
        finally:
            app.dependency_overrides[request_tenant_context] = lambda: self.context
        for response in responses:
            self.assertEqual(response.status_code, 401, response.text)
            self.assertIn("delegated_identity_required", response.text)
        lookup = workflow.get_round

        def revoked_after_admission(session, ctx, round_id):
            # The control plane changes this Workspace's authorization while the command is in flight.
            session.get(WorkspaceRecord, ctx.workspace_id).authorization_epoch += 1
            return lookup(session, ctx, round_id)
        with patch.object(workflow, "get_round", revoked_after_admission):
            response = self.reuse(row, body)
        self.assertEqual((response.status_code, response.json()["code"]), (403, "workspace_inactive"), response.text)
        with platform_session() as session:
            self.assertEqual(session.get(WorkspaceRecord, mine.workspace_id).authorization_epoch, mine.authorization_epoch)
        self.assertEqual(self.reuses(), [])
        # The same request succeeds once it comes from this Workspace with current authorization.
        self.command(row, "creatives/reuse", body, 201)
        self.assertEqual(len(self.reuses()), 1)
