"""Claims module tests for Localization (#24, spec §2F, §16.7).

Exercises `plan_inventories()`, `create_inventory_from_analysis()`,
`submit_claim_decisions()` and `get_or_create_decision_set()` through
their public interfaces. A real `TruthFactRecord` (with a real
`TruthPackageRecord`/`TruthPackageMarketRecord`/`TruthSourceAttemptRecord`
provenance chain) is produced by driving
`app.localization.truth.resolve_or_reuse_truth_package()`, the same way
`tests/test_localization_truth.py` does, rather than hand-inserting a
Fact row that would bypass its own composite foreign keys.
"""
from __future__ import annotations

import hashlib
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import (
    AssetRecord,
    AssetVersionRecord,
    GenerationRunRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    LocalizationBatchRecord,
    LocalizationClaimDecisionRecord,
    LocalizationClaimDecisionSetRecord,
    MarketPolicyRecord,
    MarketPolicyVersionRecord,
    PlanRecord,
    ProductRecord,
    SourceClaimConfirmationRecord,
    SourceClaimInventoryRecord,
    TruthFactRecord,
    TruthPackageMarketRecord,
    WorkspaceRecord,
    new_id,
)
# These modules register additional tables on the shared `Base.metadata`
# (e.g. `credential_versions`, referenced by a FK elsewhere in the schema).
# `Base.metadata.create_all()` needs every such module imported first, or it
# cannot resolve those foreign keys -- same pattern as
# tests/test_localization_truth.py.
from app.entitlements.cost_models import SpendGuardrailRecord, SpendReservationRecord  # noqa: F401
from app.entitlements.credentials import CredentialVersionRecord  # noqa: F401
from app.entitlements.models import EntitlementProjectionRecord  # noqa: F401
from app.entitlements.usage_models import UsageEventRecord  # noqa: F401
from app.generation.models import GenerationDraftRecord  # noqa: F401
from app.localization.claims import (
    ClaimDecisionInput,
    DecisionSetResult,
    InventoryPlan,
    create_inventory_from_analysis,
    get_or_create_decision_set,
    plan_inventories,
    submit_claim_decisions,
)
from app.localization.fact_sources import ManualFactSet, ManualFactSourceAdapter
from app.localization.truth import (
    FactPolicy,
    MarketResolutionCommand,
    SourceStep,
    TruthResolutionCommand,
    resolve_or_reuse_truth_package,
)
from app.platform.tenancy import TenantContext

WS_ID = "ws_claims_test"
PROD_ID = "prod_claims"


def _make_version(session, workspace_id, entry_type, *, content=None):
    entry_id = new_id("le")
    session.add(
        LibraryEntryRecord(
            id=entry_id,
            workspace_id=workspace_id,
            entry_type=entry_type,
            display_name=entry_type,
            lifecycle="active",
        )
    )
    session.flush()
    version_id = new_id("lv")
    session.add(
        LibraryVersionRecord(
            id=version_id,
            workspace_id=workspace_id,
            entry_id=entry_id,
            entry_type=entry_type,
            version_no=1,
            content_json=content if content is not None else {},
            content_hash=f"hash-{version_id}",
            published_by="tester",
        )
    )
    session.flush()
    return version_id


def _make_market_policy_version(session, market_id, *, restricted_claim_types=()):
    """A standalone MarketPolicy + effective MarketPolicyVersion this test
    fully controls (never the shared v9 Beta catalog), so
    `restricted_claim_types_json` can be set per test."""
    policy = MarketPolicyRecord(
        id=new_id("mp"),
        market_id=market_id,
        locale=market_id,
        country=market_id.split("-")[-1][:2],
        currency="USD",
    )
    session.add(policy)
    session.flush()
    version = MarketPolicyVersionRecord(
        id=new_id("mpv"),
        market_policy_id=policy.id,
        version_number=1,
        version_state="effective",
        locale=market_id,
        country=policy.country,
        currency="USD",
        pricing_context_json={},
        digit_format_json={},
        base_tone="neutral",
        reserved_words_json=[],
        restricted_claim_types_json=list(restricted_claim_types),
        requires_local_copy=False,
        shipping_address_granularity="country",
        max_fact_staleness_seconds=86400,
        qc_rules_json={},
    )
    session.add(version)
    session.flush()
    return version


class _ClaimsTestCase(unittest.TestCase):
    """Shared fixture: one Workspace, effective Product/Offer/Truth policy
    versions, one source AssetVersion, and helpers to resolve a real
    TruthPackage/Fact and to freeze a LocalizationBatch row (claims.py's
    `origin_batch_id` FK target)."""

    def setUp(self) -> None:
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(bind=self.engine)
        self.session = Session(bind=self.engine, expire_on_commit=False)

        self.session.add(
            WorkspaceRecord(id=WS_ID, name="Claims Test", lifecycle="active", budget_currency="USD")
        )
        self.session.flush()

        self.ctx = TenantContext(
            workspace_id=WS_ID, role="owner", initiator_id="user_1", initiator_role="owner",
        )

        self.product_version_id = _make_version(self.session, WS_ID, "product")
        self.offer_version_id = _make_version(self.session, WS_ID, "offer")
        self.policy_version_id = _make_version(self.session, WS_ID, "truth_resolution_policy")
        self.data_source_version_id = _make_version(self.session, WS_ID, "data_source")
        self.manual_fact_set_version_id = _make_version(self.session, WS_ID, "manual_fact_set")

        self.session.add(ProductRecord(id=PROD_ID, workspace_id=WS_ID, name="P"))
        self.session.flush()

        self.asset_version_id = self._make_asset_version("src")

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    # -- fixtures ---------------------------------------------------

    def _make_asset_version(self, tag: str) -> str:
        plan_id = f"plan_{tag}"
        if self.session.get(PlanRecord, plan_id) is None:
            self.session.add(
                PlanRecord(
                    id=plan_id, workspace_id=WS_ID, product_id=PROD_ID,
                    mode="full", cap=0, total_full=1, snapshot_hash="a" * 64,
                )
            )
            self.session.flush()

        run_id = f"run_{tag}"
        if self.session.get(GenerationRunRecord, run_id) is None:
            self.session.add(
                GenerationRunRecord(
                    id=run_id, workspace_id=WS_ID, product_id=PROD_ID, plan_id=plan_id,
                    provider="mock", model="mock", aspect_ratio="1:1", image_size="1024",
                    variants=1, use_ref=False, status="done", total=1, done=1, failed=0,
                    est_cost=Decimal("0"), currency="USD",
                )
            )
            self.session.flush()

        asset_id = f"asset_{tag}"
        av_id = f"av_{tag}"
        self.session.add(
            AssetRecord(
                id=asset_id, workspace_id=WS_ID, product_id=PROD_ID, run_id=run_id,
                combo_id="C01", combo_code="A1", tags_json={}, filename=f"{tag}.png",
                provider="mock", model="mock", prompt="test", fingerprint=f"fp_{tag}", status="ok",
            )
        )
        self.session.flush()
        self.session.add(
            AssetVersionRecord(
                id=av_id, workspace_id=WS_ID, asset_id=asset_id, kind="generated",
                aspect_ratio="1:1", language="en", object_key=f"{tag}.png",
                content_sha256=hashlib.sha256(tag.encode()).hexdigest(),
                byte_size=1, mime_type="image/png",
            )
        )
        self.session.flush()
        return av_id

    def _resolve_market_truth(
        self, market_id: str, *, restricted_claim_types: tuple[str, ...] = (), facts: dict | None = None,
    ) -> dict:
        """Drive the real Truth resolution module to produce one
        TruthPackage/Market/Fact chain, with a MarketPolicyVersion this
        test controls."""
        mpv = _make_market_policy_version(
            self.session, market_id, restricted_claim_types=restricted_claim_types
        )
        now = datetime.now(timezone.utc)
        window = (now - timedelta(days=1), now + timedelta(days=365))
        facts = facts or {
            "price": {"amount": "19.99", "currency": "USD"},
            "availability": {"available": True},
        }
        step = SourceStep(
            data_source_version_id=self.data_source_version_id,
            adapter=ManualFactSourceAdapter(),
            adapter_kind="manual",
            adapter_version="manual-v1",
            truth_data_mode="real",
            manual_fact_set=ManualFactSet(
                version_id=self.manual_fact_set_version_id, facts=facts,
                valid_from=window[0], valid_to=window[1],
            ),
            manual_fact_set_version_id=self.manual_fact_set_version_id,
        )
        market_cmd = MarketResolutionCommand(
            market=market_id, locale=mpv.locale, country=mpv.country, currency=mpv.currency,
            market_policy_version_id=mpv.id, sources=(step,),
            fact_policies=(FactPolicy("price"), FactPolicy("availability")),
        )
        command = TruthResolutionCommand(
            product_version_id=self.product_version_id,
            offer_version_id=self.offer_version_id,
            truth_resolution_policy_version_id=self.policy_version_id,
            truth_data_mode="real",
            max_staleness_seconds=86400 * 30,
            resolution_rule_version="truth-rule-v1",
            markets=(market_cmd,),
            now=now,
        )
        result = resolve_or_reuse_truth_package(self.session, self.ctx, command)
        tpm = (
            self.session.query(TruthPackageMarketRecord)
            .filter_by(workspace_id=WS_ID, truth_package_id=result.truth_package_id, market=market_id)
            .one()
        )
        return {
            "truth_package_id": result.truth_package_id,
            "truth_package_market_id": tpm.id,
            "market_policy_version_id": mpv.id,
            "target_market": market_id,
        }

    def _fact(self, truth_ctx: dict, fact_type: str) -> TruthFactRecord:
        return (
            self.session.query(TruthFactRecord)
            .filter_by(
                workspace_id=WS_ID,
                truth_package_id=truth_ctx["truth_package_id"],
                truth_package_market_id=truth_ctx["truth_package_market_id"],
                fact_type=fact_type,
            )
            .one()
        )

    def _make_batch(self, truth_package_id: str) -> str:
        batch_id = new_id("lb")
        self.session.add(
            LocalizationBatchRecord(
                id=batch_id, workspace_id=WS_ID, product_version_id=self.product_version_id,
                offer_version_id=self.offer_version_id, source_market_snapshot_json={},
                truth_package_id=truth_package_id, truth_data_mode="real",
                provider_execution_mode="mock", phase="draft",
            )
        )
        self.session.flush()
        return batch_id

    def _make_inventory(self, claims: list[dict], *, recipe_hash: str = "recipe-v1") -> SourceClaimInventoryRecord:
        return create_inventory_from_analysis(
            self.session, self.ctx,
            asset_version_id=self.asset_version_id,
            analysis_recipe_hash=recipe_hash,
            claims=claims,
            provider="mock-vision", model="mock-vision-1", rule_version="claims-rule-v1",
            analysis_execution_mode="mock", adapter_version="mock-v1",
            mock_scenario="claims_default", mock_version="v1",
        )

    def _add_confirmation(self, inventory_id: str, claim_index: int, kind: str = "accepted"):
        self.session.add(
            SourceClaimConfirmationRecord(
                id=new_id("scc"), workspace_id=WS_ID, inventory_id=inventory_id,
                claim_index=claim_index, confirmation_kind=kind,
                actor_id="reviewer_1", role="owner",
            )
        )
        self.session.flush()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestClaimsModule(_ClaimsTestCase):
    def test_replace_requires_same_scope_fact(self):
        truth_ctx = self._resolve_market_truth("de-DE")
        price_fact = self._fact(truth_ctx, "price")

        inventory = self._make_inventory(
            [{"claim_type": "price_claim", "scope_key": price_fact.scope_key, "confidence": 0.95}]
        )
        batch_id = self._make_batch(truth_ctx["truth_package_id"])

        # A replacement Fact that does not exist at all is rejected.
        missing_result = submit_claim_decisions(
            self.session, self.ctx,
            origin_batch_id=batch_id, inventory_id=inventory.id,
            truth_package_id=truth_ctx["truth_package_id"],
            truth_package_market_id=truth_ctx["truth_package_market_id"],
            target_market=truth_ctx["target_market"],
            market_policy_version_id=truth_ctx["market_policy_version_id"],
            decisions=[
                ClaimDecisionInput(claim_index=0, action="replace", replacement_fact_id="fact_does_not_exist"),
            ],
            actor_id="user_1", role="owner",
        )
        self.assertFalse(missing_result.valid)
        self.assertIn("claim_0_replace_requires_same_scope_fact", missing_result.findings)
        self.assertIsNone(missing_result.decision_set_id)

        # A Fact that exists but has a different scope is also rejected --
        # not "any Fact in the package/market", the *same-scope* Fact.
        availability_fact = self._fact(truth_ctx, "availability")
        wrong_scope_result = submit_claim_decisions(
            self.session, self.ctx,
            origin_batch_id=batch_id, inventory_id=inventory.id,
            truth_package_id=truth_ctx["truth_package_id"],
            truth_package_market_id=truth_ctx["truth_package_market_id"],
            target_market=truth_ctx["target_market"],
            market_policy_version_id=truth_ctx["market_policy_version_id"],
            decisions=[
                ClaimDecisionInput(claim_index=0, action="replace", replacement_fact_id=availability_fact.id),
            ],
            actor_id="user_1", role="owner",
        )
        self.assertFalse(wrong_scope_result.valid)
        self.assertIn("claim_0_replace_requires_same_scope_fact", wrong_scope_result.findings)

        # The same-scope Fact from the same TruthPackage market succeeds.
        ok_result = submit_claim_decisions(
            self.session, self.ctx,
            origin_batch_id=batch_id, inventory_id=inventory.id,
            truth_package_id=truth_ctx["truth_package_id"],
            truth_package_market_id=truth_ctx["truth_package_market_id"],
            target_market=truth_ctx["target_market"],
            market_policy_version_id=truth_ctx["market_policy_version_id"],
            decisions=[
                ClaimDecisionInput(claim_index=0, action="replace", replacement_fact_id=price_fact.id),
            ],
            actor_id="user_1", role="owner",
        )
        self.assertTrue(ok_result.valid, ok_result.findings)
        self.assertIsNotNone(ok_result.decision_set_id)

        stored = (
            self.session.query(LocalizationClaimDecisionRecord)
            .filter_by(decision_set_id=ok_result.decision_set_id)
            .one()
        )
        self.assertEqual(stored.replacement_fact_id, price_fact.id)
        self.assertEqual(stored.replacement_fact_scope_key, price_fact.scope_key)

    def test_remove_freezes_removal_strategy(self):
        truth_ctx = self._resolve_market_truth("en-AU")
        inventory = self._make_inventory(
            [{"claim_type": "delivery_claim", "scope_key": "hero_delivery", "confidence": 0.95}]
        )
        batch_id = self._make_batch(truth_ctx["truth_package_id"])

        missing_result = submit_claim_decisions(
            self.session, self.ctx,
            origin_batch_id=batch_id, inventory_id=inventory.id,
            truth_package_id=truth_ctx["truth_package_id"],
            truth_package_market_id=truth_ctx["truth_package_market_id"],
            target_market=truth_ctx["target_market"],
            market_policy_version_id=truth_ctx["market_policy_version_id"],
            decisions=[ClaimDecisionInput(claim_index=0, action="remove")],
            actor_id="user_1", role="owner",
        )
        self.assertFalse(missing_result.valid)
        self.assertIn("claim_0_remove_requires_removal_strategy", missing_result.findings)

        strategy = {"strategy": "mask_region", "region": {"x": 10, "y": 10, "w": 50, "h": 20}}
        ok_result = submit_claim_decisions(
            self.session, self.ctx,
            origin_batch_id=batch_id, inventory_id=inventory.id,
            truth_package_id=truth_ctx["truth_package_id"],
            truth_package_market_id=truth_ctx["truth_package_market_id"],
            target_market=truth_ctx["target_market"],
            market_policy_version_id=truth_ctx["market_policy_version_id"],
            decisions=[
                ClaimDecisionInput(claim_index=0, action="remove", removal_strategy=strategy),
            ],
            actor_id="user_1", role="owner",
        )
        self.assertTrue(ok_result.valid, ok_result.findings)

        stored = (
            self.session.query(LocalizationClaimDecisionRecord)
            .filter_by(decision_set_id=ok_result.decision_set_id)
            .one()
        )
        self.assertEqual(stored.removal_strategy_json, strategy)
        self.assertIsNotNone(stored.removal_strategy_hash)
        self.assertIsNone(stored.replacement_fact_id)
        self.assertIsNone(stored.preserve_policy_evidence_hash)

    def test_market_sensitive_alias_cannot_be_preserved(self):
        truth_ctx = self._resolve_market_truth("en-NZ")
        inventory = self._make_inventory(
            [{
                "claim_type": "delivery_claim",
                "scope_key": "hero_delivery",
                "confidence": 0.95,
            }]
        )
        batch_id = self._make_batch(truth_ctx["truth_package_id"])

        result = submit_claim_decisions(
            self.session,
            self.ctx,
            origin_batch_id=batch_id,
            inventory_id=inventory.id,
            truth_package_id=truth_ctx["truth_package_id"],
            truth_package_market_id=truth_ctx["truth_package_market_id"],
            target_market=truth_ctx["target_market"],
            market_policy_version_id=truth_ctx["market_policy_version_id"],
            decisions=[ClaimDecisionInput(claim_index=0, action="preserve")],
            actor_id="user_1",
            role="owner",
        )

        self.assertFalse(result.valid)
        self.assertIn(
            "claim_0_preserve_requires_market_neutral_policy",
            result.findings,
        )

    def test_preserve_requires_market_neutral_policy(self):
        truth_ctx = self._resolve_market_truth("fr-FR", restricted_claim_types=("price_claim",))
        inventory = self._make_inventory(
            [{"claim_type": "price_claim", "scope_key": "hero_price", "confidence": 0.95}]
        )
        batch_id = self._make_batch(truth_ctx["truth_package_id"])

        restricted_result = submit_claim_decisions(
            self.session, self.ctx,
            origin_batch_id=batch_id, inventory_id=inventory.id,
            truth_package_id=truth_ctx["truth_package_id"],
            truth_package_market_id=truth_ctx["truth_package_market_id"],
            target_market=truth_ctx["target_market"],
            market_policy_version_id=truth_ctx["market_policy_version_id"],
            decisions=[ClaimDecisionInput(claim_index=0, action="preserve")],
            actor_id="user_1", role="owner",
        )
        self.assertFalse(restricted_result.valid)
        self.assertIn("claim_0_preserve_requires_market_neutral_policy", restricted_result.findings)

        # A market-neutral Claim (its type is not on the restricted list)
        # in the same market is accepted, with policy evidence recorded.
        neutral_inventory = self._make_inventory(
            [{"claim_type": "brand_term", "scope_key": "hero_brand", "confidence": 0.95}],
            recipe_hash="recipe-v2",
        )
        ok_result = submit_claim_decisions(
            self.session, self.ctx,
            origin_batch_id=batch_id, inventory_id=neutral_inventory.id,
            truth_package_id=truth_ctx["truth_package_id"],
            truth_package_market_id=truth_ctx["truth_package_market_id"],
            target_market=truth_ctx["target_market"],
            market_policy_version_id=truth_ctx["market_policy_version_id"],
            decisions=[ClaimDecisionInput(claim_index=0, action="preserve")],
            actor_id="user_1", role="owner",
        )
        self.assertTrue(ok_result.valid, ok_result.findings)
        stored = (
            self.session.query(LocalizationClaimDecisionRecord)
            .filter_by(decision_set_id=ok_result.decision_set_id)
            .one()
        )
        self.assertIsNotNone(stored.preserve_policy_evidence_hash)

    def test_incomplete_decision_set_rejected(self):
        truth_ctx = self._resolve_market_truth("es-ES")
        inventory = self._make_inventory(
            [
                {"claim_type": "brand_term", "scope_key": "hero_brand", "confidence": 0.95},
                {"claim_type": "delivery_claim", "scope_key": "hero_delivery", "confidence": 0.95},
            ]
        )
        batch_id = self._make_batch(truth_ctx["truth_package_id"])

        result = submit_claim_decisions(
            self.session, self.ctx,
            origin_batch_id=batch_id, inventory_id=inventory.id,
            truth_package_id=truth_ctx["truth_package_id"],
            truth_package_market_id=truth_ctx["truth_package_market_id"],
            target_market=truth_ctx["target_market"],
            market_policy_version_id=truth_ctx["market_policy_version_id"],
            decisions=[ClaimDecisionInput(claim_index=0, action="preserve")],
            actor_id="user_1", role="owner",
        )
        self.assertFalse(result.valid)
        self.assertEqual(result.findings, ("incomplete_decision_set",))
        self.assertIsNone(result.decision_set_id)
        self.assertEqual(
            self.session.query(LocalizationClaimDecisionSetRecord).count(), 0
        )

    def test_canonical_hash_mismatch_rejected(self):
        truth_ctx = self._resolve_market_truth("it-IT")
        inventory = self._make_inventory(
            [{"claim_type": "brand_term", "scope_key": "hero_brand", "confidence": 0.95}]
        )
        batch_id = self._make_batch(truth_ctx["truth_package_id"])
        decisions = [ClaimDecisionInput(claim_index=0, action="preserve")]

        first = submit_claim_decisions(
            self.session, self.ctx,
            origin_batch_id=batch_id, inventory_id=inventory.id,
            truth_package_id=truth_ctx["truth_package_id"],
            truth_package_market_id=truth_ctx["truth_package_market_id"],
            target_market=truth_ctx["target_market"],
            market_policy_version_id=truth_ctx["market_policy_version_id"],
            decisions=decisions,
            actor_id="user_1", role="owner",
        )
        self.assertTrue(first.valid, first.findings)

        # Corrupt the persisted set's canonical_hash directly (defense in
        # depth against data corruption / tampering the DEFERRABLE
        # PostgreSQL trigger would also catch -- SQLite has no such
        # trigger, so claims.py must detect it itself).
        stored_set = self.session.get(LocalizationClaimDecisionSetRecord, first.decision_set_id)
        stored_set.canonical_hash = "0" * 64
        self.session.flush()

        second = submit_claim_decisions(
            self.session, self.ctx,
            origin_batch_id=batch_id, inventory_id=inventory.id,
            truth_package_id=truth_ctx["truth_package_id"],
            truth_package_market_id=truth_ctx["truth_package_market_id"],
            target_market=truth_ctx["target_market"],
            market_policy_version_id=truth_ctx["market_policy_version_id"],
            decisions=decisions,
            actor_id="user_1", role="owner",
        )
        self.assertFalse(second.valid)
        self.assertIn("canonical_hash_mismatch", second.findings)
        self.assertEqual(second.decision_set_id, first.decision_set_id)

        # No duplicate set was created to work around the corrupted row.
        self.assertEqual(
            self.session.query(LocalizationClaimDecisionSetRecord).count(), 1
        )

    def test_high_risk_claim_blocks_copy(self):
        truth_ctx = self._resolve_market_truth("ja-JP")
        inventory = self._make_inventory(
            [{"claim_type": "brand_term", "scope_key": "hero_brand", "confidence": 0.4, "risk": "high"}]
        )
        batch_id = self._make_batch(truth_ctx["truth_package_id"])
        decisions = [ClaimDecisionInput(claim_index=0, action="preserve")]

        blocked = submit_claim_decisions(
            self.session, self.ctx,
            origin_batch_id=batch_id, inventory_id=inventory.id,
            truth_package_id=truth_ctx["truth_package_id"],
            truth_package_market_id=truth_ctx["truth_package_market_id"],
            target_market=truth_ctx["target_market"],
            market_policy_version_id=truth_ctx["market_policy_version_id"],
            decisions=decisions,
            actor_id="user_1", role="owner",
        )
        self.assertFalse(blocked.valid)
        self.assertIn("claim_0_requires_confirmation", blocked.findings)
        self.assertIsNone(blocked.decision_set_id)
        self.assertEqual(
            self.session.query(LocalizationClaimDecisionSetRecord).count(), 0
        )

        # A "rejected" confirmation still blocks -- only "accepted" clears it.
        self._add_confirmation(inventory.id, 0, kind="rejected")
        still_blocked = submit_claim_decisions(
            self.session, self.ctx,
            origin_batch_id=batch_id, inventory_id=inventory.id,
            truth_package_id=truth_ctx["truth_package_id"],
            truth_package_market_id=truth_ctx["truth_package_market_id"],
            target_market=truth_ctx["target_market"],
            market_policy_version_id=truth_ctx["market_policy_version_id"],
            decisions=decisions,
            actor_id="user_1", role="owner",
        )
        self.assertFalse(still_blocked.valid)
        self.assertIn("claim_0_requires_confirmation", still_blocked.findings)

        self._add_confirmation(inventory.id, 0, kind="accepted")
        unblocked = submit_claim_decisions(
            self.session, self.ctx,
            origin_batch_id=batch_id, inventory_id=inventory.id,
            truth_package_id=truth_ctx["truth_package_id"],
            truth_package_market_id=truth_ctx["truth_package_market_id"],
            target_market=truth_ctx["target_market"],
            market_policy_version_id=truth_ctx["market_policy_version_id"],
            decisions=decisions,
            actor_id="user_1", role="owner",
        )
        self.assertTrue(unblocked.valid, unblocked.findings)
        self.assertIsNotNone(unblocked.decision_set_id)

    def test_reuse_decision_set_when_hash_matches(self):
        truth_ctx = self._resolve_market_truth("ko-KR")
        inventory = self._make_inventory(
            [{"claim_type": "brand_term", "scope_key": "hero_brand", "confidence": 0.95}]
        )
        batch_a = self._make_batch(truth_ctx["truth_package_id"])
        decisions = [ClaimDecisionInput(claim_index=0, action="preserve", rationale="first pass")]

        first = submit_claim_decisions(
            self.session, self.ctx,
            origin_batch_id=batch_a, inventory_id=inventory.id,
            truth_package_id=truth_ctx["truth_package_id"],
            truth_package_market_id=truth_ctx["truth_package_market_id"],
            target_market=truth_ctx["target_market"],
            market_policy_version_id=truth_ctx["market_policy_version_id"],
            decisions=decisions,
            actor_id="user_1", role="owner",
        )
        self.assertTrue(first.valid, first.findings)
        self.assertFalse(first.reused)

        # A different Batch, a different actor/role, but the identical
        # decisions for the identical context reuses the same set --
        # `origin_batch_id` is provenance, not part of the canonical
        # identity.
        batch_b = self._make_batch(truth_ctx["truth_package_id"])
        second = submit_claim_decisions(
            self.session, self.ctx,
            origin_batch_id=batch_b, inventory_id=inventory.id,
            truth_package_id=truth_ctx["truth_package_id"],
            truth_package_market_id=truth_ctx["truth_package_market_id"],
            target_market=truth_ctx["target_market"],
            market_policy_version_id=truth_ctx["market_policy_version_id"],
            decisions=decisions,
            actor_id="user_2", role="member",
        )
        self.assertTrue(second.valid, second.findings)
        self.assertTrue(second.reused)
        self.assertEqual(second.decision_set_id, first.decision_set_id)

        self.assertEqual(
            self.session.query(LocalizationClaimDecisionSetRecord).count(), 1
        )
        self.assertEqual(
            self.session.query(LocalizationClaimDecisionRecord)
            .filter_by(decision_set_id=first.decision_set_id)
            .count(),
            1,
        )
        # The originating Batch on the reused row is still the first one.
        stored_set = self.session.get(LocalizationClaimDecisionSetRecord, first.decision_set_id)
        self.assertEqual(stored_set.origin_batch_id, batch_a)


class TestInventoryPlanningAndCreation(_ClaimsTestCase):
    def test_plan_inventories_reports_reused_and_missing(self):
        other_av = self._make_asset_version("other")

        plan_before = plan_inventories(
            self.session, self.ctx,
            asset_version_ids=[self.asset_version_id, other_av],
            analysis_recipe_hash="recipe-v1",
        )
        self.assertEqual(plan_before.reused, {})
        self.assertEqual(set(plan_before.missing), {self.asset_version_id, other_av})

        inventory = self._make_inventory(
            [{"claim_type": "brand_term", "scope_key": "hero_brand", "confidence": 0.95}]
        )

        plan_after = plan_inventories(
            self.session, self.ctx,
            asset_version_ids=[self.asset_version_id, other_av],
            analysis_recipe_hash="recipe-v1",
        )
        self.assertEqual(plan_after.reused, {self.asset_version_id: inventory.id})
        self.assertEqual(plan_after.missing, (other_av,))

    def test_create_inventory_from_analysis_is_write_once_and_versions(self):
        first = self._make_inventory(
            [{"claim_type": "brand_term", "scope_key": "hero_brand", "confidence": 0.95}]
        )
        self.assertEqual(first.version_number, 1)
        self.assertTrue(first.evidence_hash)

        second = self._make_inventory(
            [{"claim_type": "brand_term", "scope_key": "hero_brand", "confidence": 0.5}]
        )
        self.assertEqual(second.version_number, 2)
        self.assertNotEqual(first.id, second.id)

        # Both immutable rows remain, byte-for-byte, in the table.
        rows = (
            self.session.query(SourceClaimInventoryRecord)
            .filter_by(workspace_id=WS_ID, asset_version_id=self.asset_version_id)
            .order_by(SourceClaimInventoryRecord.version_number)
            .all()
        )
        self.assertEqual([r.version_number for r in rows], [1, 2])


if __name__ == "__main__":
    unittest.main()
