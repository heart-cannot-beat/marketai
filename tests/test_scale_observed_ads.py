"""The DeliveryTarget entry: pick an already-observed ad (#129).

One confirmed, fully eligible Winner per class (``build_ready_winner``). Each
test gives it a sandbox connection of its own whose scenario holds the ads,
and maps those ads to an ExperimentVersion through the records the product
leaves behind: an Experiment candidate ad binding, a sandbox Launch
candidate or the legacy Meta ad binding. A mapping that should disagree with
the Winner points at a copy of the Winner's ExperimentVersion with one field
changed.

Covers: a matching ad becomes a DeliveryTarget bound to the connection's
Scale contract set (created by the system executor, the picker as
initiator) and passes the lineage gate; a replay is idempotent; each field
that differs from the Winner returns its own reason; creative and provenance
mapping failures; attribution measured by a contract with other windows is
refused; Launch candidates resolve the ad; internal_validation reads the ads
of its published configuration and reports when it has none; over HTTP,
Owners and Members both create, another Workspace's connection, ads and
Winner are neither visible nor usable, and delegated users cannot select
internal_validation. Fictional brand only: Acme Charge.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="marketai-scale-observed-ads-"))
os.environ.setdefault("MARKETAI_TEST_ROOT", str(_TEST_ROOT))
os.environ.setdefault("MARKETAI_DATA_DIR", str(_TEST_ROOT / "data"))
os.environ.setdefault("MARKETAI_DATABASE_URL", f"sqlite:///{_TEST_ROOT / 'marketai.db'}")
os.environ.setdefault("MARKETAI_WORKSPACE_ID", "ws_test")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select, update  # noqa: E402

from app import config  # noqa: E402
from app.adapters.persistence.database import platform_session, tenant_session  # noqa: E402
from app.adapters.persistence.models import (  # noqa: E402
    AuditEventRecord,
    ExperimentDecisionRecord,
    ExperimentVersionCandidateRecord,
    ExperimentVersionRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    MetaAccountRecord,
    MetaAdBindingRecord,
    OperationRecord,
    WorkspaceRecord,
    new_id,
)
from app.entitlements.credentials import CredentialVersionRecord  # noqa: E402
from app.launch.models import (  # noqa: E402
    LaunchCandidateRecord,
    LaunchProvisioningRecord,
    LaunchSagaStepAttemptRecord,
    MetaLaunchConfirmationRecord,
    MetaLaunchPreviewRecord,
)
from app.meta.connections import create_binding  # noqa: E402
from app.meta.credential_resolution import create_call_intent  # noqa: E402
from app.meta.evidence import create_attestation, publish_evidence  # noqa: E402
from app.meta.models import (  # noqa: E402
    FakeMetaObjectRecord,
    MetaCallOutcomeRecord,
    MetaConnectionRecord,
    MetaDataSourceBindingRecord,
    MetaObservationSyncScheduleRecord,
    ObservationContractGenerationRecord,
    ScenarioVersionRecord,
)
from app.meta.sync_pipeline import complete_sync_run, create_sync_run, record_page  # noqa: E402
from app.platform.canonical import canonical_hash  # noqa: E402
from app.platform.http_identity import request_tenant_context  # noqa: E402
from app.platform.tenancy import TenantContext  # noqa: E402
from app.scale.command_journal import IdempotencyConflictError  # noqa: E402
from app.scale.delivery_targets import ensure_metric_contract_set  # noqa: E402
from app.scale.models import (  # noqa: E402
    ScaleDeliveryTargetRecord,
    ScaleLineageGuardRecord,
    ScaleMetricSemanticContractSetMemberRecord,
)
from app.scale.observed_ads import (  # noqa: E402
    CONTRACT_SET_EXECUTOR,
    DELIVERY_TARGET_TTL,
    DeliveryTargetEntryError,
    create_delivery_target_from_ad,
    list_observed_ads,
)
from tests.support.scale_candidate_fixtures import (  # noqa: E402
    build_ready_winner,
    ensure_governance_prereqs,
)
from tests.support.scale_observed_ad_fixtures import (  # noqa: E402
    bind_ad_to_winner_copy,
    clone_record,
    copy_winner_version,
)

SANDBOX = "customer_sandbox"
INTERNAL = "internal_validation"
OTHER_LP_HASH = hashlib.sha256(b"https://acme-charge.example/other-landing").hexdigest()


class ObservedAdTestBase(unittest.TestCase):
    """One migrated database, one Workspace and one ready Winner per class."""

    workspace_seq = 0

    @classmethod
    def setUpClass(cls) -> None:
        url = config.DATABASE_URL
        if url.startswith("sqlite:///"):
            Path(url.replace("sqlite:///", "", 1)).parent.mkdir(parents=True, exist_ok=True)
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")

        ObservedAdTestBase.workspace_seq += 1
        cls.tag = f"obsad{ObservedAdTestBase.workspace_seq}{new_id('t')[-6:]}"
        cls.workspace_id = f"ws_{cls.tag}"
        with platform_session() as session:
            session.add(WorkspaceRecord(id=cls.workspace_id, name=cls.workspace_id))
        cls.owner = TenantContext(
            workspace_id=cls.workspace_id, role="owner", initiator_id="user_owner",
            request_id="req-observed-ads", trace_id="trace-observed-ads",
        )
        cls.member = TenantContext(
            workspace_id=cls.workspace_id, role="member", initiator_id="user_member",
        )
        product_id = f"prod_{cls.tag}"
        ensure_governance_prereqs(cls.owner, product_id)
        cls.chain = build_ready_winner(cls.owner, product_id, cls.tag)
        with tenant_session(cls.owner) as session:
            decision = session.get(ExperimentDecisionRecord, cls.chain["decision_id"])
            cls.version_id = decision.experiment_version_id
            cls.version_candidate_id = decision.experiment_version_candidate_id
            candidate = session.get(ExperimentVersionCandidateRecord, cls.version_candidate_id)
            cls.other_asset_version_id = session.scalar(
                select(ExperimentVersionCandidateRecord.asset_version_id).where(
                    ExperimentVersionCandidateRecord.experiment_version_id == cls.version_id,
                    ExperimentVersionCandidateRecord.id != candidate.id,
                )
            )
        cls.counter = 0

    # -- fixtures --------------------------------------------------------

    def _ctx(self, base: TenantContext | None = None, mode: str = SANDBOX) -> TenantContext:
        return replace(base or self.owner, meta_mode=mode)

    def _next(self) -> int:
        type(self).counter += 1
        return type(self).counter

    def _connection(self, mode: str = SANDBOX) -> dict:
        n = self._next()
        ctx = self._ctx(mode=mode)
        with tenant_session(ctx) as session:
            # A Workspace has one active connection per mode: retire the last test's.
            for previous in session.scalars(select(MetaConnectionRecord).where(
                    MetaConnectionRecord.workspace_id == self.workspace_id,
                    MetaConnectionRecord.meta_mode == mode, MetaConnectionRecord.state == "active")):
                self._retire(previous)
            session.flush()
            connection = MetaConnectionRecord(
                id=f"mcon_{self.tag}_{n}", workspace_id=self.workspace_id, meta_mode=mode,
                ad_account_id=f"act_{self.tag}_{n}", state="active",
            )
            session.add(connection)
            session.flush()
            scenario_id = None
            if mode != INTERNAL:
                scenario = ScenarioVersionRecord(
                    id=f"scen_{self.tag}_{n}", workspace_id=self.workspace_id, meta_mode=mode,
                    meta_connection_id=connection.id, scenario_config={},
                    scenario_hash=canonical_hash({}), is_default=True,
                )
                session.add(scenario)
                session.flush()
                scenario_id = scenario.id
            credentials = self._credentials(session) if mode == INTERNAL else {}
            binding = create_binding(
                session, context=ctx, connection_id=connection.id, ad_account_id=connection.ad_account_id,
                **credentials,
            )
            result = {"id": connection.id, "ad_account_id": connection.ad_account_id,
                      "scenario_version_id": scenario_id, "binding_id": binding.id, "mode": mode}
        self.addCleanup(self._quiesce)
        return result

    def _credentials(self, session) -> dict[str, str]:
        """The Workspace's two Meta credential versions an internal_validation
        binding names; one pair per Workspace, made on first use."""
        cls = type(self)
        if getattr(cls, "credentials", None) is None:
            credentials = {}
            for field_name, key in (("access_token_version_id", "meta_access_token"),
                                    ("app_secret_version_id", "meta_app_secret")):
                credential = CredentialVersionRecord(
                    id=new_id("cred"), workspace_id=self.workspace_id, credential_key=key,
                    version_no=1, provider="meta", state="active", encrypted_secret=b"x",
                    encrypted_data_key=b"y", encryption_key_id="k1", nonce=b"n", aad_schema_version=1,
                    created_by="platform:credential-admin", created_at=datetime.now(timezone.utc),
                )
                session.add(credential)
                session.flush()
                credentials[field_name] = credential.id
            cls.credentials = credentials
        return cls.credentials

    def _quiesce(self) -> None:
        """Leave no due observation schedule for a later test's Worker (#124)."""
        with platform_session() as session:
            session.execute(
                update(MetaObservationSyncScheduleRecord)
                .where(MetaObservationSyncScheduleRecord.workspace_id == self.workspace_id,
                       MetaObservationSyncScheduleRecord.state == "active")
                .values(state="paused")
            )

    @staticmethod
    def _retire(connection: MetaConnectionRecord) -> None:
        connection.state = "retired"
        connection.retired_at = datetime.now(timezone.utc)

    def _ad(self, connection: dict, name: str = "ad") -> str:
        ad_id = f"{name}_{self.tag}_{self._next()}"
        with tenant_session(self._ctx(mode=connection["mode"])) as session:
            session.add(FakeMetaObjectRecord(
                id=new_id("fmo"), workspace_id=self.workspace_id, meta_mode=connection["mode"],
                scenario_version_id=connection["scenario_version_id"], object_type="ad",
                remote_object_id=ad_id, state={"adset_id": f"set_{ad_id}", "status": "PAUSED"},
            ))
        return ad_id

    def _version_copy(self, session, *, asset_version_id: str | None = None, **version_fields) -> tuple:
        """A copy of the Winner's ExperimentVersion (and its Experiment, entry
        and candidate) with ``version_fields`` changed."""
        return copy_winner_version(
            session, version_id=self.version_id, version_candidate_id=self.version_candidate_id,
            suffix=f"{self.tag}_{self._next()}", asset_version_id=asset_version_id, **version_fields,
        )

    def _bind(self, connection: dict, ad_id: str, *, version: bool = True,
              asset_version_id: str | None = None, **version_fields) -> None:
        """An Experiment candidate ad binding of ``ad_id`` to a copy of the
        Winner's ExperimentVersion; ``version=False`` binds the entry only."""
        with tenant_session(self.owner) as session:
            bind_ad_to_winner_copy(
                session, workspace_id=self.workspace_id, version_id=self.version_id,
                version_candidate_id=self.version_candidate_id, suffix=f"{self.tag}_{self._next()}",
                ad_account_id=connection["ad_account_id"], ad_id=ad_id, with_version=version,
                asset_version_id=asset_version_id, **version_fields,
            )

    def _launch(self, connection: dict, ad_id: str, **version_fields) -> None:
        """What a sandbox Launch leaves behind: its candidate's applied
        ``create_ad`` step names ``ad_id``."""
        ctx = self._ctx(mode=connection["mode"])
        with tenant_session(self.owner) as session:
            _entry, candidate = self._version_copy(session, **version_fields)
            version_candidate_id, asset_version_id = candidate.id, candidate.asset_version_id
            experiment_id = session.get(ExperimentVersionRecord, candidate.experiment_version_id).experiment_id
            experiment_version_id = candidate.experiment_version_id
        with tenant_session(ctx) as session:
            n = self._next()
            operation = OperationRecord(id=new_id("op"), workspace_id=self.workspace_id,
                                        command_type="launch_provisioning", state="completed")
            session.add(operation)
            session.flush()
            preview = MetaLaunchPreviewRecord(
                id=f"lprev_{self.tag}_{n}", workspace_id=self.workspace_id, meta_mode=connection["mode"],
                meta_connection_id=connection["id"], meta_binding_id=connection["binding_id"],
                experiment_id=experiment_id, experiment_version_id=experiment_version_id,
                candidate_count=1, campaign_config={}, daily_budget_minor=500, currency="USD",
                preview_hash="p" * 64, frozen_snapshot={}, state="confirmed",
                confirmed_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
            session.add(preview)
            session.flush()
            confirmation = MetaLaunchConfirmationRecord(
                id=f"lconf_{self.tag}_{n}", workspace_id=self.workspace_id, meta_mode=connection["mode"],
                preview_id=preview.id, preview_hash="p" * 64, context_hash="c" * 64,
                token_hash=hashlib.sha256(preview.id.encode()).hexdigest(),
                actor_id="user_owner", expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
            session.add(confirmation)
            session.flush()
            provisioning = LaunchProvisioningRecord(
                id=f"lprov_{self.tag}_{n}", workspace_id=self.workspace_id, meta_mode=connection["mode"],
                meta_connection_id=connection["id"], preview_id=preview.id,
                confirmation_id=confirmation.id, operation_id=operation.id,
                campaign_idempotency_key=f"campaign-{n}", provisioning_state="provisioned",
            )
            session.add(provisioning)
            launch_candidate = LaunchCandidateRecord(
                id=f"lcand_{self.tag}_{n}", workspace_id=self.workspace_id, meta_mode=connection["mode"],
                preview_id=preview.id, candidate_index=0,
                experiment_version_candidate_id=version_candidate_id, asset_version_id=asset_version_id,
                creative_config={}, adset_idempotency_key=f"adset-{n}", ad_idempotency_key=f"ad-{n}",
            )
            session.add(launch_candidate)
            session.flush()
            connection_row = session.get(MetaConnectionRecord, connection["id"])
            binding_row = session.get(MetaDataSourceBindingRecord, connection["binding_id"])
            intent = create_call_intent(
                session, workspace_id=self.workspace_id, meta_mode=connection["mode"],
                connection=connection_row, binding=binding_row, operation_id=operation.id,
                meta_api_method="POST", meta_api_endpoint="/ads", request_parameters={"adset": n},
                call_type="saga_step", scenario_version_id=connection["scenario_version_id"],
                executor="system:worker",
            )
            session.add(LaunchSagaStepAttemptRecord(
                id=new_id("lssa"), workspace_id=self.workspace_id, meta_mode=connection["mode"],
                provisioning_id=provisioning.id, intent_id=intent.id, candidate_id=launch_candidate.id,
                step_type="create_ad", attempt_number=1, fencing_token=1, state="applied",
                remote_object_id=ad_id, executor="system:worker",
            ))

    def _pick(self, connection: dict, ad_id: str, *, base: TenantContext | None = None,
              key: str | None = None, decision_id: str | None = None) -> ScaleDeliveryTargetRecord:
        ctx = self._ctx(base, mode=connection["mode"])
        with tenant_session(ctx) as session:
            target = create_delivery_target_from_ad(
                session, ctx, decision_id=decision_id or self.chain["decision_id"],
                connection_id=connection["id"], ad_remote_id=ad_id,
                idempotency_key=key or new_id("ik"),
            )
            session.expunge(target)
            return target

    def _refused(self, connection: dict, ad_id: str, **kwargs) -> str:
        with self.assertRaises(DeliveryTargetEntryError) as caught:
            self._pick(connection, ad_id, **kwargs)
        return caught.exception.code

    def _targets_on(self, connection: dict) -> list[ScaleDeliveryTargetRecord]:
        with tenant_session(self._ctx(mode=connection["mode"])) as session:
            return list(session.scalars(select(ScaleDeliveryTargetRecord).where(
                ScaleDeliveryTargetRecord.meta_connection_id == connection["id"])))


class TestPickingAMatchingAd(ObservedAdTestBase):
    def test_a_matching_ad_becomes_a_target_on_the_connections_contract_set(self):
        connection = self._connection()
        ad_id = self._ad(connection)
        self._bind(connection, ad_id)

        target = self._pick(connection, ad_id)

        self.assertEqual(target.source_type, "curated_scenario")
        self.assertEqual(target.scenario_version_id, connection["scenario_version_id"])
        self.assertEqual(target.ad_remote_id, ad_id)
        self.assertEqual(target.adset_remote_id, f"set_{ad_id}")
        self.assertEqual(target.creative_asset_version_id, self.chain["asset_version_id"])
        self.assertEqual(target.market_code, self.chain["market"])
        self.assertEqual(target.offer_version_id, self.chain["offer_version_id"])
        self.assertEqual(target.landing_page_evidence_hash, self.chain["landing_page_evidence_hash"])
        self.assertEqual(target.attribution_setting, self.chain["attribution_semantics_hash"])
        self.assertAlmostEqual(
            (target.expires_at - target.observed_at).total_seconds(), DELIVERY_TARGET_TTL.total_seconds(),
        )
        with tenant_session(self._ctx()) as session:
            members = {
                row.contract_type: row.observation_contract_generation
                for row in session.scalars(select(ScaleMetricSemanticContractSetMemberRecord).where(
                    ScaleMetricSemanticContractSetMemberRecord.contract_set_id
                    == target.source_manifest["contract_set_id"]))
            }
            self.assertEqual(set(members), {"daily", "exact_window", "delivery_configuration"})
            self.assertEqual(members["delivery_configuration"], target.contract_generation_id)
            guard = session.scalar(select(ScaleLineageGuardRecord).where(
                ScaleLineageGuardRecord.delivery_target_id == target.id))
            self.assertIsNotNone(guard, "the lineage gate ran on the new target")
            events = {row.event_type: row for row in session.scalars(select(AuditEventRecord).where(
                AuditEventRecord.workspace_id == self.workspace_id,
                AuditEventRecord.resource_id.in_([target.id, target.source_manifest["contract_set_id"]])))}
        created = events["scale.delivery_target.created"]
        self.assertEqual((created.executor, created.initiator_id), ("user", "user_owner"))
        contract_set = events["scale.metric_contract_set.created"]
        self.assertEqual((contract_set.executor, contract_set.initiator_id, contract_set.initiator_role),
                         (CONTRACT_SET_EXECUTOR, "user_owner", "owner"))

    def test_a_member_creates_the_contract_set_through_the_system_executor(self):
        connection = self._connection()
        ad_id = self._ad(connection)
        self._bind(connection, ad_id)

        target = self._pick(connection, ad_id, base=self.member)

        with tenant_session(self._ctx()) as session:
            event = session.scalar(select(AuditEventRecord).where(
                AuditEventRecord.event_type == "scale.metric_contract_set.created",
                AuditEventRecord.resource_id == target.source_manifest["contract_set_id"]))
        self.assertEqual((event.executor, event.initiator_id, event.initiator_role),
                         (CONTRACT_SET_EXECUTOR, "user_member", "member"))

    def test_a_replay_returns_the_same_target_and_another_request_conflicts(self):
        connection = self._connection()
        ad_id = self._ad(connection)
        other_ad = self._ad(connection)
        self._bind(connection, ad_id)

        first = self._pick(connection, ad_id, key="ci-only-pick-replay")
        second = self._pick(connection, ad_id, key="ci-only-pick-replay")

        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self._targets_on(connection)), 1)
        with self.assertRaises(IdempotencyConflictError):
            self._pick(connection, other_ad, key="ci-only-pick-replay")

    def test_a_launch_candidate_resolves_the_ad_it_created(self):
        connection = self._connection()
        ad_id = self._ad(connection)
        self._launch(connection, ad_id)

        target = self._pick(connection, ad_id)

        self.assertEqual(target.creative_asset_version_id, self.chain["asset_version_id"])
        self.assertEqual([item["source"] for item in target.source_manifest["creative_bindings"]],
                         ["launch_candidate"])

    def test_the_listing_shows_the_scenario_ads_of_active_connections(self):
        connection = self._connection()
        ad_id = self._ad(connection)
        unmapped = self._ad(connection)
        self._bind(connection, ad_id)

        with tenant_session(self._ctx()) as session:
            listing = list_observed_ads(session, self._ctx(), connection_id=connection["id"])

        self.assertEqual(
            sorted((ad.ad_remote_id, ad.adset_remote_id, ad.source_type, ad.asset_version_ids) for ad in listing.ads),
            sorted([(ad_id, f"set_{ad_id}", "curated_scenario", (self.chain["asset_version_id"],)),
                    (unmapped, f"set_{unmapped}", "curated_scenario", ())]),
        )
        self.assertEqual(listing.unavailable, ())


class TestReasons(ObservedAdTestBase):
    def test_each_field_that_differs_from_the_winner_returns_its_reason(self):
        with tenant_session(self.owner) as session:
            offer = session.get(LibraryVersionRecord, self.chain["offer_version_id"])
            entry = clone_record(session, session.get(LibraryEntryRecord, offer.entry_id), id=f"le_{self.tag}_offer2")
            other_offer = clone_record(session, offer, id=f"lv_{self.tag}_offer2", entry_id=entry.id).id
        cases = {
            "target_creative_mismatch": {"asset_version_id": self.other_asset_version_id},
            "target_market_mismatch": {"market": "ZZ"},
            "target_placement_mismatch": {"placement": "story"},
            "target_offer_mismatch": {"offer_version_id": other_offer},
            "target_landing_page_mismatch": {"landing_page_evidence_hash": OTHER_LP_HASH},
        }
        for reason, fields in cases.items():
            with self.subTest(reason=reason):
                connection = self._connection()
                ad_id = self._ad(connection)
                self._bind(connection, ad_id, **fields)
                self.assertEqual(self._refused(connection, ad_id), reason)
                self.assertEqual(self._targets_on(connection), [])

    def test_attribution_measured_with_other_windows_is_refused(self):
        connection = self._connection()
        ad_id = self._ad(connection)
        self._bind(connection, ad_id)

        with patch("app.meta.scale_read_contracts.ACTION_ATTRIBUTION_WINDOWS", ("1d_click", "1d_view")):
            self.assertEqual(self._refused(connection, ad_id), "target_attribution_mismatch")
        with tenant_session(self._ctx()) as session:
            generations = list(session.scalars(select(ObservationContractGenerationRecord).where(
                ObservationContractGenerationRecord.meta_connection_id == connection["id"])))
        self.assertEqual(generations, [], "the refused pick left no generation behind")

    def test_an_ad_no_record_maps_is_unmapped(self):
        connection = self._connection()
        ad_id = self._ad(connection)
        self.assertEqual(self._refused(connection, ad_id), "creative_unmapped")

    def test_an_ad_mapped_to_two_asset_versions_is_ambiguous(self):
        connection = self._connection()
        ad_id = self._ad(connection)
        self._bind(connection, ad_id)
        with tenant_session(self.owner) as session:
            account = MetaAccountRecord(id=new_id("macct"), workspace_id=self.workspace_id,
                                        ad_account_id=connection["ad_account_id"], api_version="v21.0")
            session.add(account)
            session.flush()
            session.add(MetaAdBindingRecord(id=new_id("mab"), account_id=account.id, ad_id=ad_id,
                                            asset_version_id=self.other_asset_version_id))
        self.assertEqual(self._refused(connection, ad_id), "creative_ambiguous")

    def test_an_ad_with_no_frozen_context_is_unresolved(self):
        connection = self._connection()
        ad_id = self._ad(connection)
        self._bind(connection, ad_id, version=False)
        self.assertEqual(self._refused(connection, ad_id), "provenance_unresolved")

    def test_an_ad_whose_sources_froze_two_contexts_is_ambiguous(self):
        connection = self._connection()
        ad_id = self._ad(connection)
        self._bind(connection, ad_id)
        self._launch(connection, ad_id, market="ZZ")
        self.assertEqual(self._refused(connection, ad_id), "provenance_ambiguous")

    def test_an_ad_the_connection_has_not_observed_is_refused(self):
        connection = self._connection()
        self._ad(connection)
        self.assertEqual(self._refused(connection, "ad_never_observed"), "ad_not_observed")

    def test_a_retired_connection_is_not_found(self):
        connection = self._connection()
        ad_id = self._ad(connection)
        self._bind(connection, ad_id)
        with tenant_session(self._ctx()) as session:
            self._retire(session.get(MetaConnectionRecord, connection["id"]))
        self.assertEqual(self._refused(connection, ad_id), "connection_not_found")


class TestInternalValidation(ObservedAdTestBase):
    def test_a_connection_without_published_configuration_has_observed_nothing(self):
        connection = self._connection(INTERNAL)
        ctx = self._ctx(mode=INTERNAL)
        with tenant_session(ctx) as session:
            listing = list_observed_ads(session, ctx, connection_id=connection["id"])
        self.assertEqual(listing.ads, ())
        self.assertEqual(listing.unavailable, ((connection["id"], "delivery_configuration_not_observed"),))
        self.assertEqual(self._refused(connection, "ad_anything"), "delivery_configuration_not_observed")

    def test_the_ads_of_the_attested_configuration_read_become_evidence_targets(self):
        connection = self._connection(INTERNAL)
        ad_id = f"ad_{self.tag}_real"
        ctx = self._ctx(mode=INTERNAL)
        with tenant_session(ctx) as session:
            contract_set = ensure_metric_contract_set(
                session, ctx, connection_id=connection["id"], currency="USD", timezone_name="UTC",
            )
            generation_id = contract_set.delivery_configuration_generation_id
            operation = OperationRecord(id=new_id("op"), workspace_id=self.workspace_id,
                                        command_type="meta_sync", state="succeeded")
            session.add(operation)
            session.flush()
            run = create_sync_run(session, workspace_id=self.workspace_id, meta_mode=INTERNAL,
                                  contract_generation_id=generation_id, occurrence_id=f"occ_{self.tag}",
                                  operation_id=operation.id, executor="system:worker")
            connection_row = session.get(MetaConnectionRecord, connection["id"])
            intent = create_call_intent(
                session, workspace_id=self.workspace_id, meta_mode=INTERNAL, connection=connection_row,
                binding=session.get(MetaDataSourceBindingRecord, connection["binding_id"]),
                operation_id=operation.id, meta_api_method="GET", meta_api_endpoint="/ads",
                request_parameters={"edge": "ads"}, call_type="page_fetch",
                scenario_version_id=None, executor="system:worker",
            )
            outcome = MetaCallOutcomeRecord(id=new_id("mco"), workspace_id=self.workspace_id,
                                            meta_mode=INTERNAL, intent_id=intent.id, operation_id=operation.id,
                                            network_attempted=True, http_status=200,
                                            response_evidence_hash="r" * 64, executor="system:worker")
            session.add(outcome)
            session.flush()
            body = {"data": [{"id": ad_id, "adset_id": f"set_{ad_id}", "campaign_id": f"cmp_{ad_id}",
                              "creative": {"id": f"cr_{ad_id}"}, "status": "ACTIVE"}]}
            payload = gzip.compress(json.dumps(body).encode())
            record_page(session, sync_run_id=run.id, page_order=1,
                        payload_checksum=hashlib.sha256(payload).hexdigest(), payload_compressed=payload,
                        row_count=1, intent_id=intent.id, outcome_id=outcome.id, is_terminal=True)
            complete_sync_run(session, sync_run_id=run.id, total_pages=1, total_rows=1)
            publication = publish_evidence(session, sync_run_id=run.id, operation_id=operation.id)
            attestation = create_attestation(session, contract_generation_id=generation_id,
                                             publication_id=publication.id,
                                             evidence_watermark="[2026-11-01,2026-11-07]")
            attestation_id, publication_id, run_id = attestation.id, publication.id, run.id
        self._bind(connection, ad_id)

        with tenant_session(ctx) as session:
            listing = list_observed_ads(session, ctx, connection_id=connection["id"])
        self.assertEqual([(ad.ad_remote_id, ad.ad_creative_id, ad.source_attestation_id) for ad in listing.ads],
                         [(ad_id, f"cr_{ad_id}", attestation_id)])

        target = self._pick(connection, ad_id, base=self.member)

        self.assertEqual(target.source_type, "evidence_observation")
        self.assertEqual((target.source_sync_run_id, target.source_publication_id, target.source_attestation_id),
                         (run_id, publication_id, attestation_id))
        self.assertEqual(target.contract_generation_id, generation_id)


class TestHttp(ObservedAdTestBase):
    def setUp(self) -> None:
        from app.main import app

        self.app = app
        self.identity = self.owner
        app.dependency_overrides[request_tenant_context] = lambda: self.identity
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def _post(self, connection_id: str, ad_id: str, *, decision_id: str | None = None,
              mode: str = SANDBOX):
        return self.client.post(
            f"/api/v1/scale/{mode}/delivery-targets",
            json={"winner_decision_id": decision_id or self.chain["decision_id"],
                  "meta_connection_id": connection_id, "ad_remote_id": ad_id},
            headers={"Idempotency-Key": new_id("ci-only-ik")},
        )

    def _other_workspace(self) -> TenantContext:
        other = f"{self.workspace_id}_other{self._next()}"
        with platform_session() as session:
            session.add(WorkspaceRecord(id=other, name=other))
        return TenantContext(workspace_id=other, role="owner", initiator_id="user_other")

    def test_owners_and_members_both_create_a_target(self):
        for identity in (self.owner, self.member):
            with self.subTest(role=identity.role):
                self.identity = identity
                connection = self._connection()
                ad_id = self._ad(connection)
                self._bind(connection, ad_id)
                listed = self.client.get(f"/api/v1/scale/{SANDBOX}/observed-ads",
                                         params={"meta_connection_id": connection["id"]})
                self.assertEqual(listed.status_code, 200, listed.text)
                self.assertEqual([item["ad_remote_id"] for item in listed.json()["items"]], [ad_id])

                created = self._post(connection["id"], ad_id)

                self.assertEqual(created.status_code, 201, created.text)
                body = created.json()
                self.assertEqual(body["ad_remote_id"], ad_id)
                self.assertEqual(body["landing_page_evidence_hash"], self.chain["landing_page_evidence_hash"])

    def test_a_reason_is_a_problem_response(self):
        connection = self._connection()
        ad_id = self._ad(connection)
        self._bind(connection, ad_id, placement="story")

        refused = self._post(connection["id"], ad_id)

        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertTrue(refused.headers["content-type"].startswith("application/problem+json"))
        self.assertEqual(refused.json()["code"], "target_placement_mismatch")

    def test_another_workspaces_connection_and_ads_are_neither_visible_nor_usable(self):
        connection = self._connection()
        ad_id = self._ad(connection)
        self._bind(connection, ad_id)
        self.identity = self._other_workspace()

        listed = self.client.get(f"/api/v1/scale/{SANDBOX}/observed-ads")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertNotIn(ad_id, [item["ad_remote_id"] for item in listed.json()["items"]])
        filtered = self.client.get(f"/api/v1/scale/{SANDBOX}/observed-ads",
                                   params={"meta_connection_id": connection["id"]})
        self.assertEqual(filtered.status_code, 404, filtered.text)
        self.assertEqual(filtered.json()["code"], "connection_not_found")

        refused = self._post(connection["id"], ad_id)
        self.assertEqual(refused.status_code, 404, refused.text)
        self.assertEqual(refused.json()["code"], "connection_not_found")
        self.assertEqual(self._targets_on(connection), [])

    def test_another_workspaces_winner_is_refused(self):
        other = self._other_workspace()
        other_ctx = replace(other, meta_mode=SANDBOX)
        with tenant_session(other_ctx) as session:
            connection = MetaConnectionRecord(id=f"mcon_{other.workspace_id}", workspace_id=other.workspace_id,
                                              meta_mode=SANDBOX, ad_account_id="act_other", state="active")
            session.add(connection)
            session.flush()
            scenario = ScenarioVersionRecord(id=f"scen_{other.workspace_id}", workspace_id=other.workspace_id,
                                             meta_mode=SANDBOX, meta_connection_id=connection.id,
                                             scenario_config={}, scenario_hash=canonical_hash({}),
                                             is_default=True)
            session.add(scenario)
            session.flush()
            session.add(FakeMetaObjectRecord(id=new_id("fmo"), workspace_id=other.workspace_id,
                                             meta_mode=SANDBOX, scenario_version_id=scenario.id,
                                             object_type="ad", remote_object_id="ad_other", state={}))
        self.identity = other

        refused = self._post(connection.id, "ad_other", decision_id=self.chain["decision_id"])

        self.assertEqual(refused.status_code, 422, refused.text)
        self.assertEqual(refused.json()["code"], "winner_ineligible")

    def test_delegated_users_cannot_pick_internal_validation_ads(self):
        for identity in (self.owner, self.member):
            with self.subTest(role=identity.role):
                self.identity = identity
                listed = self.client.get(f"/api/v1/scale/{INTERNAL}/observed-ads")
                self.assertEqual(listed.status_code, 403, listed.text)
                refused = self._post("mcon_any", "ad_any", mode=INTERNAL)
                self.assertEqual(refused.status_code, 403, refused.text)


if __name__ == "__main__":
    unittest.main()
