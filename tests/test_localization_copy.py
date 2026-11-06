"""Copy module tests for Localization (#24, spec §2F, §16.7).

Exercises `plan_copy_versions()`, `create_source_locale_projection()`,
`create_copy_from_translation()`, `prepare_copy_segments()` and
`approve_copy_version()` through their public interfaces. Real
`TruthPackageRecord`/`TruthPackageMarketRecord`/`TruthFactRecord` and
`LocalizationClaimDecisionSetRecord`/`LocalizationClaimDecisionRecord`
provenance chains are produced by driving
`app.localization.truth.resolve_or_reuse_truth_package()` and
`app.localization.claims.submit_claim_decisions()`, the same way
`tests/test_localization_claims.py` does, rather than hand-inserting rows
that would bypass their own composite foreign keys.
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
    LocalizedCopyApprovalRecord,
    LocalizedCopyVersionRecord,
    MarketPolicyRecord,
    MarketPolicyVersionRecord,
    PlanRecord,
    ProductRecord,
    TruthFactRecord,
    TruthPackageMarketRecord,
    WorkspaceRecord,
    new_id,
)
# These modules register additional tables on the shared `Base.metadata`
# (e.g. `credential_versions`, referenced by a FK elsewhere in the schema).
# `Base.metadata.create_all()` needs every such module imported first, or it
# cannot resolve those foreign keys -- same pattern as
# tests/test_localization_claims.py.
from app.entitlements.cost_models import SpendGuardrailRecord, SpendReservationRecord  # noqa: F401
from app.entitlements.credentials import CredentialVersionRecord  # noqa: F401
from app.entitlements.models import EntitlementProjectionRecord  # noqa: F401
from app.entitlements.usage_models import UsageEventRecord  # noqa: F401
from app.generation.models import GenerationDraftRecord  # noqa: F401
from app.localization.claims import (
    ClaimDecisionInput,
    create_inventory_from_analysis,
    submit_claim_decisions,
)
from app.localization.copy import (
    CopyError,
    CopyPlanRequest,
    TranslationRecipe,
    approve_copy_version,
    create_copy_from_translation,
    create_source_locale_projection,
    plan_copy_versions,
    prepare_copy_segments,
)
from app.localization.evidence_tier import EvidenceTier
from app.localization.fact_sources import ManualFactSet, ManualFactSourceAdapter
from app.localization.truth import (
    FactPolicy,
    MarketResolutionCommand,
    SourceStep,
    TruthResolutionCommand,
    resolve_or_reuse_truth_package,
)
from app.platform.tenancy import TenantContext

WS_ID = "ws_copy_test"
PROD_ID = "prod_copy"


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


class _CopyTestCase(unittest.TestCase):
    """Shared fixture: one Workspace, effective Product/Offer/Truth policy
    versions, one source AssetVersion, and helpers to resolve a real
    TruthPackage/Fact, build an Inventory, and submit a real Claim
    decision set -- the exact upstream chain `copy.py` consumes."""

    def setUp(self) -> None:
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(bind=self.engine)
        self.session = Session(bind=self.engine, expire_on_commit=False)

        self.session.add(
            WorkspaceRecord(id=WS_ID, name="Copy Test", lifecycle="active", budget_currency="USD")
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
        self.now = datetime.now(timezone.utc)

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
        valid_days: int = 365,
    ) -> dict:
        mpv = _make_market_policy_version(
            self.session, market_id, restricted_claim_types=restricted_claim_types
        )
        now = self.now
        window = (now - timedelta(days=1), now + timedelta(days=valid_days))
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

    def _make_inventory(self, claims: list[dict], *, recipe_hash: str = "recipe-v1"):
        return create_inventory_from_analysis(
            self.session, self.ctx,
            asset_version_id=self.asset_version_id,
            analysis_recipe_hash=recipe_hash,
            claims=claims,
            provider="mock-vision", model="mock-vision-1", rule_version="claims-rule-v1",
            analysis_execution_mode="mock", adapter_version="mock-v1",
            mock_scenario="claims_default", mock_version="v1",
        )

    def _build_context(
        self, market_id: str, *, recipe_hash: str = "recipe-v1", valid_days: int = 365,
    ) -> dict:
        """Resolve real Truth, build an Inventory with one `preserve`
        brand-term Claim and one `replace` price Claim, and submit the
        matching decision set. Returns everything downstream `copy.py`
        calls need."""
        truth_ctx = self._resolve_market_truth(market_id, valid_days=valid_days)
        price_fact = self._fact(truth_ctx, "price")

        inventory = self._make_inventory(
            [
                {"claim_type": "brand_term", "scope_key": "hero_brand", "confidence": 0.95,
                 "ocr_text": "Buy Our Product Now"},
                {"claim_type": "price_claim", "scope_key": price_fact.scope_key, "confidence": 0.95,
                 "ocr_text": "$19.99"},
            ],
            recipe_hash=recipe_hash,
        )
        batch_id = self._make_batch(truth_ctx["truth_package_id"])

        decision_result = submit_claim_decisions(
            self.session, self.ctx,
            origin_batch_id=batch_id, inventory_id=inventory.id,
            truth_package_id=truth_ctx["truth_package_id"],
            truth_package_market_id=truth_ctx["truth_package_market_id"],
            target_market=truth_ctx["target_market"],
            market_policy_version_id=truth_ctx["market_policy_version_id"],
            decisions=[
                ClaimDecisionInput(claim_index=0, action="preserve"),
                ClaimDecisionInput(claim_index=1, action="replace", replacement_fact_id=price_fact.id),
            ],
            actor_id="user_1", role="owner",
        )
        self.assertTrue(decision_result.valid, decision_result.findings)

        return {
            "truth_ctx": truth_ctx,
            "price_fact": price_fact,
            "inventory": inventory,
            "decision_set_id": decision_result.decision_set_id,
        }

    def _make_batch(self, truth_package_id: str) -> str:
        from app.adapters.persistence.models import LocalizationBatchRecord

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

    def _copy_kwargs(self, ctxd: dict, *, copy_policy_snapshot=None) -> dict:
        truth_ctx = ctxd["truth_ctx"]
        return dict(
            source_asset_version_id=self.asset_version_id,
            inventory_id=ctxd["inventory"].id,
            truth_package_id=truth_ctx["truth_package_id"],
            truth_package_market_id=truth_ctx["truth_package_market_id"],
            claim_decision_set_id=ctxd["decision_set_id"],
            target_market=truth_ctx["target_market"],
            market_policy_version_id=truth_ctx["market_policy_version_id"],
            copy_policy_snapshot=copy_policy_snapshot if copy_policy_snapshot is not None else {"tone": "neutral"},
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCopyModule(_CopyTestCase):
    def test_source_locale_skips_translation_not_policy(self):
        ctxd = self._build_context("en-GB")
        result = create_source_locale_projection(
            self.session, self.ctx, at=self.now, **self._copy_kwargs(ctxd)
        )
        copy_version = result.copy_version

        self.assertEqual(copy_version.copy_origin, "source_locale_projection")
        # No translation call: every translation-only field stays NULL.
        self.assertIsNone(copy_version.translation_execution_mode)
        self.assertIsNone(copy_version.translation_provider)
        self.assertIsNone(copy_version.translation_model)
        self.assertIsNone(copy_version.translation_adapter_version)

        segments = {seg["claim_index"]: seg for seg in copy_version.content_json["segments"]}
        # The `preserve` Claim's text passes through untouched.
        self.assertEqual(segments[0]["kind"], "text")
        self.assertEqual(segments[0]["text"], "Buy Our Product Now")
        # The `replace` Claim's Fact policy still applied -- this is the
        # crux of "not a policy skip": the price was replaced with the
        # locked Fact value even though no translation call happened.
        self.assertEqual(segments[1]["kind"], "locked_replace")
        self.assertEqual(segments[1]["replacement_fact_id"], ctxd["price_fact"].id)
        self.assertEqual(segments[1]["value_json"], ctxd["price_fact"].value_json)
        # The raw OCR price text never survives into the final copy.
        self.assertNotIn("$19.99", str(segments[1]))

    def test_translation_receives_locked_placeholders(self):
        ctxd = self._build_context("de-DE")
        inventory_id = ctxd["inventory"].id
        decision_set_id = ctxd["decision_set_id"]

        prepared = prepare_copy_segments(
            self.session, self.ctx, inventory_id=inventory_id, decision_set_id=decision_set_id
        )
        self.assertEqual(len(prepared.translatable), 1)
        self.assertEqual(prepared.translatable[0].claim_index, 0)
        self.assertEqual(prepared.translatable[0].text, "Buy Our Product Now")

        self.assertEqual(len(prepared.locked_placeholders), 1)
        placeholder = prepared.locked_placeholders[0]
        self.assertEqual(placeholder.claim_index, 1)
        self.assertEqual(placeholder.replacement_fact_id, ctxd["price_fact"].id)
        self.assertEqual(placeholder.value_json, ctxd["price_fact"].value_json)
        # The token is opaque: it never contains the raw fact value.
        self.assertNotIn("19.99", placeholder.token)
        self.assertTrue(placeholder.token.startswith("⟦LOCK:"))

        # Completing translation for only the translatable segment
        # succeeds and deterministically injects the locked value.
        result = create_copy_from_translation(
            self.session, self.ctx,
            translated_segments={0: "Unser Produkt jetzt kaufen"},
            translation_execution_mode="mock", translation_provider="mock-translate",
            translation_model="mock-translate-1", translation_adapter_version="mock-v1",
            mock_scenario="translate_default", mock_version="v1",
            at=self.now,
            **self._copy_kwargs(ctxd),
        )
        segments = {seg["claim_index"]: seg for seg in result.copy_version.content_json["segments"]}
        self.assertEqual(segments[0]["text"], "Unser Produkt jetzt kaufen")
        self.assertEqual(segments[1]["kind"], "locked_replace")
        self.assertEqual(segments[1]["value_json"], ctxd["price_fact"].value_json)

        # The translator never receives the locked value: sending it the
        # raw fact-replacement claim index is rejected outright.
        with self.assertRaises(CopyError):
            create_copy_from_translation(
                self.session, self.ctx,
                translated_segments={0: "Unser Produkt jetzt kaufen", 1: "19,99 EUR"},
                translation_execution_mode="mock", translation_provider="mock-translate",
                translation_model="mock-translate-1", translation_adapter_version="mock-v1",
                mock_scenario="translate_default", mock_version="v1",
                at=self.now,
                **self._copy_kwargs(ctxd),
            )

        # A translator response that leaks the opaque token itself is
        # also rejected -- the token must never survive into final copy.
        with self.assertRaises(CopyError):
            create_copy_from_translation(
                self.session, self.ctx,
                translated_segments={0: f"Kaufen Sie {placeholder.token} jetzt"},
                translation_execution_mode="mock", translation_provider="mock-translate",
                translation_model="mock-translate-1", translation_adapter_version="mock-v1",
                mock_scenario="translate_default", mock_version="v1",
                at=self.now,
                **self._copy_kwargs(ctxd),
            )

    def test_reuse_requires_matching_fingerprint(self):
        ctxd = self._build_context("fr-FR")
        recipe = TranslationRecipe(
            execution_mode="mock", provider="mock-translate", model="mock-translate-1",
            adapter_version="mock-v1", mock_scenario="translate_default", mock_version="v1",
        )
        request = CopyPlanRequest(
            key="item-1",
            **self._copy_kwargs(ctxd),
            source_locale="en-US", target_locale="fr-FR",
            translation_recipe=recipe,
        )

        plan_before = plan_copy_versions(
            self.session, self.ctx, requests=[request],
            requested_evidence_tier=EvidenceTier.MOCK, at=self.now,
        )
        self.assertEqual(plan_before.reused, {})
        self.assertEqual(plan_before.missing_translations, ("item-1",))
        self.assertEqual(plan_before.source_locale_projections, ())

        result = create_copy_from_translation(
            self.session, self.ctx,
            translated_segments={0: "Achetez notre produit"},
            translation_execution_mode="mock", translation_provider="mock-translate",
            translation_model="mock-translate-1", translation_adapter_version="mock-v1",
            mock_scenario="translate_default", mock_version="v1",
            at=self.now,
            **self._copy_kwargs(ctxd),
        )

        plan_after = plan_copy_versions(
            self.session, self.ctx, requests=[request],
            requested_evidence_tier=EvidenceTier.MOCK, at=self.now,
        )
        self.assertEqual(plan_after.reused, {"item-1": result.copy_version.id})
        self.assertEqual(plan_after.missing_translations, ())

        # A different translation recipe (different model) is a different
        # semantic fingerprint -- it cannot reuse the existing version.
        different_recipe = TranslationRecipe(
            execution_mode="mock", provider="mock-translate", model="mock-translate-2",
            adapter_version="mock-v1", mock_scenario="translate_default", mock_version="v1",
        )
        different_request = CopyPlanRequest(
            key="item-2",
            **self._copy_kwargs(ctxd),
            source_locale="en-US", target_locale="fr-FR",
            translation_recipe=different_recipe,
        )
        plan_diff = plan_copy_versions(
            self.session, self.ctx, requests=[different_request],
            requested_evidence_tier=EvidenceTier.MOCK, at=self.now,
        )
        self.assertEqual(plan_diff.reused, {})
        self.assertEqual(plan_diff.missing_translations, ("item-2",))

        self.assertEqual(
            self.session.query(LocalizedCopyVersionRecord).count(), 1
        )

    def test_stale_truth_prevents_reuse(self):
        ctxd = self._build_context("it-IT", valid_days=10)
        recipe = TranslationRecipe(
            execution_mode="mock", provider="mock-translate", model="mock-translate-1",
            adapter_version="mock-v1", mock_scenario="translate_default", mock_version="v1",
        )
        result = create_copy_from_translation(
            self.session, self.ctx,
            translated_segments={0: "Compra il nostro prodotto"},
            translation_execution_mode="mock", translation_provider="mock-translate",
            translation_model="mock-translate-1", translation_adapter_version="mock-v1",
            mock_scenario="translate_default", mock_version="v1",
            at=self.now,
            **self._copy_kwargs(ctxd),
        )

        request = CopyPlanRequest(
            key="item-1",
            **self._copy_kwargs(ctxd),
            source_locale="en-US", target_locale="it-IT",
            translation_recipe=recipe,
        )

        # Fresh: reused.
        fresh_plan = plan_copy_versions(
            self.session, self.ctx, requests=[request],
            requested_evidence_tier=EvidenceTier.MOCK, at=self.now,
        )
        self.assertEqual(fresh_plan.reused, {"item-1": result.copy_version.id})

        # Well past the TruthPackage's `valid_until` (resolved with a
        # 10-day validity window): matching fingerprint, but stale Truth
        # blocks reuse.
        stale_at = self.now + timedelta(days=30)
        stale_plan = plan_copy_versions(
            self.session, self.ctx, requests=[request],
            requested_evidence_tier=EvidenceTier.MOCK, at=stale_at,
        )
        self.assertEqual(stale_plan.reused, {})
        self.assertIn("item-1", stale_plan.stale)
        self.assertIn("valid_until_expired", stale_plan.stale["item-1"])
        self.assertEqual(stale_plan.missing_translations, ("item-1",))

        # Creating fresh copy against stale Truth is also rejected --
        # freshness is required at creation, not only at reuse time.
        with self.assertRaises(CopyError):
            create_copy_from_translation(
                self.session, self.ctx,
                translated_segments={0: "Compra il nostro prodotto"},
                translation_execution_mode="mock", translation_provider="mock-translate",
                translation_model="mock-translate-1", translation_adapter_version="mock-v1",
                mock_scenario="translate_default", mock_version="v1",
                at=stale_at,
                **self._copy_kwargs(ctxd),
            )

    def test_approve_creates_immutable_record(self):
        ctxd = self._build_context("en-AU")
        result = create_source_locale_projection(
            self.session, self.ctx, at=self.now, **self._copy_kwargs(ctxd)
        )
        copy_version_id = result.copy_version.id
        content_before = result.copy_version.content_json
        evidence_hash_before = result.copy_version.evidence_hash
        series_hash_before = result.copy_version.copy_series_hash
        # Translation/injection/validation are already complete by the
        # time creation returns, and `draft -> approved` is not an
        # allowed transition -- every copy version starts at
        # `needs_review`, never `draft`.
        self.assertEqual(result.copy_version.state, "needs_review")

        approval = approve_copy_version(
            self.session, self.ctx, copy_version_id=copy_version_id,
            actor_id="user_1", role="owner",
        )
        self.assertEqual(approval.copy_version_id, copy_version_id)
        self.assertEqual(approval.actor_id, "user_1")
        self.assertEqual(approval.role, "owner")
        self.assertIsInstance(approval, LocalizedCopyApprovalRecord)

        refreshed = self.session.get(LocalizedCopyVersionRecord, copy_version_id)
        self.assertEqual(refreshed.state, "approved")
        # Immutable except `state`.
        self.assertEqual(refreshed.content_json, content_before)
        self.assertEqual(refreshed.evidence_hash, evidence_hash_before)
        self.assertEqual(refreshed.copy_series_hash, series_hash_before)

        # A second approver appends its own row without re-transitioning
        # (already-`approved`) state.
        second_approval = approve_copy_version(
            self.session, self.ctx, copy_version_id=copy_version_id,
            actor_id="user_2", role="member",
        )
        self.assertNotEqual(second_approval.id, approval.id)
        self.assertEqual(
            self.session.query(LocalizedCopyApprovalRecord)
            .filter_by(copy_version_id=copy_version_id)
            .count(),
            2,
        )
        self.assertEqual(
            self.session.get(LocalizedCopyVersionRecord, copy_version_id).state, "approved"
        )

        with self.assertRaises(CopyError):
            approve_copy_version(
                self.session, self.ctx, copy_version_id=copy_version_id,
                actor_id="user_3", role="not_a_role",
            )

        refreshed.state = "superseded"
        self.session.flush()
        with self.assertRaises(CopyError):
            approve_copy_version(
                self.session, self.ctx, copy_version_id=copy_version_id,
                actor_id="user_4", role="owner",
            )

    def test_copy_version_invisible_across_workspace_tenant(self):
        """CLAUDE.md invariant: workspace-scoped queries filter by
        ctx.workspace_id. A copy version created in one Workspace must be
        unreachable -- for both mutation and reuse -- through a different
        Workspace's TenantContext, even given the identical id / request
        parameters."""
        ctxd = self._build_context("ko-KR")
        result = create_source_locale_projection(
            self.session, self.ctx, at=self.now, **self._copy_kwargs(ctxd)
        )
        copy_version_id = result.copy_version.id

        other_ws_id = "ws_copy_test_other"
        self.session.add(
            WorkspaceRecord(id=other_ws_id, name="Other WS", lifecycle="active", budget_currency="USD")
        )
        self.session.flush()
        other_ctx = TenantContext(
            workspace_id=other_ws_id, role="owner", initiator_id="user_other", initiator_role="owner",
        )

        # A different Workspace's actor cannot approve (or even see) this
        # copy version by id.
        with self.assertRaises(CopyError):
            approve_copy_version(
                self.session, other_ctx, copy_version_id=copy_version_id,
                actor_id="user_other", role="owner",
            )
        self.assertEqual(
            self.session.query(LocalizedCopyApprovalRecord)
            .filter_by(copy_version_id=copy_version_id)
            .count(),
            0,
        )
        # The original Workspace's version is untouched by the attempt.
        self.assertEqual(
            self.session.get(LocalizedCopyVersionRecord, copy_version_id).state, "needs_review"
        )

        # A reuse plan built from a different Workspace's TenantContext
        # never reuses another Workspace's copy version, even with the
        # exact same series-identity parameters -- the lookup is always
        # scoped by workspace_id, not by copy_series_hash alone.
        request = CopyPlanRequest(
            key="item-1",
            **self._copy_kwargs(ctxd),
            source_locale="ko-KR", target_locale="ko-KR",
        )
        other_plan = plan_copy_versions(
            self.session, other_ctx, requests=[request],
            requested_evidence_tier=EvidenceTier.MOCK, at=self.now,
        )
        self.assertEqual(other_plan.reused, {})
        self.assertEqual(other_plan.source_locale_projections, ("item-1",))

        # The owning Workspace's own plan still reuses it correctly.
        own_plan = plan_copy_versions(
            self.session, self.ctx, requests=[request],
            requested_evidence_tier=EvidenceTier.MOCK, at=self.now,
        )
        self.assertEqual(own_plan.reused, {"item-1": copy_version_id})

    def test_copy_series_includes_all_provenance(self):
        ctxd = self._build_context("ja-JP")
        prepared = prepare_copy_segments(
            self.session, self.ctx,
            inventory_id=ctxd["inventory"].id, decision_set_id=ctxd["decision_set_id"],
        )

        result = create_copy_from_translation(
            self.session, self.ctx,
            translated_segments={0: "今すぐ購入"},
            translation_execution_mode="byok", translation_provider="acme-mt",
            translation_model="acme-mt-large", translation_adapter_version="acme-v3",
            at=self.now,
            **self._copy_kwargs(ctxd),
        )
        copy_version = result.copy_version

        self.assertEqual(copy_version.truth_package_id, ctxd["truth_ctx"]["truth_package_id"])
        self.assertEqual(copy_version.claim_decision_set_id, ctxd["decision_set_id"])
        self.assertEqual(copy_version.source_asset_version_id, self.asset_version_id)
        self.assertEqual(copy_version.inventory_id, ctxd["inventory"].id)
        self.assertEqual(copy_version.translation_provider, "acme-mt")
        self.assertEqual(copy_version.translation_model, "acme-mt-large")
        self.assertEqual(copy_version.translation_adapter_version, "acme-v3")
        self.assertEqual(copy_version.translation_execution_mode, "byok")
        self.assertIsNone(copy_version.mock_scenario)
        self.assertIsNone(copy_version.mock_version)
        self.assertTrue(copy_version.copy_series_hash)
        self.assertTrue(copy_version.evidence_hash)
        self.assertTrue(copy_version.copy_policy_snapshot_hash)
        # Provenance ties the copy version back to the exact source text
        # it was built from.
        self.assertEqual(copy_version.content_json["source_text_hash"], prepared.source_text_hash)

        # Provider/model info is part of the semantic identity: a
        # different provider for the identical context is a distinct
        # series, not the same one.
        other = create_copy_from_translation(
            self.session, self.ctx,
            translated_segments={0: "今すぐ購入"},
            translation_execution_mode="byok", translation_provider="other-mt",
            translation_model="other-mt-large", translation_adapter_version="other-v1",
            at=self.now,
            **self._copy_kwargs(ctxd),
        )
        self.assertNotEqual(other.copy_version.copy_series_hash, copy_version.copy_series_hash)
        self.assertEqual(other.copy_version.version_number, 1)
        self.assertEqual(copy_version.version_number, 1)


if __name__ == "__main__":
    unittest.main()
