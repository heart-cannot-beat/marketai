"""Issue #28 Localization v1 interface and persisted projection coverage."""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    ExperimentDecisionRecord,
    ExperimentVersionRecord,
    LibraryVersionRecord,
    JobRecord,
    LocalizationBatchRecord,
    LocalizationClaimDecisionSetRecord,
    MarketPolicyRecord,
    MarketPolicyVersionRecord,
    TruthPackageRecord,
    TruthPackageMarketRecord,
)
from app.main import app
from app.platform.http_identity import request_tenant_context
from app.localization.claims import create_inventory_from_analysis
from tests.support.library_fixtures import LibraryFixtures
from tests.support.truth_sources import publish_truth_sources
from tests.test_localization_batch import (
    _build_confirmed_winner,
    _make_market_policy_version,
    _make_version,
    _seed_review,
)
from tests import test_localization_routes as legacy_routes


_MARKETS = itertools.count(1)


class TestLocalizationV1Routes(LibraryFixtures):
    def setUp(self):
        self.ctx = self._make_context(role="owner")
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def _winner(
        self,
        tag: str,
        *,
        asset_language: str = "en-US",
        source_market_snapshot: dict[str, str] | None = None,
        persist_source_snapshot: bool = True,
    ) -> dict:
        winner = _build_confirmed_winner(
            self.ctx.workspace_id,
            self.ctx,
            tag,
            asset_language=asset_language,
        )
        if not persist_source_snapshot or source_market_snapshot is not None:
            # Experiment freeze now rejects missing source context and
            # normalizes case.  Rewrite both immutable candidates after the
            # valid fixture has frozen so the read projection still proves it
            # fails closed for legacy/corrupt persisted rows.
            with tenant_session(self.ctx) as session:
                decision = session.get(
                    ExperimentDecisionRecord, winner["decision_id"]
                )
                version = session.get(
                    ExperimentVersionRecord, decision.experiment_version_id
                )
                offer = session.get(
                    LibraryVersionRecord, winner["offer_version_id"]
                )
                frozen = dict(version.frozen_snapshot_json or {})
                offer_content = dict(offer.content_json or {})
                if persist_source_snapshot:
                    frozen["source_market_snapshot"] = dict(
                        source_market_snapshot or {}
                    )
                    offer_content["source_market_snapshot"] = dict(
                        source_market_snapshot or {}
                    )
                else:
                    frozen.pop("source_market_snapshot", None)
                    offer_content.pop("source_market_snapshot", None)
                version.frozen_snapshot_json = frozen
                offer.content_json = offer_content
        _seed_review(self.ctx, winner["asset_version_id"])
        return winner

    def _market(self) -> tuple[str, str]:
        market_id = f"V1{next(_MARKETS):04d}"
        with tenant_session(self.ctx) as session:
            version = _make_market_policy_version(session, market_id)
            version_id = version.id
        with tenant_session(self.ctx) as session:
            version = session.get(MarketPolicyVersionRecord, version_id)
            market = session.get(MarketPolicyRecord, version.market_policy_id)
            return version.id, market.market_id

    def _draft_body(self, tag: str) -> dict:
        winner = self._winner(tag)
        policy_version_id, market = self._market()
        return {
            "product_version_id": winner["product_version_id"],
            "offer_version_id": winner["offer_version_id"],
            "truth_data_mode": "mock",
            "provider_execution_mode": "mock",
            "requested_evidence_tier": "mock",
            "sources": [{
                "asset_version_id": winner["asset_version_id"],
                "decision_id": winner["decision_id"],
            }],
            "market_targets": [{
                "market_policy_version_id": policy_version_id,
                "market": market,
            }],
        }

    def test_contract_has_stable_operations_and_idempotency(self):
        schema = app.openapi()
        expected = {
            ("get", "/api/v1/localization/workspace"): "get_localization_workspace",
            ("get", "/api/v1/localization/batches"): "list_localization_batches",
            ("post", "/api/v1/localization/batches"): "create_localization_batch",
            ("get", "/api/v1/localization/batches/{batch_id}"): "get_localization_batch",
            ("post", "/api/v1/localization/batches/{batch_id}/start-preparation"): "start_localization_preparation",
            ("post", "/api/v1/localization/batches/{batch_id}/claim-decisions"): "submit_localization_claim_decisions",
            ("post", "/api/v1/localization/batches/{batch_id}/approve-copy"): "approve_localization_copy",
            ("post", "/api/v1/localization/batches/{batch_id}/confirm"): "confirm_localization_batch",
            ("get", "/api/v1/localization/batches/{batch_id}/items/{item_id}"): "get_localization_item",
            ("post", "/api/v1/localization/batches/{batch_id}/items/{item_id}/retry"): "retry_localization_item",
            ("post", "/api/v1/localization/batches/{batch_id}/items/{item_id}/resume"): "resume_localization_item",
            (
                "post",
                "/api/v1/localization/batches/{batch_id}/items/{item_id}"
                "/attempts/{attempt_id}/authorize-isolated-qc",
            ): "authorize_localization_isolated_qc",
            (
                "post",
                "/api/v1/localization/batches/{batch_id}/items/{item_id}"
                "/attempts/{attempt_id}/adopt",
            ): "adopt_localization_isolated_output",
            ("post", "/api/v1/localization/batches/{batch_id}/cancel"): "cancel_localization_batch",
            ("get", "/api/v1/localization/batches/{batch_id}/outcome"): "get_localization_batch_outcome",
        }
        for (method, path), operation_id in expected.items():
            operation = schema["paths"][path][method]
            self.assertEqual(operation["operationId"], operation_id)
            self.assertIn("responses", operation)
            if method == "post":
                self.assertTrue(operation["x-marketai-idempotent"])

    def test_isolated_recovery_routes_require_idempotency_and_a_tenant(self):
        """The two isolated-output recovery commands are exposed on the
        same terms as every other Localization command: an
        `Idempotency-Key` is mandatory, and a batch belonging to another
        Workspace is not found rather than forbidden."""
        body = self._draft_body("v1-iso-routes")
        resp = self.client.post(
            "/api/v1/localization/batches",
            json=body,
            headers={"Idempotency-Key": "ik-v1-iso-routes"},
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        batch_id = resp.json()["batch_id"]

        paths = (
            f"/api/v1/localization/batches/{batch_id}/items/li_absent"
            f"/attempts/la_absent/authorize-isolated-qc",
            f"/api/v1/localization/batches/{batch_id}/items/li_absent"
            f"/attempts/la_absent/adopt",
        )

        for path in paths:
            with self.subTest(path=path, case="missing idempotency key"):
                self.assertEqual(self.client.post(path).status_code, 422)

        other = self._make_context(role="owner")
        app.dependency_overrides[request_tenant_context] = lambda: other
        try:
            for path in paths:
                with self.subTest(path=path, case="other workspace"):
                    resp = self.client.post(
                        path, headers={"Idempotency-Key": "ik-foreign"}
                    )
                    self.assertEqual(resp.status_code, 404, resp.text)
        finally:
            app.dependency_overrides[request_tenant_context] = lambda: self.ctx

    def test_workspace_create_list_and_detail_are_tenant_scoped(self):
        body = self._draft_body("v1-read")
        with tenant_session(self.ctx) as session:
            truth_policy_id = _make_version(
                session,
                self.ctx.workspace_id,
                "truth_resolution_policy",
                "v1-workspace-policy",
            )
            data_source_id = _make_version(
                session,
                self.ctx.workspace_id,
                "data_source",
                "v1-workspace-source",
            )
        workspace = self.client.get("/api/v1/localization/workspace")
        self.assertEqual(workspace.status_code, 200, workspace.text)
        self.assertTrue(any(
            source["asset_version_id"] == body["sources"][0]["asset_version_id"]
            for source in workspace.json()["eligible_sources"]
        ))
        projected_source = next(
            source for source in workspace.json()["eligible_sources"]
            if source["asset_version_id"] == body["sources"][0]["asset_version_id"]
        )
        # Source context belongs to the immutable Winner chain, not the target
        # market catalog.  A standard US Winner remains usable even when US is
        # absent from the platform MarketPolicy rows.
        self.assertEqual(projected_source["source_market_snapshot"], {
            "market": "US",
            "placement": "feed",
            "locale": "en-US",
            "country": "US",
            "currency": "USD",
        })
        self.assertIsNone(projected_source["source_context_problem"])
        self.assertTrue(any(
            option["id"] == truth_policy_id
            for option in workspace.json()["truth_resolution_policies"]
        ))
        self.assertTrue(any(
            option["id"] == data_source_id
            for option in workspace.json()["data_sources"]
        ))

        created = self.client.post(
            "/api/v1/localization/batches",
            json=body,
            headers={"Idempotency-Key": "v1-read-create"},
        )
        self.assertEqual(created.status_code, 201, created.text)
        batch_id = created.json()["batch_id"]

        page = self.client.get("/api/v1/localization/batches?limit=1")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual(page.json()["items"][0]["id"], batch_id)
        self.assertEqual(page.json()["items"][0]["source_count"], 1)

        detail = self.client.get(f"/api/v1/localization/batches/{batch_id}")
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.headers["ETag"], f'"{detail.json()["revision"]}"')
        self.assertEqual(detail.json()["sources"][0]["decision_id"], body["sources"][0]["decision_id"])
        self.assertEqual(detail.json()["markets"][0]["market"], body["market_targets"][0]["market"])
        self.assertEqual(detail.json()["markets"][0]["readiness"], "pending")

        for role in ("owner", "member"):
            other = self._make_context(role=role)
            app.dependency_overrides[request_tenant_context] = lambda other=other: other
            hidden = self.client.get(f"/api/v1/localization/batches/{batch_id}")
            self.assertEqual(hidden.status_code, 404, hidden.text)
            problem = hidden.json()
            self.assertEqual(problem["code"], "localization_not_found")
            self.assertEqual(problem["status"], 404)
            self.assertIn("trace_id", problem)

            denied_write = self.client.post(
                f"/api/v1/localization/batches/{batch_id}/cancel",
                headers={
                    "Idempotency-Key": f"cross-workspace-{role}",
                    "If-Match": str(detail.json()["revision"]),
                },
            )
            self.assertEqual(denied_write.status_code, 404, denied_write.text)
            self.assertEqual(
                denied_write.json()["code"], "localization_not_found"
            )

        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        still_owned = self.client.get(f"/api/v1/localization/batches/{batch_id}")
        self.assertEqual(still_owned.status_code, 200, still_owned.text)
        self.assertEqual(still_owned.json()["phase"], "draft")

    def test_workspace_returns_typed_missing_source_context_only_for_real_gaps(self):
        winner = self._winner(
            "v1-genuinely-missing-source-locale",
            asset_language="en",
            persist_source_snapshot=False,
        )

        response = self.client.get("/api/v1/localization/workspace")

        self.assertEqual(response.status_code, 200, response.text)
        source = next(
            row for row in response.json()["eligible_sources"]
            if row["asset_version_id"] == winner["asset_version_id"]
        )
        self.assertIsNone(source["source_market_snapshot"])
        self.assertEqual(source["source_context_problem"], {
            "code": "missing_source_context",
            "missing_fields": [
                "market", "placement", "locale", "country", "currency",
            ],
            "invalid_fields": [],
            "conflicting_fields": [],
        })

    def test_workspace_rejects_conflicting_atomic_source_snapshots(self):
        winner = self._winner("v1-conflicting-source-context")
        with tenant_session(self.ctx) as session:
            decision = session.get(
                ExperimentDecisionRecord, winner["decision_id"]
            )
            version = session.get(
                ExperimentVersionRecord, decision.experiment_version_id
            )
            frozen = dict(version.frozen_snapshot_json or {})
            frozen["source_market_snapshot"] = {
                "market": "US",
                "placement": "feed",
                "locale": "en-US",
                "country": "US",
                "currency": "EUR",
            }
            version.frozen_snapshot_json = frozen

        response = self.client.get("/api/v1/localization/workspace")

        self.assertEqual(response.status_code, 200, response.text)
        source = next(
            row for row in response.json()["eligible_sources"]
            if row["asset_version_id"] == winner["asset_version_id"]
        )
        self.assertIsNone(source["source_market_snapshot"])
        self.assertEqual(
            source["source_context_problem"]["code"],
            "conflicting_source_context",
        )
        self.assertEqual(
            source["source_context_problem"]["conflicting_fields"],
            ["currency"],
        )

    def test_workspace_rejects_invalid_atomic_source_snapshot(self):
        winner = self._winner(
            "v1-invalid-source-context",
            source_market_snapshot={
                "market": "US",
                "placement": "feed",
                "locale": "en-US",
                "country": "US",
                "currency": "usd",
            },
        )

        response = self.client.get("/api/v1/localization/workspace")

        self.assertEqual(response.status_code, 200, response.text)
        source = next(
            row for row in response.json()["eligible_sources"]
            if row["asset_version_id"] == winner["asset_version_id"]
        )
        self.assertIsNone(source["source_market_snapshot"])
        self.assertEqual(
            source["source_context_problem"]["code"],
            "invalid_source_context",
        )
        self.assertEqual(
            source["source_context_problem"]["invalid_fields"],
            ["currency"],
        )

    def test_workspace_projects_exact_inventory_and_truth_policy_requirements(self):
        winner = self._winner("v1-source-claim-requirements")
        with tenant_session(self.ctx) as session:
            policy_id = _make_version(
                session,
                self.ctx.workspace_id,
                "truth_resolution_policy",
                "v1-claim-policy",
                content={
                    "rules": [
                        {"fact_type": "product_claim", "on_failure": "degrade"},
                        {"fact_type": "availability", "on_failure": "skip"},
                        {"fact_type": "price", "on_failure": "block"},
                    ],
                },
            )
            superseded = create_inventory_from_analysis(
                session,
                self.ctx,
                asset_version_id=winner["asset_version_id"],
                analysis_recipe_hash="localization-analysis-v1",
                claims=[{"claim_type": "price", "scope_key": "old-price"}],
                provider="mock-vision",
                model="mock-v1",
                rule_version="claims-v1",
                analysis_execution_mode="mock",
                adapter_version="mock-v1",
            )
            latest = create_inventory_from_analysis(
                session,
                self.ctx,
                asset_version_id=winner["asset_version_id"],
                analysis_recipe_hash="localization-analysis-v1",
                claims=[
                    {"claim_type": "shipping", "scope_key": "shipping"},
                    {"claim_type": "bundle_price", "scope_key": "bundle"},
                    {"claim_type": "discount", "scope_key": "discount"},
                    {"claim_type": "discount", "scope_key": "discount-duplicate"},
                    {"claim_type": "certification_claim", "scope_key": "cert"},
                ],
                provider="mock-vision",
                model="mock-v2",
                rule_version="claims-v2",
                analysis_execution_mode="mock",
                adapter_version="mock-v2",
            )
            other_recipe = create_inventory_from_analysis(
                session,
                self.ctx,
                asset_version_id=winner["asset_version_id"],
                analysis_recipe_hash="other-analysis-recipe",
                claims=[{"claim_type": "headline", "scope_key": "headline"}],
                provider="mock-vision",
                model="mock-v1",
                rule_version="claims-v1",
                analysis_execution_mode="mock",
                adapter_version="mock-v1",
            )
            superseded_id = superseded.id
            latest_id = latest.id
            other_recipe_id = other_recipe.id

        response = self.client.get("/api/v1/localization/workspace")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        policy = next(
            row for row in payload["truth_resolution_policies"]
            if row["id"] == policy_id
        )
        self.assertEqual(policy["fact_policies"], [
            {"fact_type": "availability", "on_failure": "skip"},
            {"fact_type": "price", "on_failure": "block"},
            {"fact_type": "product_claim", "on_failure": "degrade"},
        ])
        source = next(
            row for row in payload["eligible_sources"]
            if row["asset_version_id"] == winner["asset_version_id"]
        )
        self.assertEqual(source["reusable_inventories"], [
            {
                "id": latest_id,
                "analysis_recipe_hash": "localization-analysis-v1",
                "version_number": 2,
                "claim_types": [
                    "bundle_price",
                    "certification_claim",
                    "discount",
                    "shipping",
                ],
            },
            {
                "id": other_recipe_id,
                "analysis_recipe_hash": "other-analysis-recipe",
                "version_number": 1,
                "claim_types": ["headline"],
            },
        ])
        self.assertNotEqual(superseded_id, latest_id)

        market_policy_version_id, market = self._market()
        created = self.client.post(
            "/api/v1/localization/batches",
            json={
                "product_version_id": winner["product_version_id"],
                "offer_version_id": winner["offer_version_id"],
                "truth_data_mode": "mock",
                "provider_execution_mode": "mock",
                "requested_evidence_tier": "mock",
                "sources": [{
                    "asset_version_id": winner["asset_version_id"],
                    "decision_id": winner["decision_id"],
                }],
                "market_targets": [{
                    "market_policy_version_id": market_policy_version_id,
                    "market": market,
                }],
            },
            headers={"Idempotency-Key": "v1-source-claim-requirements"},
        )
        self.assertEqual(created.status_code, 201, created.text)
        detail = self.client.get(
            f"/api/v1/localization/batches/{created.json()['batch_id']}"
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(
            detail.json()["sources"][0]["reusable_inventories"],
            source["reusable_inventories"],
        )

    def test_create_rejects_client_owned_source_market_snapshot(self):
        body = self._draft_body("v1-client-source-context")
        client_snapshot = {
            "market": "US",
            "placement": "feed",
            "locale": "en-US",
            "country": "US",
            "currency": "EUR",
        }
        body["source_market_snapshot"] = client_snapshot
        body["sources"][0]["source_market_snapshot"] = client_snapshot

        response = self.client.post(
            "/api/v1/localization/batches",
            json=body,
            headers={"Idempotency-Key": "v1-client-source-context"},
        )

        self.assertEqual(response.status_code, 422, response.text)
        fields = {error["field"] for error in response.json()["field_errors"]}
        self.assertIn("body.source_market_snapshot", fields)
        self.assertIn("body.sources.0.source_market_snapshot", fields)
        with tenant_session(self.ctx) as session:
            self.assertIsNone(
                session.scalar(
                    select(LocalizationBatchRecord).where(
                        LocalizationBatchRecord.workspace_id
                        == self.ctx.workspace_id,
                        LocalizationBatchRecord.product_version_id
                        == body["product_version_id"],
                        LocalizationBatchRecord.offer_version_id
                        == body["offer_version_id"],
                    )
                )
            )


def test_confirmed_item_projects_attempt_qc_cost_and_lineage():
    """Use the battle-tested Issue #24 fixture driver, then read it only through v1."""
    harness = legacy_routes.TestLocalizationRoutes(methodName="test_create_draft")
    harness.setUp()
    try:
        draft = harness._build_ready_batch("v1-item")
        ready = harness.client.get(
            f"/api/v1/localization/batches/{draft['batch_id']}"
        )
        assert ready.status_code == 200, ready.text
        assert ready.json()["truth_status"] == "full"
        assert ready.json()["markets"][0]["facts"]
        assert ready.json()["inventories"][0]["claims"][0]["ocr_text"] == "Hello world"
        assert ready.json()["claim_decision_sets"][0]["target_market"] == draft["market"]
        assert ready.json()["copies"][0]["state"] == "approved"
        assert next(
            action for action in ready.json()["allowed_actions"]
            if action["action"] == "confirm"
        )["allowed"] is True
        revision = harness._batch_revision(draft["batch_id"])
        confirmed = harness.client.post(
            f"/api/v1/localization/batches/{draft['batch_id']}/confirm",
            json={
                "edit_provider": "mock-edit",
                "edit_model": "mock-edit-1",
                "edit_adapter_version": "mock-v1",
                "external_qc_steps": [],
                "max_attempts": 3,
            },
            headers={"Idempotency-Key": "v1-item-confirm", "If-Match": str(revision)},
        )
        assert confirmed.status_code == 200, confirmed.text
        item_id = confirmed.json()["item_ids"][0]

        item = harness.client.get(
            f"/api/v1/localization/batches/{draft['batch_id']}/items/{item_id}"
        )
        assert item.status_code == 200, item.text
        payload = item.json()
        assert item.headers["ETag"] == f'"{payload["revision"]}"'
        assert payload["lineage"]["source_asset_version_id"] == draft["winner"]["asset_version_id"]
        assert payload["lineage"]["truth_package_id"]
        assert "totals" in payload["cost"]
        assert "ledger" in payload["cost"]
        assert payload["max_attempts"] == 3
        assert {action["action"] for action in payload["allowed_actions"]} == {"retry", "resume"}
    finally:
        harness.doCleanups()


def test_batch_projection_and_confirm_keep_exact_inventory_across_client_restart():
    """A newer same-recipe Inventory cannot silently replace Batch truth."""
    harness = legacy_routes.TestLocalizationRoutes(methodName="test_create_draft")
    harness.setUp()
    try:
        draft = harness._build_ready_batch("v1-restart-inventory")
        batch_url = f"/api/v1/localization/batches/{draft['batch_id']}"
        before = harness.client.get(batch_url)
        assert before.status_code == 200, before.text
        before_payload = before.json()
        bound_inventory_id = before_payload["sources"][0]["inventory_id"]
        recipe_hash = before_payload["sources"][0]["analysis_recipe_hash"]
        assert bound_inventory_id == before_payload["inventories"][0]["id"]

        with tenant_session(harness.ctx) as session:
            decision_set = session.query(LocalizationClaimDecisionSetRecord).filter_by(
                workspace_id=harness.ctx.workspace_id,
                origin_batch_id=draft["batch_id"],
            ).one()
            immutable_decision = (
                decision_set.id,
                decision_set.inventory_id,
                decision_set.canonical_hash,
                decision_set.decision_count,
            )
            newer = create_inventory_from_analysis(
                session,
                harness.ctx,
                asset_version_id=draft["winner"]["asset_version_id"],
                analysis_recipe_hash=recipe_hash,
                claims=[{
                    "claim_type": "headline",
                    "scope_key": "newer-unbound-claim",
                    "ocr_text": "Must not replace the bound inventory",
                    "confidence": 0.1,
                    "risk": "high",
                    "conflicting": True,
                }],
                provider="mock-vision",
                model="mock-vision-1",
                rule_version="claims-rule-v2",
                analysis_execution_mode="mock",
                adapter_version="mock-v2",
                mock_scenario="newer",
                mock_version="v2",
            )
            assert newer.id != bound_inventory_id
            assert newer.version_number > before_payload["inventories"][0]["version_number"]

        # A new TestClient has no browser or fixture memory from the first one;
        # the exact identity is reconstructed from durable Batch/work records.
        with TestClient(app, raise_server_exceptions=False) as restarted_client:
            restarted = restarted_client.get(batch_url)
            assert restarted.status_code == 200, restarted.text
            payload = restarted.json()
            assert payload["sources"][0]["inventory_id"] == bound_inventory_id
            assert [row["id"] for row in payload["inventories"]] == [
                bound_inventory_id
            ]
            assert payload["frozen_recipe"]["analysis_recipe_hash"] == recipe_hash
            assert payload["allowed_actions"][3] == {
                "action": "confirm", "allowed": True, "reason_code": None,
            }

            confirmed = restarted_client.post(
                f"{batch_url}/confirm",
                json={
                    "edit_provider": "mock-edit",
                    "edit_model": "mock-edit-1",
                    "edit_adapter_version": "mock-v1",
                    "external_qc_steps": [],
                    "max_attempts": 3,
                },
                headers={
                    "Idempotency-Key": "v1-restart-confirm",
                    "If-Match": str(payload["revision"]),
                },
            )
            assert confirmed.status_code == 200, confirmed.text
            item_id = confirmed.json()["item_ids"][0]
            item = restarted_client.get(f"{batch_url}/items/{item_id}")
            assert item.status_code == 200, item.text
            assert item.json()["inventory_id"] == bound_inventory_id

        with tenant_session(harness.ctx) as session:
            decision_set = session.get(
                LocalizationClaimDecisionSetRecord, immutable_decision[0]
            )
            assert (
                decision_set.id,
                decision_set.inventory_id,
                decision_set.canonical_hash,
                decision_set.decision_count,
            ) == immutable_decision
            frozen = session.get(LocalizationBatchRecord, draft["batch_id"])
            assert frozen.frozen_recipe_json["analysis_inventory_ids"] == {
                draft["winner"]["asset_version_id"]: bound_inventory_id
            }
    finally:
        harness.doCleanups()


def test_high_risk_claim_is_rejected_by_v1_and_stays_rejected_after_restart():
    harness = legacy_routes.TestLocalizationRoutes(methodName="test_create_draft")
    harness.setUp()
    try:
        draft = harness._create_draft_via_http("v1-high-risk")
        recipe_hash = harness._analysis_recipe_hash("v1-high-risk")
        with tenant_session(harness.ctx) as session:
            create_inventory_from_analysis(
                session,
                harness.ctx,
                asset_version_id=draft["winner"]["asset_version_id"],
                analysis_recipe_hash=recipe_hash,
                claims=[{
                    "claim_type": "price",
                    "scope_key": "high-risk-price",
                    "ocr_text": "$999",
                    "confidence": 0.4,
                    "risk": "high",
                    "conflicting": True,
                }],
                provider="mock-vision",
                model="mock-vision-1",
                rule_version="claims-rule-v1",
                analysis_execution_mode="mock",
                adapter_version="mock-v1",
                mock_scenario="high-risk",
                mock_version="v1",
            )

        started = harness.client.post(
            f"/api/v1/localization/batches/{draft['batch_id']}/start-preparation",
            json=harness._start_preparation_body(draft, recipe_hash),
            headers={"Idempotency-Key": "v1-high-risk-start"},
        )
        assert started.status_code == 200, started.text
        rejected = harness.client.post(
            f"/api/v1/localization/batches/{draft['batch_id']}/claim-decisions",
            json={
                "asset_version_id": draft["winner"]["asset_version_id"],
                "target_market": draft["market"],
                "analysis_recipe_hash": recipe_hash,
                "decisions": [{"claim_index": 0, "action": "preserve"}],
                "copy_policy_snapshot": {},
                "source_locale": "en-US",
                "target_locale": "en-US",
                "requested_evidence_tier": "mock",
            },
            headers={
                "Idempotency-Key": "v1-high-risk-decisions",
                "If-Match": str(harness._batch_revision(draft["batch_id"])),
            },
        )
        assert rejected.status_code == 422, rejected.text
        assert rejected.json()["code"] == "batch_validation_failed"
        assert "claim_0_requires_confirmation" in rejected.json()["findings"]

        with TestClient(app, raise_server_exceptions=False) as restarted_client:
            persisted = restarted_client.get(
                f"/api/v1/localization/batches/{draft['batch_id']}"
            )
            assert persisted.status_code == 200, persisted.text
            assert persisted.json()["claim_decision_sets"] == []
            assert persisted.json()["copies"] == []
    finally:
        harness.doCleanups()


def test_preparation_commands_require_if_match():
    harness = legacy_routes.TestLocalizationRoutes(methodName="test_create_draft")
    harness.setUp()
    try:
        draft = harness._build_ready_batch("v1-precondition")
        response = harness.client.post(
            f"/api/v1/localization/batches/{draft['batch_id']}/approve-copy",
            json={
                "copy_version_id": harness.client.get(
                    f"/api/v1/localization/batches/{draft['batch_id']}"
                ).json()["copies"][0]["id"]
            },
            headers={"Idempotency-Key": "v1-precondition-approval"},
        )
        assert response.status_code == 428, response.text
        assert response.json()["code"] == "precondition_required"
    finally:
        harness.doCleanups()


def test_expired_truth_projects_stale_and_disables_confirm():
    harness = legacy_routes.TestLocalizationRoutes(methodName="test_create_draft")
    harness.setUp()
    try:
        draft = harness._build_ready_batch("v1-stale-truth")
        with tenant_session(harness.ctx) as session:
            batch = session.get(LocalizationBatchRecord, draft["batch_id"])
            truth = session.get(TruthPackageRecord, batch.truth_package_id)
            truth.valid_until = datetime.now(timezone.utc) - timedelta(seconds=1)

        refreshed = harness.client.get(
            f"/api/v1/localization/batches/{draft['batch_id']}"
        )
        assert refreshed.status_code == 200, refreshed.text
        payload = refreshed.json()
        assert payload["truth_freshness"] == "stale"
        confirm = next(
            action for action in payload["allowed_actions"]
            if action["action"] == "confirm"
        )
        assert confirm == {
            "action": "confirm",
            "allowed": False,
            "reason_code": "batch_not_ready",
        }
        rejected = harness.client.post(
            f"/api/v1/localization/batches/{draft['batch_id']}/confirm",
            json={
                "edit_provider": "mock-edit",
                "edit_model": "mock-edit-1",
                "edit_adapter_version": "mock-v1",
                "external_qc_steps": [],
                "max_attempts": 3,
            },
            headers={
                "Idempotency-Key": "v1-stale-truth-confirm",
                "If-Match": str(payload["revision"]),
            },
        )
        assert rejected.status_code == 422, rejected.text
        assert rejected.json()["code"] == "batch_validation_failed"
        assert rejected.json()["findings"] == ["truth_stale:valid_until_expired"]
    finally:
        harness.doCleanups()


def test_unavailable_pair_projects_skipped_reason_and_no_job_through_v1():
    """One unavailable market cannot erase its executable sibling."""
    harness = legacy_routes.TestLocalizationRoutes(methodName="test_create_draft")
    harness.setUp()
    try:
        tag = f"v1-pair-{next(_MARKETS)}"
        winner = harness._winner(tag)
        recipe_hash = harness._analysis_recipe_hash(tag)
        harness._seed_analysis(winner, recipe_hash, tag)
        # The policy lists the Mock DataSource by entry id (#70); both pass
        # Library publish.
        truth_policy_id = publish_truth_sources(
            harness.ctx,
            tag,
            data_source_content={
                "adapter_kind": "mock",
                "execution_mode": "mock",
                "config": {
                    "mock_scenario": "standard_9_market",
                    "mock_scenarios_by_country": {
                        "GB": "standard_9_market",
                        "AU": "unavailable",
                    },
                },
            },
            fact_rules={"price": "block", "availability": "block"},
        ).policy_version_id
        with tenant_session(harness.ctx) as session:
            ready_market = f"GB{next(_MARKETS):06d}"[:10]
            unavailable_market = f"AU{next(_MARKETS):06d}"[:10]
            ready_policy = _make_market_policy_version(session, ready_market)
            unavailable_policy = _make_market_policy_version(
                session, unavailable_market
            )
            market_targets = [
                {
                    "market": ready_market,
                    "market_policy_version_id": ready_policy.id,
                },
                {
                    "market": unavailable_market,
                    "market_policy_version_id": unavailable_policy.id,
                },
            ]

        created = harness.client.post(
            "/api/v1/localization/batches",
            json={
                "product_version_id": winner["product_version_id"],
                "offer_version_id": winner["offer_version_id"],
                "truth_data_mode": "mock",
                "provider_execution_mode": "mock",
                "requested_evidence_tier": "mock",
                "sources": [
                    {
                        "asset_version_id": winner["asset_version_id"],
                        "decision_id": winner["decision_id"],
                    }
                ],
                "market_targets": market_targets,
            },
            headers={"Idempotency-Key": f"{tag}-create"},
        )
        assert created.status_code == 201, created.text
        batch_id = created.json()["batch_id"]
        revision = harness.client.get(
            f"/api/v1/localization/batches/{batch_id}"
        ).json()["revision"]
        started = harness.client.post(
            f"/api/v1/localization/batches/{batch_id}/start-preparation",
            json={
                "truth_resolution_policy_version_id": truth_policy_id,
                "analysis_recipe_hash": recipe_hash,
                "analysis_provider": "mock-vision",
                "analysis_model": "mock-vision-1",
                "analysis_adapter_version": "mock-v1",
            },
            headers={
                "Idempotency-Key": f"{tag}-start",
                "If-Match": str(revision),
            },
        )
        assert started.status_code == 200, started.text
        assert started.json()["missing_analysis"] == []

        for ordinal, target in enumerate(market_targets):
            decision = harness.client.post(
                f"/api/v1/localization/batches/{batch_id}/claim-decisions",
                json={
                    "asset_version_id": winner["asset_version_id"],
                    "target_market": target["market"],
                    "analysis_recipe_hash": recipe_hash,
                    "decisions": [{"claim_index": 0, "action": "preserve"}],
                    "copy_policy_snapshot": {},
                    "source_locale": "en-US",
                    "target_locale": "en-US",
                    "requested_evidence_tier": "mock",
                },
                headers={
                    "Idempotency-Key": f"{tag}-decision-{ordinal}",
                    "If-Match": str(harness._batch_revision(batch_id)),
                },
            )
            assert decision.status_code == 200, decision.text
            approved = harness.client.post(
                f"/api/v1/localization/batches/{batch_id}/approve-copy",
                json={"copy_version_id": decision.json()["copy_version_id"]},
                headers={
                    "Idempotency-Key": f"{tag}-approve-{ordinal}",
                    "If-Match": str(harness._batch_revision(batch_id)),
                },
            )
            assert approved.status_code == 200, approved.text

        confirmed = harness.client.post(
            f"/api/v1/localization/batches/{batch_id}/confirm",
            json={
                "edit_provider": "mock-edit",
                "edit_model": "mock-edit-1",
                "edit_adapter_version": "mock-v1",
                "external_qc_steps": [],
                "max_attempts": 3,
            },
            headers={
                "Idempotency-Key": "v1-unavailable-pair-confirm",
                "If-Match": str(harness._batch_revision(batch_id)),
            },
        )
        assert confirmed.status_code == 200, confirmed.text
        assert len(confirmed.json()["item_ids"]) == 2
        detail = harness.client.get(
            f"/api/v1/localization/batches/{batch_id}"
        )
        assert detail.status_code == 200, detail.text
        items = {item["target_market"]: item for item in detail.json()["items"]}
        assert items[ready_market]["state"] == "queued"
        skipped = items[unavailable_market]
        assert skipped["state"] == "skipped"
        assert skipped["reason_code"] == "skipped_market_unavailable"
        assert skipped["attempt_count"] == 0
        item = harness.client.get(
            f"/api/v1/localization/batches/{batch_id}/items/{skipped['id']}"
        )
        assert item.status_code == 200, item.text
        assert item.json()["attempts"] == []
        with tenant_session(harness.ctx) as session:
            jobs = session.scalars(
                select(JobRecord).where(
                    JobRecord.workspace_id == harness.ctx.workspace_id,
                    JobRecord.type == "localization.edit_attempt",
                    JobRecord.resource_id.in_(confirmed.json()["item_ids"]),
                )
            ).all()
            assert [job.resource_id for job in jobs] == [items[ready_market]["id"]]
    finally:
        harness.doCleanups()
