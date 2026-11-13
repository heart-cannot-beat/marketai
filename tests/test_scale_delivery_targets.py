"""Tests for ScaleDeliveryTargetRecord and its population/lookup module
(spec §4.3, issue #86 Task 7).

Flat scratch-SQLite style (mirroring ``tests/test_meta_evidence_publication.py``):
no shared ``db_session`` fixture exists in ``tests/conftest.py`` for this
suite. Every table in ``Base.metadata`` is created (not a hand-picked
subset) because ``ScaleDeliveryTargetRecord`` declares composite FKs into
several other domains (Meta, Creative Library, Generation) and this
project's SQLite connections run with ``PRAGMA foreign_keys=ON`` (see
``app/adapters/persistence/database.py``), so every referenced table must
physically exist even for a row that never touches most of them.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import (
    AssetRecord,
    AssetVersionRecord,
    GenerationRunRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    OperationRecord,
    PlanRecord,
    ProductRecord,
    WorkspaceRecord,
    new_id,
)
from app.meta.evidence import create_attestation, publish_evidence
from app.meta.models import MetaConnectionRecord, ScenarioVersionRecord
from app.meta.observation import create_contract_generation
from app.meta.sync_pipeline import create_sync_run
from app.platform.canonical import canonical_hash
from app.platform.tenancy import TenantContext
from app.scale.canonical_lineage import canonical_lineage_hash
from app.scale.delivery_targets import (
    DeliveryTargetExpiredError,
    DeliveryTargetNotFoundError,
    DeliveryTargetRequest,
    InvalidSourceProvenanceError,
    create_delivery_target,
    validate_delivery_target,
)
from app.scale.models import ScaleDeliveryTargetRecord

OBSERVED_AT = datetime(2026, 9, 1, tzinfo=timezone.utc)
FAR_FUTURE = OBSERVED_AT + timedelta(days=365)
PAST = OBSERVED_AT - timedelta(days=1)


class DeliveryTargetTestBase(unittest.TestCase):
    def setUp(self) -> None:
        tmp_dir = Path(tempfile.mkdtemp(prefix="marketai-sdt-"))
        self.addCleanup(shutil.rmtree, tmp_dir, ignore_errors=True)
        self.engine = create_engine(
            f"sqlite:///{(tmp_dir / 'sdt.db').as_posix()}",
            connect_args={"check_same_thread": False},
        )
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(bind=self.engine)
        self.session = Session(bind=self.engine)
        self.addCleanup(self.session.close)

        self.ws = WorkspaceRecord(id=new_id("ws"), name="Test WS")
        self.session.add(self.ws)
        self.session.flush()

        self._asset_counter = 0
        self._library_counter = 0

    # -- fixture builders ---------------------------------------------------

    def _make_asset_version(self) -> str:
        self._asset_counter += 1
        n = self._asset_counter
        product = ProductRecord(id=new_id("prod"), workspace_id=self.ws.id, name=f"Product {n}")
        self.session.add(product)
        self.session.flush()

        plan = PlanRecord(
            id=new_id("plan"), workspace_id=self.ws.id, product_id=product.id,
            mode="full", snapshot_hash="p" * 64,
        )
        self.session.add(plan)
        self.session.flush()

        run = GenerationRunRecord(
            id=new_id("run"), workspace_id=self.ws.id, product_id=product.id,
            plan_id=plan.id, provider="mock-vision", model="mock-vision-1",
            aspect_ratio="1:1", image_size="1024x1024", variants=1, use_ref=False,
            status="succeeded", currency="USD",
        )
        self.session.add(run)
        self.session.flush()

        asset = AssetRecord(
            id=new_id("asset"), workspace_id=self.ws.id, product_id=product.id,
            run_id=run.id, combo_id=f"combo_{n}", combo_code=f"combo-{n}",
            tags_json={}, filename=f"asset_{n}.png", provider="mock-vision",
            model="mock-vision-1", prompt="a test asset", fingerprint=f"fp_{n}",
            status="succeeded",
        )
        self.session.add(asset)
        self.session.flush()

        version = AssetVersionRecord(
            id=new_id("astv"), workspace_id=self.ws.id, asset_id=asset.id,
            kind="image", aspect_ratio="1:1", object_key=f"assets/{n}.png",
            content_sha256=f"s{n}" * 16, byte_size=1024, mime_type="image/png",
        )
        self.session.add(version)
        self.session.flush()
        return version.id

    def _make_library_version(self, entry_type: str) -> str:
        self._library_counter += 1
        n = self._library_counter
        entry = LibraryEntryRecord(
            id=new_id("lent"), workspace_id=self.ws.id, entry_type=entry_type,
            display_name=f"{entry_type} {n}",
        )
        self.session.add(entry)
        self.session.flush()

        content = {"n": n}
        version = LibraryVersionRecord(
            id=new_id("lver"), workspace_id=self.ws.id, entry_id=entry.id,
            entry_type=entry_type, version_no=1, content_json=content,
            content_hash=canonical_hash(content), published_by="test-fixture",
        )
        self.session.add(version)
        self.session.flush()
        return version.id

    def _make_curated_scenario_context(self, meta_mode: str = "controlled_mock") -> dict:
        conn = MetaConnectionRecord(
            id=new_id("mcon"), workspace_id=self.ws.id,
            meta_mode=meta_mode, ad_account_id="act_1",
        )
        self.session.add(conn)
        self.session.flush()
        gen = create_contract_generation(
            self.session, workspace_id=self.ws.id, meta_mode=meta_mode,
            connection_id=conn.id, contract_type="delivery_configuration",
            ad_account_id="act_1", fields=["status"], attribution_window={},
            breakdowns=[], time_window={}, local_timezone="UTC",
        )
        scenario = ScenarioVersionRecord(
            id=new_id("scv"), workspace_id=self.ws.id, meta_mode=meta_mode,
            meta_connection_id=conn.id, scenario_config={}, scenario_hash="h" * 64,
        )
        self.session.add(scenario)
        self.session.flush()
        return {
            "meta_connection_id": conn.id,
            "contract_generation_id": gen.id,
            "scenario_version_id": scenario.id,
        }

    def _make_evidence_observation_context(self) -> dict:
        meta_mode = "internal_validation"
        conn = MetaConnectionRecord(
            id=new_id("mcon"), workspace_id=self.ws.id,
            meta_mode=meta_mode, ad_account_id="act_1",
        )
        self.session.add(conn)
        self.session.flush()
        gen = create_contract_generation(
            self.session, workspace_id=self.ws.id, meta_mode=meta_mode,
            connection_id=conn.id, contract_type="delivery_configuration",
            ad_account_id="act_1", fields=["status"], attribution_window={},
            breakdowns=[], time_window={}, local_timezone="UTC",
        )
        op = OperationRecord(
            id=new_id("op"), workspace_id=self.ws.id,
            command_type="meta_sync", state="succeeded",
        )
        self.session.add(op)
        self.session.flush()
        run = create_sync_run(
            self.session, workspace_id=self.ws.id, meta_mode=meta_mode,
            contract_generation_id=gen.id, occurrence_id=f"occ_{new_id('x')}",
            operation_id=op.id, executor="system:worker",
        )
        pub = publish_evidence(self.session, sync_run_id=run.id, operation_id=op.id)
        attestation = create_attestation(
            self.session, contract_generation_id=gen.id, publication_id=pub.id,
            evidence_watermark="[2026-09-01,2026-09-01]",
        )
        return {
            "meta_connection_id": conn.id,
            "contract_generation_id": gen.id,
            "source_sync_run_id": run.id,
            "source_publication_id": pub.id,
            "source_attestation_id": attestation.id,
        }

    def _curated_scenario_request(self, **overrides) -> DeliveryTargetRequest:
        ctx = self._make_curated_scenario_context()
        offer_id = self._make_library_version("offer")
        landing_id = "f" * 64  # the Landing Page evidence hash a Winner froze (#129)
        asset_version_id = self._make_asset_version()
        fields = dict(
            meta_connection_id=ctx["meta_connection_id"],
            ad_account_id="act_1",
            scenario_scope_key="scn_1",
            campaign_remote_id="camp_1",
            adset_remote_id="adset_1",
            ad_remote_id="ad_1",
            ad_creative_id="creative_1",
            creative_asset_version_id=asset_version_id,
            creative_binding_evidence_hash="e" * 64,
            market_code="US",
            placement="feed",
            attribution_setting="7d_click_1d_view",
            contract_generation_id=ctx["contract_generation_id"],
            offer_version_id=offer_id,
            landing_page_evidence_hash=landing_id,
            source_type="curated_scenario",
            source_manifest={"schema_version": 1, "source_type": "curated_scenario"},
            observed_at=OBSERVED_AT,
            expires_at=FAR_FUTURE,
            scenario_version_id=ctx["scenario_version_id"],
        )
        fields.update(overrides)
        return DeliveryTargetRequest(**fields)


class TestCreateDeliveryTargetCuratedScenario(DeliveryTargetTestBase):
    def test_creates_target_with_computed_fingerprint(self):
        request = self._curated_scenario_request()
        ctx = TenantContext(
            workspace_id=self.ws.id, role="system", executor="system:worker",
            initiator_id="user_1", meta_mode="controlled_mock",
        )
        target = create_delivery_target(self.session, ctx, request)

        self.assertEqual(target.workspace_id, self.ws.id)
        self.assertEqual(target.meta_mode, "controlled_mock")
        self.assertEqual(target.source_type, "curated_scenario")
        self.assertEqual(len(target.target_fingerprint), 64)
        self.assertEqual(len(target.source_manifest_hash), 64)
        self.assertEqual(
            target.remote_binding_key,
            f"{request.meta_connection_id}/act_1/camp_1/adset_1/ad_1",
        )
        self.assertIsNone(target.source_sync_run_id)
        self.assertIsNone(target.source_publication_id)
        self.assertIsNone(target.source_attestation_id)

    def test_fingerprint_deterministic_across_identical_requests(self):
        """Same delivery-slot fields -> same target_fingerprint, even on a
        distinct row (different id). Built from one shared upstream context
        (a second MetaConnectionRecord in the same workspace/mode would
        collide with the active-connection partial unique index, which is
        not what this test is proving)."""
        ctx_data = self._make_curated_scenario_context()
        offer_id = self._make_library_version("offer")
        landing_id = "f" * 64  # the Landing Page evidence hash a Winner froze (#129)
        asset_version_id = self._make_asset_version()

        def _request() -> DeliveryTargetRequest:
            return DeliveryTargetRequest(
                meta_connection_id=ctx_data["meta_connection_id"],
                ad_account_id="act_1",
                scenario_scope_key="scn_1",
                campaign_remote_id="camp_1",
                adset_remote_id="adset_1",
                ad_remote_id="ad_1",
                ad_creative_id="creative_1",
                creative_asset_version_id=asset_version_id,
                creative_binding_evidence_hash="e" * 64,
                market_code="US",
                placement="feed",
                attribution_setting="7d_click_1d_view",
                contract_generation_id=ctx_data["contract_generation_id"],
                offer_version_id=offer_id,
                landing_page_evidence_hash=landing_id,
                source_type="curated_scenario",
                source_manifest={"schema_version": 1, "source_type": "curated_scenario"},
                observed_at=OBSERVED_AT,
                expires_at=FAR_FUTURE,
                scenario_version_id=ctx_data["scenario_version_id"],
            )

        ctx = TenantContext(
            workspace_id=self.ws.id, role="system", executor="system:worker",
            initiator_id="user_1", meta_mode="controlled_mock",
        )
        first = create_delivery_target(self.session, ctx, _request())
        second = create_delivery_target(self.session, ctx, _request())

        self.assertNotEqual(first.id, second.id)
        self.assertEqual(first.target_fingerprint, second.target_fingerprint)

    def test_source_manifest_hash_matches_canonical_lineage_hash(self):
        request = self._curated_scenario_request(
            source_manifest={"schema_version": 1, "k": "v"},
        )
        ctx = TenantContext(
            workspace_id=self.ws.id, role="system", executor="system:worker",
            initiator_id="user_1", meta_mode="controlled_mock",
        )
        target = create_delivery_target(self.session, ctx, request)
        expected = canonical_lineage_hash(1, {"schema_version": 1, "k": "v"})
        self.assertEqual(target.source_manifest_hash, expected)

    def test_wrong_mode_for_curated_scenario_rejected(self):
        request = self._curated_scenario_request()
        ctx = TenantContext(
            workspace_id=self.ws.id, role="system", executor="system:worker",
            initiator_id="user_1", meta_mode="internal_validation",
        )
        with self.assertRaises(InvalidSourceProvenanceError):
            create_delivery_target(self.session, ctx, request)

    def test_missing_scenario_version_id_rejected(self):
        request = self._curated_scenario_request(scenario_version_id=None)
        ctx = TenantContext(
            workspace_id=self.ws.id, role="system", executor="system:worker",
            initiator_id="user_1", meta_mode="controlled_mock",
        )
        with self.assertRaises(InvalidSourceProvenanceError):
            create_delivery_target(self.session, ctx, request)

    def test_extra_evidence_refs_on_curated_scenario_rejected(self):
        """Source-type biconditional: curated_scenario must NOT carry any
        evidence_observation reference."""
        request = self._curated_scenario_request(source_sync_run_id="msync_stray")
        ctx = TenantContext(
            workspace_id=self.ws.id, role="system", executor="system:worker",
            initiator_id="user_1", meta_mode="controlled_mock",
        )
        with self.assertRaises(InvalidSourceProvenanceError):
            create_delivery_target(self.session, ctx, request)


class TestCreateDeliveryTargetEvidenceObservation(DeliveryTargetTestBase):
    def test_creates_target_with_evidence_provenance(self):
        ectx = self._make_evidence_observation_context()
        offer_id = self._make_library_version("offer")
        landing_id = "f" * 64  # the Landing Page evidence hash a Winner froze (#129)
        asset_version_id = self._make_asset_version()
        request = DeliveryTargetRequest(
            meta_connection_id=ectx["meta_connection_id"],
            ad_account_id="act_1",
            scenario_scope_key="__no_scenario__",
            campaign_remote_id="camp_1",
            adset_remote_id="adset_1",
            ad_remote_id="ad_1",
            ad_creative_id="creative_1",
            creative_asset_version_id=asset_version_id,
            creative_binding_evidence_hash="e" * 64,
            market_code="US",
            placement="feed",
            attribution_setting="7d_click_1d_view",
            contract_generation_id=ectx["contract_generation_id"],
            offer_version_id=offer_id,
            landing_page_evidence_hash=landing_id,
            source_type="evidence_observation",
            source_manifest={"schema_version": 1, "source_type": "evidence_observation"},
            observed_at=OBSERVED_AT,
            expires_at=FAR_FUTURE,
            source_sync_run_id=ectx["source_sync_run_id"],
            source_publication_id=ectx["source_publication_id"],
            source_attestation_id=ectx["source_attestation_id"],
        )
        ctx = TenantContext(
            workspace_id=self.ws.id, role="system", executor="system:worker",
            initiator_id="user_1", meta_mode="internal_validation",
        )
        target = create_delivery_target(self.session, ctx, request)

        self.assertEqual(target.source_type, "evidence_observation")
        self.assertIsNone(target.scenario_version_id)
        self.assertEqual(target.source_sync_run_id, ectx["source_sync_run_id"])
        self.assertEqual(target.source_publication_id, ectx["source_publication_id"])
        self.assertEqual(target.source_attestation_id, ectx["source_attestation_id"])

    def test_missing_evidence_refs_rejected(self):
        ectx = self._make_evidence_observation_context()
        offer_id = self._make_library_version("offer")
        landing_id = "f" * 64  # the Landing Page evidence hash a Winner froze (#129)
        asset_version_id = self._make_asset_version()
        request = DeliveryTargetRequest(
            meta_connection_id=ectx["meta_connection_id"],
            ad_account_id="act_1",
            scenario_scope_key="__no_scenario__",
            campaign_remote_id="camp_1",
            adset_remote_id="adset_1",
            ad_remote_id="ad_1",
            ad_creative_id="creative_1",
            creative_asset_version_id=asset_version_id,
            creative_binding_evidence_hash="e" * 64,
            market_code="US",
            placement="feed",
            attribution_setting="7d_click_1d_view",
            contract_generation_id=ectx["contract_generation_id"],
            offer_version_id=offer_id,
            landing_page_evidence_hash=landing_id,
            source_type="evidence_observation",
            source_manifest={"schema_version": 1},
            observed_at=OBSERVED_AT,
            expires_at=FAR_FUTURE,
            source_sync_run_id=ectx["source_sync_run_id"],
            # source_publication_id and source_attestation_id omitted
        )
        ctx = TenantContext(
            workspace_id=self.ws.id, role="system", executor="system:worker",
            initiator_id="user_1", meta_mode="internal_validation",
        )
        with self.assertRaises(InvalidSourceProvenanceError):
            create_delivery_target(self.session, ctx, request)


class TestValidateDeliveryTarget(DeliveryTargetTestBase):
    def _create(self, **overrides) -> ScaleDeliveryTargetRecord:
        request = self._curated_scenario_request(**overrides)
        ctx = TenantContext(
            workspace_id=self.ws.id, role="system", executor="system:worker",
            initiator_id="user_1", meta_mode="controlled_mock",
        )
        return create_delivery_target(self.session, ctx, request)

    def _ctx(self) -> TenantContext:
        return TenantContext(
            workspace_id=self.ws.id, role="system", executor="system:worker",
            initiator_id="user_1", meta_mode="controlled_mock",
        )

    def test_validate_returns_live_target(self):
        target = self._create()
        loaded = validate_delivery_target(self.session, self._ctx(), target_id=target.id)
        self.assertEqual(loaded.id, target.id)

    def test_validate_not_found(self):
        with self.assertRaises(DeliveryTargetNotFoundError):
            validate_delivery_target(self.session, self._ctx(), target_id="sdt_missing")

    def test_validate_expired(self):
        target = self._create(expires_at=PAST)
        with self.assertRaises(DeliveryTargetExpiredError):
            validate_delivery_target(self.session, self._ctx(), target_id=target.id)

    def test_validate_scoped_to_meta_mode(self):
        """A target created under controlled_mock is invisible to a
        customer_sandbox context in the same workspace."""
        target = self._create()
        other_ctx = TenantContext(
            workspace_id=self.ws.id, role="system", executor="system:worker",
            initiator_id="user_1", meta_mode="customer_sandbox",
        )
        with self.assertRaises(DeliveryTargetNotFoundError):
            validate_delivery_target(self.session, other_ctx, target_id=target.id)


class TestScaleDeliveryTargetRecordShape(DeliveryTargetTestBase):
    def test_table_name(self):
        self.assertEqual(ScaleDeliveryTargetRecord.__tablename__, "scale_delivery_targets")

    def test_adset_remote_id_has_no_underscore(self):
        """Naming divergence from ScaleEnrollmentRecord.ad_set_remote_id is
        intentional (spec §4.3)."""
        columns = {c.name for c in ScaleDeliveryTargetRecord.__table__.columns}
        self.assertIn("adset_remote_id", columns)
        self.assertNotIn("ad_set_remote_id", columns)

    def test_id_prefix(self):
        target_id = new_id("sdt")
        self.assertTrue(target_id.startswith("sdt_"))

    def test_database_rejects_source_type_biconditional_violation(self):
        """Proves the DB CHECK enforces the same rule Python does -- direct
        ORM construction bypasses create_delivery_target's validation."""
        ctx = self._make_curated_scenario_context()
        offer_id = self._make_library_version("offer")
        landing_id = "f" * 64  # the Landing Page evidence hash a Winner froze (#129)
        asset_version_id = self._make_asset_version()
        bad = ScaleDeliveryTargetRecord(
            id=new_id("sdt"), workspace_id=self.ws.id, meta_mode="controlled_mock",
            meta_connection_id=ctx["meta_connection_id"], ad_account_id="act_1",
            scenario_scope_key="scn_1", campaign_remote_id="camp_1",
            adset_remote_id="adset_1", ad_remote_id="ad_1",
            ad_creative_id="creative_1", creative_asset_version_id=asset_version_id,
            creative_binding_evidence_hash="e" * 64, market_code="US", placement="feed",
            attribution_setting="7d_click_1d_view",
            contract_generation_id=ctx["contract_generation_id"],
            offer_version_id=offer_id, landing_page_evidence_hash=landing_id,
            source_type="curated_scenario",
            scenario_version_id=None,  # violates ck_sdt_source_type_biconditional
            source_manifest={}, source_manifest_hash="h" * 64,
            target_fingerprint="f" * 64, remote_binding_key="k",
            observed_at=OBSERVED_AT, expires_at=FAR_FUTURE,
        )
        self.session.add(bad)
        with self.assertRaises(IntegrityError):
            self.session.flush()
        self.session.rollback()

    def test_database_rejects_empty_hierarchy_identifier(self):
        ctx = self._make_curated_scenario_context()
        offer_id = self._make_library_version("offer")
        landing_id = "f" * 64  # the Landing Page evidence hash a Winner froze (#129)
        asset_version_id = self._make_asset_version()
        bad = ScaleDeliveryTargetRecord(
            id=new_id("sdt"), workspace_id=self.ws.id, meta_mode="controlled_mock",
            meta_connection_id=ctx["meta_connection_id"], ad_account_id="act_1",
            scenario_scope_key="scn_1", campaign_remote_id="",  # violates hierarchy CHECK
            adset_remote_id="adset_1", ad_remote_id="ad_1",
            ad_creative_id="creative_1", creative_asset_version_id=asset_version_id,
            creative_binding_evidence_hash="e" * 64, market_code="US", placement="feed",
            attribution_setting="7d_click_1d_view",
            contract_generation_id=ctx["contract_generation_id"],
            offer_version_id=offer_id, landing_page_evidence_hash=landing_id,
            source_type="curated_scenario",
            scenario_version_id=ctx["scenario_version_id"],
            source_manifest={}, source_manifest_hash="h" * 64,
            target_fingerprint="f" * 64, remote_binding_key="k",
            observed_at=OBSERVED_AT, expires_at=FAR_FUTURE,
        )
        self.session.add(bad)
        with self.assertRaises(IntegrityError):
            self.session.flush()
        self.session.rollback()
