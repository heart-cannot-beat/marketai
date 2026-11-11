"""The DeliveryTarget entry under the real roles on PostgreSQL (#129).

On a cluster released with ``make db-release`` the API runs as
``marketai_domain_api``. The Winner is a fixture seeded as the schema owner,
the connection and its binding go through the API, and the sandbox ad is
what a sandbox Launch leaves behind, written as the Worker. A copy of the
Winner's ExperimentVersion bound to the ad is the record a Launch or an
Experiment candidate ad binding leaves (fixture).

1. An Owner and a Member each pick the observed ad through the API, as the
   API role under RLS: the listing names the ad and its creative, the pick
   makes the DeliveryTarget on the connection's Scale contract set, which
   the system executor created with the picker as initiator, and the
   lineage gate's guard row exists.
2. A replay with the same key returns the same target; a refused pick
   (another placement) answers its reason and writes nothing.
3. Another Workspace sees none of the ads, cannot name the connection, and
   its own pick of the first Workspace's Winner is ``winner_ineligible``.

Run with ``DOMAIN_OWNER_DATABASE_URL``, ``DOMAIN_API_DATABASE_URL``,
``DOMAIN_WORKER_DATABASE_URL``, ``MARKETAI_DATABASE_URL`` (the owner) and
``MARKETAI_TENANT_CONTEXT_SECRET``. Fictional brand only: Acme Charge.
"""
from __future__ import annotations

import os
import unittest
import uuid
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    AuditEventRecord,
    ExperimentDecisionRecord,
    WorkspaceRecord,
)
from app.main import app
from app.meta.models import MetaObservationSyncScheduleRecord
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext
from app.scale.models import ScaleDeliveryTargetRecord, ScaleLineageGuardRecord
from app.scale.observed_ads import CONTRACT_SET_EXECUTOR
from tests.support.meta_observation import AD_ACCOUNT, seed_sandbox_ads, worker_context
from tests.support.scale_candidate_fixtures import build_ready_winner, ensure_governance_prereqs
from tests.support.scale_contract_set import create_connection_through_api
from tests.support.scale_observed_ad_fixtures import bind_ad_to_winner_copy

URLS = [os.getenv(key) for key in (
    "DOMAIN_OWNER_DATABASE_URL", "DOMAIN_API_DATABASE_URL", "DOMAIN_WORKER_DATABASE_URL",
)]
OWNER, API, WORKER = range(3)
SANDBOX = "customer_sandbox"


@unittest.skipUnless(all(URLS) and os.getenv("MARKETAI_TENANT_CONTEXT_SECRET"),
                     "requires isolated PostgreSQL runtime roles")
class ObservedAdDeliveryTargetPostgresTests(unittest.TestCase):
    def setUp(self):
        self.engines = [create_engine(url) for url in URLS]
        for engine in self.engines:
            self.addCleanup(engine.dispose)
        self.owner_engine = self.engines[OWNER]
        self.sessions = [sessionmaker(bind=e, expire_on_commit=False, autoflush=False) for e in self.engines]
        self.current: TenantContext | None = None
        app.dependency_overrides[request_tenant_context] = lambda: self.current
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.workspaces: list[str] = []
        self.addCleanup(self._quiesce)

    @contextmanager
    def _as(self, index: int):
        with patch("app.adapters.persistence.database.SessionLocal", self.sessions[index]), \
             patch("app.platform.jobs.engine", self.engines[index]):
            yield

    def _workspace(self, prefix: str) -> TenantContext:
        workspace_id = f"ws_obsad_pg_{prefix}_{uuid.uuid4().hex[:10]}"
        with Session(self.owner_engine) as session, session.begin():  # the control plane opens it
            session.add(WorkspaceRecord(id=workspace_id, name=f"Acme Charge {workspace_id[-4:]}"))
        self.workspaces.append(workspace_id)
        return TenantContext(workspace_id=workspace_id, role="owner", initiator_id="usr_acme_owner",
                             request_id="test-req", trace_id="trace-test")

    def _quiesce(self) -> None:
        """Leave no due observation schedule behind: the pick made the
        connection's Scale contracts, and a sandbox connection's are scheduled."""
        if not self.workspaces:
            return
        with Session(self.owner_engine) as session, session.begin():
            session.execute(update(MetaObservationSyncScheduleRecord)
                            .where(MetaObservationSyncScheduleRecord.workspace_id.in_(self.workspaces),
                                   MetaObservationSyncScheduleRecord.state == "active")
                            .values(state="paused"))

    def _epoch(self, workspace_id: str) -> int:
        with Session(self.owner_engine) as session:
            return int(session.get(WorkspaceRecord, workspace_id).authorization_epoch)

    def _connection_with_ads(self, owner: TenantContext, count: int) -> tuple[str, list[str]]:
        self.current = owner
        with self._as(API):
            connection_id = create_connection_through_api(self, self.client, SANDBOX)["id"]
        with self._as(WORKER):  # what a sandbox Launch leaves behind
            ads = seed_sandbox_ads(
                worker_context(owner.workspace_id, SANDBOX, authorization_epoch=self._epoch(owner.workspace_id)),
                connection_id, count,
            )
        return connection_id, ads

    def _winner(self, owner: TenantContext) -> dict:
        tag = uuid.uuid4().hex[:8]
        sandbox = replace(owner, meta_mode=SANDBOX)
        with self._as(OWNER):  # fixtures: the Winner's product entries are elsewhere
            ensure_governance_prereqs(sandbox, f"prod_obsad_pg_{tag}")
            chain = build_ready_winner(sandbox, f"prod_obsad_pg_{tag}", tag)
            with tenant_session(owner) as session:
                decision = session.get(ExperimentDecisionRecord, chain["decision_id"])
                chain["version_id"] = decision.experiment_version_id
                chain["version_candidate_id"] = decision.experiment_version_candidate_id
        return chain

    def _bind(self, owner: TenantContext, chain: dict, ad_id: str, **version_fields) -> None:
        with self._as(OWNER), tenant_session(owner) as session:
            bind_ad_to_winner_copy(
                session, workspace_id=owner.workspace_id, version_id=chain["version_id"],
                version_candidate_id=chain["version_candidate_id"], suffix=f"{chain['tag']}_{ad_id[-8:]}",
                ad_account_id=AD_ACCOUNT, ad_id=ad_id, **version_fields,
            )

    def _api(self, method: str, path: str, *, identity: TenantContext, expect: int, **kwargs) -> dict:
        self.current = identity
        with self._as(API):
            response = self.client.request(method, f"/api/v1/scale/{SANDBOX}{path}", **kwargs)
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def _pick(self, identity: TenantContext, decision_id: str, connection_id: str, ad_id: str, *,
              expect: int = 201, key: str | None = None) -> dict:
        return self._api(
            "POST", "/delivery-targets", identity=identity, expect=expect,
            json={"winner_decision_id": decision_id, "meta_connection_id": connection_id, "ad_remote_id": ad_id},
            headers={"Idempotency-Key": key or f"ci-only-ik-{uuid.uuid4().hex[:12]}"},
        )

    def _targets(self, workspace_id: str) -> list[ScaleDeliveryTargetRecord]:
        with Session(self.owner_engine) as session:
            rows = list(session.scalars(select(ScaleDeliveryTargetRecord).where(
                ScaleDeliveryTargetRecord.workspace_id == workspace_id)))
            for row in rows:
                session.expunge(row)
            return rows

    def test_owners_and_members_pick_observed_ads_as_the_api_role(self):
        owner = self._workspace("a")
        member = replace(owner, role="member", initiator_id="usr_acme_member")
        chain = self._winner(owner)
        connection_id, (owner_ad, member_ad, other_placement_ad) = self._connection_with_ads(owner, 3)
        for ad_id in (owner_ad, member_ad):
            self._bind(owner, chain, ad_id)
        self._bind(owner, chain, other_placement_ad, placement="story")

        listing = self._api("GET", "/observed-ads", identity=member, expect=200)
        assets = {item["ad_remote_id"]: item["asset_version_ids"] for item in listing["items"]}
        self.assertEqual(assets[owner_ad], [chain["asset_version_id"]])

        by_member = self._pick(member, chain["decision_id"], connection_id, member_ad, key="ci-only-member-pick")
        by_owner = self._pick(owner, chain["decision_id"], connection_id, owner_ad)
        replay = self._pick(member, chain["decision_id"], connection_id, member_ad, key="ci-only-member-pick")
        refused = self._pick(owner, chain["decision_id"], connection_id, other_placement_ad, expect=409)

        self.assertEqual(replay["id"], by_member["id"])
        self.assertEqual(refused["code"], "target_placement_mismatch")
        self.assertEqual({target.id for target in self._targets(owner.workspace_id)},
                         {by_member["id"], by_owner["id"]})
        for target in (by_member, by_owner):
            self.assertEqual(target["landing_page_evidence_hash"], chain["landing_page_evidence_hash"])
            self.assertEqual(target["attribution_setting"], chain["attribution_semantics_hash"])
            self.assertEqual(target["contract_generation_id"], by_member["contract_generation_id"])
        with Session(self.owner_engine) as session:
            event = session.scalar(select(AuditEventRecord).where(
                AuditEventRecord.workspace_id == owner.workspace_id,
                AuditEventRecord.event_type == "scale.metric_contract_set.created"))
            guards = session.scalars(select(ScaleLineageGuardRecord.delivery_target_id).where(
                ScaleLineageGuardRecord.workspace_id == owner.workspace_id)).all()
        self.assertEqual((event.executor, event.initiator_id, event.initiator_role),
                         (CONTRACT_SET_EXECUTOR, "usr_acme_member", "member"))
        self.assertEqual(set(guards), {by_member["id"], by_owner["id"]})

    def test_another_workspace_neither_sees_nor_uses_the_connection_ads_or_winner(self):
        owner_a = self._workspace("a")
        chain = self._winner(owner_a)
        connection_a, (ad_a,) = self._connection_with_ads(owner_a, 1)
        self._bind(owner_a, chain, ad_a)
        owner_b = self._workspace("b")

        listing = self._api("GET", "/observed-ads", identity=owner_b, expect=200)
        self.assertEqual(listing, {"items": [], "unavailable": []})
        named = self._api("GET", "/observed-ads", identity=owner_b, expect=404,
                          params={"meta_connection_id": connection_a})
        self.assertEqual(named["code"], "connection_not_found")
        refused = self._pick(owner_b, chain["decision_id"], connection_a, ad_a, expect=404)
        self.assertEqual(refused["code"], "connection_not_found")

        connection_b, (ad_b,) = self._connection_with_ads(owner_b, 1)
        foreign_winner = self._pick(owner_b, chain["decision_id"], connection_b, ad_b, expect=422)
        self.assertEqual(foreign_winner["code"], "winner_ineligible")
        self.assertEqual(self._targets(owner_a.workspace_id), [])
        self.assertEqual(self._targets(owner_b.workspace_id), [])


if __name__ == "__main__":
    unittest.main()
