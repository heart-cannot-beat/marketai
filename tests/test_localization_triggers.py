"""PostgreSQL evidence: immutability triggers on Localization tables (#24,
task 19, spec §3I).

``migrations/versions/u1a2b3c4d5e6_localization_truth_bounded_qc.py``
(``_step8_pg_enforcement`` / ``_create_append_only_trigger``) installs a
``BEFORE UPDATE OR DELETE`` trigger, reusing the shared
``marketai_reject_update_or_delete()`` function first defined in
``migrations/versions/t1a2b3c4d5e6_immutable_contextual_decisions.py``, on
every table named in that migration's ``APPEND_ONLY_TABLES`` -- which
includes ``truth_facts``, ``truth_source_attempts`` and
``localized_asset_lineages``. Every test below exercises that same
trigger, which fires regardless of role (including
``marketai_domain_owner``, which is otherwise exempt from RLS but not
from a trigger): ``RAISE EXCEPTION 'UPDATE denied on append-only table
%'`` / ``'DELETE denied on append-only table %'``.

``market_policy_versions`` has *no* such trigger and no CHECK constraint
preventing a lower ``version_number``: ``_apply_grants`` in that same
migration grants the runtime roles only ``SELECT``/``INSERT`` on it (no
``UPDATE`` at all), so forward-only versioning is enforced today purely
by *never granting UPDATE* + always inserting a new row with a higher
``version_number`` -- an application-level convention this migration
does not additionally enforce with a monotonic-version trigger or CHECK.
``test_market_policy_version_forward_only`` documents that gap explicitly
via ``skipTest`` rather than asserting behaviour the schema does not
provide (see the task-19 brief: "if no triggers are defined there ...
write the tests as documenting what SHOULD exist, with clear skip
messages explaining the gap").

Requires a PostgreSQL cluster provisioned with
``deploy/postgres/control-plane-domain-roles.sql`` and
``deploy/postgres/domain-tenant-roles.sql``, migrated to head -- same
requirement, same connection/signing pattern, as
``tests/test_localization_rls.py``. Skips entirely when the connection
strings are absent so ``make verify`` still runs on a clean checkout; PG
CI supplies them.
"""
from __future__ import annotations

import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone

OWNER_URL = os.environ.get("DOMAIN_OWNER_DATABASE_URL")
API_URL = os.environ.get("DOMAIN_API_DATABASE_URL")
CONTEXT_SECRET = os.environ.get("MARKETAI_TENANT_CONTEXT_SECRET")

CONFIGURED = all((OWNER_URL, API_URL, CONTEXT_SECRET))

SKIP_REASON = (
    "set DOMAIN_OWNER_DATABASE_URL, DOMAIN_API_DATABASE_URL and "
    "MARKETAI_TENANT_CONTEXT_SECRET against a cluster provisioned with "
    "deploy/postgres/domain-tenant-roles.sql and migrated to head "
    "(matches tests/test_postgres_rls.py's requirements)"
)


@unittest.skipUnless(CONFIGURED, SKIP_REASON)
class TestImmutableTriggers(unittest.TestCase):
    """Append-only Localization tables reject UPDATE/DELETE at the
    database layer, not only in application code -- proven here as the
    owner role, which is otherwise exempt from RLS (``BYPASSRLS``) but is
    not exempt from a ``BEFORE UPDATE OR DELETE`` trigger."""

    @classmethod
    def setUpClass(cls) -> None:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        from app.adapters.persistence.models import (
            LibraryEntryRecord,
            LibraryVersionRecord,
            MarketPolicyRecord,
            MarketPolicyVersionRecord,
            TruthFactRecord,
            TruthPackageMarketRecord,
            TruthPackageRecord,
            TruthSourceAttemptRecord,
            WorkspaceRecord,
            new_id,
        )

        cls._create_engine = staticmethod(create_engine)
        cls.owner = create_engine(OWNER_URL, future=True)
        cls.ws = f"ws_loc_trig_{uuid.uuid4().hex[:8]}"

        now = datetime.now(timezone.utc)

        with Session(cls.owner, future=True) as s:
            s.begin()
            s.add(WorkspaceRecord(id=cls.ws, name=cls.ws))
            s.flush()

            versions = []
            for kind in ("product", "offer", "truth_resolution_policy", "data_source"):
                entry_id = new_id("le")
                s.add(LibraryEntryRecord(
                    id=entry_id, workspace_id=cls.ws, entry_type=kind,
                    display_name=f"{kind}-{cls.ws}", lifecycle="active",
                ))
                s.flush()
                version_id = new_id("lv")
                s.add(LibraryVersionRecord(
                    id=version_id, workspace_id=cls.ws, entry_id=entry_id, entry_type=kind,
                    version_no=1, content_json={}, content_hash=f"hash-{version_id}",
                    state="effective", published_by="trigger-test",
                ))
                s.flush()
                versions.append(version_id)
            pv_id, ov_id, policy_id, data_source_id = versions

            cls.truth_package_id = new_id("tp")
            s.add(TruthPackageRecord(
                id=cls.truth_package_id, workspace_id=cls.ws,
                product_version_id=pv_id, offer_version_id=ov_id,
                truth_resolution_policy_version_id=policy_id, truth_data_mode="real",
                canonical_hash=f"hash-{cls.truth_package_id}".ljust(64, "0")[:64],
                status="full", valid_until=now + timedelta(days=1), created_at=now,
            ))
            s.flush()

            market_policy = MarketPolicyRecord(
                id=new_id("mp"), market_id=f"TR{uuid.uuid4().hex[:8]}", locale="en-TR",
                country="TR", currency="USD",
            )
            s.add(market_policy)
            s.flush()
            mpv = MarketPolicyVersionRecord(
                id=new_id("mpv"), market_policy_id=market_policy.id, version_number=1,
                version_state="effective", locale="en-TR", country="TR", currency="USD",
                pricing_context_json={}, digit_format_json={}, base_tone="neutral",
                reserved_words_json=[], restricted_claim_types_json=[],
                requires_local_copy=False, shipping_address_granularity="country",
                max_fact_staleness_seconds=86400, qc_rules_json={},
            )
            s.add(mpv)
            s.flush()
            cls.market_policy_version_id = mpv.id

            cls.truth_package_market_id = new_id("tpm")
            s.add(TruthPackageMarketRecord(
                id=cls.truth_package_market_id, workspace_id=cls.ws,
                truth_package_id=cls.truth_package_id, market_policy_version_id=mpv.id,
                market="US", readiness="ready", readiness_detail_json={}, created_at=now,
            ))
            s.flush()

            cls.attempt_id = new_id("tsa")
            s.add(TruthSourceAttemptRecord(
                id=cls.attempt_id, workspace_id=cls.ws, truth_package_id=cls.truth_package_id,
                truth_package_market_id=cls.truth_package_market_id, fact_type="price",
                scope_key="k1", priority_ordinal=1, data_source_version_id=data_source_id,
                adapter_kind="api", adapter_version="v1", resolution_rule_version="truth-rule-v1",
                truth_data_mode="real", requested_context_json={}, outcome="accepted",
                evidence_hash="e" * 64, started_at=now, completed_at=now, created_at=now,
            ))
            s.flush()

            cls.fact_id = new_id("tf")
            s.add(TruthFactRecord(
                id=cls.fact_id, workspace_id=cls.ws, truth_package_id=cls.truth_package_id,
                truth_package_market_id=cls.truth_package_market_id, fact_type="price",
                scope_key="k1", accepted_source_attempt_id=cls.attempt_id,
                value_json={"amount": "19.99"}, evidence_hash="e" * 64,
                external_object_ref="ref", requested_context_json={}, observed_at=now,
                valid_until=now + timedelta(days=1), created_at=now,
            ))
            s.flush()
            s.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        from sqlalchemy import text

        with cls.owner.begin() as conn:
            for table in (
                "truth_facts", "truth_source_attempts", "truth_package_markets",
                "truth_packages", "library_versions", "library_entries",
            ):
                trigger = f"trg_{table}_append_only"
                conn.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}"))
                conn.execute(text(f"DELETE FROM {table} WHERE workspace_id = :w"), {"w": cls.ws})
                conn.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}"))
            conn.execute(text("DELETE FROM market_policy_versions WHERE id = :id"), {"id": cls.market_policy_version_id})
            conn.execute(text("DELETE FROM market_policies WHERE id IN (SELECT market_policy_id FROM market_policy_versions WHERE id = :id)"), {"id": cls.market_policy_version_id})
            conn.execute(text("DELETE FROM workspaces WHERE id = :w"), {"w": cls.ws})
        cls.owner.dispose()

    # -- connection helper ---------------------------------------------

    def owner_connection(self):
        conn = self.owner.connect()
        self.addCleanup(conn.close)
        transaction = conn.begin()
        self.addCleanup(lambda: transaction.rollback() if transaction.is_active else None)
        return conn

    # ------------------------------------------------------------- tests --

    def test_truth_fact_update_rejected(self) -> None:
        from sqlalchemy import text
        from sqlalchemy.exc import DBAPIError

        conn = self.owner_connection()
        with self.assertRaises(DBAPIError) as ctx_mgr:
            conn.execute(
                text("UPDATE truth_facts SET evidence_hash = :h WHERE id = :id"),
                {"h": "f" * 64, "id": self.fact_id},
            )
        self.assertIn("denied", str(ctx_mgr.exception).lower())
        self.assertIn("truth_facts", str(ctx_mgr.exception).lower())

    def test_source_attempt_update_rejected(self) -> None:
        """`truth_source_attempts` -- one immutable attempt at resolving
        one Fact from one market Source (spec §1.7) -- is append-only for
        the same reason `truth_facts` is: an accepted/failed/skipped
        attempt is evidence, and evidence is never rewritten."""
        from sqlalchemy import text
        from sqlalchemy.exc import DBAPIError

        conn = self.owner_connection()
        with self.assertRaises(DBAPIError) as ctx_mgr:
            conn.execute(
                text("UPDATE truth_source_attempts SET outcome = 'failed', "
                     "failure_code = 'rewritten', evidence_hash = NULL WHERE id = :id"),
                {"id": self.attempt_id},
            )
        self.assertIn("denied", str(ctx_mgr.exception).lower())
        self.assertIn("truth_source_attempts", str(ctx_mgr.exception).lower())

    def test_lineage_delete_rejected(self) -> None:
        """`localized_asset_lineages` carries the full provenance chain
        for one localized AssetVersion (spec §2I/§16.11) and is the
        heaviest-FK table this migration defines -- every column it
        depends on only exists once a Batch has been driven all the way
        through a real Attempt -> QC -> finalize -> publish. Building that
        by hand-inserting rows would bypass the very composite FKs the
        migration relies on to make the row valid in the first place, so
        this one test instead drives the real `app.localization.batch`/
        `worker`/`binding` pipeline -- the exact fixture already proven
        correct (against SQLite) by `tests/test_localization_cas.py`'s
        `_LocalizationFixtureBase._run_to_finalized` -- against whatever
        database `app.adapters.persistence.database.engine` is configured
        for. That must be the *same* PostgreSQL cluster named by
        `DOMAIN_OWNER_DATABASE_URL`/`DOMAIN_API_DATABASE_URL` for this test
        to mean anything (a plain SQLite `DATABASE_URL`, this class's
        normal default, cannot produce a row this dedicated owner
        connection would even see); the guard below skips cleanly and
        explains why whenever that isn't the case, rather than asserting
        something a mismatched target database can't actually prove."""
        from app.adapters.persistence.database import engine as app_engine

        if app_engine.dialect.name != "postgresql":
            self.skipTest(
                "app.adapters.persistence.database.engine (DATABASE_URL) "
                "must also point at the same PostgreSQL cluster named by "
                "DOMAIN_OWNER_DATABASE_URL -- this test drives the real "
                "Batch/Attempt/QC/finalize/publish pipeline through that "
                "engine to produce a genuine localized_asset_lineages row, "
                "then attempts the DELETE through a dedicated owner "
                "connection to prove the append-only trigger, not just "
                "grants, is what rejects it"
            )

        import asyncio

        from sqlalchemy import select, text
        from sqlalchemy.exc import DBAPIError

        from app.adapters.persistence.database import tenant_session
        from app.adapters.persistence.models import LocalizedAssetLineageRecord
        from app.platform.tenancy import TenantContext

        # Reuses the exact fixture builders `tests/test_localization_cas.py`
        # already exercises against SQLite -- they take Workspace/context
        # as plain parameters, not module globals, so they compose here
        # with this test's own Workspace unchanged.
        from tests import test_localization_cas as fx

        ws_id = f"ws_loc_trig_lineage_{uuid.uuid4().hex[:8]}"
        ctx = TenantContext(workspace_id=ws_id, role="owner", initiator_id="user_1", initiator_role="owner")
        now = datetime.now(timezone.utc)

        with tenant_session(ctx) as s:
            fx._ensure_workspace(s, ws_id)
            s.merge(fx.EntitlementProjectionRecord(
                workspace_id=ws_id, entitlement_version=1, lease_sequence=1,
                projection_version=1, schema_version=2, semantic_hash="h" * 8,
                valid_until=now + timedelta(days=30), issued_at=now - timedelta(days=1),
                period_start=now - timedelta(days=1), period_end=now + timedelta(days=29),
                resolved_entitlements_json={"storage.bytes.max": 10 * 1024 ** 3},
            ))
            s.add(fx.SpendGuardrailRecord(
                workspace_id=ws_id, revision=1, budget_currency="USD",
                per_operation_limit=fx.Decimal("100.00"), rolling_24h_limit=fx.Decimal("500.00"),
                period_limit=fx.Decimal("5000.00"), outstanding_exposure_limit=fx.Decimal("1000.00"),
                created_at=now, updated_at=now, updated_by="user_1",
            ))

        admission = fx.PaidOperationAdmission(
            policy=fx.EntitlementPolicy(),
            quote_engine=fx.CostQuoteEngine(price_registry=fx.PRICE_REGISTRY, price_rule_version="test-v1"),
            reservation_book=fx.ReservationBook(),
        )
        coordinator = fx.ExternalCallCoordinator(session_factory=tenant_session)

        winner = fx._build_confirmed_winner(ws_id, ctx, "trigtest")
        fx._seed_review(ctx, winner["asset_version_id"])
        with tenant_session(ctx) as s:
            mpv_id = fx._make_market_policy_version(s, f"TRG{uuid.uuid4().hex[:6]}"[:10]).id
        with tenant_session(ctx) as s:
            row = s.get(fx.MarketPolicyVersionRecord, mpv_id)
            market = s.get(fx.MarketPolicyRecord, row.market_policy_id).market_id

        tag = "trigtest"
        # `_truth_command_for` reads/writes through `tenant_session(CTX)`
        # (the module's own fixed Workspace) via closures over `fx.WS_ID`/
        # `fx.CTX` -- not this test's `ws_id`/`ctx` -- for the LibraryVersion
        # rows it seeds. Point those module globals at this test's own
        # Workspace/context for the remainder of this fixture build, and
        # restore them on exit so `test_localization_cas.py`'s own suite
        # (which may run in the same process) is unaffected.
        original_ws_id, original_ctx = fx.WS_ID, fx.CTX
        fx.WS_ID, fx.CTX = ws_id, ctx
        try:
            truth_cmd, _dep_ids = fx._LocalizationFixtureBase._truth_command_for(
                self, tag, winner["product_version_id"], winner["offer_version_id"], [(mpv_id, market)],
            )

            with tenant_session(ctx) as s:
                draft = fx.create_draft(
                    s, ctx, product_version_id=winner["product_version_id"],
                    offer_version_id=winner["offer_version_id"], source_market_snapshot={"market": "US", "placement": "feed", "locale": "en-US", "country": "US", "currency": "USD"},
                    truth_data_mode="real", provider_execution_mode="mock",
                    sources=[fx.BatchSourceInput(
                        asset_version_id=winner["asset_version_id"], decision_id=winner["decision_id"],
                        source_market_snapshot={"market": "US", "placement": "feed", "locale": "en-US", "country": "US", "currency": "USD"},
                    )],
                    market_targets=[fx.BatchMarketTargetInput(market_policy_version_id=mpv_id, market=market)],
                    actor_id="user_1",
                )
                batch_id = draft.batch_id

            recipe_hash = f"recipe-{tag}"
            with tenant_session(ctx) as s:
                fx.create_inventory_from_analysis(
                    s, ctx, asset_version_id=winner["asset_version_id"], analysis_recipe_hash=recipe_hash,
                    claims=[{
                        "claim_type": "headline", "scope_key": f"k1_{tag}", "ocr_text": "Hello world",
                        "confidence": 0.99, "risk": "low", "conflicting": False,
                    }],
                    provider="mock-vision", model="mock-vision-1", rule_version="claims-rule-v1",
                    analysis_execution_mode="mock", adapter_version="mock-v1",
                    mock_scenario="default", mock_version="v1",
                )
            with tenant_session(ctx) as s:
                fx.start_preparation(
                    s, ctx, batch_id=batch_id, truth_command=truth_cmd, analysis_recipe_hash=recipe_hash,
                    analysis_provider="mock-vision", analysis_model="mock-vision-1",
                    analysis_adapter_version="mock-v1", actor_id="user_1", admission=admission,
                )
            with tenant_session(ctx) as s:
                scd = fx.submit_claim_decisions(
                    s, ctx, batch_id=batch_id,
                    expected_revision=s.get(
                        fx.LocalizationBatchRecord, batch_id
                    ).revision,
                    asset_version_id=winner["asset_version_id"],
                    target_market=market, analysis_recipe_hash=recipe_hash,
                    decisions=[fx.ClaimDecisionInput(claim_index=0, action="preserve")],
                    copy_policy_snapshot={}, source_locale="en-US", target_locale="en-US",
                    translation_recipe=None, requested_evidence_tier=fx.EvidenceTier.MOCK,
                    actor_id="user_1", role="owner", admission=admission,
                )
                copy_version_id = scd.copy_version_id
            with tenant_session(ctx) as s:
                fx.approve_copy(
                    s, ctx, batch_id=batch_id,
                    expected_revision=s.get(
                        fx.LocalizationBatchRecord, batch_id
                    ).revision,
                    copy_version_id=copy_version_id,
                    actor_id="user_1", role="owner",
                )
            with tenant_session(ctx) as s:
                expected_revision = s.get(fx.LocalizationBatchRecord, batch_id).revision
            with tenant_session(ctx) as s:
                confirmed = fx.confirm(
                    s, ctx, batch_id=batch_id, expected_revision=expected_revision,
                    edit_provider="mock-edit", edit_model="mock-edit-1",
                    edit_adapter_version="mock-edit-v1", actor_id="user_1", role="owner",
                    admission=admission,
                )
            item_id = confirmed.item_ids[0]

            asyncio.run(fx.worker.handle_edit_attempt(
                ctx, batch_id=batch_id, item_id=item_id, attempt_number=1,
                session_factory=tenant_session, coordinator=coordinator,
            ))
            with tenant_session(ctx) as s:
                attempt_id = s.scalar(
                    select(fx.LocalizationAttemptRecord.id).where(
                        fx.LocalizationAttemptRecord.workspace_id == ws_id,
                        fx.LocalizationAttemptRecord.item_id == item_id,
                        fx.LocalizationAttemptRecord.attempt_number == 1,
                    )
                )
            with tenant_session(ctx) as s:
                fx.worker.handle_run_deterministic_qc(
                    s, ctx, batch_id=batch_id, item_id=item_id, attempt_id=attempt_id,
                )
            with tenant_session(ctx) as s:
                fx.worker.handle_finalize_item(
                    s, ctx, batch_id=batch_id, item_id=item_id, attempt_id=attempt_id,
                )

            with tenant_session(ctx) as s:
                lineage_id = s.scalar(
                    select(LocalizedAssetLineageRecord.id).where(
                        LocalizedAssetLineageRecord.workspace_id == ws_id,
                        LocalizedAssetLineageRecord.item_id == item_id,
                    )
                )
            self.assertIsNotNone(lineage_id, "finalize did not produce a lineage row")
        finally:
            fx.WS_ID, fx.CTX = original_ws_id, original_ctx

        self.addCleanup(self._delete_lineage_fixture_workspace, ws_id)

        conn = self.owner_connection()
        with self.assertRaises(DBAPIError) as ctx_mgr:
            conn.execute(
                text("DELETE FROM localized_asset_lineages WHERE id = :id"), {"id": lineage_id},
            )
        self.assertIn("denied", str(ctx_mgr.exception).lower())
        self.assertIn("localized_asset_lineages", str(ctx_mgr.exception).lower())

    def _delete_lineage_fixture_workspace(self, ws_id: str) -> None:
        """`workspaces.id ON DELETE CASCADE` would otherwise be blocked by
        the very append-only triggers this test proves exist."""
        from sqlalchemy import text

        with self.owner.begin() as conn:
            for table in (
                "localized_asset_lineages", "market_asset_binding_events",
                "market_asset_bindings", "localization_output_publications",
                "localization_qc_records", "localization_attempt_outputs",
                "localization_attempts", "localization_semantic_claims",
                "localization_item_provider_calls", "localization_items",
                "localized_copy_approvals", "localized_copy_versions",
                "localization_claim_decisions", "localization_claim_decision_sets",
                "source_claim_confirmations", "source_claim_inventories",
                "localization_batch_market_targets", "localization_batch_commands",
                "localization_batch_sources", "localization_batches",
                "truth_facts", "truth_source_attempts", "truth_package_markets",
                "truth_packages", "library_versions", "library_entries",
            ):
                trigger = f"trg_{table}_append_only"
                has_trigger = conn.execute(
                    text("SELECT 1 FROM pg_trigger WHERE tgname = :n"), {"n": trigger},
                ).scalar()
                if has_trigger:
                    conn.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}"))
                conn.execute(text(f"DELETE FROM {table} WHERE workspace_id = :w"), {"w": ws_id})
                if has_trigger:
                    conn.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}"))
            conn.execute(text("DELETE FROM workspaces WHERE id = :w"), {"w": ws_id})

    def test_market_policy_version_forward_only(self) -> None:
        """No trigger or CHECK constraint enforces
        ``market_policy_versions.version_number`` monotonicity in
        ``migrations/versions/u1a2b3c4d5e6_localization_truth_bounded_qc.py``:
        ``_apply_grants`` grants the runtime roles ``SELECT``/``INSERT``
        only (no ``UPDATE``) on this table, so in practice a version is
        never rewritten, only superseded by inserting a new row with a
        higher ``version_number`` -- but that "forward-only" property is
        an application convention, not a database-enforced invariant the
        way the append-only tables' triggers are. This test documents that
        gap rather than asserting behaviour the schema does not provide
        (task 19 brief)."""
        self.skipTest(
            "no DB-level trigger or CHECK enforces "
            "market_policy_versions.version_number monotonicity; the "
            "table is INSERT/SELECT-only for the runtime roles "
            "(migrations/versions/u1a2b3c4d5e6_localization_truth_bounded_qc.py "
            "_apply_grants) and forward-only versioning is an application "
            "convention only -- see this module's docstring. If a "
            "monotonic-version trigger is added, replace this skip with a "
            "real assertion that an UPDATE lowering version_number is "
            "rejected."
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
