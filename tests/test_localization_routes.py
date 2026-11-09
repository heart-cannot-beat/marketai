"""HTTP-level tests for the Localization batch command router (Issue #24
Task 18, spec §2E/§15/§16.13).

Exercises the real FastAPI app via ``TestClient`` with
``request_tenant_context`` overridden to a fixed ``TenantContext`` -- the
same pattern ``tests/test_v1_generation_routes.py`` uses -- so requests
hit the actual ``app/localization/routes.py`` bodies, ``tenant_session``
and the error-translation wiring, not the batch module directly.

Fixture-building (a confirmed Winner source, an effective MarketPolicy-
Version, entitlement/spend-guardrail provisioning) reuses the module-level
helpers ``tests/test_localization_batch.py`` already built and validated
for the same Batch command sequence, rather than re-deriving the
Experiments v2 winner chain here.
"""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi.testclient import TestClient

from tests.support.library_fixtures import LibraryFixtures
from tests.support.truth_sources import publish_truth_sources
from tests.test_localization_batch import (
    _build_confirmed_winner,
    _ensure_workspace,
    _make_market_policy_version,
    _make_version,
    _seed_review,
)

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    LibraryVersionRecord,
    MarketPolicyRecord,
    MarketPolicyVersionRecord,
)
from app.entitlements.cost_models import SpendGuardrailRecord
from app.entitlements.models import EntitlementProjectionRecord
from app.localization.claims import create_inventory_from_analysis
from app.main import app
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext

# Distinct prefix from test_localization_batch.py's own "US..." counter --
# both files share one suite-wide database under pytest (tests/conftest.py),
# and `market_policies.market_id` is unique across it.
_MARKET_ID_COUNTER = itertools.count(1)

#: The country of every market this harness creates: `_make_market_policy_version`
#: takes it from the first two letters of the market id.
MARKET_COUNTRY = "RT"


def _next_market_id() -> str:
    return f"{MARKET_COUNTRY}{next(_MARKET_ID_COUNTER):04d}"[:10]


class TestLocalizationRoutes(LibraryFixtures):
    def setUp(self):
        self.ctx = self._make_context(role="owner")
        self._provision_entitlements(self.ctx)
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    # ------------------------------------------------------------------
    # fixtures
    # ------------------------------------------------------------------

    def _provision_entitlements(self, ctx: TenantContext) -> None:
        """Mirrors `tests/test_localization_batch.py`'s `setUpClass`
        entitlement/guardrail seeding, generalized to any Workspace --
        `confirm()` always admits at least the edit-attempt Job, so any
        route test that reaches `confirm()` needs this."""
        now = datetime.now(timezone.utc)
        with tenant_session(ctx) as s:
            _ensure_workspace(s, ctx.workspace_id)
            if s.get(EntitlementProjectionRecord, ctx.workspace_id) is None:
                s.add(EntitlementProjectionRecord(
                    workspace_id=ctx.workspace_id, entitlement_version=1, lease_sequence=1,
                    projection_version=1, schema_version=2, semantic_hash="h" * 8,
                    valid_until=now + timedelta(days=30), issued_at=now - timedelta(days=1),
                    period_start=now - timedelta(days=1), period_end=now + timedelta(days=29),
                    resolved_entitlements_json={},
                ))
            if s.get(SpendGuardrailRecord, ctx.workspace_id) is None:
                s.add(SpendGuardrailRecord(
                    workspace_id=ctx.workspace_id, revision=1, budget_currency="USD",
                    per_operation_limit=Decimal("100.00"), rolling_24h_limit=Decimal("500.00"),
                    period_limit=Decimal("5000.00"), outstanding_exposure_limit=Decimal("1000.00"),
                    created_at=now, updated_at=now, updated_by="user_1",
                ))

    def _winner(
        self,
        tag: str,
        offer_markets: tuple[str, ...] = ("US",),
        library_versions: tuple[str, str] | None = None,
    ) -> dict:
        winner = _build_confirmed_winner(
            self.ctx.workspace_id, self.ctx, tag,
            offer_markets=offer_markets, library_versions=library_versions,
        )
        _seed_review(self.ctx, winner["asset_version_id"], product_id=winner["product_id"])
        return winner

    def _market_target(self) -> tuple[str, str]:
        market_id = _next_market_id()
        with tenant_session(self.ctx) as s:
            mpv = _make_market_policy_version(s, market_id)
            mpv_id = mpv.id
        with tenant_session(self.ctx) as s:
            row = s.get(MarketPolicyVersionRecord, mpv_id)
            mp = s.get(MarketPolicyRecord, row.market_policy_id)
            return row.id, mp.market_id

    def _create_draft_via_http(
        self,
        tag: str,
        offer_markets: tuple[str, ...] = ("US",),
        library_versions: tuple[str, str] | None = None,
    ) -> dict:
        """A Batch draft; `offer_markets` is its Offer's `market_scope`.

        Manual fact rows name the Offer entry (`offer_entry_id`) and publish
        only for an Offer whose scope covers their market (#54): pass
        `("US", MARKET_COUNTRY)` to publish facts for the Batch's market.
        `library_versions` runs the Batch on a `(ProductVersion,
        OfferVersion)` pair published through the Library.
        """
        winner = self._winner(tag, offer_markets, library_versions)
        mpv_id, market = self._market_target()
        body = {
            "product_version_id": winner["product_version_id"],
            "offer_version_id": winner["offer_version_id"],
            "truth_data_mode": "real",
            "provider_execution_mode": "mock",
            "sources": [{
                "asset_version_id": winner["asset_version_id"],
                "decision_id": winner["decision_id"],
            }],
            "market_targets": [{"market_policy_version_id": mpv_id, "market": market}],
        }
        resp = self.client.post(
            "/api/localization/batches", json=body,
            headers={"Idempotency-Key": f"ik-{tag}"},
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        data = resp.json()
        data["winner"] = winner
        data["market_policy_version_id"] = mpv_id
        data["market"] = market
        with tenant_session(self.ctx) as s:
            data["offer_entry_id"] = s.get(LibraryVersionRecord, winner["offer_version_id"]).entry_id
        return data

    def _analysis_recipe_hash(self, tag: str) -> str:
        return f"recipe-{tag}"

    def _seed_analysis(self, winner: dict, recipe_hash: str, tag: str) -> None:
        with tenant_session(self.ctx) as s:
            create_inventory_from_analysis(
                s, self.ctx, asset_version_id=winner["asset_version_id"],
                analysis_recipe_hash=recipe_hash,
                claims=[{
                    "claim_type": "headline", "scope_key": f"k1_{tag}", "ocr_text": "Hello world",
                    "confidence": 0.99, "risk": "low", "conflicting": False,
                }],
                provider="mock-vision", model="mock-vision-1", rule_version="claims-rule-v1",
                analysis_execution_mode="mock", adapter_version="mock-v1",
                mock_scenario="default", mock_version="v1",
            )

    def _start_preparation_body(self, draft: dict, recipe_hash: str) -> dict:
        now = datetime.now(timezone.utc)
        # The policy lists the DataSource by entry id (#70); both go through
        # Library publish. The request names only the policy and fact set.
        sources = publish_truth_sources(
            self.ctx,
            draft["batch_id"],
            data_source_content={"adapter_kind": "manual", "execution_mode": "real"},
            fact_rules={"price": "skip", "availability": "skip"},
            max_staleness_seconds=86400 * 30,
        )
        with tenant_session(self.ctx) as s:
            country = s.get(
                MarketPolicyVersionRecord,
                draft["market_policy_version_id"],
            ).country
            manual_fact_set_version_id = _make_version(
                s,
                self.ctx.workspace_id,
                "manual_fact_set",
                draft["batch_id"],
                content={
                    "facts": [
                        {
                            "fact_id": "price-1",
                            "fact_type": "price",
                            "country_code": country,
                            "valid_from": (now - timedelta(days=1)).isoformat(),
                            "valid_to": (now + timedelta(days=365)).isoformat(),
                            "amount": "19.99",
                            "currency": "USD",
                        },
                        {
                            "fact_id": "availability-1",
                            "fact_type": "availability",
                            "country_code": country,
                            "valid_from": (now - timedelta(days=1)).isoformat(),
                            "valid_to": (now + timedelta(days=365)).isoformat(),
                            "available": True,
                        },
                    ]
                },
            )
        return {
            "truth_resolution_policy_version_id": sources.policy_version_id,
            "manual_fact_set_version_id": manual_fact_set_version_id,
            "analysis_recipe_hash": recipe_hash,
            "analysis_provider": "mock-vision",
            "analysis_model": "mock-vision-1",
            "analysis_adapter_version": "mock-v1",
        }

    def _build_ready_batch(self, tag: str) -> dict:
        """Drive the real command sequence, through HTTP where the route
        exists, up to (but not including) `confirm()`: one source, one
        target market, Truth resolved, analysis reused (pre-seeded so no
        admission is required), Claim decisions submitted (deterministic
        source-locale projection), and copy approved."""
        draft = self._create_draft_via_http(tag)
        recipe_hash = self._analysis_recipe_hash(tag)
        self._seed_analysis(draft["winner"], recipe_hash, tag)

        resp = self.client.post(
            f"/api/localization/batches/{draft['batch_id']}/start-preparation",
            json=self._start_preparation_body(draft, recipe_hash),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["missing_analysis"], [])

        resp = self.client.post(
            f"/api/localization/batches/{draft['batch_id']}/claim-decisions",
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
            headers={"If-Match": str(self._batch_revision(draft["batch_id"]))},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        copy_version_id = resp.json()["copy_version_id"]
        self.assertTrue(copy_version_id)

        resp = self.client.post(
            f"/api/localization/batches/{draft['batch_id']}/approve-copy",
            json={"copy_version_id": copy_version_id},
            headers={"If-Match": str(self._batch_revision(draft["batch_id"]))},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        return draft

    def _batch_revision(self, batch_id: str) -> int:
        resp = self.client.get(f"/api/localization/batches/{batch_id}")
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["revision"]

    # ------------------------------------------------------------------
    # tests
    # ------------------------------------------------------------------

    def test_create_draft(self):
        draft = self._create_draft_via_http("cd1")
        self.assertEqual(draft["phase"], "draft")
        self.assertFalse(draft["reused"])
        self.assertTrue(draft["batch_id"])

        get_resp = self.client.get(f"/api/localization/batches/{draft['batch_id']}")
        self.assertEqual(get_resp.status_code, 200, get_resp.text)
        body = get_resp.json()
        self.assertEqual(body["id"], draft["batch_id"])
        self.assertEqual(body["phase"], "draft")
        self.assertEqual(len(body["sources"]), 1)
        self.assertEqual(len(body["market_targets"]), 1)
        self.assertEqual(get_resp.headers["ETag"], f'"{body["revision"]}"')

    def test_create_draft_requires_idempotency_key(self):
        winner = self._winner("cdnoik")
        mpv_id, market = self._market_target()
        resp = self.client.post(
            "/api/localization/batches",
            json={
                "product_version_id": winner["product_version_id"],
                "offer_version_id": winner["offer_version_id"],
                "truth_data_mode": "real",
                "provider_execution_mode": "mock",
                "sources": [{
                    "asset_version_id": winner["asset_version_id"],
                    "decision_id": winner["decision_id"],
                }],
                "market_targets": [{"market_policy_version_id": mpv_id, "market": market}],
            },
        )
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_get_batch_unknown_returns_404(self):
        resp = self.client.get("/api/localization/batches/lb_missing")
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_create_draft_validation_findings_reach_client(self):
        winner = self._winner("cdfind")
        resp = self.client.post(
            "/api/localization/batches",
            json={
                "product_version_id": winner["product_version_id"],
                "offer_version_id": winner["offer_version_id"],
                "truth_data_mode": "real",
                "provider_execution_mode": "mock",
                "sources": [{
                    "asset_version_id": winner["asset_version_id"],
                    "decision_id": winner["decision_id"],
                }],
                # No MarketPolicyVersion with this id exists -> batch.py's
                # BatchValidationError carries a
                # "market_US:invalid_market_policy_version" finding.
                "market_targets": [{"market_policy_version_id": "mpv_missing", "market": "US"}],
            },
            headers={"Idempotency-Key": "ik-cdfind"},
        )
        self.assertEqual(resp.status_code, 422, resp.text)
        body = resp.json()
        self.assertEqual(body["code"], "batch_validation_failed")
        # The findings list must actually reach the client, not be
        # silently dropped by app.library.api's shared HTTPException
        # handler -- this is exactly what register_problem_handlers fixes.
        self.assertIn("market_US:invalid_market_policy_version", body["findings"])

    def test_start_preparation(self):
        tag = "sp1"
        draft = self._create_draft_via_http(tag)
        recipe_hash = self._analysis_recipe_hash(tag)
        self._seed_analysis(draft["winner"], recipe_hash, tag)

        # Built once and replayed verbatim below: the manual facts' validity
        # window is part of what `resolve_or_reuse_truth_package` hashes, so
        # rebuilding the body with a fresh `now` per call would produce a
        # genuinely different TruthPackage rather than a replay.
        request_body = self._start_preparation_body(draft, recipe_hash)

        resp = self.client.post(
            f"/api/localization/batches/{draft['batch_id']}/start-preparation",
            json=request_body,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["truth_package_id"])
        self.assertEqual(body["preparation_stage"], "claim_review")
        self.assertEqual(body["missing_analysis"], [])
        self.assertFalse(body["replayed"])

        # Same command replayed -- idempotent, not a duplicate side effect.
        # Replay comes from the frozen Batch even if a selected Library
        # version has since been withdrawn; it must not reinterpret history.
        with tenant_session(self.ctx) as session:
            session.get(
                LibraryVersionRecord,
                request_body["truth_resolution_policy_version_id"],
            ).state = "withdrawn"
        replay = self.client.post(
            f"/api/localization/batches/{draft['batch_id']}/start-preparation",
            json=request_body,
        )
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertTrue(replay.json()["replayed"])

    def test_start_preparation_rejects_browser_supplied_truth_command(self):
        draft = self._create_draft_via_http("sp-spoof")
        recipe_hash = self._analysis_recipe_hash("sp-spoof")
        self._seed_analysis(draft["winner"], recipe_hash, "sp-spoof")
        body = self._start_preparation_body(draft, recipe_hash)
        body["truth_command"] = {
            "product_version_id": "spoofed-product",
            "markets": [],
            "fact_policies": [
                {"fact_type": "price", "on_failure": "block"}
            ],
        }

        response = self.client.post(
            f"/api/localization/batches/{draft['batch_id']}/start-preparation",
            json=body,
        )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["code"], "validation_error")
        self.assertTrue(
            any(
                error["field"].endswith("truth_command")
                for error in response.json()["field_errors"]
            )
        )

    def test_start_preparation_rejects_caller_supplied_data_sources(self):
        # #70: the DataSources come from the policy's source_priority; the
        # request no longer carries data_source_version_ids at all.
        draft = self._create_draft_via_http("sp-sources")
        recipe_hash = self._analysis_recipe_hash("sp-sources")
        self._seed_analysis(draft["winner"], recipe_hash, "sp-sources")
        body = self._start_preparation_body(draft, recipe_hash)
        body["data_source_version_ids"] = ["lv_caller_choice"]

        response = self.client.post(
            f"/api/localization/batches/{draft['batch_id']}/start-preparation",
            json=body,
        )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["code"], "validation_error")
        self.assertTrue(
            any(
                error["field"].endswith("data_source_version_ids")
                for error in response.json()["field_errors"]
            )
        )

    def test_start_preparation_rejects_policy_missing_claim_fact_family(self):
        tag = "sp-policy-gap"
        draft = self._create_draft_via_http(tag)
        recipe_hash = self._analysis_recipe_hash(tag)
        with tenant_session(self.ctx) as session:
            create_inventory_from_analysis(
                session,
                self.ctx,
                asset_version_id=draft["winner"]["asset_version_id"],
                analysis_recipe_hash=recipe_hash,
                claims=[{
                    "claim_type": "delivery_claim",
                    "scope_key": "hero-delivery",
                    "confidence": 0.99,
                }],
                provider="mock-vision",
                model="mock-vision-1",
                rule_version="claims-rule-v1",
                analysis_execution_mode="mock",
                adapter_version="mock-v1",
                mock_scenario="default",
                mock_version="v1",
            )
        body = self._start_preparation_body(draft, recipe_hash)

        response = self.client.post(
            f"/api/localization/batches/{draft['batch_id']}/start-preparation",
            json=body,
        )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["code"], "batch_validation_failed")
        self.assertIn(
            "truth_policy_missing_fact_type:delivery",
            response.json()["findings"],
        )

    def test_confirm(self):
        draft = self._build_ready_batch("cf1")
        revision = self._batch_revision(draft["batch_id"])
        resp = self.client.post(
            f"/api/localization/batches/{draft['batch_id']}/confirm",
            json={
                "edit_provider": "mock-edit", "edit_model": "mock-edit-1",
                "edit_adapter_version": "mock-edit-v1",
            },
            headers={"If-Match": f'"{revision}"'},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(len(body["item_ids"]), 1)
        self.assertFalse(body["replayed"])

        get_resp = self.client.get(f"/api/localization/batches/{draft['batch_id']}")
        self.assertEqual(get_resp.status_code, 200, get_resp.text)
        self.assertEqual(get_resp.json()["phase"], "running")
        self.assertEqual(len(get_resp.json()["items"]), 1)

    def test_confirm_requires_if_match(self):
        draft = self._build_ready_batch("cfnoim")
        resp = self.client.post(
            f"/api/localization/batches/{draft['batch_id']}/confirm",
            json={
                "edit_provider": "mock-edit", "edit_model": "mock-edit-1",
                "edit_adapter_version": "mock-edit-v1",
            },
        )
        self.assertEqual(resp.status_code, 428, resp.text)

    def test_confirm_stale_revision_returns_409(self):
        draft = self._build_ready_batch("cfstale")
        resp = self.client.post(
            f"/api/localization/batches/{draft['batch_id']}/confirm",
            json={
                "edit_provider": "mock-edit", "edit_model": "mock-edit-1",
                "edit_adapter_version": "mock-edit-v1",
            },
            headers={"If-Match": '"999"'},
        )
        # 409, matching the codebase-wide CAS-conflict convention
        # (app.platform.etag.check_revision_match, app.generation.v1_router,
        # app.generation.api's own StaleRevisionError handler) -- not 412.
        self.assertEqual(resp.status_code, 409, resp.text)
        body = resp.json()
        self.assertEqual(body["code"], "stale_revision")
        # The structured detail fields batch.StaleRevisionError carries
        # (record/record_id/expected_revision) must actually reach the
        # client -- app.library.api's shared HTTPException handler would
        # silently drop them, which is exactly why this router registers
        # its own per-exception-type handler (register_problem_handlers).
        self.assertEqual(body["record"], "batch")
        self.assertEqual(body["record_id"], draft["batch_id"])
        self.assertEqual(body["expected_revision"], 999)
        self.assertIn("expected revision 999", body["detail"])

    def test_cancel(self):
        draft = self._build_ready_batch("cn1")
        revision = self._batch_revision(draft["batch_id"])
        resp = self.client.post(
            f"/api/localization/batches/{draft['batch_id']}/cancel",
            headers={"If-Match": f'"{revision}"'},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertFalse(body["already_terminal"])

        get_resp = self.client.get(f"/api/localization/batches/{draft['batch_id']}")
        self.assertEqual(get_resp.json()["phase"], "cancelled")

        # Cancelling an already-terminal Batch is a no-op, not an error.
        again = self.client.post(
            f"/api/localization/batches/{draft['batch_id']}/cancel",
            headers={"If-Match": f'"{revision}"'},
        )
        self.assertEqual(again.status_code, 200, again.text)
        self.assertTrue(again.json()["already_terminal"])

    def test_list_eligible_sources(self):
        winner = self._winner("es1")
        resp = self.client.get("/api/localization/eligible-sources")
        self.assertEqual(resp.status_code, 200, resp.text)
        items = resp.json()["items"]
        self.assertTrue(any(i["asset_version_id"] == winner["asset_version_id"] for i in items))

    def test_get_batch_outcome(self):
        draft = self._create_draft_via_http("bo1")
        resp = self.client.get(f"/api/localization/batches/{draft['batch_id']}/outcome")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["batch_id"], draft["batch_id"])
        self.assertEqual(body["outcome"], "none")
        self.assertEqual(body["item_count"], 0)
