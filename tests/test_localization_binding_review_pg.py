"""Issue #128 against an isolated migrated PostgreSQL with the real API, Worker and Readonly roles.

The exact-version review a user records in the Assets area
(``POST /api/v1/generation/asset-versions/{id}/reviews``) moves the target-
market MarketAssetBinding in the same API transaction, under RLS. The SQLite
suite (``tests/test_localization_binding_review.py``) checks no grants and
no RLS; here the linkage runs as the roles that serve it in production:

* the Winner, its review, the configuration versions and the claim inventory
  are seeded as the schema owner; the Batch commands run as the API role; the
  edit, the deterministic QC and finalize run as the Worker role, which
  publishes the localized child and a ``pending_review`` binding;
* an Owner's approval through the API makes the binding ``approved`` and
  ``ready_for_experiment``; a Member's rejection that supersedes it clears
  readiness; each appends one binding event and both reviews remain;
* a review of an unrelated version, a review attempt from another
  Workspace's Member or Owner, and a request without a verified identity
  move nothing;
* then the privilege matrix of the two binding tables.

Run with ``DOMAIN_OWNER_DATABASE_URL``, ``DOMAIN_API_DATABASE_URL``,
``DOMAIN_WORKER_DATABASE_URL``, ``MARKETAI_DATABASE_URL`` (the owner) and
``MARKETAI_TENANT_CONTEXT_SECRET`` against a cluster released with
``make db-release``.
"""
from __future__ import annotations

import asyncio
import os
import unittest
import uuid

from sqlalchemy import select, text

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    AssetVersionRecord,
    LocalizationAttemptRecord,
    MarketAssetBindingEventRecord,
    MarketAssetBindingRecord,
    QualityReviewRecord,
    new_id,
)
from app.library.contracts import ROLE_MEMBER, ROLE_OWNER
from app.localization import worker
from app.main import app
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext
from app.providers.dispatch import ExternalCallCoordinator
from tests.test_localization_adopt_pg import (
    API,
    OWNER,
    URLS,
    WORKER,
    LocalizationPostgresFixture,
)

REVIEWS = "/api/v1/generation/asset-versions/{version_id}/reviews"
BINDING_TABLES = ("market_asset_bindings", "market_asset_binding_events")
#: role -> table -> privileges it must hold; every other DML privilege must be absent.
GRANTED = {
    "marketai_domain_api": {
        "market_asset_bindings": {"SELECT", "INSERT", "UPDATE"},
        "market_asset_binding_events": {"SELECT", "INSERT"},
    },
    "marketai_domain_worker": {
        "market_asset_bindings": {"SELECT", "INSERT", "UPDATE"},
        "market_asset_binding_events": {"SELECT", "INSERT"},
    },
    "marketai_domain_readonly": {
        "market_asset_bindings": {"SELECT"},
        "market_asset_binding_events": {"SELECT"},
    },
}
DML = ("SELECT", "INSERT", "UPDATE", "DELETE")


@unittest.skipUnless(all(URLS) and os.getenv("MARKETAI_TENANT_CONTEXT_SECRET"),
                     "requires isolated PostgreSQL runtime roles")
class BindingReviewPostgresTests(LocalizationPostgresFixture, unittest.TestCase):
    # -- the fixture --------------------------------------------------------

    def _published_binding(self, owner: TenantContext) -> dict:
        """A localized child and its binding, published by finalize as the Worker."""
        seed = self._seed(owner)
        built = {**self._confirmed_batch(owner, seed, external_qc_steps=()),
                 "asset_version_id": seed["asset_version_id"]}
        with self._as(WORKER):
            asyncio.run(worker.handle_edit_attempt(
                owner, batch_id=built["batch_id"], item_id=built["item_id"], attempt_number=1,
                session_factory=tenant_session,
                coordinator=ExternalCallCoordinator(session_factory=tenant_session),
            ))
        with self._as(OWNER), tenant_session(owner) as session:
            attempt_id = session.scalar(select(LocalizationAttemptRecord.id).where(
                LocalizationAttemptRecord.workspace_id == owner.workspace_id,
                LocalizationAttemptRecord.item_id == built["item_id"],
                LocalizationAttemptRecord.attempt_number == 1))
        with self._as(WORKER), tenant_session(owner) as session:
            worker.handle_run_deterministic_qc(session, owner, batch_id=built["batch_id"],
                                               item_id=built["item_id"], attempt_id=attempt_id)
        with self._as(WORKER), tenant_session(owner) as session:
            worker.handle_finalize_item(session, owner, batch_id=built["batch_id"],
                                        item_id=built["item_id"], attempt_id=attempt_id)
        with self._as(OWNER), tenant_session(owner) as session:
            [binding] = session.scalars(select(MarketAssetBindingRecord).where(
                MarketAssetBindingRecord.workspace_id == owner.workspace_id,
                MarketAssetBindingRecord.item_id == built["item_id"])).all()
            built["binding_id"], built["child_id"] = binding.id, binding.bound_asset_version_id
            self.assertEqual((binding.binding_kind, binding.review_state, binding.ready_for_experiment),
                             ("localized", "pending_review", False))
        return built

    def _unrelated_version(self, owner: TenantContext, built: dict) -> str:
        """Another version of the source's Asset, seeded as the owner."""
        with self._as(OWNER), tenant_session(owner) as session:
            source = session.get(AssetVersionRecord, built["asset_version_id"])
            version = AssetVersionRecord(
                id=new_id("av"), workspace_id=owner.workspace_id, asset_id=source.asset_id,
                kind="generated", aspect_ratio="1:1", object_key=f"unrelated/{uuid.uuid4().hex}.png",
                content_sha256=uuid.uuid4().hex * 2, byte_size=1, mime_type="image/png",
            )
            session.add(version)
            session.flush()
            return version.id

    # -- requests and reads -------------------------------------------------

    def _review(self, caller: TenantContext | None, version_id: str, value: str, expect: int,
                supersedes: str | None = None) -> dict:
        """POST the review through the API role; ``None`` sends no verified identity."""
        override = app.dependency_overrides.pop(request_tenant_context)
        if caller is not None:
            self.current = caller
            app.dependency_overrides[request_tenant_context] = override
        try:
            with self._as(API):
                response = self.client.post(
                    REVIEWS.format(version_id=version_id),
                    json={"review_value": value, "reason": "reviewed in the Assets area",
                          "supersedes_review_id": supersedes},
                    headers={"Idempotency-Key": f"ik-{uuid.uuid4().hex}"},
                )
        finally:
            app.dependency_overrides[request_tenant_context] = override
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def _binding(self, owner: TenantContext, built: dict) -> tuple:
        with self._as(OWNER), tenant_session(owner) as session:
            binding = session.get(MarketAssetBindingRecord, built["binding_id"])
            events = session.scalars(select(MarketAssetBindingEventRecord).where(
                MarketAssetBindingEventRecord.workspace_id == owner.workspace_id,
                MarketAssetBindingEventRecord.binding_id == built["binding_id"],
            ).order_by(MarketAssetBindingEventRecord.sequence)).all()
            return (binding.review_state, binding.ready_for_experiment, binding.revision,
                    [(e.event_type, e.new_state_json.get("quality_review_id"), e.actor_id) for e in events])

    # -- tests --------------------------------------------------------------

    def test_the_assets_review_moves_the_binding_under_the_api_role(self):
        workspace_id = self._workspace("ws_bind_review_pg")
        owner = self._context(workspace_id, ROLE_OWNER, "owner_a")
        member = self._context(workspace_id, ROLE_MEMBER, "member_a")
        other = self._workspace("ws_bind_review_pg_other")
        strangers = [self._context(other, ROLE_MEMBER, "member_b"), self._context(other, ROLE_OWNER, "owner_b")]
        built = self._published_binding(owner)
        child = built["child_id"]
        [published] = self._binding(owner, built)[3]
        self.assertEqual(published[:2], ("published", None))

        # Nobody without a verified Owner/Member identity reaches the route.
        refused = self._review(None, child, "approved", expect=401)
        self.assertEqual(refused["code"], "delegated_identity_required")
        # Another Workspace cannot find this version, let alone its binding.
        for stranger in strangers:
            with self.subTest(stranger=stranger.role):
                self.assertEqual(self._review(stranger, child, "approved", expect=404)["code"],
                                 "asset_version_not_found")
                with self._as(API), tenant_session(stranger) as session:
                    self.assertIsNone(session.get(MarketAssetBindingRecord, built["binding_id"]))
        # Reviewing an unrelated version of the same Workspace moves nothing.
        self._review(owner, self._unrelated_version(owner, built), "approved", expect=201)
        self.assertEqual(self._binding(owner, built), ("pending_review", False, 1, [published]))

        approval = self._review(owner, child, "approved", expect=201)
        self.assertEqual(self._binding(owner, built), (
            "approved", True, 2,
            [published, ("review_state_changed", approval["id"], owner.initiator_id)],
        ))

        rejection = self._review(member, child, "rejected", expect=201, supersedes=approval["id"])
        self.assertEqual(self._binding(owner, built), (
            "rejected", False, 3,
            [published, ("review_state_changed", approval["id"], owner.initiator_id),
             ("review_state_changed", rejection["id"], member.initiator_id)],
        ))
        with self._as(OWNER), tenant_session(owner) as session:
            reviews = {r.id: r for r in session.scalars(select(QualityReviewRecord).where(
                QualityReviewRecord.asset_version_id == child)).all()}
        self.assertEqual(set(reviews), {approval["id"], rejection["id"]})
        self.assertEqual(reviews[rejection["id"]].supersedes_review_id, approval["id"])

        # The strangers' attempts after the change still move nothing.
        for stranger in strangers:
            self._review(stranger, child, "approved", expect=404)
        self.assertEqual(self._binding(owner, built)[:3], ("rejected", False, 3))
        with self.api.connect() as connection:  # no tenant context: no row
            self.assertEqual(connection.execute(text("SELECT count(*) FROM market_asset_bindings")).scalar(), 0)

    def test_the_privilege_matrix_of_the_binding_tables_holds(self):
        with self.owner.connect() as connection:
            for table in BINDING_TABLES:
                forced = connection.execute(text(
                    "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE relname = :t"
                ), {"t": table}).scalar()
                self.assertTrue(forced, table)
            for role, tables in GRANTED.items():
                for table, expected in tables.items():
                    for privilege in DML:
                        held = connection.execute(
                            text("SELECT has_table_privilege(:r, :t, :p)"),
                            {"r": role, "t": table, "p": privilege},
                        ).scalar()
                        self.assertEqual(held, privilege in expected, f"{role} {privilege} {table}")


if __name__ == "__main__":
    unittest.main()
