"""AssetVersion review drives MarketAssetBinding.review_state (#128).

Localization contract §3 step 11: the user reviews the exact AssetVersion in
the Assets area, and an Approved review makes the target-market binding
``ready_for_experiment``. Spec §16.11: approval is exact-version, a later
rejection removes readiness without deleting history, and ``source_reuse``
references the reviewed source version without copying its review row.

The bindings here come from the real Winner -> Batch -> edit -> QC ->
finalize pipeline (``BindingTestBase._run_to_finalized``), and every review
goes through ``app.generation.review.create_review`` -- the function
``POST /generation/asset-versions/{id}/reviews`` calls -- never through a
binding-specific command, because there is none. The permission tests send
real ES256 delegated tokens through ``DelegatedIdentityMiddleware`` to the
real FastAPI app: only a verified Owner or Member reaches the review route.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    MarketAssetBindingEventRecord,
    MarketAssetBindingRecord,
    QualityReviewRecord,
    new_id,
)
from app.generation.review import ReviewError, create_review, find_review_head
from app.localization.binding import (
    REVIEW_EVENT_TYPE,
    apply_asset_version_review,
    apply_dependency_change,
    assess_binding_eligibility,
    publish_binding,
)
from app.main import app
from app.platform.delegated_identity import DelegatedIdentityMiddleware
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext
from tests.test_delegated_identity import (
    AUDIENCE,
    DEFAULT_CONFIG,
    ISSUER,
    FakeAdmissionReader,
    FakeJwksStore,
    FakeProjectionSyncer,
    admitted,
    generate_keypair,
    sign,
)
from tests.test_localization_binding import (
    CTX,
    WS_ID,
    BindingTestBase,
    _ensure_workspace,
    _seed_asset_versions,
)

OTHER_WS_ID = "ws_lb_binding_review_other"
OTHER_CTX = TenantContext(
    workspace_id=OTHER_WS_ID, role="owner", initiator_id="user_other", initiator_role="owner",
)


class ReviewLinkageBase(BindingTestBase):
    async def _published(self, tag: str) -> dict:
        """A finalized localized publication and its active binding."""
        built = await self._run_to_finalized(tag)
        with tenant_session(CTX) as s:
            binding = s.scalar(select(MarketAssetBindingRecord).where(
                MarketAssetBindingRecord.workspace_id == WS_ID,
                MarketAssetBindingRecord.item_id == built["item_id"],
                MarketAssetBindingRecord.active.is_(True),
            ))
        self.assertIsNotNone(binding, "finalize published no binding")
        self.assertEqual(binding.binding_kind, "localized")
        self.assertEqual(binding.bound_asset_version_id, built["child_asset_version_id"])
        built["binding_id"] = binding.id
        return built

    def _review(
        self, asset_version_id: str, value: str, *, ctx: TenantContext = CTX,
        supersede: bool = True,
    ) -> str:
        """Record a review through create_review, correcting the current head."""
        with tenant_session(ctx) as s:
            head = find_review_head(s, ctx, asset_version_id) if supersede else None
            review = create_review(
                s, ctx, asset_version_id, value, f"{value} in the Assets area",
                ctx.initiator_id, supersedes_review_id=head.id if head is not None else None,
            )
            return review.id

    def _binding(self, binding_id: str) -> MarketAssetBindingRecord:
        with tenant_session(CTX) as s:
            return s.get(MarketAssetBindingRecord, binding_id)

    def _events(self, binding_id: str) -> list[MarketAssetBindingEventRecord]:
        with tenant_session(CTX) as s:
            return list(s.scalars(select(MarketAssetBindingEventRecord).where(
                MarketAssetBindingEventRecord.workspace_id == WS_ID,
                MarketAssetBindingEventRecord.binding_id == binding_id,
            ).order_by(MarketAssetBindingEventRecord.sequence)).all())

    def _reviews(self, asset_version_id: str) -> list[QualityReviewRecord]:
        with tenant_session(CTX) as s:
            return list(s.scalars(select(QualityReviewRecord).where(
                QualityReviewRecord.workspace_id == WS_ID,
                QualityReviewRecord.asset_version_id == asset_version_id,
            )).all())

    def _state(self, binding_id: str) -> tuple[str, bool, int]:
        binding = self._binding(binding_id)
        return binding.review_state, binding.ready_for_experiment, binding.revision

    def _assert_review_event(
        self, event: MarketAssetBindingEventRecord, *, review_id: str,
        before: str, after: str, ready: bool, actor_id: str = "user_1",
    ) -> None:
        self.assertEqual(event.event_type, REVIEW_EVENT_TYPE)
        self.assertEqual(event.previous_state_json["review_state"], before)
        self.assertEqual(event.new_state_json["review_state"], after)
        self.assertEqual(event.new_state_json["ready_for_experiment"], ready)
        self.assertEqual(event.new_state_json["quality_review_id"], review_id)
        self.assertEqual(
            event.new_state_json["revision"], event.previous_state_json["revision"] + 1,
        )
        self.assertIsNotNone(event.dependency_change_id)
        self.assertEqual(event.actor_id, actor_id)


class TestReviewMovesBinding(ReviewLinkageBase):
    async def test_approval_makes_the_binding_experiment_ready(self) -> None:
        built = await self._published("rv_approve01")
        binding_id = built["binding_id"]
        self.assertEqual(self._state(binding_id), ("pending_review", False, 1))

        review_id = self._review(built["child_asset_version_id"], "approved")

        self.assertEqual(self._state(binding_id), ("approved", True, 2))
        published, reviewed = self._events(binding_id)
        self.assertEqual(published.event_type, "published")
        self._assert_review_event(
            reviewed, review_id=review_id, before="pending_review", after="approved", ready=True,
        )
        with tenant_session(CTX) as s:
            report = assess_binding_eligibility(s, CTX, binding_id=binding_id)
        self.assertEqual(report.review_state, "approved")
        self.assertNotIn("review_state:pending_review", report.findings)

    async def test_rejection_clears_readiness_and_keeps_history(self) -> None:
        built = await self._published("rv_reject01")
        binding_id, child = built["binding_id"], built["child_asset_version_id"]
        approval = self._review(child, "approved")
        rejection = self._review(child, "rejected")

        self.assertEqual(self._state(binding_id), ("rejected", False, 3))
        published, approved, rejected = self._events(binding_id)
        self._assert_review_event(
            approved, review_id=approval, before="pending_review", after="approved", ready=True,
        )
        self._assert_review_event(
            rejected, review_id=rejection, before="approved", after="rejected", ready=False,
        )
        # History is appended, never rewritten: both reviews remain, and the
        # rejection supersedes the approval instead of replacing it.
        reviews = {r.id: r for r in self._reviews(child)}
        self.assertEqual(set(reviews), {approval, rejection})
        self.assertEqual(reviews[approval].review_value, "approved")
        self.assertEqual(reviews[rejection].supersedes_review_id, approval)
        self.assertTrue(self._binding(binding_id).active)

    async def test_needs_changes_and_clear_withdraw_readiness(self) -> None:
        built = await self._published("rv_changes01")
        binding_id, child = built["binding_id"], built["child_asset_version_id"]

        changes = self._review(child, "needs_changes")
        self.assertEqual(self._state(binding_id), ("needs_changes", False, 2))
        approval = self._review(child, "approved")
        self.assertEqual(self._state(binding_id), ("approved", True, 3))
        # A cleared head is "no effective review": back to pending_review.
        cleared = self._review(child, "cleared")
        self.assertEqual(self._state(binding_id), ("pending_review", False, 4))

        events = self._events(binding_id)
        self.assertEqual(
            [e.event_type for e in events], ["published"] + [REVIEW_EVENT_TYPE] * 3,
        )
        self._assert_review_event(
            events[1], review_id=changes, before="pending_review", after="needs_changes",
            ready=False,
        )
        self._assert_review_event(
            events[2], review_id=approval, before="needs_changes", after="approved", ready=True,
        )
        self._assert_review_event(
            events[3], review_id=cleared, before="approved", after="pending_review", ready=False,
        )

    async def test_a_superseding_review_recomputes_from_the_new_head(self) -> None:
        built = await self._published("rv_supersede01")
        binding_id, child = built["binding_id"], built["child_asset_version_id"]
        self._review(child, "rejected")
        self._review(child, "approved")
        self.assertEqual(self._state(binding_id), ("approved", True, 3))

        # A correction that leaves the effective value where it was moves
        # nothing and appends nothing.
        self._review(child, "approved")
        self.assertEqual(self._state(binding_id), ("approved", True, 3))
        self.assertEqual(len(self._events(binding_id)), 3)
        self.assertEqual(len(self._reviews(child)), 3)

    async def test_approval_never_revives_stale_truth(self) -> None:
        built = await self._published("rv_stale01")
        binding_id = built["binding_id"]
        with tenant_session(CTX) as s:
            apply_dependency_change(
                s, CTX, change_kind="data_source_version",
                changed_version_id=built["data_source_version_id"], actor_id="user_1",
            )
        self.assertEqual(self._binding(binding_id).truth_freshness, "stale")

        self._review(built["child_asset_version_id"], "approved")

        binding = self._binding(binding_id)
        self.assertEqual((binding.review_state, binding.ready_for_experiment), ("approved", False))
        self.assertEqual(
            [e.event_type for e in self._events(binding_id)],
            ["published", "stale_truth", REVIEW_EVENT_TYPE],
        )

    async def test_one_review_moves_a_binding_at_most_once(self) -> None:
        built = await self._published("rv_once01")
        binding_id, child = built["binding_id"], built["child_asset_version_id"]
        review_id = self._review(child, "approved")
        with tenant_session(CTX) as s:
            again = apply_asset_version_review(
                s, CTX, asset_version_id=child, review_id=review_id, actor_id="user_1",
            )
        self.assertEqual((again.changed_binding_ids, again.unchanged_binding_ids), ((), (binding_id,)))
        self.assertEqual(len(self._events(binding_id)), 2)


class TestSourceReuseFollowsTheSourceReview(ReviewLinkageBase):
    async def test_source_reuse_starts_from_the_source_review_without_copying_it(self) -> None:
        built = await self._published("rv_reuse01")
        source = built["asset_version_id"]
        [source_review] = self._reviews(source)
        self.assertEqual(source_review.review_value, "approved")

        with tenant_session(CTX) as s:
            reused = publish_binding(
                s, CTX, item_id=built["item_id"], batch_id=built["batch_id"],
                publication_id=None, actor_id="user_1",
            )
        self.assertEqual(reused.binding_kind, "source_reuse")
        self.assertEqual(reused.superseded_binding_id, built["binding_id"])
        self.assertEqual((reused.review_state, reused.ready_for_experiment), ("approved", True))
        [published] = self._events(reused.binding_id)
        self.assertEqual(published.event_type, "published")
        self.assertEqual(published.new_state_json["review_state"], "approved")
        # The source's review row is referenced, not cloned; the localized
        # child still has none.
        self.assertEqual([r.id for r in self._reviews(source)], [source_review.id])
        self.assertEqual(self._reviews(built["child_asset_version_id"]), [])

        # Rejecting the source later moves the binding that references it.
        rejection = self._review(source, "rejected")
        self.assertEqual(self._state(reused.binding_id), ("rejected", False, 2))
        self._assert_review_event(
            self._events(reused.binding_id)[-1], review_id=rejection,
            before="approved", after="rejected", ready=False,
        )

    async def test_a_superseded_binding_keeps_its_last_review_state(self) -> None:
        built = await self._published("rv_superseded01")
        localized_id, child = built["binding_id"], built["child_asset_version_id"]
        self._review(child, "approved")
        with tenant_session(CTX) as s:
            reused = publish_binding(
                s, CTX, item_id=built["item_id"], batch_id=built["batch_id"],
                publication_id=None, actor_id="user_1",
            )
        before = self._binding(localized_id)
        self.assertFalse(before.active)
        events_before = len(self._events(localized_id))

        self._review(child, "rejected")

        after = self._binding(localized_id)
        self.assertEqual(
            (after.review_state, after.ready_for_experiment, after.revision),
            (before.review_state, before.ready_for_experiment, before.revision),
        )
        self.assertEqual(len(self._events(localized_id)), events_before)
        # The active source_reuse binding is bound to the source, not the child.
        self.assertEqual(self._state(reused.binding_id), ("approved", True, 1))


class TestReviewLinkageBoundaries(ReviewLinkageBase):
    async def test_reviewing_an_unrelated_version_moves_no_binding(self) -> None:
        built = await self._published("rv_unrelated01")
        binding_id = built["binding_id"]
        with tenant_session(CTX) as s:
            [unrelated] = _seed_asset_versions(s, WS_ID, "rv_unrelated01_other", count=1)

        self._review(unrelated, "approved")

        self.assertEqual(self._state(binding_id), ("pending_review", False, 1))
        self.assertEqual([e.event_type for e in self._events(binding_id)], ["published"])

    async def test_another_workspace_cannot_move_this_workspace_bindings(self) -> None:
        built = await self._published("rv_tenant01")
        binding_id, child = built["binding_id"], built["child_asset_version_id"]
        with tenant_session(OTHER_CTX) as s:
            _ensure_workspace(s, OTHER_WS_ID)
            [their_version] = _seed_asset_versions(s, OTHER_WS_ID, "rv_tenant01_theirs", count=1)

        # This Workspace's version does not exist for the other Workspace.
        with self.assertRaises(ReviewError) as refused:
            self._review(child, "approved", ctx=OTHER_CTX)
        self.assertEqual(refused.exception.code, "asset_version_not_found")
        # Its own review, and the linkage called with this Workspace's
        # version id, both resolve inside the other Workspace only.
        self._review(their_version, "approved", ctx=OTHER_CTX)
        with tenant_session(OTHER_CTX) as s:
            crossed = apply_asset_version_review(
                s, OTHER_CTX, asset_version_id=child, review_id=new_id("qr"),
                actor_id="user_other",
            )
        self.assertEqual((crossed.changed_binding_ids, crossed.unchanged_binding_ids), ((), ()))

        self.assertEqual(self._state(binding_id), ("pending_review", False, 1))
        self.assertEqual([e.event_type for e in self._events(binding_id)], ["published"])
        self.assertEqual(self._reviews(child), [])


class TestReviewPermissionsAreUnchanged(ReviewLinkageBase):
    """Only a verified Owner or Member reaches the review route; the binding
    moves only through it."""

    def setUp(self) -> None:
        super().setUp()
        private_key, jwk = generate_keypair()
        self.private_key = private_key
        self.client = TestClient(DelegatedIdentityMiddleware(
            app,
            jwks_store=FakeJwksStore({"dsk_test": jwk}),
            admission_reader=FakeAdmissionReader(
                {WS_ID: admitted(workspace_id=WS_ID, authorization_epoch=CTX.authorization_epoch)}
            ),
            projection_syncer=FakeProjectionSyncer(),
            config=DEFAULT_CONFIG,
        ))
        self.addCleanup(self.client.close)
        saved = app.dependency_overrides.pop(request_tenant_context, None)
        if saved is not None:
            self.addCleanup(app.dependency_overrides.__setitem__, request_tenant_context, saved)

    def _token(self, role: str, sub: str) -> str:
        now = int(datetime.now(timezone.utc).timestamp())
        return sign(self.private_key, {
            "iss": ISSUER, "aud": AUDIENCE, "sub": sub, "workspace_id": WS_ID,
            "membership_id": f"mem_{sub}", "role": role, "session_id": f"sess_{sub}",
            "authorization_epoch": CTX.authorization_epoch, "entitlement_version": 1,
            "jti": f"jti_{uuid.uuid4()}", "iat": now, "nbf": now, "exp": now + 30,
        })

    def _post_review(self, version_id: str, value: str, token: str | None):
        headers = {"Idempotency-Key": f"ik-{uuid.uuid4().hex}"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return self.client.post(
            f"/api/v1/generation/asset-versions/{version_id}/reviews",
            json={"review_value": value, "reason": "reviewed in the Assets area"},
            headers=headers,
        )

    async def test_only_a_verified_owner_or_member_can_move_the_binding(self) -> None:
        built = await self._published("rv_perm01")
        binding_id, child = built["binding_id"], built["child_asset_version_id"]

        no_identity = self._post_review(child, "approved", None)
        self.assertEqual(no_identity.status_code, 401, no_identity.text)
        wrong_role = self._post_review(child, "approved", self._token("viewer", "viewer_1"))
        self.assertEqual(wrong_role.status_code, 401, wrong_role.text)
        self.assertEqual(wrong_role.json()["code"], "invalid_role")
        self.assertEqual(self._state(binding_id), ("pending_review", False, 1))
        self.assertEqual(self._reviews(child), [])

        member = self._post_review(child, "approved", self._token("member", "member_1"))
        self.assertEqual(member.status_code, 201, member.text)
        self.assertEqual(self._state(binding_id), ("approved", True, 2))
        self._assert_review_event(
            self._events(binding_id)[-1], review_id=member.json()["id"],
            before="pending_review", after="approved", ready=True, actor_id="member_1",
        )
