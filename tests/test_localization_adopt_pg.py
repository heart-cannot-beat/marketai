"""Issue #46 against an isolated migrated PostgreSQL with the real API, Worker and Readonly roles.

Adopting a preserved isolated Localization output answered 500 ``permission
denied for table asset_versions`` on PostgreSQL. ``adopt_isolated_output``
publishes the child AssetVersion in the API transaction, through the same
publication path as finalize and with no new Operation (spec §2H, §3H), but
``marketai_domain_api`` held SELECT and UPDATE on ``asset_versions`` and no
INSERT. The SQLite tests (``tests/test_localization_worker.py``) check no
grants. Here the recovery runs as the roles that serve it in production:

* the Winner, its review, the configuration versions and the claim inventory
  are seeded as the schema owner, as ``web/e2e/localization/seed_prerequisites.py``
  seeds them; the request-side commands (draft, preparation, claim decisions,
  copy approval, confirmation, QC authorization) run as the API role, and the
  edit and the QC checks run as the Worker role;
* while the deterministic plan or the frozen external step is missing,
  ``POST .../adopt`` answers 422 ``qc_plan_incomplete`` and publishes nothing;
* once every frozen check has passed, it answers 200 and publishes what
  finalize publishes: a localized child AssetVersion with no QualityReview,
  an ``adopted`` publication with its recheck hash, the lineage, and an active
  binding that is not ready for an experiment. Adopting again replays the
  same result;
* another Workspace's Member and Owner can neither read the Batch, the Item or
  the output nor adopt it, before or after the adoption.

Then the privilege matrix of ``asset_versions``, and the migration's
downgrade and upgrade again.

The fixture's Jobs and Operations are cancelled at the end, as the owner:
``marketai_health_stats_v1`` counts open ones in every Workspace, and later
suites (``tests/test_health_readiness.py``) share this database. The rows
stay, because the Localization tables refuse DELETE.

Run with ``DOMAIN_OWNER_DATABASE_URL``, ``DOMAIN_API_DATABASE_URL``,
``DOMAIN_WORKER_DATABASE_URL``, ``MARKETAI_DATABASE_URL`` (the owner) and
``MARKETAI_TENANT_CONTEXT_SECRET`` against a cluster released with
``make db-release``.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.orm import sessionmaker

from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import (
    AssetVersionRecord,
    JobRecord,
    LocalizationAttemptOutputRecord,
    LocalizationAttemptRecord,
    LocalizationBatchRecord,
    LocalizationItemRecord,
    LocalizationOutputPublicationRecord,
    LocalizedAssetLineageRecord,
    MarketAssetBindingRecord,
    MarketPolicyRecord,
    MarketPolicyVersionRecord,
    QualityReviewRecord,
    WorkspaceRecord,
    utcnow,
)
from app.entitlements.admission import PaidOperationAdmission
from app.entitlements.cost_models import SpendGuardrailRecord
from app.entitlements.models import EntitlementProjectionRecord
from app.entitlements.policy import EntitlementPolicy
from app.entitlements.quote_engine import CostQuoteEngine
from app.entitlements.reservation_book import ReservationBook
from app.library.contracts import ROLE_MEMBER, ROLE_OWNER
from app.localization import worker
from app.localization.batch import (
    BatchMarketTargetInput,
    BatchSourceInput,
    approve_copy,
    authorize_isolated_qc,
    confirm,
    create_draft,
    start_preparation,
    submit_claim_decisions,
)
from app.localization.claims import ClaimDecisionInput, create_inventory_from_analysis
from app.localization.evidence_tier import EvidenceTier
from app.localization.fact_sources import ManualFactSet, ManualFactSourceAdapter
from app.localization.truth import (
    FactPolicy,
    MarketResolutionCommand,
    SourceStep,
    TruthResolutionCommand,
)
from app.main import app
from app.platform.http_identity import request_tenant_context
from app.platform.operations import OperationRecord
from app.platform.tenancy import TenantContext
from app.providers.dispatch import ExternalCallCoordinator
from tests.test_localization_batch import (
    _build_confirmed_winner,
    _make_market_policy_version,
    _make_version,
    _seed_review,
)

URLS = [os.getenv(key) for key in ("DOMAIN_OWNER_DATABASE_URL", "DOMAIN_API_DATABASE_URL", "DOMAIN_WORKER_DATABASE_URL")]
OWNER, API, WORKER = range(3)
ROOT = Path(__file__).resolve().parents[1]
#: The migration that grants the API INSERT on asset_versions (#46), and the head it follows.
REVISION, DOWN_REVISION = "zz9c0d1e2f3a", "zz8b9c0d1e2f"
#: role -> privileges it must hold on asset_versions; every other DML privilege must be absent.
GRANTED = {
    "marketai_domain_api": {"SELECT", "INSERT", "UPDATE"},
    "marketai_domain_worker": {"SELECT", "INSERT", "UPDATE"},
    "marketai_domain_readonly": {"SELECT"},
}
DML = ("SELECT", "INSERT", "UPDATE", "DELETE")
BASE = "/api/v1/localization"
SOURCE_MARKET = {"market": "US", "placement": "feed", "locale": "en-US", "country": "US", "currency": "USD"}
#: The mock providers the confirmation and preparation freeze, priced for admission.
PRICE_REGISTRY = {
    "mock-vision": {"mock-vision-1": Decimal("0.01")},
    "mock-edit": {"mock-edit-1": Decimal("0.05")},
    "mock-translate": {"mock-translate-1": Decimal("0.02")},
}
OPEN_JOB_STATES = ("queued", "retry_wait", "leased", "running", "uncertain")
SETTLED_OPERATION_STATES = ("succeeded", "failed", "partially_succeeded", "cancelled")


def _admission() -> PaidOperationAdmission:
    return PaidOperationAdmission(
        policy=EntitlementPolicy(),
        quote_engine=CostQuoteEngine(price_registry=PRICE_REGISTRY, price_rule_version="test-v1"),
        reservation_book=ReservationBook(),
    )


@unittest.skipUnless(all(URLS) and os.getenv("MARKETAI_TENANT_CONTEXT_SECRET"),
                     "requires isolated PostgreSQL runtime roles")
class LocalizationAdoptPostgresTests(unittest.TestCase):
    def setUp(self):
        self.owner, self.api, self.worker = [create_engine(url) for url in URLS]
        for engine in (self.owner, self.api, self.worker):
            self.addCleanup(engine.dispose)
        self.sessions = [sessionmaker(bind=e, expire_on_commit=False, autoflush=False)
                         for e in (self.owner, self.api, self.worker)]
        self.current: TenantContext | None = None
        app.dependency_overrides[request_tenant_context] = lambda: self.current
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def _as(self, index: int):
        return patch("app.adapters.persistence.database.SessionLocal", self.sessions[index])

    @staticmethod
    def _context(workspace_id: str, role: str, actor: str) -> TenantContext:
        return TenantContext(workspace_id=workspace_id, role=role, initiator_id=actor,
                             initiator_role=role, request_id="test-req", trace_id="trace-test")

    def _workspace(self, prefix: str) -> str:
        workspace_id = f"{prefix}_{uuid.uuid4().hex[:10]}"
        with self._as(OWNER), platform_session() as session:  # the control plane opens Workspaces
            session.add(WorkspaceRecord(id=workspace_id, name=workspace_id))
        self.addCleanup(self._retire, workspace_id)
        return workspace_id

    def _retire(self, workspace_id: str) -> None:
        """Close every Job and Operation the fixture opened in its own Workspace."""
        with self.owner.begin() as connection:
            connection.execute(update(JobRecord).where(
                JobRecord.workspace_id == workspace_id, JobRecord.state.in_(OPEN_JOB_STATES)).values(
                    state="cancelled", finished_at=utcnow(), lease_owner=None, lease_expires_at=None,
                    heartbeat_at=None))
            connection.execute(update(OperationRecord).where(
                OperationRecord.workspace_id == workspace_id,
                OperationRecord.state.notin_(SETTLED_OPERATION_STATES)).values(
                    state="cancelled", updated_at=utcnow()))

    # -- the fixture --------------------------------------------------------

    def _seed(self, owner: TenantContext) -> dict:
        """What has no creation surface, as the owner (the e2e seed's objects)."""
        tag = f"adopt{uuid.uuid4().hex[:10]}"
        product_id = f"prod_{tag}"
        now = datetime.now(timezone.utc)
        with self._as(OWNER):
            with tenant_session(owner) as session:
                session.add(EntitlementProjectionRecord(
                    workspace_id=owner.workspace_id, entitlement_version=1, lease_sequence=1,
                    projection_version=1, schema_version=2, semantic_hash="h" * 8,
                    valid_until=now + timedelta(days=30), issued_at=now - timedelta(days=1),
                    period_start=now - timedelta(days=1), period_end=now + timedelta(days=29),
                    resolved_entitlements_json={},
                ))
                session.add(SpendGuardrailRecord(
                    workspace_id=owner.workspace_id, revision=1, budget_currency="USD",
                    per_operation_limit=Decimal("100.00"), rolling_24h_limit=Decimal("500.00"),
                    period_limit=Decimal("5000.00"), outstanding_exposure_limit=Decimal("1000.00"),
                    created_at=now, updated_at=now, updated_by=owner.initiator_id,
                ))
            winner = _build_confirmed_winner(owner.workspace_id, owner, tag, product_id=product_id)
            _seed_review(owner, winner["asset_version_id"], product_id=product_id)
            with tenant_session(owner) as session:
                market_policy = _make_market_policy_version(session, f"GB{uuid.uuid4().hex[:6].upper()}")
                policy = session.get(MarketPolicyRecord, market_policy.market_policy_id)
                seed = {
                    **winner, "market_policy_version_id": market_policy.id, "market": policy.market_id,
                    "locale": market_policy.locale, "country": market_policy.country,
                    "currency": market_policy.currency, "recipe_hash": f"recipe-{tag}",
                    "truth_policy_version_id": _make_version(session, owner.workspace_id,
                                                             "truth_resolution_policy", tag),
                    "data_source_version_id": _make_version(session, owner.workspace_id, "data_source", tag),
                    "manual_fact_set_version_id": _make_version(session, owner.workspace_id,
                                                                "manual_fact_set", tag),
                }
                create_inventory_from_analysis(
                    session, owner, asset_version_id=winner["asset_version_id"],
                    analysis_recipe_hash=seed["recipe_hash"],
                    claims=[{"claim_type": "headline", "scope_key": f"headline:{tag}",
                             "ocr_text": "Hello world", "confidence": 0.99,
                             "risk": "low", "conflicting": False}],
                    provider="mock-vision", model="mock-vision-1", rule_version="claims-rule-v1",
                    analysis_execution_mode="mock", adapter_version="mock-v1",
                    mock_scenario="default", mock_version="v1",
                )
        return seed

    @staticmethod
    def _truth_command(seed: dict) -> TruthResolutionCommand:
        now = datetime.now(timezone.utc)
        step = SourceStep(
            data_source_version_id=seed["data_source_version_id"], adapter=ManualFactSourceAdapter(),
            adapter_kind="manual", adapter_version="manual-v1", truth_data_mode="real",
            manual_fact_set=ManualFactSet(
                version_id=seed["manual_fact_set_version_id"],
                facts={"price": {"amount": "19.99", "currency": "USD"}, "availability": {"available": True}},
                valid_from=now - timedelta(days=1), valid_to=now + timedelta(days=365),
            ),
            manual_fact_set_version_id=seed["manual_fact_set_version_id"],
        )
        return TruthResolutionCommand(
            product_version_id=seed["product_version_id"], offer_version_id=seed["offer_version_id"],
            truth_resolution_policy_version_id=seed["truth_policy_version_id"], truth_data_mode="real",
            max_staleness_seconds=86400 * 30, resolution_rule_version="truth-rule-v1",
            markets=(MarketResolutionCommand(
                market=seed["market"], locale=seed["locale"], country=seed["country"],
                currency=seed["currency"], market_policy_version_id=seed["market_policy_version_id"],
                sources=(step,), fact_policies=(FactPolicy("price"), FactPolicy("availability")),
            ),),
            now=now,
        )

    def _confirmed_batch(self, owner: TenantContext, seed: dict) -> dict:
        """Draft to confirmation with one frozen external step, as the API role."""
        actor, admission = owner.initiator_id, _admission()
        with self._as(API):
            with tenant_session(owner) as session:
                batch_id = create_draft(
                    session, owner, product_version_id=seed["product_version_id"],
                    offer_version_id=seed["offer_version_id"], source_market_snapshot=SOURCE_MARKET,
                    truth_data_mode="real", provider_execution_mode="mock",
                    sources=[BatchSourceInput(asset_version_id=seed["asset_version_id"],
                                              decision_id=seed["decision_id"],
                                              source_market_snapshot=SOURCE_MARKET)],
                    market_targets=[BatchMarketTargetInput(
                        market_policy_version_id=seed["market_policy_version_id"], market=seed["market"])],
                    actor_id=actor,
                ).batch_id
            with tenant_session(owner) as session:
                start_preparation(
                    session, owner, batch_id=batch_id, truth_command=self._truth_command(seed),
                    analysis_recipe_hash=seed["recipe_hash"], analysis_provider="mock-vision",
                    analysis_model="mock-vision-1", analysis_adapter_version="mock-v1",
                    actor_id=actor, admission=admission,
                )
            with tenant_session(owner) as session:
                copy_version_id = submit_claim_decisions(
                    session, owner, batch_id=batch_id, expected_revision=session.get(LocalizationBatchRecord, batch_id).revision,
                    asset_version_id=seed["asset_version_id"], target_market=seed["market"],
                    analysis_recipe_hash=seed["recipe_hash"],
                    decisions=[ClaimDecisionInput(claim_index=0, action="preserve")],
                    copy_policy_snapshot={}, source_locale="en-US", target_locale="en-US",
                    translation_recipe=None, requested_evidence_tier=EvidenceTier.MOCK,
                    actor_id=actor, role=ROLE_OWNER, admission=admission,
                ).copy_version_id
            with tenant_session(owner) as session:
                approve_copy(session, owner, batch_id=batch_id,
                             expected_revision=session.get(LocalizationBatchRecord, batch_id).revision,
                             copy_version_id=copy_version_id, actor_id=actor, role=ROLE_OWNER)
            with tenant_session(owner) as session:
                item_id = confirm(
                    session, owner, batch_id=batch_id, expected_revision=session.get(LocalizationBatchRecord, batch_id).revision,
                    edit_provider="mock-edit", edit_model="mock-edit-1", edit_adapter_version="mock-edit-v1",
                    external_qc_steps=("ocr_check",), actor_id=actor, role=ROLE_OWNER, admission=admission,
                ).item_ids[0]
        return {"batch_id": batch_id, "item_id": item_id}

    def _isolated_output(self, owner: TenantContext, built: dict) -> str:
        """The edit completes as the Worker; its output is preserved, unpublished."""
        with self._as(WORKER):
            asyncio.run(worker.handle_edit_attempt(
                owner, batch_id=built["batch_id"], item_id=built["item_id"], attempt_number=1,
                session_factory=tenant_session,
                coordinator=ExternalCallCoordinator(session_factory=tenant_session),
            ))
        with self._as(OWNER), tenant_session(owner) as session:
            attempt = session.scalar(select(LocalizationAttemptRecord).where(
                LocalizationAttemptRecord.workspace_id == owner.workspace_id,
                LocalizationAttemptRecord.item_id == built["item_id"],
                LocalizationAttemptRecord.attempt_number == 1))
            self.assertIsNotNone(session.scalar(select(LocalizationAttemptOutputRecord).where(
                LocalizationAttemptOutputRecord.workspace_id == owner.workspace_id,
                LocalizationAttemptOutputRecord.attempt_id == attempt.id)), "the edit left no output")
            return attempt.id

    # -- requests -----------------------------------------------------------

    def _request(self, caller: TenantContext, method: str, path: str, expect: int) -> dict:
        self.current = caller
        with self._as(API):
            response = self.client.request(method, f"{BASE}{path}",
                                           headers={"Idempotency-Key": f"ik-{uuid.uuid4().hex}"})
        self.assertEqual(response.status_code, expect, f"{method} {path}: {response.text}")
        return response.json()

    def _adopt(self, caller: TenantContext, built: dict, expect: int = 200) -> dict:
        return self._request(caller, "POST", f"/batches/{built['batch_id']}/items/{built['item_id']}"
                                             f"/attempts/{built['attempt_id']}/adopt", expect)

    def _published(self, owner: TenantContext, built: dict) -> dict:
        """Everything adoption may have written for this Item, read as the owner."""
        with self._as(OWNER), tenant_session(owner) as session:
            item = session.get(LocalizationItemRecord, built["item_id"])
            return {
                "item": (item.state, item.reason_code),
                "children": session.scalars(select(AssetVersionRecord).where(
                    AssetVersionRecord.origin_kind == "localized",
                    AssetVersionRecord.parent_version_id == built["asset_version_id"])).all(),
                "publications": session.scalars(select(LocalizationOutputPublicationRecord).where(
                    LocalizationOutputPublicationRecord.workspace_id == owner.workspace_id,
                    LocalizationOutputPublicationRecord.item_id == built["item_id"])).all(),
                "lineages": session.scalars(select(LocalizedAssetLineageRecord).where(
                    LocalizedAssetLineageRecord.workspace_id == owner.workspace_id,
                    LocalizedAssetLineageRecord.item_id == built["item_id"])).all(),
                "bindings": session.scalars(select(MarketAssetBindingRecord).where(
                    MarketAssetBindingRecord.workspace_id == owner.workspace_id,
                    MarketAssetBindingRecord.item_id == built["item_id"])).all(),
            }

    def _assert_nothing_published(self, owner: TenantContext, built: dict) -> None:
        published = self._published(owner, built)
        self.assertNotEqual(published["item"][0], "succeeded")
        for kind in ("children", "publications", "lineages", "bindings"):
            self.assertEqual(published[kind], [], kind)

    def _assert_strangers_are_refused(self, strangers: list[TenantContext], built: dict, owner: TenantContext,
                                      child_id: str | None = None) -> None:
        """Another Workspace reads nothing of this output and cannot adopt it."""
        batch, item = built["batch_id"], built["item_id"]
        for stranger in strangers:
            with self.subTest(stranger=stranger.role):
                for path in (f"/batches/{batch}", f"/batches/{batch}/items/{item}"):
                    self.assertEqual(self._request(stranger, "GET", path, 404)["code"], "localization_not_found")
                self.assertEqual(self._adopt(stranger, built, expect=404)["code"], "localization_not_found")
                with self._as(API), tenant_session(stranger) as session:
                    self.assertIsNone(session.get(LocalizationBatchRecord, batch))
                    self.assertIsNone(session.get(LocalizationItemRecord, item))
                    self.assertEqual(session.scalars(select(LocalizationAttemptOutputRecord).where(
                        LocalizationAttemptOutputRecord.attempt_id == built["attempt_id"])).all(), [])
                    self.assertEqual(session.scalar(select(func.count()).select_from(
                        LocalizationOutputPublicationRecord)), 0)
                    if child_id is not None:
                        self.assertIsNone(session.get(AssetVersionRecord, child_id))
        # The owner still sees its own Item: the 404s above are the boundary, not a broken route.
        self._request(owner, "GET", f"/batches/{batch}/items/{item}", 200)

    # -- tests --------------------------------------------------------------

    def test_adopting_an_isolated_output_publishes_it_under_the_api_role(self):
        workspace_id = self._workspace("ws_adopt_pg")
        owner = self._context(workspace_id, ROLE_OWNER, "owner_a")
        other = self._workspace("ws_adopt_pg_other")
        strangers = [self._context(other, ROLE_MEMBER, "member_b"), self._context(other, ROLE_OWNER, "owner_b")]
        seed = self._seed(owner)
        built = {**self._confirmed_batch(owner, seed), "asset_version_id": seed["asset_version_id"]}
        built["attempt_id"] = self._isolated_output(owner, built)

        # Nothing of the frozen plan has run: refused, nothing published.
        refused = self._adopt(owner, built, expect=422)
        self.assertEqual(refused["code"], "batch_validation_failed")
        self.assertIn("qc_plan_incomplete", refused["findings"])
        self.assertIn("external_qc_missing:ocr_check", refused["findings"])
        self._assert_nothing_published(owner, built)

        # The deterministic plan passes as the Worker; the external step is still owed.
        with self._as(WORKER), tenant_session(owner) as session:
            worker.handle_run_deterministic_qc(session, owner, batch_id=built["batch_id"],
                                               item_id=built["item_id"], attempt_id=built["attempt_id"])
        refused = self._adopt(owner, built, expect=422)
        self.assertIn("qc_plan_incomplete", refused["findings"])
        self.assertEqual([f for f in refused["findings"] if f.startswith("qc_missing:")], [])
        self.assertIn("external_qc_missing:ocr_check", refused["findings"])
        self._assert_nothing_published(owner, built)
        self._assert_strangers_are_refused(strangers, built, owner)

        # The API authorizes the missing step, the Worker records its pass.
        with self._as(API), tenant_session(owner) as session:
            authorized = authorize_isolated_qc(
                session, owner, batch_id=built["batch_id"], item_id=built["item_id"],
                attempt_id=built["attempt_id"], actor_id=owner.initiator_id, role=ROLE_OWNER,
                admission=_admission())
        self.assertEqual(authorized.authorized_steps, ("ocr_check",))
        with self._as(WORKER), tenant_session(owner) as session:
            worker.handle_ocr_check(session, owner, batch_id=built["batch_id"], item_id=built["item_id"],
                                    attempt_id=built["attempt_id"])
        self._assert_strangers_are_refused(strangers, built, owner)
        self._assert_nothing_published(owner, built)

        adopted = self._adopt(owner, built)
        self.assertFalse(adopted["replayed"])
        self.assertEqual((adopted["item_id"], adopted["attempt_id"]), (built["item_id"], built["attempt_id"]))

        published = self._published(owner, built)
        self.assertEqual(published["item"], ("succeeded", "adopted_isolated_output"))
        [child] = published["children"]
        [publication] = published["publications"]
        [lineage] = published["lineages"]
        [binding] = published["bindings"]
        self.assertEqual((child.id, publication.id, binding.id),
                         (adopted["asset_version_id"], adopted["publication_id"], adopted["binding_id"]))
        self.assertEqual(child.workspace_id, workspace_id)
        self.assertEqual((publication.publication_kind, publication.actor_id, publication.asset_version_id),
                         ("adopted", owner.initiator_id, child.id))
        self.assertTrue(publication.recheck_hash)
        self.assertEqual((lineage.asset_version_id, lineage.publication_id), (child.id, publication.id))
        # Spec §16.11: the localized child starts unreviewed, not experiment-ready.
        self.assertTrue(binding.active)
        self.assertEqual((binding.binding_kind, binding.review_state, binding.ready_for_experiment),
                         ("localized", "pending_review", False))
        with self._as(OWNER), tenant_session(owner) as session:
            self.assertEqual(session.scalars(select(QualityReviewRecord).where(
                QualityReviewRecord.asset_version_id == child.id)).all(), [])

        # A retry replays the same evidence and publishes nothing new.
        replayed = self._adopt(owner, built)
        self.assertTrue(replayed["replayed"])
        self.assertEqual({key: replayed[key] for key in ("asset_version_id", "publication_id", "binding_id")},
                         {key: adopted[key] for key in ("asset_version_id", "publication_id", "binding_id")})
        again = self._published(owner, built)
        self.assertEqual([len(again[kind]) for kind in ("children", "publications", "lineages", "bindings")],
                         [1, 1, 1, 1])

        self._assert_strangers_are_refused(strangers, built, owner, child_id=child.id)
        with self.api.connect() as connection:  # no tenant context: no row
            self.assertEqual(connection.execute(text("SELECT count(*) FROM asset_versions")).scalar(), 0)

    def test_the_privilege_matrix_of_asset_versions_holds(self):
        with self.owner.connect() as connection:
            forced = connection.execute(text(
                "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE relname = 'asset_versions'"
            )).scalar()
            self.assertTrue(forced)
            for role, expected in GRANTED.items():
                for privilege in DML:
                    held = connection.execute(text("SELECT has_table_privilege(:r, 'asset_versions', :p)"),
                                              {"r": role, "p": privilege}).scalar()
                    self.assertEqual(held, privilege in expected, f"{role} {privilege} asset_versions")

    def _alembic(self, *arguments: str) -> str:
        completed = subprocess.run(
            [sys.executable, "-m", "alembic", *arguments], cwd=ROOT, capture_output=True, text=True,
            env={**os.environ, "MARKETAI_DATABASE_URL": URLS[OWNER]}, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout

    def _api_inserts(self) -> bool:
        with self.owner.connect() as connection:
            return connection.execute(text(
                "SELECT has_table_privilege('marketai_domain_api', 'asset_versions', 'INSERT')")).scalar()

    def test_the_migration_grants_insert_and_its_downgrade_revokes_it(self):
        head = self._alembic("current")  # later migrations may follow REVISION
        self.assertIn("(head)", head)
        self.addCleanup(self._alembic, "upgrade", "head")  # never leave the cluster below head
        self._alembic("downgrade", DOWN_REVISION)
        self.assertIn(DOWN_REVISION, self._alembic("current"))
        self.assertFalse(self._api_inserts())
        self._alembic("upgrade", REVISION)
        self.assertIn(REVISION, self._alembic("current"))
        self.assertTrue(self._api_inserts())
        self._alembic("upgrade", "head")
        self.assertEqual(self._alembic("current"), head)
        self.test_the_privilege_matrix_of_asset_versions_holds()


if __name__ == "__main__":
    unittest.main()
