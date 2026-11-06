"""Tests for Enrollment and Episode lifecycle commands (Issue #25, Task 11;
Issue #86 Task 11 activate/resume preview-hardening).

Spec: docs/superpowers/specs/2026-09-08-scale-section-3-evidence-advice-orchestration.md §3.7, §3.9
Design: docs/superpowers/specs/2026-09-08-scale-deterministic-evidence-advice-design.md §6
Task: .superpowers/sdd/2026-09-08-scale-deterministic-evidence-advice/task-11-brief.md
Hardening spec: docs/superpowers/specs/2026-09-14-harden-scale-enrollment-lineage-design.md §5

``EnrollmentTestBase`` (lightweight, per-test in-memory SQLite engine) still
exercises pause/close/withdrawal/no-delivery-closure state-machine behavior
directly against ORM rows -- none of that changed under issue #86.
``activate_enrollment``/``resume_enrollment`` themselves, however, now
require a genuine candidate-gated lineage chain and a consumed
``ScaleActivationPreviewRecord`` (issue #86 Task 11) that this lightweight
fixture cannot provide; ``EnrollmentTestBase._activate()`` therefore builds
the 'active' starting state directly via ORM (reusing the unchanged
``_build_episode_and_delivery_source`` helper) rather than calling
``activate_enrollment``. ``activate_enrollment``/``resume_enrollment``'s own
preview-gated behavior is covered below by ``ActivationFlowTestBase``'s
subclasses, which use the real alembic-migrated database and
``tests/support/scale_candidate_fixtures`` (mirrors
``tests/test_scale_router.py``'s ``ScaleRouterTestBase`` pattern -- a fresh
Workspace per test method, since this real database persists across test
methods within one run, unlike the lightweight per-test in-memory engine),
and by ``tests/test_scale_activation_flow.py``'s comprehensive preview-flow
matrix.
"""
from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import config
from app.adapters.persistence.database import Base, tenant_session
from app.adapters.persistence.models import WorkspaceRecord, new_id, utcnow
from app.platform.tenancy import TenantContext
from app.scale.activation_preview import create_activation_preview
from app.scale.candidates import create_enrollment_candidate
from app.scale.enrollment import (
    DeliveryConfigBaseline,
    DraftEnrollmentRequest,
    EnrollmentLifecycleError,
    EpisodeContext,
    _build_episode_and_delivery_source,
    activate_enrollment,
    apply_no_delivery_episode_closure,
    close_enrollment,
    create_draft_enrollment,
    create_draft_enrollment_from_request,
    handle_upstream_withdrawal,
    pause_enrollment,
    resume_enrollment,
)
from app.scale.models import (
    ScaleDeliveryConfigSourceRecord,
    ScaleEnrollmentEventRecord,
    ScaleEnrollmentRecord,
    ScaleEpisodeEventRecord,
    ScaleEpisodeRecord,
)
from tests.support.scale_candidate_fixtures import (
    build_ready_winner_and_target,
    ensure_governance_prereqs,
    seed_activation_evidence_prereqs,
)

OBSERVED_AT = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _draft_request(**overrides) -> DraftEnrollmentRequest:
    fields = dict(
        meta_connection_id="mcon_1",
        ad_account_id="act_1",
        scenario_scope_key="__no_scenario__",
        winner_decision_id="wd_1",
        winner_confirmation_id="wc_1",
        asset_version_id="av_1",
        product_version_id="pv_1",
        offer_version_id="ov_1",
        market_code="US",
        landing_page_version_id=None,
        market_asset_binding_id=None,
        campaign_remote_id="camp_1",
        ad_set_remote_id="adset_1",
        ad_remote_id="ad_1",
        creator_id="user_1",
    )
    fields.update(overrides)
    return DraftEnrollmentRequest(**fields)


def _delivery_config(**overrides) -> DeliveryConfigBaseline:
    fields = dict(
        contract_generation_id="gen-1",
        ad_creative_id="creative_1",
        campaign_status="ACTIVE",
        ad_set_status="ACTIVE",
        ad_status="ACTIVE",
        budget_source="ad_set",
        budget_amount=Decimal("50"),
        optimization_goal="OFFSITE_CONVERSIONS",
        bid_strategy="LOWEST_COST_WITHOUT_CAP",
        attribution_setting="7d_click_1d_view",
        targeting_safety_hash="t" * 64,
        evidence_hash="e" * 64,
        comparability_hash="c" * 64,
        raw_response_hash="r" * 64,
        sync_run_id="sync_1",
        occurrence_id="occ_delivery",
        call_intent_id="call_delivery",
        observed_at=OBSERVED_AT,
    )
    fields.update(overrides)
    return DeliveryConfigBaseline(**fields)


def _episode_context(**overrides) -> EpisodeContext:
    fields = dict(
        metric_semantic_contract_set_id="smcs_1",
        timezone_name="UTC",
        currency="USD",
        attribution_semantics_hash="a" * 64,
        started_local_day=date(2026, 9, 1),
    )
    fields.update(overrides)
    return EpisodeContext(**fields)


class EnrollmentTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(
            bind=self.engine,
            tables=[
                WorkspaceRecord.__table__,
                ScaleEnrollmentRecord.__table__,
                ScaleEnrollmentEventRecord.__table__,
                ScaleEpisodeRecord.__table__,
                ScaleEpisodeEventRecord.__table__,
                ScaleDeliveryConfigSourceRecord.__table__,
            ],
        )
        self.session = Session(bind=self.engine)
        self.addCleanup(self.session.close)

        self.ws = WorkspaceRecord(id=new_id("ws"), name="Test WS")
        self.session.add(self.ws)
        self.session.flush()

        self.mode = "internal_validation"
        self.context = TenantContext(
            workspace_id=self.ws.id,
            role="system",
            executor="system:worker",
            initiator_id="user_1",
            meta_mode=self.mode,
        )

    def _draft(self, idempotency_key="draft-1", **overrides) -> ScaleEnrollmentRecord:
        enrollment = create_draft_enrollment_from_request(
            self.session, self.context, _draft_request(**overrides),
            idempotency_key=idempotency_key,
        )
        self.session.flush()
        return enrollment

    def _activate(self, enrollment, idempotency_key="activate-1", **overrides):
        """Builds the 'active' state directly via ORM: ``activate_enrollment``
        itself now requires a genuine candidate-gated lineage chain plus a
        consumed ``ScaleActivationPreviewRecord`` (issue #86 Task 11) this
        lightweight fixture cannot provide -- see module docstring. Reuses
        the unchanged ``_build_episode_and_delivery_source`` helper so the
        Episode/DeliveryConfigSource shape stays identical to what
        ``activate_enrollment`` itself builds today.
        """
        episode, delivery_source = _build_episode_and_delivery_source(
            self.context, enrollment, _delivery_config(), _episode_context(**overrides),
        )
        enrollment.lifecycle = "active"
        enrollment.current_episode_id = episode.id
        enrollment.revision += 1
        enrollment.updated_at = utcnow()
        self.session.add(episode)
        self.session.flush()
        self.session.add(delivery_source)
        self.session.flush()
        return enrollment, episode


class TestCreateDraftEnrollment(EnrollmentTestBase):
    def test_creates_draft_and_appends_event(self):
        enrollment = self._draft()

        self.assertEqual(enrollment.lifecycle, "draft")
        self.assertEqual(enrollment.revision, 1)
        self.assertEqual(
            enrollment.remote_binding_key, "mcon_1/act_1/camp_1/adset_1/ad_1"
        )

        events = self.session.execute(
            select(ScaleEnrollmentEventRecord).filter_by(enrollment_id=enrollment.id)
        ).scalars().all()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "draft_created")
        self.assertIsNone(events[0].previous_lifecycle)
        self.assertEqual(events[0].current_lifecycle, "draft")

    def test_idempotent_replay_returns_same_enrollment_without_duplicate(self):
        first = self._draft(idempotency_key="dup-key")
        second = self._draft(idempotency_key="dup-key")
        self.assertEqual(first.id, second.id)

        rows = self.session.execute(select(ScaleEnrollmentRecord)).scalars().all()
        self.assertEqual(len(rows), 1)

    def test_scenario_scope_forbidden_outside_controlled_mock(self):
        with self.assertRaises(EnrollmentLifecycleError) as ctx:
            self._draft(scenario_scope_key="scenario_1")
        self.assertEqual(ctx.exception.code, "scenario_scope_forbidden")

    def test_scenario_scope_required_for_controlled_mock(self):
        self.context = TenantContext(
            workspace_id=self.ws.id, role="system", executor="system:worker",
            initiator_id="user_1", meta_mode="controlled_mock",
        )
        with self.assertRaises(EnrollmentLifecycleError) as ctx:
            self._draft()
        self.assertEqual(ctx.exception.code, "scenario_scope_required")

    def test_remote_binding_shape_requires_campaign(self):
        with self.assertRaises(EnrollmentLifecycleError) as ctx:
            self._draft(campaign_remote_id=None, ad_set_remote_id=None, ad_remote_id=None)
        self.assertEqual(ctx.exception.code, "remote_binding_shape_invalid")

    def test_remote_binding_shape_requires_ad_set_for_ad(self):
        with self.assertRaises(EnrollmentLifecycleError) as ctx:
            self._draft(ad_set_remote_id=None, ad_remote_id="ad_1")
        self.assertEqual(ctx.exception.code, "remote_binding_shape_invalid")

    def test_required_field_missing(self):
        with self.assertRaises(EnrollmentLifecycleError) as ctx:
            self._draft(winner_decision_id="")
        self.assertEqual(ctx.exception.code, "required_field_missing")


class ActivationFlowTestBase(unittest.TestCase):
    """Heavy, alembic-migrated fixture base for ``activate_enrollment``/
    ``resume_enrollment``'s own preview-gated behavior (issue #86 Task 11).
    These can no longer run against ``EnrollmentTestBase``'s lightweight
    in-memory engine: both now require a genuine candidate-gated lineage
    chain plus a consumed ``ScaleActivationPreviewRecord`` (module
    docstring). A fresh Workspace per test method (mirrors
    ``tests/test_scale_router.py``'s ``ScaleRouterTestBase``) keeps each
    test's remote-binding-uniqueness window independent, since this real
    database persists across test methods within one run, unlike
    ``EnrollmentTestBase``'s fresh-per-test in-memory engine."""

    workspace_seq = 0
    mode = "customer_sandbox"

    @classmethod
    def setUpClass(cls) -> None:
        url = config.DATABASE_URL
        if url.startswith("sqlite:///"):
            Path(url.replace("sqlite:///", "", 1)).parent.mkdir(parents=True, exist_ok=True)
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")

    def setUp(self) -> None:
        ActivationFlowTestBase.workspace_seq += 1
        self.workspace_id = f"ws_actflow_{ActivationFlowTestBase.workspace_seq}"
        self.ctx = TenantContext(
            workspace_id=self.workspace_id, role="owner", initiator_id="user_1",
            meta_mode=self.mode,
        )
        ensure_governance_prereqs(self.ctx, f"prod_{self.workspace_id}")
        self._chain = build_ready_winner_and_target(
            self.ctx, f"prod_{self.workspace_id}", self.workspace_id,
            target_meta_mode=self.mode,
        )
        seed_activation_evidence_prereqs(
            self.ctx, delivery_target_id=self._chain["delivery_target_id"],
            meta_mode=self.mode, tag=self.workspace_id,
        )

    def _new_candidate_id(self) -> str:
        with tenant_session(self.ctx) as session:
            candidate = create_enrollment_candidate(
                session, self.ctx,
                decision_id=self._chain["decision_id"],
                delivery_target_id=self._chain["delivery_target_id"],
                idempotency_key=new_id("candidate"),
            )
            return candidate.id

    def _draft(self) -> tuple[str, int]:
        candidate_id = self._new_candidate_id()
        with tenant_session(self.ctx) as session:
            enrollment = create_draft_enrollment(
                session, self.ctx,
                enrollment_candidate_id=candidate_id,
                idempotency_key=new_id("draft"),
            )
            return enrollment.id, enrollment.revision

    def _create_preview(self, enrollment_id: str, expected_revision: int) -> str:
        with tenant_session(self.ctx) as session:
            preview = create_activation_preview(
                session, self.ctx,
                enrollment_id=enrollment_id,
                expected_revision=expected_revision,
                idempotency_key=new_id("preview"),
            )
            return preview.id

    def _activate(
        self, enrollment_id: str, expected_revision: int, *, idempotency_key: str,
    ) -> tuple[ScaleEnrollmentRecord, ScaleEpisodeRecord]:
        preview_id = self._create_preview(enrollment_id, expected_revision)
        with tenant_session(self.ctx) as session:
            return activate_enrollment(
                session, self.ctx,
                enrollment_id=enrollment_id,
                activation_preview_id=preview_id,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )


class TestActivateEnrollment(ActivationFlowTestBase):
    def test_activates_and_creates_first_episode(self):
        enrollment_id, revision = self._draft()
        enrollment, episode = self._activate(enrollment_id, revision, idempotency_key="activate-1")

        self.assertEqual(enrollment.lifecycle, "active")
        self.assertEqual(enrollment.revision, revision + 1)
        self.assertEqual(enrollment.current_episode_id, episode.id)

        self.assertEqual(episode.phase, "collecting")
        self.assertEqual(episode.enrollment_id, enrollment.id)
        self.assertIsNotNone(episode.metric_semantic_contract_set_id)

        with tenant_session(self.ctx) as session:
            delivery_source = session.get(
                ScaleDeliveryConfigSourceRecord, episode.delivery_config_baseline_source_id,
            )
            self.assertIsNotNone(delivery_source)
            self.assertEqual(delivery_source.episode_id, episode.id)

            enrollment_events = session.execute(
                select(ScaleEnrollmentEventRecord)
                .filter_by(enrollment_id=enrollment.id)
                .order_by(ScaleEnrollmentEventRecord.sequence)
            ).scalars().all()
            self.assertEqual(
                [e.event_type for e in enrollment_events], ["draft_created", "activated"],
            )

            episode_events = session.execute(
                select(ScaleEpisodeEventRecord).filter_by(episode_id=episode.id)
            ).scalars().all()
            self.assertEqual(len(episode_events), 1)
            self.assertEqual(episode_events[0].event_type, "opened")

    def test_rejects_activation_from_non_draft(self):
        """The enrollment is already active, so a fresh
        ``create_activation_preview`` call would itself reject it -- the
        second ``activate_enrollment`` call's own lifecycle check fires
        first regardless of the preview id's validity, matching what a
        real client would see if it raced two activate attempts (its
        second preview-creation call would already have failed)."""
        enrollment_id, revision = self._draft()
        enrollment, _episode = self._activate(enrollment_id, revision, idempotency_key="activate-1")
        with tenant_session(self.ctx) as session:
            with self.assertRaises(EnrollmentLifecycleError) as ctx:
                activate_enrollment(
                    session, self.ctx,
                    enrollment_id=enrollment_id,
                    activation_preview_id="does-not-matter",
                    expected_revision=enrollment.revision,
                    idempotency_key="activate-2",
                )
        self.assertEqual(ctx.exception.code, "invalid_transition")

    def test_rejects_stale_revision(self):
        enrollment_id, revision = self._draft()
        preview_id = self._create_preview(enrollment_id, revision)
        with tenant_session(self.ctx) as session:
            with self.assertRaises(EnrollmentLifecycleError) as ctx:
                activate_enrollment(
                    session, self.ctx,
                    enrollment_id=enrollment_id,
                    activation_preview_id=preview_id,
                    expected_revision=revision + 1,
                    idempotency_key="activate-stale",
                )
        self.assertEqual(ctx.exception.code, "revision_conflict")

    def test_idempotent_replay_returns_same_episode(self):
        """Replays the exact same ``activation_preview_id`` -- the second
        call's own ``claim_command`` replay short-circuits before ever
        touching the (already-consumed) preview again, exactly like a
        genuine client retry with the same Idempotency-Key would."""
        enrollment_id, revision = self._draft()
        preview_id = self._create_preview(enrollment_id, revision)
        with tenant_session(self.ctx) as session:
            enrollment, episode = activate_enrollment(
                session, self.ctx,
                enrollment_id=enrollment_id,
                activation_preview_id=preview_id,
                expected_revision=revision,
                idempotency_key="act-dup",
            )
        with tenant_session(self.ctx) as session:
            enrollment2, episode2 = activate_enrollment(
                session, self.ctx,
                enrollment_id=enrollment_id,
                activation_preview_id=preview_id,
                expected_revision=revision,
                idempotency_key="act-dup",
            )
        self.assertEqual(enrollment.id, enrollment2.id)
        self.assertEqual(episode.id, episode2.id)

    def test_rejects_duplicate_binding(self):
        """Both drafts are candidates against the exact same shared
        DeliveryTarget, so they share the same server-derived
        ``remote_binding_key`` -- exactly the scenario the active/paused
        binding-uniqueness gate exists for."""
        enrollment_a_id, revision_a = self._draft()
        self._activate(enrollment_a_id, revision_a, idempotency_key="activate-a")

        enrollment_b_id, revision_b = self._draft()
        preview_b_id = self._create_preview(enrollment_b_id, revision_b)
        with tenant_session(self.ctx) as session:
            with self.assertRaises(EnrollmentLifecycleError) as ctx:
                activate_enrollment(
                    session, self.ctx,
                    enrollment_id=enrollment_b_id,
                    activation_preview_id=preview_b_id,
                    expected_revision=revision_b,
                    idempotency_key="activate-b",
                )
        self.assertEqual(ctx.exception.code, "duplicate_binding")

    def test_binding_reusable_after_close(self):
        enrollment_a_id, revision_a = self._draft()
        enrollment_a, _ep = self._activate(enrollment_a_id, revision_a, idempotency_key="activate-a")
        with tenant_session(self.ctx) as session:
            close_enrollment(
                session, self.ctx, enrollment_a.id,
                expected_revision=enrollment_a.revision, idempotency_key="close-a",
            )

        enrollment_b_id, revision_b = self._draft()
        enrollment_b, episode_b = self._activate(enrollment_b_id, revision_b, idempotency_key="activate-b")
        self.assertEqual(enrollment_b.lifecycle, "active")
        self.assertEqual(episode_b.phase, "collecting")


class TestResumeEnrollment(ActivationFlowTestBase):
    def _paused(self) -> tuple[ScaleEnrollmentRecord, ScaleEpisodeRecord]:
        enrollment_id, revision = self._draft()
        enrollment, episode = self._activate(enrollment_id, revision, idempotency_key="activate-1")
        with tenant_session(self.ctx) as session:
            paused = pause_enrollment(
                session, self.ctx, enrollment.id,
                expected_revision=enrollment.revision, idempotency_key="pause-1",
            )
        return paused, episode

    def test_resume_continues_open_episode(self):
        paused, episode = self._paused()
        preview_id = self._create_preview(paused.id, paused.revision)
        with tenant_session(self.ctx) as session:
            resumed, resumed_episode = resume_enrollment(
                session, self.ctx,
                enrollment_id=paused.id,
                activation_preview_id=preview_id,
                expected_revision=paused.revision,
                idempotency_key="resume-1",
            )
        self.assertEqual(resumed.lifecycle, "active")
        self.assertEqual(resumed_episode.id, episode.id)
        self.assertEqual(resumed_episode.phase, "collecting")

    def test_resume_creates_new_episode_after_no_delivery_closure(self):
        enrollment_id, revision = self._draft()
        enrollment, episode = self._activate(enrollment_id, revision, idempotency_key="activate-1")
        with tenant_session(self.ctx) as session:
            paused, _closed_episode = apply_no_delivery_episode_closure(
                session, self.ctx, enrollment.id, episode.id,
                idempotency_key="no-delivery-1",
            )

        preview_id = self._create_preview(enrollment.id, paused.revision)
        with tenant_session(self.ctx) as session:
            resumed, new_episode = resume_enrollment(
                session, self.ctx,
                enrollment_id=enrollment.id,
                activation_preview_id=preview_id,
                expected_revision=paused.revision,
                idempotency_key="resume-new",
            )

        self.assertEqual(resumed.lifecycle, "active")
        self.assertNotEqual(new_episode.id, episode.id)
        self.assertEqual(new_episode.phase, "collecting")
        self.assertEqual(resumed.current_episode_id, new_episode.id)


class TestFullLifecycleViaPreview(ActivationFlowTestBase):
    """The draft -> active -> paused -> active -> closed path through the
    real preview protocol (module docstring). Mirrors
    ``tests/test_scale_router.py``'s HTTP-level
    ``test_full_lifecycle_activate_pause_resume_close``, at the domain layer."""

    def test_draft_active_paused_active_closed(self):
        enrollment_id, revision = self._draft()
        active, episode = self._activate(enrollment_id, revision, idempotency_key="activate-1")
        self.assertEqual(active.lifecycle, "active")

        with tenant_session(self.ctx) as session:
            paused = pause_enrollment(
                session, self.ctx, active.id,
                expected_revision=active.revision, idempotency_key="pause-1",
            )
        self.assertEqual(paused.lifecycle, "paused")

        resume_preview_id = self._create_preview(active.id, paused.revision)
        with tenant_session(self.ctx) as session:
            resumed, resumed_episode = resume_enrollment(
                session, self.ctx,
                enrollment_id=active.id,
                activation_preview_id=resume_preview_id,
                expected_revision=paused.revision,
                idempotency_key="resume-1",
            )
        self.assertEqual(resumed.lifecycle, "active")
        self.assertEqual(resumed_episode.id, episode.id)

        with tenant_session(self.ctx) as session:
            closed = close_enrollment(
                session, self.ctx, active.id,
                expected_revision=resumed.revision, idempotency_key="close-1",
            )
        self.assertEqual(closed.lifecycle, "closed")

        with tenant_session(self.ctx) as session:
            refreshed_episode = session.get(ScaleEpisodeRecord, episode.id)
            self.assertEqual(refreshed_episode.phase, "closed")
            self.assertEqual(refreshed_episode.closure_reason, "enrollment_closed")


class TestPauseResumeEnrollment(EnrollmentTestBase):
    """Pause only: ``resume_enrollment``'s own behavior (continuing an open
    Episode, building a fresh one after a no-delivery closure) now requires
    the real preview protocol -- see ``TestResumeEnrollment`` above."""

    def _active(self):
        draft = self._draft()
        return self._activate(draft)

    def test_pause_retains_binding_and_episode(self):
        enrollment, episode = self._active()
        initial_generation = enrollment.evaluation_cancellation_generation

        paused = pause_enrollment(
            self.session, self.context, enrollment.id,
            expected_revision=enrollment.revision, idempotency_key="pause-1",
        )
        self.session.flush()

        self.assertEqual(paused.lifecycle, "paused")
        self.assertEqual(paused.current_episode_id, episode.id)
        self.assertEqual(paused.evaluation_cancellation_generation, initial_generation + 1)

        # Episode untouched.
        self.session.refresh(episode)
        self.assertEqual(episode.phase, "collecting")

    def test_pause_rejects_from_draft(self):
        draft = self._draft()
        with self.assertRaises(EnrollmentLifecycleError) as ctx:
            pause_enrollment(
                self.session, self.context, draft.id,
                expected_revision=draft.revision, idempotency_key="pause-draft",
            )
        self.assertEqual(ctx.exception.code, "invalid_transition")


class TestCloseEnrollment(EnrollmentTestBase):
    def test_close_closes_episode_and_releases_binding(self):
        draft = self._draft()
        enrollment, episode = self._activate(draft)

        closed = close_enrollment(
            self.session, self.context, enrollment.id,
            expected_revision=enrollment.revision, idempotency_key="close-1",
        )
        self.session.flush()

        self.assertEqual(closed.lifecycle, "closed")
        self.assertIsNotNone(closed.closed_at)

        self.session.refresh(episode)
        self.assertEqual(episode.phase, "closed")
        self.assertEqual(episode.closure_reason, "enrollment_closed")
        self.assertIsNotNone(episode.ended_at)

    def test_close_rejects_from_draft(self):
        draft = self._draft()
        with self.assertRaises(EnrollmentLifecycleError) as ctx:
            close_enrollment(
                self.session, self.context, draft.id,
                expected_revision=draft.revision, idempotency_key="close-draft",
            )
        self.assertEqual(ctx.exception.code, "invalid_transition")

    def test_close_is_idempotent(self):
        draft = self._draft()
        enrollment, _episode = self._activate(draft)
        first = close_enrollment(
            self.session, self.context, enrollment.id,
            expected_revision=enrollment.revision, idempotency_key="close-dup",
        )
        self.session.flush()
        second = close_enrollment(
            self.session, self.context, enrollment.id,
            expected_revision=enrollment.revision, idempotency_key="close-dup",
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.lifecycle, "closed")


class TestUpstreamWithdrawal(EnrollmentTestBase):
    def test_withdrawal_closes_enrollment_and_episode(self):
        draft = self._draft()
        enrollment, episode = self._activate(draft)
        initial_generation = enrollment.evaluation_cancellation_generation

        closed = handle_upstream_withdrawal(
            self.session, self.context, enrollment.id,
            idempotency_key="upstream-evt-1",
        )
        self.session.flush()

        self.assertEqual(closed.lifecycle, "closed")
        self.assertEqual(closed.transition_reason, "upstream_withdrawal")
        self.assertEqual(closed.evaluation_cancellation_generation, initial_generation + 1)

        self.session.refresh(episode)
        self.assertEqual(episode.phase, "closed")
        self.assertEqual(episode.closure_reason, "upstream_withdrawal")

    def test_withdrawal_from_paused_also_closes(self):
        draft = self._draft()
        enrollment, episode = self._activate(draft)
        paused = pause_enrollment(
            self.session, self.context, enrollment.id,
            expected_revision=enrollment.revision, idempotency_key="pause-1",
        )
        self.session.flush()

        closed = handle_upstream_withdrawal(
            self.session, self.context, paused.id,
            idempotency_key="upstream-evt-2",
        )
        self.session.flush()
        self.assertEqual(closed.lifecycle, "closed")

    def test_withdrawal_is_idempotent_on_semantic_event_id(self):
        draft = self._draft()
        enrollment, _episode = self._activate(draft)

        first = handle_upstream_withdrawal(
            self.session, self.context, enrollment.id,
            idempotency_key="upstream-evt-dup",
        )
        self.session.flush()
        second = handle_upstream_withdrawal(
            self.session, self.context, enrollment.id,
            idempotency_key="upstream-evt-dup",
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.lifecycle, "closed")

        events = self.session.execute(
            select(ScaleEnrollmentEventRecord).filter_by(
                enrollment_id=enrollment.id, event_type="upstream_withdrawal",
            )
        ).scalars().all()
        self.assertEqual(len(events), 1)

    def test_withdrawal_from_draft_rejected(self):
        draft = self._draft()
        with self.assertRaises(EnrollmentLifecycleError) as ctx:
            handle_upstream_withdrawal(
                self.session, self.context, draft.id, idempotency_key="upstream-draft",
            )
        self.assertEqual(ctx.exception.code, "not_withdrawable")

    def test_binding_reusable_after_withdrawal(self):
        draft_a = self._draft(idempotency_key="draft-a")
        enrollment_a, _ep = self._activate(draft_a, idempotency_key="activate-a")
        handle_upstream_withdrawal(
            self.session, self.context, enrollment_a.id, idempotency_key="upstream-a",
        )
        self.session.flush()

        draft_b = self._draft(idempotency_key="draft-b", winner_decision_id="wd_2")
        enrollment_b, episode_b = self._activate(draft_b, idempotency_key="activate-b")
        self.assertEqual(enrollment_b.lifecycle, "active")
        self.assertEqual(episode_b.phase, "collecting")


class TestNoDeliveryEpisodeClosure(EnrollmentTestBase):
    def test_closes_episode_and_pauses_enrollment(self):
        draft = self._draft()
        enrollment, episode = self._activate(draft)

        paused, closed_episode = apply_no_delivery_episode_closure(
            self.session, self.context, enrollment.id, episode.id,
            idempotency_key="no-delivery-1",
        )
        self.session.flush()

        self.assertEqual(paused.lifecycle, "paused")
        self.assertEqual(paused.transition_reason, "no_delivery_7_complete_days")
        self.assertEqual(paused.current_episode_id, episode.id)

        self.assertEqual(closed_episode.phase, "closed")
        self.assertEqual(closed_episode.closure_reason, "no_delivery_7_complete_days")
        self.assertIsNotNone(closed_episode.ended_at)

    def test_rejects_episode_mismatch(self):
        draft = self._draft()
        enrollment, episode = self._activate(draft)
        with self.assertRaises(EnrollmentLifecycleError) as ctx:
            apply_no_delivery_episode_closure(
                self.session, self.context, enrollment.id, "sep_not_current",
                idempotency_key="no-delivery-2",
            )
        self.assertEqual(ctx.exception.code, "episode_mismatch")

    def test_rejects_when_episode_already_closed(self):
        draft = self._draft()
        enrollment, episode = self._activate(draft)
        close_enrollment(
            self.session, self.context, enrollment.id,
            expected_revision=enrollment.revision, idempotency_key="close-1",
        )
        self.session.flush()
        self.session.refresh(enrollment)
        # Enrollment is closed now, so lifecycle gate fires first.
        with self.assertRaises(EnrollmentLifecycleError) as ctx:
            apply_no_delivery_episode_closure(
                self.session, self.context, enrollment.id, episode.id,
                idempotency_key="no-delivery-3",
            )
        self.assertEqual(ctx.exception.code, "invalid_transition")

    def test_is_idempotent(self):
        draft = self._draft()
        enrollment, episode = self._activate(draft)

        first, _ = apply_no_delivery_episode_closure(
            self.session, self.context, enrollment.id, episode.id,
            idempotency_key="no-delivery-dup",
        )
        self.session.flush()
        second, _ = apply_no_delivery_episode_closure(
            self.session, self.context, enrollment.id, episode.id,
            idempotency_key="no-delivery-dup",
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.lifecycle, "paused")


if __name__ == "__main__":
    unittest.main()
