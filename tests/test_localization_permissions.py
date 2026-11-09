"""Permission and tenancy tests for the Localization batch command router
(Issue #24 Task 18, spec §15 "Permissions", §16.13).

Every command exposed by ``app/localization/routes.py`` is Member+ (Owner,
Member and the system/Worker executor are the only roles ``TenantContext``
admits -- there is no role below Member), so this router has no per-route
Owner gate of its own. The Owner-only actions spec §15 actually describes
(publish/revoke DataSourceVersion, OfferVersion, manual Facts,
BrandLocalizationPolicyVersion; credential management) live in
``app.library.service``/``app.library.datasources`` -- out of this
router's scope -- so ``test_member_cannot_publish_data_source`` exercises
that boundary directly rather than through this router.
"""
from __future__ import annotations

import itertools
from contextlib import contextmanager
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
from app.adapters.persistence.models import MarketPolicyRecord, MarketPolicyVersionRecord
from app.entitlements.cost_models import SpendGuardrailRecord
from app.entitlements.models import EntitlementProjectionRecord
from app.library import service as library_service
from app.library.contracts import ENTRY_DATA_SOURCE
from app.localization.claims import create_inventory_from_analysis
from app.main import app
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext

# Distinct prefix from test_localization_batch.py's "US..." counter and
# test_localization_routes.py's "RT..." counter -- all three files share one
# suite-wide database under pytest (tests/conftest.py), and
# `market_policies.market_id` is unique across it.
_MARKET_ID_COUNTER = itertools.count(1)


def _next_market_id() -> str:
    return f"PM{next(_MARKET_ID_COUNTER):04d}"[:10]


class TestLocalizationPermissions(LibraryFixtures):
    def setUp(self):
        self.owner_ctx = self._make_context(role="owner")
        self._provision_entitlements(self.owner_ctx)
        app.dependency_overrides[request_tenant_context] = lambda: self.owner_ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    # ------------------------------------------------------------------
    # fixtures
    # ------------------------------------------------------------------

    def _provision_entitlements(self, ctx: TenantContext) -> None:
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

    def _member_ctx(self, ctx: TenantContext) -> TenantContext:
        return TenantContext(
            workspace_id=ctx.workspace_id, role="member", initiator_id="member_1",
            request_id="test-req", trace_id="trace-test",
        )

    @contextmanager
    def _as(self, ctx: TenantContext):
        """Temporarily switch ``request_tenant_context`` to ``ctx`` --
        matches ``tests/test_v1_generation_routes.py``'s
        ``_as_other_workspace`` pattern, generalized to any context (not
        only a different Workspace)."""
        app.dependency_overrides[request_tenant_context] = lambda: ctx
        try:
            yield ctx
        finally:
            app.dependency_overrides[request_tenant_context] = lambda: self.owner_ctx

    def _create_draft(self, ctx: TenantContext, tag: str) -> dict:
        """Build a confirmed Winner + a Batch draft directly against
        ``ctx`` (domain calls, not HTTP -- the currently active
        ``request_tenant_context`` override is irrelevant here), then
        create the draft over HTTP under ``ctx``."""
        winner = _build_confirmed_winner(ctx.workspace_id, ctx, tag)
        _seed_review(ctx, winner["asset_version_id"])
        market_id = _next_market_id()
        with tenant_session(ctx) as s:
            mpv = _make_market_policy_version(s, market_id)
            mpv_id = mpv.id
        with tenant_session(ctx) as s:
            row = s.get(MarketPolicyVersionRecord, mpv_id)
            mp = s.get(MarketPolicyRecord, row.market_policy_id)
            market = mp.market_id

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
        with self._as(ctx):
            resp = self.client.post(
                "/api/localization/batches", json=body, headers={"Idempotency-Key": f"ik-{tag}"},
            )
        self.assertEqual(resp.status_code, 201, resp.text)
        data = resp.json()
        data["winner"] = winner
        data["market_policy_version_id"] = mpv_id
        data["market"] = market
        return data

    # ------------------------------------------------------------------
    # tests
    # ------------------------------------------------------------------

    def test_member_cannot_publish_data_source(self):
        """spec §15: "Owner: publishes/revokes DataSourceVersion ... ".
        This command lives in app.library.service, not this router
        (Task 18's routes are all Member+) -- exercised directly to prove
        the Owner boundary this router deliberately does not re-implement.
        """
        owner = self.owner_ctx
        member = self._member_ctx(owner)
        entry = library_service.create_entry(
            owner, entry_type=ENTRY_DATA_SOURCE, display_name="Perm test DataSource", content={},
        )
        entry_id = entry["id"]
        revision = entry["draft"]["revision"]

        with self.assertRaises(library_service.LibraryForbidden):
            library_service.publish(member, entry_id, revision)

        # Owner is unaffected by the Member's rejected attempt: the Draft
        # is still exactly where the Member found it (no partial write).
        with tenant_session(owner) as s:
            from app.adapters.persistence.models import LibraryDraftRecord
            draft = s.query(LibraryDraftRecord).filter(
                LibraryDraftRecord.workspace_id == owner.workspace_id,
                LibraryDraftRecord.entry_id == entry_id,
            ).one()
            self.assertEqual(draft.revision, revision)

    def test_member_can_start_preparation(self):
        tag = "mcsp"
        owner = self.owner_ctx
        member = self._member_ctx(owner)
        draft = self._create_draft(owner, tag)

        recipe_hash = f"recipe-{tag}"
        with tenant_session(owner) as s:
            create_inventory_from_analysis(
                s, owner, asset_version_id=draft["winner"]["asset_version_id"],
                analysis_recipe_hash=recipe_hash,
                claims=[{
                    "claim_type": "headline", "scope_key": f"k1_{tag}", "ocr_text": "Hello world",
                    "confidence": 0.99, "risk": "low", "conflicting": False,
                }],
                provider="mock-vision", model="mock-vision-1", rule_version="claims-rule-v1",
                analysis_execution_mode="mock", adapter_version="mock-v1",
                mock_scenario="default", mock_version="v1",
            )

        now = datetime.now(timezone.utc)
        # Only an Owner publishes the DataSource and the policy that lists it
        # by entry id (#70); the Member then starts preparation with it.
        sources = publish_truth_sources(
            owner,
            tag,
            data_source_content={"adapter_kind": "manual", "execution_mode": "real"},
            fact_rules={"price": "skip", "availability": "skip"},
            max_staleness_seconds=86400 * 30,
        )
        with tenant_session(owner) as s:
            country = s.get(
                MarketPolicyVersionRecord,
                draft["market_policy_version_id"],
            ).country
            manual_fact_set_version_id = _make_version(
                s,
                owner.workspace_id,
                "manual_fact_set",
                tag,
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

        body = {
            "truth_resolution_policy_version_id": sources.policy_version_id,
            "manual_fact_set_version_id": manual_fact_set_version_id,
            "analysis_recipe_hash": recipe_hash,
            "analysis_provider": "mock-vision",
            "analysis_model": "mock-vision-1",
            "analysis_adapter_version": "mock-v1",
        }

        # The Member -- not the Owner who created the draft -- runs the
        # command (spec §15: "Member: ... starts/cancels/retries Batch").
        with self._as(member):
            resp = self.client.post(
                f"/api/localization/batches/{draft['batch_id']}/start-preparation", json=body,
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["missing_analysis"], [])

    def test_cross_workspace_rejected(self):
        draft = self._create_draft(self.owner_ctx, "xw1")
        other_ctx = self._make_context(role="owner")

        with self._as(other_ctx):
            resp = self.client.get(f"/api/localization/batches/{draft['batch_id']}")
        self.assertEqual(resp.status_code, 404, resp.text)

        # The owning Workspace can still see it -- this really is a
        # tenancy boundary, not a broken lookup.
        resp = self.client.get(f"/api/localization/batches/{draft['batch_id']}")
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_worker_scope_from_job_context(self):
        """The Worker's TenantContext is derived from the human request
        that queued the work (``TenantContext.for_system_execution``,
        app/platform/tenancy.py) -- it inherits the Workspace, it never
        accepts one from a request body. This router's request bodies do
        not even declare a ``workspace_id`` field, so a spoofed value in
        the JSON payload is simply an ignored extra field; this test
        proves the *effective* Workspace is always the one bound to
        ``request_tenant_context``, regardless of what a payload claims.
        """
        draft = self._create_draft(self.owner_ctx, "wsc1")
        worker_ctx = self.owner_ctx.for_system_execution()
        self.assertEqual(worker_ctx.workspace_id, self.owner_ctx.workspace_id)
        self.assertEqual(worker_ctx.role, "system")
        self.assertTrue(worker_ctx.is_system)

        with self._as(worker_ctx):
            resp = self.client.get(f"/api/localization/batches/{draft['batch_id']}")
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.json()["id"], draft["batch_id"])
            revision = resp.json()["revision"]

            # A payload-supplied "workspace_id" is not a field this router's
            # CancelInput-equivalent (cancel takes no body) or any other
            # command body declares -- it cannot steer the command to a
            # different Workspace even if a hostile caller sends it.
            resp = self.client.post(
                f"/api/localization/batches/{draft['batch_id']}/cancel",
                json={"workspace_id": "some-other-workspace"},
                headers={"If-Match": f'"{revision}"'},
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["batch_id"], draft["batch_id"])

        # The batch was cancelled in the Worker's *actual* (inherited)
        # Workspace, verifiable from the human Owner's own context.
        resp = self.client.get(f"/api/localization/batches/{draft['batch_id']}")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["phase"], "cancelled")

        # A different Workspace still never sees it -- the Worker context
        # did not leak scope to any other tenant either.
        other_ctx = self._make_context(role="owner")
        with self._as(other_ctx):
            resp = self.client.get(f"/api/localization/batches/{draft['batch_id']}")
        self.assertEqual(resp.status_code, 404, resp.text)
