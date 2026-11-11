"""Tests for ``ScaleLineageGuardRecord`` and ``get_or_create_lineage_guard()``
(spec §7.3, issue #86 Task 8).

Flat scratch-SQLite style (mirroring ``tests/test_scale_delivery_targets.py``,
whose asset-version/library-version/curated-scenario fixture builders are
reused here almost verbatim): every table in ``Base.metadata`` is created
because ``ScaleLineageGuardRecord`` declares composite FKs into several other
domains and this project's SQLite connections run with ``PRAGMA foreign_
keys=ON``. ``winner_decision_id``, ``winner_confirmation_id`` and
``market_asset_binding_id`` have no database-level FK (see
``ScaleLineageGuardRecord``'s docstring in ``app/scale/models.py`` for why),
so plain fabricated ids are used for those three fields; every other
identity field is a real, FK-satisfying row.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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
    PlanRecord,
    ProductRecord,
    WorkspaceRecord,
    new_id,
)
from app.meta.models import MetaConnectionRecord, ScenarioVersionRecord
from app.meta.observation import create_contract_generation
from app.platform.canonical import canonical_hash
from app.platform.tenancy import TenantContext
from app.scale.delivery_targets import DeliveryTargetRequest, create_delivery_target
from app.scale.lineage_guard import get_or_create_lineage_guard
from app.scale.models import ScaleLineageGuardRecord

OBSERVED_AT = datetime(2026, 9, 1, tzinfo=timezone.utc)
FAR_FUTURE = OBSERVED_AT + timedelta(days=365)


class LineageGuardTestBase(unittest.TestCase):
    def setUp(self) -> None:
        tmp_dir = Path(tempfile.mkdtemp(prefix="marketai-slg-"))
        self.addCleanup(shutil.rmtree, tmp_dir, ignore_errors=True)
        self.engine = create_engine(
            f"sqlite:///{(tmp_dir / 'slg.db').as_posix()}",
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

    # -- fixture builders (mirrors tests/test_scale_delivery_targets.py) ---

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

    def _make_delivery_target(
        self, meta_mode: str = "controlled_mock", scenario_scope_key: str = "scn_1",
        upstream: dict | None = None,
    ) -> tuple[str, dict]:
        """Returns ``(delivery_target_id, upstream_ids)`` where
        ``upstream_ids`` carries ``meta_connection_id``/``contract_generation_id``
        so a caller can reuse them for the guard's own FK-satisfying fields.

        ``upstream`` may be supplied to reuse an existing
        MetaConnection/contract-generation pair: only one *active*
        MetaConnectionRecord may exist per (workspace, meta_mode) (a
        partial unique index), so building two delivery targets in the
        same mode must share one connection, not create two."""
        if upstream is None:
            upstream = self._make_curated_scenario_context(meta_mode)
        offer_id = self._make_library_version("offer")
        landing_id = "f" * 64  # the Landing Page evidence hash a Winner froze (#129)
        creative_asset_version_id = self._make_asset_version()
        request = DeliveryTargetRequest(
            meta_connection_id=upstream["meta_connection_id"],
            ad_account_id="act_1",
            scenario_scope_key=scenario_scope_key,
            campaign_remote_id="camp_1",
            adset_remote_id="adset_1",
            ad_remote_id="ad_1",
            ad_creative_id="creative_1",
            creative_asset_version_id=creative_asset_version_id,
            creative_binding_evidence_hash="e" * 64,
            market_code="US",
            placement="feed",
            attribution_setting="7d_click_1d_view",
            contract_generation_id=upstream["contract_generation_id"],
            offer_version_id=offer_id,
            landing_page_evidence_hash=landing_id,
            source_type="curated_scenario",
            source_manifest={"schema_version": 1, "source_type": "curated_scenario"},
            observed_at=OBSERVED_AT,
            expires_at=FAR_FUTURE,
            scenario_version_id=upstream["scenario_version_id"],
        )
        ctx = TenantContext(
            workspace_id=self.ws.id, role="system", executor="system:worker",
            initiator_id="user_1", meta_mode=meta_mode,
        )
        target = create_delivery_target(self.session, ctx, request)
        return target.id, upstream

    def _guard_fields(
        self, meta_mode: str = "controlled_mock", scenario_scope_key: str = "scn_1",
        upstream: dict | None = None,
    ) -> dict:
        """A complete, FK-satisfying set of ``get_or_create_lineage_guard()``
        keyword arguments (except ``winner_decision_id``/
        ``winner_confirmation_id``, which the caller supplies so tests can
        vary them to prove the guard's identity is exactly the
        (decision, target) pair)."""
        delivery_target_id, upstream = self._make_delivery_target(
            meta_mode, scenario_scope_key, upstream=upstream,
        )
        return dict(
            asset_version_id=self._make_asset_version(),
            product_version_id=self._make_library_version("product"),
            offer_version_id=self._make_library_version("offer"),
            market_asset_binding_id=new_id("mab"),
            delivery_target_id=delivery_target_id,
            meta_connection_id=upstream["meta_connection_id"],
            contract_generation_id=upstream["contract_generation_id"],
        )

    def _ctx(self, meta_mode: str = "controlled_mock") -> TenantContext:
        return TenantContext(
            workspace_id=self.ws.id, role="system", executor="system:worker",
            initiator_id="user_1", meta_mode=meta_mode,
        )


class TestGetOrCreateLineageGuard(LineageGuardTestBase):
    def test_creates_guard_with_revision_one(self):
        fields = self._guard_fields()
        guard = get_or_create_lineage_guard(
            self.session, self._ctx(),
            winner_decision_id="wdec_1", winner_confirmation_id="wconf_1",
            scenario_scope_key="scn_1", **fields,
        )
        self.assertEqual(guard.revision, 1)
        self.assertEqual(guard.workspace_id, self.ws.id)
        self.assertEqual(guard.meta_mode, "controlled_mock")
        self.assertEqual(guard.winner_decision_id, "wdec_1")
        self.assertEqual(guard.winner_confirmation_id, "wconf_1")
        self.assertEqual(guard.delivery_target_id, fields["delivery_target_id"])
        self.assertEqual(guard.scenario_scope_key, "scn_1")
        self.assertTrue(guard.id.startswith("slg_"))

    def test_second_call_same_pair_returns_same_row_unchanged(self):
        """INSERT ... ON CONFLICT DO NOTHING: a second call for the exact
        same (decision, target) pair returns the first row, not a new one,
        and does not touch its persisted identity fields even when called
        with (deliberately wrong) different upstream ids."""
        fields = self._guard_fields()
        ctx = self._ctx()
        first = get_or_create_lineage_guard(
            self.session, ctx,
            winner_decision_id="wdec_1", winner_confirmation_id="wconf_1",
            scenario_scope_key="scn_1", **fields,
        )
        self.session.flush()

        other_fields = dict(fields)
        other_fields["asset_version_id"] = self._make_asset_version()
        second = get_or_create_lineage_guard(
            self.session, ctx,
            winner_decision_id="wdec_1", winner_confirmation_id="wconf_1",
            scenario_scope_key="scn_1", **other_fields,
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.asset_version_id, fields["asset_version_id"])
        self.assertNotEqual(second.asset_version_id, other_fields["asset_version_id"])
        self.assertEqual(second.revision, 1)

    def test_distinct_delivery_target_creates_distinct_guard(self):
        """Same Winner decision, a different DeliveryTarget -> a distinct
        guard row (the unique identity is the *pair*, not either id alone)."""
        upstream = self._make_curated_scenario_context("controlled_mock")
        fields_a = self._guard_fields(scenario_scope_key="scn_a", upstream=upstream)
        fields_b = self._guard_fields(scenario_scope_key="scn_b", upstream=upstream)
        ctx = self._ctx()

        guard_a = get_or_create_lineage_guard(
            self.session, ctx, winner_decision_id="wdec_1",
            winner_confirmation_id="wconf_1", scenario_scope_key="scn_a", **fields_a,
        )
        guard_b = get_or_create_lineage_guard(
            self.session, ctx, winner_decision_id="wdec_1",
            winner_confirmation_id="wconf_1", scenario_scope_key="scn_b", **fields_b,
        )
        self.assertNotEqual(guard_a.id, guard_b.id)
        self.assertNotEqual(guard_a.delivery_target_id, guard_b.delivery_target_id)

    def test_distinct_winner_decision_creates_distinct_guard(self):
        fields = self._guard_fields()
        ctx = self._ctx()
        guard_a = get_or_create_lineage_guard(
            self.session, ctx, winner_decision_id="wdec_1",
            winner_confirmation_id="wconf_1", scenario_scope_key="scn_1", **fields,
        )
        guard_b = get_or_create_lineage_guard(
            self.session, ctx, winner_decision_id="wdec_2",
            winner_confirmation_id="wconf_1", scenario_scope_key="scn_1", **fields,
        )
        self.assertNotEqual(guard_a.id, guard_b.id)
        self.assertEqual(guard_a.delivery_target_id, guard_b.delivery_target_id)

    def test_scoped_to_meta_mode(self):
        """A guard created under controlled_mock is invisible (a distinct
        row) under a different meta_mode context in the same workspace."""
        controlled_fields = self._guard_fields(meta_mode="controlled_mock", scenario_scope_key="scn_1")
        sandbox_fields = self._guard_fields(meta_mode="customer_sandbox", scenario_scope_key="__no_scenario__")

        controlled = get_or_create_lineage_guard(
            self.session, self._ctx("controlled_mock"),
            winner_decision_id="wdec_1", winner_confirmation_id="wconf_1",
            scenario_scope_key="scn_1", **controlled_fields,
        )
        sandbox = get_or_create_lineage_guard(
            self.session, self._ctx("customer_sandbox"),
            winner_decision_id="wdec_1", winner_confirmation_id="wconf_1",
            **sandbox_fields,
        )
        self.assertNotEqual(controlled.id, sandbox.id)
        self.assertEqual(controlled.meta_mode, "controlled_mock")
        self.assertEqual(sandbox.meta_mode, "customer_sandbox")
        self.assertEqual(sandbox.scenario_scope_key, "__no_scenario__")

    def test_customer_sandbox_defaults_scenario_scope_key(self):
        fields = self._guard_fields(meta_mode="customer_sandbox", scenario_scope_key="__no_scenario__")
        guard = get_or_create_lineage_guard(
            self.session, self._ctx("customer_sandbox"),
            winner_decision_id="wdec_1", winner_confirmation_id="wconf_1", **fields,
        )
        self.assertEqual(guard.scenario_scope_key, "__no_scenario__")

    def test_controlled_mock_requires_explicit_scenario_scope_key(self):
        fields = self._guard_fields()
        with self.assertRaises(ValueError):
            get_or_create_lineage_guard(
                self.session, self._ctx(),
                winner_decision_id="wdec_1", winner_confirmation_id="wconf_1", **fields,
            )

    def test_returned_guard_is_row_locked_for_update(self):
        """with_for_update() is a documented no-op on SQLite (spec §7.2's
        same fallback note); this proves the call succeeds and returns a
        live, attached ORM row rather than asserting lock semantics SQLite
        cannot provide."""
        fields = self._guard_fields()
        guard = get_or_create_lineage_guard(
            self.session, self._ctx(),
            winner_decision_id="wdec_1", winner_confirmation_id="wconf_1",
            scenario_scope_key="scn_1", **fields,
        )
        reloaded = self.session.get(ScaleLineageGuardRecord, guard.id)
        self.assertIsNotNone(reloaded)
        self.assertIs(reloaded, guard)


class TestScaleLineageGuardRecordShape(LineageGuardTestBase):
    def test_table_name(self):
        self.assertEqual(ScaleLineageGuardRecord.__tablename__, "scale_lineage_guards")

    def test_id_prefix(self):
        fields = self._guard_fields()
        guard = get_or_create_lineage_guard(
            self.session, self._ctx(),
            winner_decision_id="wdec_1", winner_confirmation_id="wconf_1",
            scenario_scope_key="scn_1", **fields,
        )
        self.assertTrue(guard.id.startswith("slg_"))

    def test_database_rejects_unknown_asset_version(self):
        """Proves the asset_version_id FK is enforced at the database
        layer, direct ORM construction bypassing get_or_create_lineage_guard()."""
        fields = self._guard_fields()
        bad = ScaleLineageGuardRecord(
            id=new_id("slg"), workspace_id=self.ws.id, meta_mode="controlled_mock",
            scenario_scope_key="scn_1", winner_decision_id="wdec_1",
            winner_confirmation_id="wconf_1", asset_version_id="astv_missing",
            product_version_id=fields["product_version_id"],
            offer_version_id=fields["offer_version_id"],
            market_asset_binding_id=fields["market_asset_binding_id"],
            delivery_target_id=fields["delivery_target_id"],
            meta_connection_id=fields["meta_connection_id"],
            contract_generation_id=fields["contract_generation_id"],
        )
        self.session.add(bad)
        with self.assertRaises(IntegrityError):
            self.session.flush()
        self.session.rollback()

    def test_database_rejects_scenario_biconditional_violation(self):
        fields = self._guard_fields()
        bad = ScaleLineageGuardRecord(
            id=new_id("slg"), workspace_id=self.ws.id, meta_mode="controlled_mock",
            scenario_scope_key="__no_scenario__",  # violates the biconditional
            winner_decision_id="wdec_1", winner_confirmation_id="wconf_1",
            asset_version_id=fields["asset_version_id"],
            product_version_id=fields["product_version_id"],
            offer_version_id=fields["offer_version_id"],
            market_asset_binding_id=fields["market_asset_binding_id"],
            delivery_target_id=fields["delivery_target_id"],
            meta_connection_id=fields["meta_connection_id"],
            contract_generation_id=fields["contract_generation_id"],
        )
        self.session.add(bad)
        with self.assertRaises(IntegrityError):
            self.session.flush()
        self.session.rollback()

    def test_database_rejects_duplicate_winner_target_pair(self):
        fields = self._guard_fields()
        ctx = self._ctx()
        get_or_create_lineage_guard(
            self.session, ctx, winner_decision_id="wdec_1",
            winner_confirmation_id="wconf_1", scenario_scope_key="scn_1", **fields,
        )
        self.session.flush()

        duplicate = ScaleLineageGuardRecord(
            id=new_id("slg"), workspace_id=self.ws.id, meta_mode="controlled_mock",
            scenario_scope_key="scn_1", winner_decision_id="wdec_1",
            winner_confirmation_id="wconf_1", asset_version_id=fields["asset_version_id"],
            product_version_id=fields["product_version_id"],
            offer_version_id=fields["offer_version_id"],
            market_asset_binding_id=fields["market_asset_binding_id"],
            delivery_target_id=fields["delivery_target_id"],
            meta_connection_id=fields["meta_connection_id"],
            contract_generation_id=fields["contract_generation_id"],
        )
        self.session.add(duplicate)
        with self.assertRaises(IntegrityError):
            self.session.flush()
        self.session.rollback()
