# tests/test_meta_api_routes.py
"""Meta/Launch domain API route tests (Task 16, spec §8.2).

Flat in ``tests/`` and using the real ``app.main.app`` with ``TestClient``
and the shared migrated engine -- mirroring ``tests/test_experiment_api.py``
and ``tests/test_upload_intents.py`` -- since no shared ``client``/
``auth_headers`` fixture exists in ``tests/conftest.py`` (the brief's own
Step 1 snippet assumes fixtures that do not exist in this codebase). The
auth dependency is overridden with a fixed ``TenantContext`` per
``tests/test_tenant_safety.py``'s established pattern.

``WorkspaceRecord`` uses ``name`` (not ``display_name``/``slug``).

Critical: ``request_tenant_context()`` never populates ``meta_mode`` in
production -- neither ``DelegatedIdentityMiddleware._build_tenant_context``
nor the ``LOCAL_IDENTITY_ENABLED`` fallback set it (see
``app/platform/delegated_identity.py`` / ``app/platform/http_identity.py``).
So the injected ``TenantContext`` here deliberately carries
``meta_mode=None``, matching production, and every request supplies
``meta_mode`` itself (a body field on POSTs, a query parameter on GETs) --
exactly what a real caller must do. Routes bind it via
``_effective_context()``/``replace()`` (``app/meta/api.py``,
``app/launch/api.py``).

The brief's ``test_create_contract_route`` hardcodes a nonexistent
``connection_id="mcon_test"`` and asserts ``resp.status_code in (201, 404)``
-- kept verbatim as ``test_create_contract_route_unknown_connection``, plus
a real-connection variant that asserts a clean 201.
"""
from __future__ import annotations

import asyncio
import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import patch
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import config as app_config
from app.adapters.persistence.database import engine, platform_session, tenant_session
from app.adapters.persistence.models import (
    AssetRecord,
    AssetVersionRecord,
    DimensionConceptKeyRecord,
    GenerationRunRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    PlanRecord,
    ProductRecord,
    QualityReviewRecord,
    JobRecord,
    WorkspaceRecord,
    utcnow,
)
from app.generation.models import RunItemRecord
from app.experiments.service import add_entry_v2, create_experiment_v2, freeze_experiment_v2
from app.main import app
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext
from app.worker_runtime import WorkerRuntime
from tests.experiment_governance_fixtures import (
    govern_asset_versions,
    govern_product_offer_versions,
)

META_MODE = "customer_sandbox"


def _migrate_to_head() -> None:
    db_file = engine.url.database
    if db_file and db_file != ":memory:":
        Path(db_file).parent.mkdir(parents=True, exist_ok=True)
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(cfg, "head")


def _ensure_workspace(workspace_id: str) -> None:
    with platform_session() as session:
        if session.get(WorkspaceRecord, workspace_id) is None:
            session.add(WorkspaceRecord(id=workspace_id, name=workspace_id, created_at=utcnow()))


class MetaApiRoutesTestCase(unittest.TestCase):
    """Base: shared migration, a fresh Workspace + client per test.

    ``MetaConnectionRecord`` enforces at most one *active* connection per
    ``(workspace_id, meta_mode)`` (``uq_meta_connection_active``, a partial
    unique index) -- so a shared class-level Workspace would make every test
    after the first one's connection collide. Each test method gets its own
    Workspace instead.
    """

    @classmethod
    def setUpClass(cls) -> None:
        _migrate_to_head()

    def setUp(self) -> None:
        workspace_id = f"ws_meta_api_{uuid.uuid4().hex[:12]}"
        _ensure_workspace(workspace_id)
        # `meta_mode=None`, matching what `request_tenant_context()` actually
        # produces in production -- see the module docstring. Every route
        # under test must derive its effective meta_mode from the request,
        # not from this injected context.
        self.ctx = TenantContext(
            workspace_id=workspace_id, role="owner", initiator_id="usr_meta_api",
        )
        previous_confirmation_secret = app_config.LAUNCH_CONFIRMATION_SECRET
        app_config.LAUNCH_CONFIRMATION_SECRET = "meta-api-test-confirmation-secret"
        self.addCleanup(
            setattr, app_config, "LAUNCH_CONFIRMATION_SECRET", previous_confirmation_secret,
        )
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    # -- helpers ----------------------------------------------------------

    def _create_connection(self, ad_account_id: str = "act_123456") -> dict:
        resp = self.client.post(
            "/api/v1/meta/connections",
            json={"ad_account_id": ad_account_id, "meta_mode": META_MODE},
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()

    def _create_binding(self, connection_id: str, ad_account_id: str = "act_123456") -> dict:
        resp = self.client.post(
            f"/api/v1/meta/connections/{connection_id}/bindings",
            json={"ad_account_id": ad_account_id, "meta_mode": META_MODE},
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()


# ---------------------------------------------------------------------------
# meta_mode extraction: request body/query, never the injected context
# ---------------------------------------------------------------------------


class MetaModeFromRequestTests(MetaApiRoutesTestCase):
    def test_injected_context_carries_no_meta_mode(self):
        """Sanity check for the whole suite's premise (module docstring):
        the dependency-overridden context matches production -- no
        meta_mode -- so every passing test below proves the route read it
        from the request, not from context."""
        self.assertIsNone(self.ctx.meta_mode)

    def test_create_connection_route_meta_mode_comes_from_body(self):
        resp = self.client.post(
            "/api/v1/meta/connections",
            json={"ad_account_id": "act_frombody", "meta_mode": META_MODE},
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(resp.json()["meta_mode"], META_MODE)

    def test_get_connection_route_meta_mode_comes_from_query(self):
        created = self._create_connection("act_fromquery")
        # Without the required `meta_mode` query param, FastAPI rejects the
        # request before the route body ever runs (422) -- there is no
        # fallback to a context-carried meta_mode to silently succeed with.
        missing = self.client.get(f"/api/v1/meta/connections/{created['id']}")
        self.assertEqual(missing.status_code, 422)

        found = self.client.get(
            f"/api/v1/meta/connections/{created['id']}", params={"meta_mode": META_MODE},
        )
        self.assertEqual(found.status_code, 200)
        self.assertEqual(found.json()["id"], created["id"])

        # A well-formed but wrong meta_mode does not fall back to context
        # either -- it looks in the wrong mode's scope and finds nothing.
        wrong_mode = self.client.get(
            f"/api/v1/meta/connections/{created['id']}", params={"meta_mode": "controlled_mock"},
        )
        self.assertEqual(wrong_mode.status_code, 404)


# ---------------------------------------------------------------------------
# Connection CRUD
# ---------------------------------------------------------------------------


class ConnectionRouteTests(MetaApiRoutesTestCase):
    def test_create_connection_route(self):
        resp = self.client.post(
            "/api/v1/meta/connections",
            json={"ad_account_id": "act_123456", "meta_mode": META_MODE},
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertTrue(data["id"].startswith("mcon_"))
        self.assertEqual(data["state"], "active")
        self.assertEqual(data["meta_mode"], META_MODE)

    def test_get_connection_route(self):
        created = self._create_connection("act_get_1")
        resp = self.client.get(
            f"/api/v1/meta/connections/{created['id']}", params={"meta_mode": META_MODE},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["id"], created["id"])

    def test_get_connection_route_not_found(self):
        resp = self.client.get(
            "/api/v1/meta/connections/mcon_does_not_exist", params={"meta_mode": META_MODE},
        )
        self.assertEqual(resp.status_code, 404)

    def test_retire_connection_route(self):
        created = self._create_connection("act_retire_1")
        resp = self.client.post(
            f"/api/v1/meta/connections/{created['id']}/retire", json={"meta_mode": META_MODE},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["state"], "retired")

    def test_retire_connection_route_twice_is_rejected(self):
        created = self._create_connection("act_retire_2")
        first = self.client.post(
            f"/api/v1/meta/connections/{created['id']}/retire", json={"meta_mode": META_MODE},
        )
        self.assertEqual(first.status_code, 200)
        second = self.client.post(
            f"/api/v1/meta/connections/{created['id']}/retire", json={"meta_mode": META_MODE},
        )
        self.assertEqual(second.status_code, 400)

    def test_create_connection_member_forbidden(self):
        """Member role -> 403 from service-layer Owner check."""
        app.dependency_overrides[request_tenant_context] = lambda: TenantContext(
            workspace_id=self.ctx.workspace_id, role="member",
            initiator_id="usr_member",
        )
        resp = self.client.post("/api/v1/meta/connections", json={
            "meta_mode": META_MODE, "ad_account_id": "act_test_member",
        })
        self.assertEqual(resp.status_code, 403)

    def test_create_connection_duplicate_returns_409(self):
        """Second create for same (workspace, mode) -> 409, original unchanged."""
        resp1 = self.client.post("/api/v1/meta/connections", json={
            "meta_mode": META_MODE, "ad_account_id": "act_dup_test",
        })
        self.assertEqual(resp1.status_code, 201, resp1.text)
        original_id = resp1.json()["id"]

        resp2 = self.client.post("/api/v1/meta/connections", json={
            "meta_mode": META_MODE, "ad_account_id": "act_dup_test_2",
        })
        self.assertEqual(resp2.status_code, 409)

        # Original connection unchanged
        resp3 = self.client.get(
            f"/api/v1/meta/connections/{original_id}",
            params={"meta_mode": META_MODE},
        )
        self.assertEqual(resp3.status_code, 200)
        self.assertEqual(resp3.json()["state"], "active")
        self.assertEqual(resp3.json()["ad_account_id"], "act_dup_test")

    def test_list_meta_connections(self):
        """GET /api/v1/meta/connections returns connections for workspace+mode."""
        resp = self.client.get(
            "/api/v1/meta/connections", params={"meta_mode": META_MODE},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["items"], [])

        # Create one -- no ad_account_id, so the service generates the
        # sandbox value (Task 1: `_sandbox_ad_account_id`).
        create_resp = self.client.post("/api/v1/meta/connections", json={
            "meta_mode": META_MODE,
        })
        self.assertEqual(create_resp.status_code, 201, create_resp.text)

        resp2 = self.client.get(
            "/api/v1/meta/connections", params={"meta_mode": META_MODE},
        )
        self.assertEqual(resp2.status_code, 200)
        items = resp2.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["state"], "active")
        self.assertTrue(items[0]["ad_account_id"].startswith("act_sandbox_"))

    def test_create_connection_cross_workspace_denied(self):
        """Connection created in workspace A is not visible from workspace B."""
        resp = self.client.post("/api/v1/meta/connections", json={
            "meta_mode": META_MODE,
        })
        self.assertEqual(resp.status_code, 201, resp.text)
        conn_id = resp.json()["id"]

        # Switch to workspace B
        other_workspace = f"ws_meta_api_{uuid.uuid4().hex[:12]}"
        _ensure_workspace(other_workspace)
        app.dependency_overrides[request_tenant_context] = lambda: TenantContext(
            workspace_id=other_workspace, role="owner",
            initiator_id="usr_meta_api",
        )
        resp2 = self.client.get(
            f"/api/v1/meta/connections/{conn_id}",
            params={"meta_mode": META_MODE},
        )
        self.assertEqual(resp2.status_code, 404)


# ---------------------------------------------------------------------------
# Binding create + supersede
# ---------------------------------------------------------------------------


class BindingRouteTests(MetaApiRoutesTestCase):
    def test_create_binding_route(self):
        connection = self._create_connection("act_bind_1")
        resp = self.client.post(
            f"/api/v1/meta/connections/{connection['id']}/bindings",
            json={"ad_account_id": "act_bind_1", "meta_mode": META_MODE},
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertTrue(data["id"].startswith("mbind_"))
        self.assertEqual(data["state"], "active")

    def test_list_bindings_route(self):
        connection = self._create_connection("act_bind_list")
        self._create_binding(connection["id"], "act_bind_list")
        resp = self.client.get(
            f"/api/v1/meta/connections/{connection['id']}/bindings", params={"meta_mode": META_MODE},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["meta_connection_id"], connection["id"])

    def test_supersede_binding_route(self):
        connection = self._create_connection("act_supersede_1")
        binding = self._create_binding(connection["id"], "act_supersede_1")
        resp = self.client.post(
            f"/api/v1/meta/connections/{connection['id']}/bindings/{binding['id']}/supersede",
            json={"ad_account_id": "act_supersede_1", "meta_mode": META_MODE},
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        new_binding = resp.json()
        self.assertNotEqual(new_binding["id"], binding["id"])
        self.assertEqual(new_binding["state"], "active")

        old = self.client.get(
            f"/api/v1/meta/connections/{connection['id']}/bindings", params={"meta_mode": META_MODE},
        ).json()["items"]
        old_row = next(row for row in old if row["id"] == binding["id"])
        self.assertEqual(old_row["state"], "superseded")
        self.assertEqual(old_row["superseded_by_id"], new_binding["id"])


# ---------------------------------------------------------------------------
# Contract generation create
# ---------------------------------------------------------------------------


class ContractRouteTests(MetaApiRoutesTestCase):
    def _contract_body(self, connection_id: str, **overrides) -> dict:
        body = {
            "connection_id": connection_id,
            "meta_mode": META_MODE,
            "contract_type": "daily_insights",
            "ad_account_id": "act_1",
            "fields": ["impressions", "spend"],
            "attribution_window": {"click": "7d"},
            "breakdowns": [],
            "time_window": {"granularity": "daily"},
            "local_timezone": "UTC",
        }
        body.update(overrides)
        return body

    def test_create_contract_route(self):
        connection = self._create_connection("act_contract_1")
        resp = self.client.post(
            "/api/v1/meta/contracts", json=self._contract_body(connection["id"]),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        data = resp.json()
        self.assertTrue(data["id"].startswith("ocg_"))
        self.assertEqual(data["generation_number"], 1)
        self.assertEqual(data["state"], "active")

    def test_create_contract_route_unknown_connection(self):
        # Brief's Step 1 test verbatim: a hardcoded, nonexistent connection_id.
        resp = self.client.post(
            "/api/v1/meta/contracts", json=self._contract_body("mcon_test"),
        )
        self.assertIn(resp.status_code, (201, 404))
        if resp.status_code != 201:
            self.assertEqual(resp.status_code, 404)

    def test_create_contract_route_duplicate_active_is_conflict(self):
        connection = self._create_connection("act_contract_dup")
        body = self._contract_body(connection["id"])
        first = self.client.post("/api/v1/meta/contracts", json=body)
        self.assertEqual(first.status_code, 201)
        second = self.client.post("/api/v1/meta/contracts", json=body)
        self.assertEqual(second.status_code, 409)

    def test_list_contracts_route(self):
        connection = self._create_connection("act_contract_list")
        self.client.post(
            "/api/v1/meta/contracts", json=self._contract_body(connection["id"]),
        )
        resp = self.client.get(
            "/api/v1/meta/contracts",
            params={"connection_id": connection["id"], "meta_mode": META_MODE},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["meta_connection_id"], connection["id"])


# ---------------------------------------------------------------------------
# Launch: multi-candidate preview create/get/confirm
# ---------------------------------------------------------------------------


class LaunchPreviewRouteTests(MetaApiRoutesTestCase):
    def _frozen_experiment(self) -> dict:
        suffix = uuid.uuid4().hex[:10]
        product_id = f"prod_launch_{suffix}"
        product_entry_id = f"le_product_{suffix}"
        offer_entry_id = f"le_offer_{suffix}"
        product_version_id = f"pv_launch_{suffix}"
        offer_version_id = f"ov_launch_{suffix}"
        plan_id = f"plan_launch_{suffix}"
        run_id = f"run_launch_{suffix}"
        asset_version_ids: list[str] = []
        with platform_session() as session:
            session.add(ProductRecord(
                id=product_id, workspace_id=self.ctx.workspace_id,
                name="Launch product", product_profile_entry_id=product_entry_id,
            ))
            for entry_id, version_id, entry_type in (
                (product_entry_id, product_version_id, "product"),
                (offer_entry_id, offer_version_id, "offer"),
            ):
                session.add(LibraryEntryRecord(
                    id=entry_id, workspace_id=self.ctx.workspace_id,
                    entry_type=entry_type, display_name=f"Launch {entry_type}",
                ))
                session.flush()
                content = {} if entry_type == "product" else {
                    "product_version_id": product_version_id,
                    "market_scope": ["US"],
                    "landing_page_url": "https://example.test/launch",
                    "valid_from": "2026-01-01",
                    "valid_to": "2027-01-01",
                }
                session.add(LibraryVersionRecord(
                    id=version_id, workspace_id=self.ctx.workspace_id,
                    entry_id=entry_id, entry_type=entry_type, version_no=1,
                    content_json=content, content_hash=str(len(content) + 1) * 64,
                    state="effective", eligibility="eligible", published_by="system",
                ))
            session.flush()
            session.add(PlanRecord(
                id=plan_id, workspace_id=self.ctx.workspace_id,
                product_id=product_id, mode="full", cap=0, total_full=2,
                snapshot_hash="p" * 64,
            ))
            session.flush()
            session.add(GenerationRunRecord(
                id=run_id, workspace_id=self.ctx.workspace_id,
                product_id=product_id, plan_id=plan_id,
                provider="mock", model="mock", aspect_ratio="1:1",
                image_size="1024", variants=1, use_ref=False,
                status="done", total=2, done=2, failed=0,
                est_cost=Decimal("0"), currency="USD",
                execution_mode="sandbox",
            ))
            session.flush()
            for index in range(2):
                concept_key_id = f"concept_launch_{suffix}_{index}"
                run_item_id = f"ritem_launch_{suffix}_{index}"
                session.add(DimensionConceptKeyRecord(
                    id=concept_key_id, workspace_id=self.ctx.workspace_id,
                    scope="workspace", name=f"Launch concept {index}",
                    created_by="test",
                ))
                session.add(RunItemRecord(
                    id=run_item_id, workspace_id=self.ctx.workspace_id,
                    generation_run_id=run_id, ordinal=index,
                    combination_identity_json={
                        "concepts": [{
                            "dimension_key": "creative_angle",
                            "concept_key_id": concept_key_id,
                            "stable_dimension_signature": "creative_angle:v1",
                            "value": {"index": index},
                        }],
                    },
                    combination_fingerprint=str(index + 3) * 64,
                    stable_dimension_signature="creative_angle:v1",
                    semantic_fingerprint=str(index + 5) * 64,
                    execution_mode="sandbox", state="succeeded",
                ))
                session.flush()
                asset_id = f"asset_launch_{suffix}_{index}"
                asset_version_id = f"av_launch_{suffix}_{index}"
                session.add(AssetRecord(
                    id=asset_id, workspace_id=self.ctx.workspace_id,
                    product_id=product_id, run_id=run_id,
                    combo_id=f"C{index}", combo_code=f"C{index}", tags_json={},
                    filename=f"launch-{index}.png", provider="mock", model="mock",
                    prompt="test", fingerprint=f"launch-{suffix}-{index}", status="ok",
                ))
                session.flush()
                session.add(AssetVersionRecord(
                    id=asset_version_id, workspace_id=self.ctx.workspace_id,
                    asset_id=asset_id, kind="generated", aspect_ratio="1:1",
                    language="en", object_key=f"launch-{index}.png",
                    content_sha256=str(index + 1) * 64, byte_size=1,
                    mime_type="image/png", origin_kind="generation",
                    execution_mode="sandbox",
                    producing_run_item_id=run_item_id,
                    truth_dependency_expected=False,
                    truth_dependency_policy_version="truth-dependency-v1",
                    truth_dependency_evidence_json={"expected": False},
                    truth_dependency_evidence_hash=str(index + 7) * 64,
                ))
                session.flush()
                session.add(QualityReviewRecord(
                    id=f"review_launch_{suffix}_{index}",
                    workspace_id=self.ctx.workspace_id,
                    product_id=product_id, asset_id=asset_id,
                    asset_version_id=asset_version_id,
                    legacy_unscoped=False, review_value="approved",
                    actor_id="reviewer",
                ))
                asset_version_ids.append(asset_version_id)
            govern_product_offer_versions(
                session,
                self.ctx,
                product_id=product_id,
                product_version_id=product_version_id,
                offer_version_id=offer_version_id,
            )
            govern_asset_versions(
                session,
                self.ctx,
                product_id=product_id,
                asset_version_ids=asset_version_ids,
                tag=f"launch-{suffix}",
            )

        experiment = create_experiment_v2(
            self.ctx, product_id=product_id, name="Launch experiment",
            draft_context={
                "market": "US", "currency": "USD", "placement": "feed",
                "product_version_id": product_version_id,
                "offer_version_id": offer_version_id,
                "budget": {"daily_amount": "5", "total_amount": "100"},
                "schedule": {
                    "start_at": "2026-09-10T00:00:00+00:00",
                    "end_at": "2026-09-20T00:00:00+00:00", "timezone": "UTC",
                },
                "attribution": {"click_window_days": 7, "view_window_days": 1},
                "statistical_plan": {"primary_metric": "ctr"},
            },
        )
        add_entry_v2(
            self.ctx, experiment["id"], asset_version_id=asset_version_ids[0],
            expected_revision=1,
        )
        add_entry_v2(
            self.ctx, experiment["id"], asset_version_id=asset_version_ids[1],
            expected_revision=2,
        )
        return freeze_experiment_v2(self.ctx, experiment["id"], expected_revision=3)

    def _preview_body(self, connection_id: str, ad_account_id: str) -> dict:
        frozen = self._frozen_experiment()
        return {
            "experiment_id": frozen["id"],
            "experiment_version_id": frozen["version_id"],
            "connection_id": connection_id,
            "meta_mode": META_MODE,
            "ad_account_id": ad_account_id,
            "candidates": [
                {
                    "experiment_version_candidate_id": candidate["id"],
                    "asset_version_id": candidate["asset_version_id"],
                    "creative_config": {"headline": f"Test {index + 1}"},
                }
                for index, candidate in enumerate(frozen["candidates"])
            ],
            "daily_budget_minor": 500,
            "currency": "USD",
            "campaign_config": {"objective": "OUTCOME_TRAFFIC", "initial_status": "PAUSED"},
        }

    def test_create_multi_candidate_preview_route(self):
        connection = self._create_connection("act_preview_1")
        self._create_binding(connection["id"], "act_preview_1")
        resp = self.client.post(
            "/api/v1/launch/previews",
            json=self._preview_body(connection["id"], "act_preview_1"),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        data = resp.json()
        self.assertTrue(data["id"].startswith("lprev_"))
        self.assertEqual(data["candidate_count"], 2)
        # spec §3.15: total_daily_cost_minor = daily_budget_minor * candidate_count
        self.assertEqual(data["total_daily_cost_minor"], 1000)
        self.assertEqual(len(data["candidates"]), 2)
        self.assertEqual(data["state"], "draft")
        self.assertTrue(data["meta_binding_id"])

    def test_create_multi_candidate_preview_route_unknown_connection(self):
        resp = self.client.post(
            "/api/v1/launch/previews",
            json=self._preview_body("mcon_missing", "act_preview_missing"),
        )
        self.assertEqual(resp.status_code, 404)

    def test_get_multi_candidate_preview_route(self):
        connection = self._create_connection("act_preview_get")
        self._create_binding(connection["id"], "act_preview_get")
        created = self.client.post(
            "/api/v1/launch/previews",
            json=self._preview_body(connection["id"], "act_preview_get"),
        ).json()
        resp = self.client.get(
            f"/api/v1/launch/previews/{created['id']}", params={"meta_mode": META_MODE},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["id"], created["id"])

    def test_confirm_multi_candidate_preview_route(self):
        connection = self._create_connection("act_preview_confirm")
        self._create_binding(connection["id"], "act_preview_confirm")
        created = self.client.post(
            "/api/v1/launch/previews",
            json=self._preview_body(connection["id"], "act_preview_confirm"),
        ).json()
        resp = self.client.post(
            f"/api/v1/launch/previews/{created['id']}/confirm",
            json={"meta_mode": META_MODE, "preview_hash": created["preview_hash"]},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        confirmation = resp.json()
        self.assertEqual(confirmation["state"], "confirmed")
        self.assertTrue(confirmation["confirmation_token"].startswith("confirm-v2."))
        self.assertTrue(confirmation["confirmation_context_hash"])
        self.assertTrue(confirmation["confirmation_expires_at"])

    def test_confirm_multi_candidate_preview_route_hash_mismatch(self):
        connection = self._create_connection("act_preview_confirm_bad")
        self._create_binding(connection["id"], "act_preview_confirm_bad")
        created = self.client.post(
            "/api/v1/launch/previews",
            json=self._preview_body(connection["id"], "act_preview_confirm_bad"),
        ).json()
        resp = self.client.post(
            f"/api/v1/launch/previews/{created['id']}/confirm",
            json={"meta_mode": META_MODE, "preview_hash": "0" * 64},
        )
        self.assertEqual(resp.status_code, 409)

    def test_execute_multi_candidate_preview_route(self):
        """A confirmed preview provisions one durable, PAUSED Sandbox saga."""
        connection = self._create_connection("act_preview_execute")
        self._create_binding(connection["id"], "act_preview_execute")
        created = self.client.post(
            "/api/v1/launch/previews",
            json=self._preview_body(connection["id"], "act_preview_execute"),
        ).json()
        confirmation = self.client.post(
            f"/api/v1/launch/previews/{created['id']}/confirm",
            json={"meta_mode": META_MODE, "preview_hash": created["preview_hash"]},
        ).json()
        idempotency_key = f"launch-execute-{uuid.uuid4().hex}"
        resp = self.client.post(
            f"/api/v1/launch/previews/{created['id']}/execute",
            json={
                "meta_mode": META_MODE,
                "confirmation_token": confirmation["confirmation_token"],
            },
            headers={"Idempotency-Key": idempotency_key},
        )
        self.assertEqual(resp.status_code, 202, resp.text)
        accepted = resp.json()
        self.assertEqual(accepted["provisioning_state"], "pending")
        self.assertEqual(accepted["operation_state"], "queued")

        # The repository test database is shared by the full suite.  Put this
        # test's Job at the front of the durable queue so the assertion does
        # not depend on how many unrelated jobs earlier tests left behind.
        with tenant_session(self.ctx) as session:
            launch_job = session.scalar(
                select(JobRecord).where(
                    JobRecord.workspace_id == self.ctx.workspace_id,
                    JobRecord.resource_type == "launch_provisioning",
                    JobRecord.resource_id == accepted["id"],
                )
            )
            self.assertIsNotNone(launch_job)
            launch_job.created_at = datetime(1970, 1, 1, tzinfo=timezone.utc)
        self.assertTrue(
            asyncio.run(WorkerRuntime(owner="launch-route-worker").run_once())
        )
        status = self.client.get(
            f"/api/v1/launch/operations/{accepted['id']}",
            params={"meta_mode": META_MODE},
        )
        self.assertEqual(status.status_code, 200, status.text)
        evidence = status.json()
        self.assertEqual(evidence["activation_state"], "paused")
        self.assertEqual(evidence["operation_state"], "succeeded")
        self.assertTrue(evidence["parity_check"]["passed"])
        self.assertTrue(evidence["parity_check"]["status_parity_verified"])
        self.assertGreaterEqual(len(evidence["steps"]), 3)

        replay = self.client.post(
            f"/api/v1/launch/previews/{created['id']}/execute",
            json={
                "meta_mode": META_MODE,
                "confirmation_token": confirmation["confirmation_token"],
            },
            headers={"Idempotency-Key": idempotency_key},
        )
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json()["id"], accepted["id"])

    def test_worker_survives_an_unexpected_saga_failure(self):
        """A database error inside the saga fails one Launch, not the Worker.

        PostgreSQL aborts the saga's transaction on the first failed
        statement, so the saga cannot flush its own ``-> failed`` transition.
        The Worker must record the outcome in a fresh unit of work, fail the
        Job and keep claiming work instead of letting the error escape
        ``run_once`` (which would stop every other Workspace's queue).
        """
        connection = self._create_connection("act_preview_saga_crash")
        self._create_binding(connection["id"], "act_preview_saga_crash")
        created = self.client.post(
            "/api/v1/launch/previews",
            json=self._preview_body(connection["id"], "act_preview_saga_crash"),
        ).json()
        confirmation = self.client.post(
            f"/api/v1/launch/previews/{created['id']}/confirm",
            json={"meta_mode": META_MODE, "preview_hash": created["preview_hash"]},
        ).json()
        resp = self.client.post(
            f"/api/v1/launch/previews/{created['id']}/execute",
            json={
                "meta_mode": META_MODE,
                "confirmation_token": confirmation["confirmation_token"],
            },
            headers={"Idempotency-Key": f"launch-execute-crash-{uuid.uuid4().hex}"},
        )
        self.assertEqual(resp.status_code, 202, resp.text)
        accepted = resp.json()

        with tenant_session(self.ctx) as session:
            launch_job = session.scalar(
                select(JobRecord).where(
                    JobRecord.workspace_id == self.ctx.workspace_id,
                    JobRecord.resource_type == "launch_provisioning",
                    JobRecord.resource_id == accepted["id"],
                )
            )
            self.assertIsNotNone(launch_job)
            launch_job.created_at = datetime(1970, 1, 1, tzinfo=timezone.utc)
            launch_job_id = launch_job.id

        with patch(
            "app.worker_runtime.execute_provisioning",
            side_effect=RuntimeError("current transaction is aborted"),
        ):
            self.assertTrue(
                asyncio.run(WorkerRuntime(owner="launch-crash-worker").run_once())
            )

        status = self.client.get(
            f"/api/v1/launch/operations/{accepted['id']}",
            params={"meta_mode": META_MODE},
        )
        self.assertEqual(status.status_code, 200, status.text)
        evidence = status.json()
        self.assertEqual(evidence["provisioning_state"], "failed")
        self.assertEqual(evidence["operation_state"], "failed")
        with tenant_session(self.ctx) as session:
            failed_job = session.get(JobRecord, launch_job_id)
            self.assertEqual(failed_job.state, "failed")
            self.assertEqual(failed_job.error_code, "launch_provisioning_failed")
            self.assertIn("RuntimeError", failed_job.error_detail)

    def test_execute_admits_controlled_mock_mode(self):
        """controlled_mock is admitted through the PAUSED provisioning
        execute endpoint alongside customer_sandbox (ADR 0009);
        internal_validation remains forbidden -- see
        test_execute_rejects_non_customer_sandbox_mode below."""
        conn_resp = self.client.post(
            "/api/v1/meta/connections",
            json={"ad_account_id": "act_preview_cm", "meta_mode": "controlled_mock"},
        )
        self.assertEqual(conn_resp.status_code, 201, conn_resp.text)
        connection = conn_resp.json()
        bind_resp = self.client.post(
            f"/api/v1/meta/connections/{connection['id']}/bindings",
            json={"ad_account_id": "act_preview_cm", "meta_mode": "controlled_mock"},
        )
        self.assertEqual(bind_resp.status_code, 201, bind_resp.text)

        body = self._preview_body(connection["id"], "act_preview_cm")
        body["meta_mode"] = "controlled_mock"
        created = self.client.post("/api/v1/launch/previews", json=body).json()
        confirmation = self.client.post(
            f"/api/v1/launch/previews/{created['id']}/confirm",
            json={"meta_mode": "controlled_mock", "preview_hash": created["preview_hash"]},
        ).json()
        resp = self.client.post(
            f"/api/v1/launch/previews/{created['id']}/execute",
            json={
                "meta_mode": "controlled_mock",
                "confirmation_token": confirmation["confirmation_token"],
            },
            headers={"Idempotency-Key": f"launch-execute-cm-{uuid.uuid4().hex}"},
        )
        self.assertEqual(resp.status_code, 202, resp.text)
        accepted = resp.json()
        self.assertEqual(accepted["provisioning_state"], "pending")
        self.assertEqual(accepted["operation_state"], "queued")

    def test_execute_rejects_internal_validation_for_delegated_user(self):
        # Delegated users may launch in customer_sandbox and controlled_mock,
        # but internal_validation requires platform-operator identity.
        resp = self.client.post(
            "/api/v1/launch/previews/preview-any/execute",
            json={
                "meta_mode": "internal_validation",
                "confirmation_token": "x" * 32,
            },
            headers={"Idempotency-Key": "launch-forbidden-mode"},
        )
        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertEqual(resp.json()["code"], "meta_mode_not_permitted")

    def test_preview_is_not_visible_from_another_workspace(self):
        connection = self._create_connection("act_preview_isolation")
        self._create_binding(connection["id"], "act_preview_isolation")
        created = self.client.post(
            "/api/v1/launch/previews",
            json=self._preview_body(connection["id"], "act_preview_isolation"),
        ).json()

        other_workspace = f"ws_meta_api_{uuid.uuid4().hex[:12]}"
        _ensure_workspace(other_workspace)
        other_context = TenantContext(
            workspace_id=other_workspace,
            role="member",
            initiator_id="usr_other",
        )
        app.dependency_overrides[request_tenant_context] = lambda: other_context
        hidden = self.client.get(
            f"/api/v1/launch/previews/{created['id']}",
            params={"meta_mode": META_MODE},
        )
        self.assertEqual(hidden.status_code, 404, hidden.text)


# ---------------------------------------------------------------------------
# Audit route
# ---------------------------------------------------------------------------


class AuditRouteTests(MetaApiRoutesTestCase):
    def test_list_audit_events_route(self):
        connection = self._create_connection("act_audit_1")
        self._create_binding(connection["id"], "act_audit_1")
        resp = self.client.get("/api/v1/audit")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("items", body)
        self.assertIn("next_cursor", body)

    def test_list_audit_events_route_filters_by_meta_mode(self):
        resp = self.client.get(f"/api/v1/audit?meta_mode={META_MODE}&limit=5")
        self.assertEqual(resp.status_code, 200)
        for item in resp.json()["items"]:
            self.assertEqual(item["meta_mode"], META_MODE)


if __name__ == "__main__":
    unittest.main()
