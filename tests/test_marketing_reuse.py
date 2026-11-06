"""Historical files retain their origin while acquiring a new review intent."""
from unittest.mock import patch
from sqlalchemy import select
from tests.test_marketing_workflow import MarketingWorkflowFixtures
from app import config
from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import AssetVersionRecord, WorkspaceRecord
from app.generation.models import GenerationRecipeSnapshotRecord
from app.library.contracts import ENTRY_PRODUCT
from app.main import app
from app.marketing import workflow
from app.marketing.workflow_models import MarketingCreativeReuseRecord
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


class TestReuseStaysWithinItsTest(MarketingWorkflowFixtures):
    """#293 plan B: only this test's creatives can be reused, even between two tests of one product card."""
    MISMATCH = "只能复用本测试的素材；同一产品其他测试中的素材，目前不能跨测试使用。"

    def setUp(self):
        super().setUp()
        _, hero = self._reference_version(self.context, name="Card hero")
        _, group = self._group_version(self.context, hero["id"])
        self.card, _ = self._publish(self.context, ENTRY_PRODUCT, "Alpha", self._product_content(hero["id"], group["id"]))

    def create(self):
        # Both tests start from the same product card; a test in another Workspace pastes its own text.
        response = self.client.post("/api/v1/marketing/projects?include_languages=true&include_plan_review=true&include_brand_style=true&include_execution_plan=true",
            headers=self.headers(), json={"source_text": "Alpha 充电器\n65W，多口，面向差旅办公用户。",
                                          **({"product_id": self.card} if self.card else {})})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def started(self):
        """A test with its own generated creatives in its first round."""
        project = self.confirmed()
        self.generate(project)
        return project, self.start_round(project)

    def next_round(self, project, row):
        self.command(row, "close", {"reason": "user_stopped", "note": "Prepare the next small round"})
        project = self.read(project)
        response = self.edit(project, "confirm_plan", plan=project["plan"])
        self.assertEqual(response.status_code, 200, response.text)
        return self.start_round(response.json())

    def reuse(self, row, body):
        return self.client.post(f"/api/v1/marketing/rounds/{row['id']}/creatives/reuse", headers=self.headers(row["revision"]), json=body)

    def reuses(self):
        with tenant_session(self.context) as session:
            return session.scalars(select(MarketingCreativeReuseRecord).where(
                MarketingCreativeReuseRecord.workspace_id == self.context.workspace_id)).all()

    def foreign_version(self):
        """A committed creative of a test in another Workspace."""
        mine, card = self.context, self.card
        self.context, self.card = self._make_context(), None
        try:
            return self.started()[1]["assets"][0]["asset_version_id"]
        finally:
            self.context, self.card = mine, card

    def test_a_creative_of_another_test_of_the_same_card_is_refused_with_the_scope_spelled_out(self):
        first, first_round = self.started()
        second, second_round = self.started()
        # Each confirmed understanding anchors its own test; the product list folds both into the one card (J-32).
        self.assertNotEqual(first["product_id"], second["product_id"])
        listed = self.client.get("/api/v1/products").json()["items"]
        self.assertEqual(len(listed), 1)
        self.assertNotIn(listed[0]["product_id"], (first["product_id"], second["product_id"]))
        refused = self.reuse(second_round, {"asset_version_id": first_round["assets"][0]["asset_version_id"],
                                            "combination_id": second_round["assets"][0]["combination_id"]})
        self.assertEqual(refused.status_code, 422, refused.text)
        self.assertEqual((refused.json()["code"], refused.json()["detail"]), ("asset_product_mismatch", self.MISMATCH))
        self.assertEqual(self.reuses(), [])
        current = self.client.get(f"/api/v1/marketing/rounds/{second_round['id']}").json()["data"]
        self.assertEqual((current["revision"], len(current["assets"])), (second_round["revision"], len(second_round["assets"])))

    def test_the_same_test_reuses_its_own_creative_in_its_next_round(self):
        project, first = self.started()
        source = first["assets"][0]
        row = self.next_round(project, first)
        row = self.command(row, "creatives/reuse", {"asset_version_id": source["asset_version_id"],
                                                    "combination_id": source["combination_id"]}, 201)
        reused = [a for a in row["assets"] if a["asset_version_id"] == source["asset_version_id"]]
        self.assertEqual(len(reused), 1)
        self.assertFalse(reused[0]["ready"])
        self.assertEqual(len(self.reuses()), 1)

    def test_reuse_needs_this_workspace_a_delegated_identity_and_current_authorization(self):
        foreign = self.foreign_version()
        project, first = self.started()
        source = first["assets"][0]
        row = self.next_round(project, first)
        body = {"asset_version_id": source["asset_version_id"], "combination_id": source["combination_id"]}
        mine = self.context
        self.context = self._make_context()
        response = self.reuse(row, body)
        self.assertEqual((response.status_code, response.json()["code"]), (404, "round_not_found"), response.text)
        self.context = mine
        response = self.reuse(row, {**body, "asset_version_id": foreign})
        self.assertEqual((response.status_code, response.json()["code"]), (404, "asset_not_found"), response.text)
        app.dependency_overrides.pop(request_tenant_context)
        try:
            with patch.object(config, "LOCAL_IDENTITY_ENABLED", False):
                response = self.reuse(row, body)
        finally:
            app.dependency_overrides[request_tenant_context] = lambda: self.context
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
