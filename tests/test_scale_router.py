"""HTTP-level tests for the Scale v1 sub-router (Issue #25, Task 15;
route topology restructured under Issue #86 Task 6).

Spec: docs/product/scale-advice-semantics-contract.md §4.1-§4.3, ADR 0008
Spec (route restructure): docs/superpowers/specs/2026-09-14-harden-scale-
enrollment-lineage-design.md §3.10-§3.11
Task: .superpowers/sdd/2026-09-14-harden-scale-enrollment-lineage/task-6-brief.md

Exercises the real FastAPI app via ``TestClient`` with ``request_tenant_context``
overridden to a fixed ``TenantContext`` -- the same pattern
``tests/test_v1_library_routes.py`` uses -- so requests hit the actual route
bodies, ``tenant_session``, and the RFC 9457 problem-handler wiring
registered in ``app/main.py`` (both the app-wide
``app.library.api.register_problem_handlers`` and this task's own
``app.scale.v1_router.register_scale_v1_problem_handlers``), rather than
calling ``app.scale.enrollment``/``app.scale.advice`` directly.

**Task 6 route topology**: every mode-scoped route now takes ``meta_mode``
exclusively as a URL path segment (``/api/v1/scale/{meta_mode}/...``), never
as a query parameter or request-body field; ``available-meta-modes`` alone
stays at the root (no ``meta_mode`` segment). The platform-scoped mirror at
``/api/platform/v1/scale/{meta_mode}/...`` (``app.scale.platform_router``)
is JWT-only through ``require_platform_operator_jwt`` -- its own tests below
follow ``tests/test_platform_operator_identity.py``'s PO16-23 pattern of
setting ``app.state.platform_operator_jwt_verifier``/``app.state.
admission_reader`` directly (plain attributes, not FastAPI dependencies)
rather than ``app.dependency_overrides``, since that is the real seam
``app.scale.platform_router._platform_context`` reads.

Suites:

* ``TestScaleRouterMount`` -- the router import/mount contract.
* ``TestEnrollmentLifecycleRoutes`` -- the full draft -> active -> paused ->
  active -> closed HTTP lifecycle, ETag/If-Match on every command, 404/409/
  428/422 problem responses, and cross-tenant isolation, all against
  path-mode URLs.
* ``TestAdviceRoutes`` -- disposition/snooze/convert against a Thread and
  two Occurrences seeded directly through ``app.scale.advice.
  process_advice_candidates`` (mirrors ``tests/test_scale_advice.py``'s own
  fixture-building, since no HTTP route in this task's scope drives
  evidence sealing/evaluation -- that pipeline is scheduler/Worker-driven,
  per ``app.scale.v1_router``'s own module docstring).
* ``TestMetaModeGate`` -- the meta-mode authorization gate wired through
  the new path-based ``_mode_effective_context`` seam.
* ``TestStubRoutes`` -- the remaining 501 stubs (Targets, Exports,
  Command-outcomes, Breakdowns), now under ``{meta_mode}``. Enrollment
  Candidates (issue #86 Task 9) is no longer a stub -- see
  ``test_enrollment_candidates_list_is_real``.
* ``TestRouteTopologyRestructure`` -- Task 6 specifics: old query-mode
  routes are gone, path-mode routes exist, ``available-meta-modes`` stays
  root-level, invalid path modes are rejected by FastAPI itself (422).
* ``TestScaleProblem*`` -- #368: every Scale domain error answers with the
  full app-level problem (``trace_id``, ``retryable``, ``X-Trace-Id``) the
  BFF's ``ProblemDetails`` validator requires, with its Workspace and role
  boundaries. #381: a malformed Target amount is a 422 on its field and a
  malformed cursor a 400 ``invalid_cursor``, never a 500.
* ``TestPlatformScaleRouter*`` -- the new ``/api/platform/v1/scale/*``
  mirror: JWT-only auth (PO20), meta-mode gating for platform identities,
  and a smoke test of the full Enrollment create/list/activate path.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

# Import order matters (mirrors tests/support/library_fixtures.py): these
# env vars must land before `from app.main import app` / `from app import
# config`, both of which read them at import time.
_TEST_ROOT = Path(tempfile.mkdtemp(prefix="marketai-scale-router-"))
os.environ.setdefault("MARKETAI_TEST_ROOT", str(_TEST_ROOT))
os.environ.setdefault("MARKETAI_DATA_DIR", str(_TEST_ROOT / "data"))
os.environ.setdefault("MARKETAI_DATABASE_URL", f"sqlite:///{_TEST_ROOT / 'marketai.db'}")
os.environ.setdefault("MARKETAI_WORKSPACE_ID", "ws_test")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config  # noqa: E402
from app.adapters.persistence.database import SessionLocal, platform_session, tenant_session  # noqa: E402
from app.adapters.persistence.models import ProductRecord, WorkspaceRecord, new_id  # noqa: E402
from app.platform.http_identity import request_tenant_context  # noqa: E402
from app.platform.pagination import encode_cursor  # noqa: E402
from app.platform.platform_operator_identity import (  # noqa: E402
    PlatformOperatorIdentity,
    PlatformTokenError,
)
from app.platform.tenancy import TenantContext  # noqa: E402
from app.platform.workspace_admission import WorkspaceAdmission  # noqa: E402
from app.scale._contracts import AdviceCandidate, AdviceEvidenceItem  # noqa: E402
from app.scale.advice import process_advice_candidates  # noqa: E402
from app.scale.candidates import create_enrollment_candidate  # noqa: E402
from app.scale.models import (  # noqa: E402
    NO_TARGET_MARKER,
    ScaleAdviceDispositionRecord,
    ScaleAdviceSnoozeRecord,
    ScaleAdviceThreadRecord,
    ScaleEnrollmentRecord,
    ScaleEpisodeRecord,
    ScaleEvaluationAdviceCandidateRecord,
    ScaleEvaluationRecord,
    ScalePolicyVersionRecord,
)
from app.scale.types import AdviceSeverity, AdviceType  # noqa: E402
from tests.support.scale_candidate_fixtures import (  # noqa: E402
    build_ready_winner_and_target,
    ensure_governance_prereqs,
    seed_activation_evidence_prereqs,
)
from tests.test_scale_breakdowns import (  # noqa: E402
    _add_segment,
    _seed_enrollment_and_episode,
    _seed_sealed_snapshot,
)


class TestScaleRouterMount(unittest.TestCase):
    def test_router_prefix(self):
        from app.scale.v1_router import router
        self.assertEqual(router.prefix, "/scale")

    def test_included_in_v1(self):
        # ``app.routes`` on this FastAPI version mixes flattened ``APIRoute``
        # objects with lazy ``_IncludedRouter`` wrappers that carry no
        # ``.path`` attribute of their own; the generated OpenAPI schema is
        # the stable, version-independent view of the actually-mounted paths.
        from app.main import app
        paths = list(app.openapi()["paths"].keys())
        self.assertTrue(any("/api/v1/scale" in p for p in paths))

    def test_mode_scoped_paths_include_meta_mode_segment(self):
        """Task 6, B6/B8: every mode-scoped Scale route is mounted under
        ``/api/v1/scale/{meta_mode}/...``, not as a flat path."""
        from app.main import app
        paths = list(app.openapi()["paths"].keys())
        self.assertIn("/api/v1/scale/available-meta-modes", paths)
        self.assertIn("/api/v1/scale/{meta_mode}/enrollments", paths)
        self.assertIn("/api/v1/scale/{meta_mode}/advice-threads", paths)
        self.assertIn("/api/v1/scale/{meta_mode}/targets", paths)
        self.assertNotIn("/api/v1/scale/enrollments", paths)
        self.assertNotIn("/api/v1/scale/advice-threads", paths)

    def test_platform_scale_paths_are_mounted(self):
        from app.main import app
        paths = list(app.openapi()["paths"].keys())
        self.assertTrue(any(p.startswith("/api/platform/v1/scale/") for p in paths))


class TestEvidenceProjectionSerialization(unittest.TestCase):
    def test_exposes_exact_windows_versions_quality_and_decimal_signals(self):
        from app.scale.v1_router import _evidence_detail_dict

        evaluation = SimpleNamespace(
            policy_version_id="policy-1",
            target_version_id="target-1",
            algorithm_version="beta-v1",
            metric_semantic_contract_set_hash="contract-hash",
            data_quality_state="warning",
        )
        snapshot = SimpleNamespace(
            id="snapshot-1",
            safe_watermark=date(2026, 9, 7),
            last_complete_local_day=date(2026, 9, 7),
            last_sync_at=datetime(2026, 9, 8, tzinfo=timezone.utc),
            freshness_reference_at=datetime(2026, 9, 8, 1, tzinfo=timezone.utc),
            complete_local_days_in_episode=14,
            active_delivery_days_in_episode=12,
            current_window_start_local_day=date(2026, 9, 1),
            current_window_end_local_day=date(2026, 9, 8),
            baseline_window_start_local_day=date(2026, 8, 25),
            baseline_window_end_local_day=date(2026, 9, 1),
            rolling_window_start_local_day=date(2026, 8, 25),
            rolling_window_end_local_day=date(2026, 9, 8),
        )
        issue = SimpleNamespace(
            code="contract_generation_warning",
            severity="warning",
            is_blocking=False,
            contract_type="daily",
            window_role="current_7d",
            local_day=None,
            segment_key=None,
            detail="Compatible contract generation changed.",
            recovery_action="refresh_evidence",
        )
        signal = SimpleNamespace(
            signal_id="ctr_relative_change",
            state="hit",
            expected=Decimal("-0.25"),
            observed=Decimal("-0.31"),
            interval_lower=Decimal("-0.40"),
            interval_upper=Decimal("-0.22"),
            threshold=Decimal("-0.25"),
            p_value=Decimal("0.01"),
            adjusted_q_value=None,
            reason_code="ctr_decline_supported",
        )

        payload = _evidence_detail_dict(evaluation, snapshot, [issue], [signal])

        self.assertEqual(payload["current_window"]["start_local_day"], date(2026, 9, 1))
        self.assertEqual(payload["policy_version_id"], "policy-1")
        self.assertEqual(payload["data_quality_state"], "warning")
        self.assertEqual(payload["quality_issues"][0]["recovery_action"], "refresh_evidence")
        self.assertEqual(payload["signals"][0]["observed"], "-0.31")
        self.assertEqual(payload["signals"][0]["interval_lower"], "-0.40")


class ScaleRouterTestBase(unittest.TestCase):
    """Shared HTTP fixture: one migrated temp database, one Workspace per
    test, a dependency-overridden ``request_tenant_context``."""

    workspace_seq = 0
    # A delegated user (role="owner") is not authorized for internal_validation
    # (ADR 0009, `app.platform.meta_mode_auth.require_meta_mode`); these
    # fixtures exercise generic Enrollment/Advice CRUD, not meta-mode
    # authorization itself (see tests/test_meta_mode_auth.py for that), so
    # they use a mode this identity is permitted to select.
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
        from app.main import app

        ScaleRouterTestBase.workspace_seq += 1
        self.workspace_id = f"ws_scale_router_{ScaleRouterTestBase.workspace_seq}"
        with platform_session() as session:
            session.add(WorkspaceRecord(id=self.workspace_id, name=self.workspace_id))

        # Bound to the HTTP layer via dependency override: carries no
        # meta_mode (matches request_tenant_context's own real-world shape;
        # every route binds meta_mode itself, per the module docstring).
        self.ctx = TenantContext(
            workspace_id=self.workspace_id, role="owner", initiator_id="user_1",
            request_id="test-req", trace_id="trace-test",
        )
        self.app = app
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    # -- fixture-building helpers (bypass HTTP; used to seed Advice rows) --

    def _raw_session(self):
        session = SessionLocal()
        self.addCleanup(session.close)
        return session

    def _switch_to_other_tenant(self) -> str:
        """Override the dependency to a second, freshly-provisioned
        Workspace -- everything created under ``self.workspace_id`` must be
        invisible (empty list / 404) once this is called."""
        other_ws = f"{self.workspace_id}-other"
        with platform_session() as session:
            session.add(WorkspaceRecord(id=other_ws, name=other_ws))
        self.app.dependency_overrides[request_tenant_context] = lambda: TenantContext(
            workspace_id=other_ws, role="owner", initiator_id="user_2",
        )
        return other_ws


# ---------------------------------------------------------------------------
# Enrollment lifecycle
# ---------------------------------------------------------------------------


def _enrollment_body(**overrides) -> dict:
    body = dict(
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
    body.update(overrides)
    return body


class EnrollmentChainTestBase(ScaleRouterTestBase):
    """Issue #86 Task 9: ``/enrollments`` now requires a genuine,
    server-proven ``enrollment_candidate_id`` -- ``_create()`` builds one
    fresh Candidate per call against a shared, class-scoped ready Winner/
    DeliveryTarget pair (``tests/support/scale_candidate_fixtures``; the
    heavy Localization Batch chain that pair needs is built once in
    ``setUp`` and re-consumed candidate-by-candidate, exactly as
    ``ScaleLineageGuardRecord`` itself is designed to be reused across
    repeat Candidate creations for the same pair -- see that record's
    docstring).

    Issue #86 Task 11: ``activate``/``resume`` are now gated entirely by a
    server-consumed ``ScaleActivationPreviewRecord`` -- ``_create_preview()``
    drives the real ``POST .../activation-previews`` route (never fabricates
    one), and ``seed_activation_evidence_prereqs`` (``tests/support/
    scale_candidate_fixtures``) seeds the attestation + metric-semantic-
    contract-set membership that preview creation's
    ``resolve_activation_evidence`` call requires."""

    def setUp(self) -> None:
        super().setUp()
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
        candidate_ctx = TenantContext(
            workspace_id=self.ctx.workspace_id, role=self.ctx.role,
            initiator_id=self.ctx.initiator_id, meta_mode=self.mode,
        )
        with tenant_session(candidate_ctx) as session:
            candidate = create_enrollment_candidate(
                session, candidate_ctx,
                decision_id=self._chain["decision_id"],
                delivery_target_id=self._chain["delivery_target_id"],
                idempotency_key=new_id("candidate"),
            )
            return candidate.id

    def _create(self) -> dict:
        candidate_id = self._new_candidate_id()
        resp = self.client.post(
            f"/api/v1/scale/{self.mode}/enrollments",
            json={"enrollment_candidate_id": candidate_id},
            headers={"Idempotency-Key": f"ik-create-{new_id('t')}"},
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()

    def _create_preview(self, enrollment_id: str, expected_revision: int) -> str:
        resp = self.client.post(
            f"/api/v1/scale/{self.mode}/enrollments/{enrollment_id}/activation-previews",
            json={"expected_revision": expected_revision},
            headers={"Idempotency-Key": f"ik-preview-{new_id('t')}"},
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()["id"]


class TestEnrollmentLifecycleRoutes(EnrollmentChainTestBase):
    """The Enrollment lifecycle over HTTP, on the chain above."""

    def test_create_requires_idempotency_key(self):
        resp = self.client.post(
            f"/api/v1/scale/{self.mode}/enrollments",
            json={"enrollment_candidate_id": self._new_candidate_id()},
        )
        self.assertEqual(resp.status_code, 422)

    def test_create_returns_draft_with_activate_only_allowed(self):
        enrollment = self._create()
        self.assertEqual(enrollment["lifecycle"], "draft")
        self.assertEqual(enrollment["revision"], 1)
        actions = {a["action"]: a["allowed"] for a in enrollment["allowed_actions"]}
        self.assertEqual(actions, {"activate": True, "pause": False, "resume": False, "close": False})

    def test_list_enrollments_scoped_by_meta_mode(self):
        self._create()
        self._create()
        resp = self.client.get(f"/api/v1/scale/{self.mode}/enrollments")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(len(resp.json()["items"]), 2)

    def test_get_enrollment_returns_etag(self):
        enrollment = self._create()
        resp = self.client.get(f"/api/v1/scale/{self.mode}/enrollments/{enrollment['id']}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["ETag"], '"1"')

    def test_get_unknown_enrollment_returns_404(self):
        resp = self.client.get(f"/api/v1/scale/{self.mode}/enrollments/does-not-exist")
        self.assertEqual(resp.status_code, 404)

    def test_cross_tenant_enrollment_is_not_found(self):
        enrollment = self._create()
        self._switch_to_other_tenant()
        resp = self.client.get(f"/api/v1/scale/{self.mode}/enrollments/{enrollment['id']}")
        self.assertEqual(resp.status_code, 404)

    def test_list_enrollments_cross_tenant_returns_empty(self):
        self._create()
        self._create()
        self._switch_to_other_tenant()
        resp = self.client.get(f"/api/v1/scale/{self.mode}/enrollments")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["items"], [])

    def test_activate_enrollment_cross_tenant_returns_404(self):
        enrollment = self._create()
        self._switch_to_other_tenant()
        resp = self.client.put(
            f"/api/v1/scale/{self.mode}/enrollments/{enrollment['id']}/activate",
            json={"activation_preview_id": "does-not-matter"},
            headers={"If-Match": '"1"', "Idempotency-Key": "ik-activate-cross-tenant"},
        )
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(resp.json()["code"], "enrollment_not_found")

    def test_activate_without_if_match_returns_428(self):
        enrollment = self._create()
        resp = self.client.put(
            f"/api/v1/scale/{self.mode}/enrollments/{enrollment['id']}/activate",
            json={"activation_preview_id": "does-not-matter"},
            headers={"Idempotency-Key": "ik-activate-1"},
        )
        self.assertEqual(resp.status_code, 428)
        self.assertEqual(resp.headers["content-type"], "application/problem+json")
        self.assertEqual(resp.json()["code"], "precondition_required")

    def test_full_lifecycle_activate_pause_resume_close(self):
        enrollment = self._create()
        enrollment_id = enrollment["id"]

        activate_preview_id = self._create_preview(enrollment_id, 1)
        activate_resp = self.client.put(
            f"/api/v1/scale/{self.mode}/enrollments/{enrollment_id}/activate",
            json={"activation_preview_id": activate_preview_id},
            headers={"If-Match": '"1"', "Idempotency-Key": "ik-activate-1"},
        )
        self.assertEqual(activate_resp.status_code, 200, activate_resp.text)
        active = activate_resp.json()
        self.assertEqual(active["lifecycle"], "active")
        self.assertEqual(active["revision"], 2)
        self.assertIsNotNone(active["current_episode_id"])
        actions = {a["action"]: a["allowed"] for a in active["allowed_actions"]}
        self.assertEqual(actions, {"activate": False, "pause": True, "resume": False, "close": True})

        episode_resp = self.client.get(
            f"/api/v1/scale/{self.mode}/enrollments/{enrollment_id}"
            f"/episodes/{active['current_episode_id']}",
        )
        self.assertEqual(episode_resp.status_code, 200, episode_resp.text)
        self.assertEqual(episode_resp.json()["phase"], "collecting")

        evidence_resp = self.client.get(
            f"/api/v1/scale/{self.mode}/enrollments/{enrollment_id}/evidence",
        )
        self.assertEqual(evidence_resp.status_code, 404)
        self.assertEqual(evidence_resp.json()["code"], "evidence_not_available")

        stale_pause_resp = self.client.put(
            f"/api/v1/scale/{self.mode}/enrollments/{enrollment_id}/pause",
            json={},
            headers={"If-Match": '"1"', "Idempotency-Key": "ik-pause-1"},
        )
        self.assertEqual(stale_pause_resp.status_code, 409, stale_pause_resp.text)
        self.assertEqual(stale_pause_resp.json()["code"], "revision_conflict")

        pause_resp = self.client.put(
            f"/api/v1/scale/{self.mode}/enrollments/{enrollment_id}/pause",
            json={},
            headers={"If-Match": '"2"', "Idempotency-Key": "ik-pause-1"},
        )
        self.assertEqual(pause_resp.status_code, 200, pause_resp.text)
        paused = pause_resp.json()
        self.assertEqual(paused["lifecycle"], "paused")
        self.assertEqual(paused["revision"], 3)

        resume_preview_id = self._create_preview(enrollment_id, 3)
        resume_resp = self.client.put(
            f"/api/v1/scale/{self.mode}/enrollments/{enrollment_id}/resume",
            json={"activation_preview_id": resume_preview_id},
            headers={"If-Match": '"3"', "Idempotency-Key": "ik-resume-1"},
        )
        self.assertEqual(resume_resp.status_code, 200, resume_resp.text)
        resumed = resume_resp.json()
        self.assertEqual(resumed["lifecycle"], "active")
        self.assertEqual(resumed["revision"], 4)
        self.assertEqual(resumed["current_episode_id"], active["current_episode_id"])

        close_resp = self.client.put(
            f"/api/v1/scale/{self.mode}/enrollments/{enrollment_id}/close",
            json={},
            headers={"If-Match": '"4"', "Idempotency-Key": "ik-close-1"},
        )
        self.assertEqual(close_resp.status_code, 200, close_resp.text)
        closed = close_resp.json()
        self.assertEqual(closed["lifecycle"], "closed")
        self.assertEqual(closed["revision"], 5)
        self.assertIsNotNone(closed["closed_at"])
        actions = {a["action"]: a["allowed"] for a in closed["allowed_actions"]}
        self.assertEqual(actions, {"activate": False, "pause": False, "resume": False, "close": False})

    def test_activate_invalid_transition_returns_409(self):
        """Activating an already-active Enrollment (a second activate call
        with a fresh Idempotency-Key, so it is not the idempotent-replay
        path) is a 409 domain rejection, not a 500."""
        enrollment = self._create()
        enrollment_id = enrollment["id"]
        first_preview_id = self._create_preview(enrollment_id, 1)
        self.client.put(
            f"/api/v1/scale/{self.mode}/enrollments/{enrollment_id}/activate",
            json={"activation_preview_id": first_preview_id},
            headers={"If-Match": '"1"', "Idempotency-Key": "ik-activate-first"},
        )
        # No second preview is created: the Enrollment is already active, so
        # `create_activation_preview` itself would reject it -- the second
        # activate call's own lifecycle check fires first regardless of
        # this value.
        second = self.client.put(
            f"/api/v1/scale/{self.mode}/enrollments/{enrollment_id}/activate",
            json={"activation_preview_id": "does-not-matter"},
            headers={"If-Match": '"2"', "Idempotency-Key": "ik-activate-second"},
        )
        self.assertEqual(second.status_code, 409, second.text)
        self.assertEqual(second.json()["code"], "invalid_transition")

    def test_activate_rejects_delivery_config_in_body(self):
        """Issue #86 Task 11: the pre-hardening ``delivery_config``/
        ``episode_context`` shape is now an unknown field -- 422, not
        silently ignored."""
        enrollment = self._create()
        resp = self.client.put(
            f"/api/v1/scale/{self.mode}/enrollments/{enrollment['id']}/activate",
            json={
                "activation_preview_id": "does-not-matter",
                "delivery_config": {"contract_generation_id": "gen-1"},
            },
            headers={"If-Match": '"1"', "Idempotency-Key": "ik-activate-extra-field"},
        )
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_resume_rejects_delivery_config_in_body(self):
        enrollment = self._create()
        resp = self.client.put(
            f"/api/v1/scale/{self.mode}/enrollments/{enrollment['id']}/resume",
            json={
                "activation_preview_id": "does-not-matter",
                "delivery_config": {},
            },
            headers={"If-Match": '"1"', "Idempotency-Key": "ik-resume-extra-field"},
        )
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_create_rejects_unknown_field(self):
        body = _enrollment_body()
        body["unexpected_field"] = "nope"
        resp = self.client.post(
            f"/api/v1/scale/{self.mode}/enrollments", json=body, headers={"Idempotency-Key": "ik-extra"},
        )
        self.assertEqual(resp.status_code, 422)

    def test_create_rejects_meta_mode_in_body(self):
        """Task 6: ``meta_mode`` is exclusively a URL path segment now --
        a body still carrying it is an unknown/extra field (422), the
        same as any other stray field (``extra='forbid'``)."""
        body = _enrollment_body(meta_mode=self.mode)
        resp = self.client.post(
            f"/api/v1/scale/{self.mode}/enrollments", json=body, headers={"Idempotency-Key": "ik-meta-mode-body"},
        )
        self.assertEqual(resp.status_code, 422)


# ---------------------------------------------------------------------------
# Advice
# ---------------------------------------------------------------------------


_SIGNAL_THRESHOLDS = {
    "confidence_level": "0.95",
    "confidence_z": "1.96",
}


class TestAdviceRoutes(ScaleRouterTestBase):
    def setUp(self) -> None:
        super().setUp()
        session = self._raw_session()

        product_id = new_id("prod")
        product = ProductRecord(id=product_id, workspace_id=self.workspace_id, name="Product 1")
        session.add(product)

        policy = ScalePolicyVersionRecord(
            id=new_id("spol"),
            algorithm_version="beta-v1",
            canonical_schema_version="scale-snapshot-v1",
            decimal_precision=28,
            decimal_rounding="round_half_even",
            z_value=Decimal("1.96"),
            evidence_floor_minimum_spend=Decimal("100"),
            evidence_floor_minimum_days=14,
            evidence_floor_minimum_conversions=5,
            activity_floor_minimum_impressions=1000,
            sample_floor_minimum_clicks=20,
            spend_floor_minimum_daily_spend=Decimal("100"),
            freshness_floor_maximum_lag_hours=24,
            signal_thresholds=dict(_SIGNAL_THRESHOLDS),
            streak_rules={},
            bootstrap_prng_algorithm="bootstrap-prng-v1",
            bootstrap_draw_count=200,
            bootstrap_valid_draw_ratio=Decimal("0.5"),
            bootstrap_percentile_method="nearest_rank",
            bootstrap_draw_order="sequential",
            breakdown_families={},
            breakdown_sample_gate_minimum=1,
            breakdown_share_gate_minimum_ratio=Decimal("0.05"),
            bh_fdr_alpha=Decimal("0.10"),
            advice_severity_thresholds={},
            notification_cooldown_hours=24,
            conversion_safe_values={},
            mode_permissions={},
            is_published=True,
            created_by="user_1",
            published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        session.add(policy)

        enrollment = ScaleEnrollmentRecord(
            id=new_id("senr"),
            workspace_id=self.workspace_id,
            meta_mode=self.mode,
            meta_connection_id="mcon_1",
            scenario_scope_key="__no_scenario__",
            ad_account_id="act_1",
            winner_decision_id="wd_1",
            winner_confirmation_id="wc_1",
            asset_version_id="av_1",
            product_version_id=product_id,
            offer_version_id="ov_1",
            market_code="US",
            remote_binding_key="mcon_1/act_1/camp_1",
            lifecycle="active",
            creator_id="user_1",
            original_initiator_id="user_1",
        )
        session.add(enrollment)
        session.flush()

        episode = ScaleEpisodeRecord(
            id=new_id("sep"),
            workspace_id=self.workspace_id,
            meta_mode=self.mode,
            enrollment_id=enrollment.id,
            winner_decision_id="wd_1",
            winner_confirmation_id="wc_1",
            asset_version_id="av_1",
            product_version_id=product_id,
            offer_version_id="ov_1",
            market_code="US",
            meta_connection_id="mcon_1",
            scenario_scope_key="__no_scenario__",
            ad_account_id="act_1",
            delivery_config_baseline_source_id="sdcs_baseline",
            delivery_evidence_hash="e" * 64,
            delivery_comparability_hash="c" * 64,
            metric_semantic_contract_set_id="smcs_1",
            timezone_name="UTC",
            currency="USD",
            attribution_semantics_hash="a" * 64,
            phase="collecting",
            started_local_day=date(2026, 1, 1),
        )
        session.add(episode)
        enrollment.current_episode_id = episode.id
        session.flush()

        evaluation = ScaleEvaluationRecord(
            id=new_id("sev"),
            workspace_id=self.workspace_id,
            meta_mode=self.mode,
            episode_id=episode.id,
            snapshot_id="snap-1",
            snapshot_canonical_hash="hash-snap-1",
            metric_semantic_contract_set_hash="mcsh-1",
            policy_version_id=policy.id,
            target_version_id=None,
            normalized_target_key=NO_TARGET_MARKER,
            algorithm_version="beta-v1",
            canonical_schema_version="scale-eval-v1",
            maturity="economics_ready",
            data_quality_state="pass",
            health=None,
            health_reason="ok",
            streak_compat_policy_version_id=policy.id,
            streak_compat_target_version_id=None,
            streak_compat_metric_contract_set_hash="mcsh-1",
            streak_compat_configuration_baseline_hash="cbh-1",
            streak_compat_data_continuity_generation="gen-1",
            streak_compat_canonical_hash="cch-1",
            streak_healthy_evaluations=0,
            canonical_result_payload={},
            result_hash=new_id("rh"),
        )
        session.add(evaluation)
        session.flush()

        refresh_candidate = AdviceCandidate(
            advice_type=AdviceType.REFRESH_CREATIVE,
            reason_signature="rs-refresh",
            severity=AdviceSeverity.WARNING,
            signal_ids=("sig_1",),
            evidence=(AdviceEvidenceItem(key="observed", value=Decimal("1.5")),),
        )
        review_candidate = AdviceCandidate(
            advice_type=AdviceType.REVIEW_OFFER_OR_LANDING_PAGE,
            reason_signature="rs-review",
            severity=AdviceSeverity.INFO,
            signal_ids=("sig_2",),
            evidence=(AdviceEvidenceItem(key="observed", value=Decimal("2.0")),),
        )
        for candidate in (refresh_candidate, review_candidate):
            session.add(ScaleEvaluationAdviceCandidateRecord(
                id=new_id("seac"),
                workspace_id=self.workspace_id,
                meta_mode=self.mode,
                evaluation_id=evaluation.id,
                advice_type=candidate.advice_type.value,
                reason_signature=candidate.reason_signature,
                severity=candidate.severity.value,
                signal_ids=list(candidate.signal_ids),
                evidence=[{"key": e.key, "value": str(e.value)} for e in candidate.evidence],
            ))
        session.flush()
        session.commit()

        setup_context = TenantContext(
            workspace_id=self.workspace_id, role="system", executor="system:worker",
            initiator_id="user_1", meta_mode=self.mode,
        )
        thread_ids = process_advice_candidates(
            session, setup_context, episode.id, evaluation.id,
            [refresh_candidate, review_candidate],
            trigger_kind="live", evaluated_local_day=date(2026, 1, 1), projection_eligible=True,
        )
        self.assertEqual(len(thread_ids), 2)

        threads = {
            t.reason_signature: t
            for t in session.query(ScaleAdviceThreadRecord).filter_by(
                workspace_id=self.workspace_id, meta_mode=self.mode,
            )
        }
        self.refresh_thread_id = threads["rs-refresh"].id
        self.refresh_occurrence_id = threads["rs-refresh"].latest_occurrence_id
        self.review_thread_id = threads["rs-review"].id
        self.review_occurrence_id = threads["rs-review"].latest_occurrence_id

    def test_list_advice_threads_returns_convert_eligibility(self):
        resp = self.client.get(f"/api/v1/scale/{self.mode}/advice-threads")
        self.assertEqual(resp.status_code, 200, resp.text)
        by_id = {t["id"]: t for t in resp.json()["items"]}
        refresh_actions = {a["action"]: a["allowed"] for a in by_id[self.refresh_thread_id]["allowed_actions"]}
        review_actions = {a["action"]: a["allowed"] for a in by_id[self.review_thread_id]["allowed_actions"]}
        self.assertTrue(refresh_actions["convert"])
        self.assertFalse(review_actions["convert"])

    def test_list_advice_threads_cross_tenant_returns_empty(self):
        self._switch_to_other_tenant()
        resp = self.client.get(f"/api/v1/scale/{self.mode}/advice-threads")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["items"], [])

    def test_create_disposition(self):
        resp = self.client.post(
            f"/api/v1/scale/{self.mode}/advice-threads/{self.refresh_thread_id}/disposition",
            json={"kind": "acknowledged", "note": "looks fine"},
            headers={"Idempotency-Key": "ik-disposition-1"},
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        body = resp.json()
        self.assertEqual(body["kind"], "acknowledged")
        self.assertEqual(body["thread_id"], self.refresh_thread_id)

    def test_create_disposition_requires_idempotency_key(self):
        resp = self.client.post(
            f"/api/v1/scale/{self.mode}/advice-threads/{self.refresh_thread_id}/disposition",
            json={"kind": "acknowledged"},
        )
        self.assertEqual(resp.status_code, 422)

    def test_create_disposition_replay_returns_same_row_not_a_duplicate(self):
        """A browser timeout-retry replaying the same Idempotency-Key must
        not insert a second disposition row."""
        first = self.client.post(
            f"/api/v1/scale/{self.mode}/advice-threads/{self.refresh_thread_id}/disposition",
            json={"kind": "acknowledged", "note": "first"},
            headers={"Idempotency-Key": "ik-disposition-replay"},
        )
        self.assertEqual(first.status_code, 201, first.text)
        second = self.client.post(
            f"/api/v1/scale/{self.mode}/advice-threads/{self.refresh_thread_id}/disposition",
            json={"kind": "acknowledged", "note": "first"},
            headers={"Idempotency-Key": "ik-disposition-replay"},
        )
        self.assertEqual(second.status_code, 201, second.text)
        self.assertEqual(first.json()["id"], second.json()["id"])

        with platform_session() as session:
            count = session.query(ScaleAdviceDispositionRecord).filter_by(
                workspace_id=self.workspace_id, meta_mode=self.mode, thread_id=self.refresh_thread_id,
            ).count()
        self.assertEqual(count, 1)

    def test_create_disposition_rejects_converted_to_draft(self):
        resp = self.client.post(
            f"/api/v1/scale/{self.mode}/advice-threads/{self.refresh_thread_id}/disposition",
            json={"kind": "converted_to_draft"},
            headers={"Idempotency-Key": "ik-disposition-bad-kind"},
        )
        self.assertEqual(resp.status_code, 422, resp.text)
        self.assertEqual(resp.json()["code"], "unsupported_disposition_kind")

    def test_create_disposition_cross_tenant_returns_404(self):
        self._switch_to_other_tenant()
        resp = self.client.post(
            f"/api/v1/scale/{self.mode}/advice-threads/{self.refresh_thread_id}/disposition",
            json={"kind": "acknowledged"},
            headers={"Idempotency-Key": "ik-disposition-cross-tenant"},
        )
        self.assertEqual(resp.status_code, 404)

    def test_create_snooze(self):
        until = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        resp = self.client.post(
            f"/api/v1/scale/{self.mode}/advice-threads/{self.refresh_thread_id}/snooze",
            json={"until": until, "reason": "waiting on creative"},
            headers={"Idempotency-Key": "ik-snooze-1"},
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(resp.json()["thread_id"], self.refresh_thread_id)

    def test_create_snooze_requires_idempotency_key(self):
        until = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        resp = self.client.post(
            f"/api/v1/scale/{self.mode}/advice-threads/{self.refresh_thread_id}/snooze",
            json={"until": until},
        )
        self.assertEqual(resp.status_code, 422)

    def test_create_snooze_replay_returns_same_row_not_a_duplicate(self):
        until = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        first = self.client.post(
            f"/api/v1/scale/{self.mode}/advice-threads/{self.refresh_thread_id}/snooze",
            json={"until": until, "reason": "waiting"},
            headers={"Idempotency-Key": "ik-snooze-replay"},
        )
        self.assertEqual(first.status_code, 201, first.text)
        second = self.client.post(
            f"/api/v1/scale/{self.mode}/advice-threads/{self.refresh_thread_id}/snooze",
            json={"until": until, "reason": "waiting"},
            headers={"Idempotency-Key": "ik-snooze-replay"},
        )
        self.assertEqual(second.status_code, 201, second.text)
        self.assertEqual(first.json()["id"], second.json()["id"])

        with platform_session() as session:
            count = session.query(ScaleAdviceSnoozeRecord).filter_by(
                workspace_id=self.workspace_id, meta_mode=self.mode, thread_id=self.refresh_thread_id,
            ).count()
        self.assertEqual(count, 1)

    def test_convert_refresh_creative_occurrence_creates_generation_draft(self):
        resp = self.client.post(
            f"/api/v1/scale/{self.mode}/advice-occurrences/{self.refresh_occurrence_id}/convert",
            json={},
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        body = resp.json()
        self.assertIsNotNone(body["generation_draft_id"])
        self.assertIsNone(body["scale_test_plan_draft_id"])

    def test_convert_non_convertible_occurrence_returns_422(self):
        resp = self.client.post(
            f"/api/v1/scale/{self.mode}/advice-occurrences/{self.review_occurrence_id}/convert",
            json={},
        )
        self.assertEqual(resp.status_code, 422, resp.text)
        self.assertEqual(resp.json()["code"], "not_convertible")

    def test_convert_unknown_occurrence_returns_404(self):
        resp = self.client.post(
            f"/api/v1/scale/{self.mode}/advice-occurrences/does-not-exist/convert",
            json={},
        )
        self.assertEqual(resp.status_code, 404)


# ---------------------------------------------------------------------------
# Meta-mode authorization gate (Task 2, ADR 0009) -- HTTP-level wiring
# ---------------------------------------------------------------------------


class TestMetaModeGate(ScaleRouterTestBase):
    """``self.ctx`` (role="owner", no explicit identity_kind) is inferred as
    a delegated user (see ``app.platform.tenancy.TenantContext.
    __post_init__``); these tests confirm ``require_meta_mode`` is actually
    wired through the router's ``_mode_effective_context`` seam and the
    routers' ``except MetaModeNotPermitted`` handlers return the correct
    status code for each ``exc.code`` (see ``tests/test_meta_mode_auth.py``
    for the gate's own unit tests)."""

    def test_delegated_user_internal_validation_returns_403(self):
        resp = self.client.get("/api/v1/scale/internal_validation/enrollments")
        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertEqual(resp.json()["code"], "meta_mode_not_permitted")

    def test_unknown_meta_mode_returns_422(self):
        """Task 6: an unrecognized ``{meta_mode}`` path segment is rejected
        by FastAPI's own ``Path(pattern=...)`` validation (422) before any
        handler -- or ``require_meta_mode``'s own "unknown meta_mode" 400 --
        ever runs (spec §3.10)."""
        resp = self.client.get("/api/v1/scale/banana/enrollments")
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_available_meta_modes_returns_modes_list(self):
        resp = self.client.get("/api/v1/scale/available-meta-modes")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(set(resp.json()["modes"]), {"customer_sandbox", "controlled_mock"})


# ---------------------------------------------------------------------------
# Task 6: route topology restructure specifics
# ---------------------------------------------------------------------------


class TestRouteTopologyRestructure(ScaleRouterTestBase):
    """B6/B8: route paths include ``{meta_mode}``; the old flat/query-mode
    routes no longer exist; ``available-meta-modes`` stays root-level."""

    def test_enrollment_list_uses_path_mode(self):
        resp = self.client.get(f"/api/v1/scale/{self.mode}/enrollments")
        self.assertIn(resp.status_code, (200, 404))
        self.assertNotIn(resp.status_code, (405, 422))

    def test_old_flat_enrollments_route_returns_not_found(self):
        """The pre-Task-6 flat ``/scale/enrollments`` path (with or without
        a ``meta_mode`` query parameter) no longer resolves to any route --
        ``/enrollments`` is now interpreted as (and fails to match) a
        ``{meta_mode}`` path segment with nothing after it."""
        resp = self.client.get(f"/api/v1/scale/enrollments?meta_mode={self.mode}")
        self.assertIn(resp.status_code, (404, 405))

    def test_available_meta_modes_at_root(self):
        resp = self.client.get("/api/v1/scale/available-meta-modes")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("customer_sandbox", resp.json()["modes"])

    def test_invalid_meta_mode_rejected(self):
        resp = self.client.get("/api/v1/scale/bogus_mode/enrollments")
        self.assertEqual(resp.status_code, 422)


# ---------------------------------------------------------------------------
# Stub routes (spec §4.3 paths with no backing service module yet)
# ---------------------------------------------------------------------------


class TestFormerStubRoutes(ScaleRouterTestBase):
    """Issue #87: routes that were 501 stubs are now real endpoints."""

    def test_enrollment_candidates_list_is_real(self):
        """Issue #86 Task 9: discovery is no longer a 501 stub -- it is a
        real, always-200 read (spec §4.4, ``list_eligible_candidates``:
        "discovery only, never gates any command")."""
        resp = self.client.get(f"/api/v1/scale/{self.mode}/enrollment-candidates")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json(), {"items": []})

    def test_list_targets_is_real(self):
        resp = self.client.get(f"/api/v1/scale/{self.mode}/targets")
        self.assertEqual(resp.status_code, 200, resp.text)


# ---------------------------------------------------------------------------
# Problem responses (#368): every Scale domain error must pass the BFF's
# ProblemDetails validator, or the browser sees 502 invalid_domain_response
# (or a 500) instead of the error's own copy.
# ---------------------------------------------------------------------------

#: ``ProblemDetails.required`` in the OpenAPI contract the BFF validates against.
PROBLEM_REQUIRED = tuple(json.loads(
    (Path(__file__).resolve().parents[1] / "contracts/openapi/marketai-domain-v1.json").read_text()
)["components"]["schemas"]["ProblemDetails"]["required"])


class ScaleProblemAssertions:
    def _assert_problem(self, resp, status: int, code: str) -> dict:
        self.assertEqual(resp.status_code, status, resp.text)
        self.assertTrue(
            resp.headers["content-type"].startswith("application/problem+json"),
            resp.headers["content-type"],
        )
        body = resp.json()
        self.assertTrue(set(PROBLEM_REQUIRED) <= body.keys(), sorted(body))
        self.assertEqual((body["status"], body["code"]), (status, code))
        self.assertEqual(body["type"], f"urn:marketai:error:{code.replace('_', '-')}")
        self.assertTrue(body["trace_id"])
        self.assertEqual(body["trace_id"], resp.headers.get("X-Trace-Id"))
        self.assertIs(body["retryable"], False)
        self.assertNotIn("message", body)
        return body


class TestScaleProblemHandlerShapes(ScaleProblemAssertions, unittest.TestCase):
    """Each handler ``register_scale_v1_problem_handlers`` registers, on a
    probe app beside the app-wide handlers (as ``app.main`` wires them)."""

    def _cases(self) -> list[tuple[Exception, int, str]]:
        from app.experiments.winner_eligibility import WinnerEligibilityFinding
        from app.scale import v1_router as scale

        return [
            (scale.EnrollmentLifecycleError("revision_conflict"), 409, "revision_conflict"),
            (scale.EnrollmentLifecycleError("required_field_missing"), 422, "required_field_missing"),
            (scale.AdviceLifecycleError("thread_not_found", "sth_1"), 404, "thread_not_found"),
            (scale.WinnerIneligibleError((WinnerEligibilityFinding("winner_not_confirmed", "x"),)),
             422, "winner_ineligible"),
            (scale.DeliveryTargetNotFoundError("sdt_1"), 404, "delivery_target_not_found"),
            (scale.DeliveryTargetExpiredError("sdt_1"), 409, "delivery_target_expired"),
            (scale.WinnerTargetMismatchError("market_code", "US", "CA"), 409, "winner_target_mismatch"),
            (scale.CandidateNotFoundError("secd_1"), 404, "candidate_not_found"),
            (scale.CandidateAlreadyConsumedError("secd_1"), 409, "candidate_already_consumed"),
            (scale.CandidateExpiredError("secd_1"), 409, "candidate_expired"),
            (scale.CandidateFingerprintDriftError("secd_1"), 409, "candidate_fingerprint_drift"),
            (scale.PreviewEnrollmentNotFoundError("senr_1"), 404, "enrollment_not_found"),
            (scale.PreviewLifecycleInvalidError("senr_1"), 422, "invalid_transition"),
            (scale.PreviewExpiredError("sacp_1"), 409, "preview_expired"),
            (scale.PreviewAlreadyConsumedError("sacp_1"), 409, "preview_already_consumed"),
            (scale.PreviewEnrollmentMismatchError("sacp_1"), 409, "preview_enrollment_mismatch"),
            (scale.PreviewRevisionMismatchError("sacp_1"), 409, "preview_revision_mismatch"),
            (scale.IdempotencyConflictError("activate", "ik-1", "a", "b"), 409, "idempotency_conflict"),
            (scale.TargetLifecycleError("owner_required"), 403, "owner_required"),
            (scale.TargetLifecycleError("at_least_one_target"), 422, "at_least_one_target"),
            (scale.TargetNotFoundError("stgt_1"), 404, "target_not_found"),
            (scale.TargetPreviewExpiredError("stpp_1"), 409, "preview_expired"),
            (scale.TargetPreviewConsumedError("stpp_1"), 409, "preview_already_consumed"),
            (scale.TargetLifecycleError("invalid_lifecycle", "cannot withdraw withdrawn target"),
             409, "invalid_lifecycle"),
            (scale.TargetLifecycleError("idempotency_result_missing", "stgt_1"),
             400, "idempotency_result_missing"),
            (scale.BreakdownNotAvailableError("invalid_breakdown_type", "'bogus'"),
             422, "invalid_breakdown_type"),
            (scale.BreakdownNotAvailableError("enrollment_not_found", "senr_1"), 404, "enrollment_not_found"),
            (scale.BreakdownNotAvailableError("snapshot_not_found", "sesn_1"), 404, "snapshot_not_found"),
            (scale.BreakdownNotAvailableError("snapshot_not_sealed", "sesn_1"), 422, "snapshot_not_sealed"),
            (scale.CommandOutcomeNotFoundError("c368missing"), 404, "command_outcome_not_found"),
            (scale.ExportError("enrollment_not_found", "senr_1"), 404, "enrollment_not_found"),
            (scale.ExportError("episode_not_found", "sep_1"), 404, "episode_not_found"),
            (scale.ExportError("idempotency_result_missing", "sexp_1"), 400, "idempotency_result_missing"),
            (scale.ExportNotFoundError("sexp_1"), 404, "export_not_found"),
            (scale.ExportNotCompleteError("sexp_1"), 409, "export_not_complete"),
        ]

    def test_every_scale_error_answers_with_the_full_problem(self):
        from fastapi import FastAPI

        from app.library import api as library_api
        from app.scale.v1_router import register_scale_v1_problem_handlers

        cases = self._cases()
        probe = FastAPI()
        library_api.register_problem_handlers(probe)
        register_scale_v1_problem_handlers(probe)

        @probe.get("/probe/{case}")
        def raise_case(case: int):
            raise cases[case][0]

        client = TestClient(probe, raise_server_exceptions=False)
        self.addCleanup(client.close)
        for index, (exc, status, code) in enumerate(cases):
            with self.subTest(type(exc).__name__, code=code):
                resp = client.get(f"/probe/{index}", headers={"X-Trace-Id": f"trace-368-{index}"})
                body = self._assert_problem(resp, status, code)
                self.assertEqual(body["trace_id"], f"trace-368-{index}")
                self.assertEqual(body["instance"], "/probe/{case}")
                self.assertEqual(body["detail"], str(exc))
                self.assertEqual(body["field_errors"], [])

        # Without a browser trace id the response still carries one of its own.
        resp = client.get("/probe/0")
        self._assert_problem(resp, 409, "revision_conflict")


class TestScaleProblemResponses(ScaleProblemAssertions, ScaleRouterTestBase):
    """Target, Breakdown, Command-outcome and Export refusals through the
    real app, with their Workspace and role boundaries."""

    def _url(self, path: str) -> str:
        return f"/api/v1/scale/{self.mode}{path}"

    def _post(self, path: str, body: dict | None = None, key: str | None = None):
        return self.client.post(
            self._url(path), json=body if body is not None else {},
            headers={"Idempotency-Key": key or f"ik-368-{new_id('k')}"},
        )

    def _preview_body(self, **metrics) -> dict:
        return {
            "product_version_id": f"pv_{self.workspace_id}",
            "offer_version_id": f"ov_{self.workspace_id}",
            "market_code": "US",
            "currency": "USD",
            **metrics,
        }

    def _publication_preview(self, key: str | None = None) -> str:
        resp = self._post("/target-publication-previews", self._preview_body(target_cpa="20.00"), key)
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()["id"]

    def _published_target(self) -> str:
        resp = self._post("/targets", {"publication_preview_id": self._publication_preview()})
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()["id"]

    def _as(self, context: TenantContext) -> None:
        self.app.dependency_overrides[request_tenant_context] = lambda: context

    def _assert_no_preview_written(self) -> None:
        from app.scale.models import ScaleCommandJournalRecord, ScaleTargetPublicationPreviewRecord

        # Closed here, not at cleanup: a subtest loop must not hold one pooled
        # connection per call.
        with SessionLocal() as session:
            for record in (ScaleTargetPublicationPreviewRecord, ScaleCommandJournalRecord):
                self.assertEqual(
                    session.query(record).filter_by(workspace_id=self.workspace_id).count(), 0, record,
                )

    # -- TargetLifecycleError ------------------------------------------------

    def test_a_preview_without_any_target_metric_is_at_least_one_target(self):
        resp = self._post("/target-publication-previews", self._preview_body())
        self._assert_problem(resp, 422, "at_least_one_target")

    def test_a_member_cannot_preview_a_target_and_nothing_is_written(self):
        self._as(TenantContext(
            workspace_id=self.workspace_id, role="member", initiator_id="user_member",
        ))
        resp = self._post("/target-publication-previews", self._preview_body(target_cpa="20.00"))
        self._assert_problem(resp, 403, "owner_required")
        self._assert_no_preview_written()

    def test_a_missing_target_is_target_not_found(self):
        self._assert_problem(self.client.get(self._url("/targets/stgt_missing")), 404, "target_not_found")
        resp = self._post("/targets/stgt_missing/withdrawal-previews", {"reason": "#368"})
        self._assert_problem(resp, 404, "target_not_found")

    def test_another_workspace_cannot_read_a_target(self):
        from app.scale.targets import create_publication_preview, publish_target

        owner = replace(self.ctx, meta_mode=self.mode)
        with tenant_session(owner) as session:
            preview = create_publication_preview(
                session, owner,
                product_version_id="pv_1", offer_version_id="ov_1", market_code="US",
                currency="USD", target_cpa=Decimal("20.00"), breakeven_roas=None,
                minimum_economic_spend=None, idempotency_key="ci-only-ik-368-preview",
            )
            target_id = publish_target(
                session, owner, preview_id=preview.id, idempotency_key="ci-only-ik-368-publish",
            ).id

        self._switch_to_other_tenant()
        self._assert_problem(self.client.get(self._url(f"/targets/{target_id}")), 404, "target_not_found")
        resp = self._post(f"/targets/{target_id}/withdrawal-previews", {"reason": "#368"})
        self._assert_problem(resp, 404, "target_not_found")

        self._as(self.ctx)
        resp = self.client.get(self._url(f"/targets/{target_id}"))
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual((resp.json()["lifecycle_state"], resp.json()["revision"]), ("active", 1))

    def test_an_expired_publication_preview_is_preview_expired(self):
        from app.scale.models import ScaleTargetPublicationPreviewRecord

        preview_id = self._publication_preview()
        session = self._raw_session()
        session.get(ScaleTargetPublicationPreviewRecord, preview_id).expires_at = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        )
        session.commit()

        resp = self._post("/targets", {"publication_preview_id": preview_id})
        self._assert_problem(resp, 409, "preview_expired")

    def test_a_consumed_publication_preview_is_preview_already_consumed(self):
        preview_id = self._publication_preview()
        self.assertEqual(self._post("/targets", {"publication_preview_id": preview_id}).status_code, 201)

        resp = self._post("/targets", {"publication_preview_id": preview_id})
        self._assert_problem(resp, 409, "preview_already_consumed")

    def test_withdrawing_a_withdrawn_target_is_invalid_lifecycle(self):
        target_id = self._published_target()
        preview = self._post(f"/targets/{target_id}/withdrawal-previews", {"reason": "#368"})
        self.assertEqual(preview.status_code, 201, preview.text)
        withdrawn = self._post(f"/targets/{target_id}/withdraw", {"withdrawal_preview_id": preview.json()["id"]})
        self.assertEqual(withdrawn.status_code, 200, withdrawn.text)

        resp = self._post(f"/targets/{target_id}/withdrawal-previews", {"reason": "again"})
        self._assert_problem(resp, 409, "invalid_lifecycle")

    # -- Target amounts (#381) -----------------------------------------------

    def _amounts_body(self, field: str, value: str) -> dict:
        """Every amount valid except ``field``, so only it can be refused."""
        valid = {"target_cpa": "20.00", "breakeven_roas": "2.5", "minimum_economic_spend": "100"}
        return self._preview_body(**{**valid, field: value})

    def _assert_amount_refused(self, resp, field: str, code: str, params: dict | None = None) -> None:
        body = self._assert_problem(resp, 422, "validation_error")
        self.assertEqual(
            [(e["field"], e["code"], e.get("params")) for e in body["field_errors"]],
            [(f"body.{field}", code, params)],
        )
        self._assert_no_preview_written()

    def test_an_amount_that_is_not_a_plain_decimal_is_decimal_parsing(self):
        malformed = (
            "1,5", "abc", " ", " 20", "20 ", "NaN", "sNaN", "Infinity", "1e3", "1_000",
            "١٢", "+5", ".5", "5.", "0x10",
        )
        for field in ("target_cpa", "breakeven_roas", "minimum_economic_spend"):
            for value in malformed:
                with self.subTest(field=field, value=value):
                    resp = self._post("/target-publication-previews", self._amounts_body(field, value))
                    self._assert_amount_refused(resp, field, "decimal_parsing")

    def test_target_cpa_and_roas_must_be_greater_than_zero(self):
        for field in ("target_cpa", "breakeven_roas"):
            for value in ("0", "0.00", "-5", "-0"):
                with self.subTest(field=field, value=value):
                    resp = self._post("/target-publication-previews", self._amounts_body(field, value))
                    self._assert_amount_refused(resp, field, "greater_than", {"gt": 0})

    def test_minimum_economic_spend_may_be_zero_but_not_negative(self):
        resp = self._post(
            "/target-publication-previews", self._amounts_body("minimum_economic_spend", "-5"),
        )
        self._assert_amount_refused(resp, "minimum_economic_spend", "greater_than_equal", {"ge": 0})

        resp = self._post(
            "/target-publication-previews", self._amounts_body("minimum_economic_spend", "0"),
        )
        self.assertEqual(resp.status_code, 201, resp.text)

    def test_an_amount_must_fit_its_column(self):
        cases = (
            ("target_cpa", "1234567890123", "decimal_whole_digits", {"whole_digits": 12}),
            ("minimum_economic_spend", "1234567890123", "decimal_whole_digits", {"whole_digits": 12}),
            ("breakeven_roas", "1234567", "decimal_whole_digits", {"whole_digits": 6}),
            ("target_cpa", "1.1234567890123", "decimal_max_places", {"decimal_places": 12}),
            ("minimum_economic_spend", "1.1234567890123", "decimal_max_places", {"decimal_places": 12}),
            ("breakeven_roas", "1.23456", "decimal_max_places", {"decimal_places": 4}),
        )
        for field, value, code, params in cases:
            with self.subTest(field=field, value=value):
                resp = self._post("/target-publication-previews", self._amounts_body(field, value))
                self._assert_amount_refused(resp, field, code, params)

    def test_valid_amounts_are_previewed_as_entered(self):
        resp = self._post("/target-publication-previews", self._preview_body(
            target_cpa="20.00", breakeven_roas="2.5", minimum_economic_spend="100",
        ))
        self.assertEqual(resp.status_code, 201, resp.text)
        body = resp.json()
        self.assertEqual(
            (body["target_cpa"], body["breakeven_roas"], body["minimum_economic_spend"]),
            ("20.00", "2.5", "100"),
        )

        # The widest each column holds; trailing zeros past it change nothing.
        for metrics in (
            {"target_cpa": "999999999999.999999999999"},
            {"breakeven_roas": "999999.9999"},
            {"breakeven_roas": "2.50000"},
            {"target_cpa": "20", "minimum_economic_spend": "999999999999.999999999999"},
        ):
            with self.subTest(**metrics):
                resp = self._post("/target-publication-previews", self._preview_body(**metrics))
                self.assertEqual(resp.status_code, 201, resp.text)

    def test_an_empty_amount_is_still_not_entered(self):
        resp = self._post("/target-publication-previews", self._preview_body(
            target_cpa="", breakeven_roas="", minimum_economic_spend="",
        ))
        self._assert_problem(resp, 422, "at_least_one_target")

        resp = self._post("/target-publication-previews", self._preview_body(
            target_cpa="20", breakeven_roas="", minimum_economic_spend=None,
        ))
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual((resp.json()["breakeven_roas"], resp.json()["minimum_economic_spend"]), (None, None))

    def test_a_member_is_refused_a_malformed_amount_before_the_role(self):
        self._as(TenantContext(
            workspace_id=self.workspace_id, role="member", initiator_id="user_member",
        ))
        resp = self._post("/target-publication-previews", self._amounts_body("target_cpa", "1,5"))
        self._assert_amount_refused(resp, "target_cpa", "decimal_parsing")

        resp = self._post("/target-publication-previews", self._preview_body(target_cpa="20"))
        self._assert_problem(resp, 403, "owner_required")
        self._assert_no_preview_written()

    # -- Cursors (#381) ------------------------------------------------------

    def _seed_placements(self, workspace_id: str) -> tuple[str, str]:
        """An Enrollment's sealed snapshot with two placement segments."""
        session = self._raw_session()
        enrollment_id, episode_id = _seed_enrollment_and_episode(session, workspace_id)
        snapshot_id = _seed_sealed_snapshot(session, workspace_id, episode_id)
        for key in ("audience_network", "facebook_feed"):
            _add_segment(session, workspace_id, snapshot_id, breakdown_type="placement", segment_key=key)
        session.commit()
        return enrollment_id, snapshot_id

    def _breakdowns(self, enrollment_id: str, snapshot_id: str, cursor: str | None = None,
                    breakdown_type: str = "placement"):
        params = {"snapshot_id": snapshot_id, "breakdown_type": breakdown_type, "limit": 1}
        if cursor is not None:
            params["cursor"] = cursor
        return self.client.get(self._url(f"/enrollments/{enrollment_id}/breakdowns"), params=params)

    def test_breakdowns_page_and_a_malformed_cursor_is_invalid_cursor(self):
        enrollment_id, snapshot_id = self._seed_placements(self.workspace_id)
        first = self._breakdowns(enrollment_id, snapshot_id)
        self.assertEqual(first.status_code, 200, first.text)
        cursor = first.json()["next_cursor"]
        second = self._breakdowns(enrollment_id, snapshot_id, cursor)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(
            [s["segment_key"] for s in first.json()["segments"] + second.json()["segments"]],
            ["audience_network", "facebook_feed"],
        )

        filters_key = f"breakdowns:{snapshot_id}:placement"
        for label, malformed in (
            ("not base64 JSON", "abc"),
            ("not an object", "MQ=="),
            ("offset not an integer", encode_cursor(self.workspace_id, filters_key, ("x",))),
            ("negative offset", encode_cursor(self.workspace_id, filters_key, (-1,))),
        ):
            with self.subTest(label):
                resp = self._breakdowns(enrollment_id, snapshot_id, malformed)
                body = self._assert_problem(resp, 400, "invalid_cursor")
                self.assertEqual(body["field_errors"], [])

        resp = self._breakdowns(enrollment_id, snapshot_id, cursor, breakdown_type="age_gender")
        self._assert_problem(resp, 400, "invalid_cursor")

    def test_another_workspaces_breakdown_cursor_is_invalid_cursor(self):
        enrollment_id, snapshot_id = self._seed_placements(self.workspace_id)
        cursor = self._breakdowns(enrollment_id, snapshot_id).json()["next_cursor"]

        other_ws = self._switch_to_other_tenant()
        own_enrollment, own_snapshot = self._seed_placements(other_ws)
        self._assert_problem(self._breakdowns(own_enrollment, own_snapshot, cursor), 400, "invalid_cursor")
        self._assert_problem(
            self._breakdowns(enrollment_id, snapshot_id, cursor), 404, "enrollment_not_found",
        )

        self._as(self.ctx)
        self.assertEqual(self._breakdowns(enrollment_id, snapshot_id, cursor).status_code, 200)

    def test_a_malformed_target_list_cursor_is_invalid_cursor(self):
        filters_key = "targets::"
        resp = self.client.get(
            self._url("/targets"), params={"cursor": encode_cursor(self.workspace_id, filters_key, (0,))},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        for label, malformed in (
            ("not base64 JSON", "abc"),
            ("not an object", "MQ=="),
            ("offset not an integer", encode_cursor(self.workspace_id, filters_key, ("x",))),
            ("negative offset", encode_cursor(self.workspace_id, filters_key, (-1,))),
            ("another workspace", encode_cursor(f"{self.workspace_id}-other", filters_key, (0,))),
        ):
            with self.subTest(label):
                resp = self.client.get(self._url("/targets"), params={"cursor": malformed})
                self._assert_problem(resp, 400, "invalid_cursor")

    # -- BreakdownNotAvailableError -----------------------------------------

    def test_breakdowns_of_a_missing_enrollment_or_an_unknown_type(self):
        path = "/enrollments/senr_missing/breakdowns?snapshot_id=s"
        self._assert_problem(
            self.client.get(self._url(f"{path}&breakdown_type=placement")), 404, "enrollment_not_found",
        )
        self._assert_problem(
            self.client.get(self._url(f"{path}&breakdown_type=bogus")), 422, "invalid_breakdown_type",
        )

    # -- CommandOutcomeNotFoundError ----------------------------------------

    def test_a_missing_command_outcome_is_command_outcome_not_found(self):
        resp = self.client.get(self._url("/command-outcomes/c368missing"))
        self._assert_problem(resp, 404, "command_outcome_not_found")

    def test_another_workspace_cannot_read_a_command_outcome(self):
        from app.scale.command_journal import compute_identity_hash

        self._publication_preview(key="ci-only-ik-368-outcome")
        identity = compute_identity_hash(
            "create_target_publication_preview", "ci-only-ik-368-outcome",
        )
        own = self.client.get(self._url(f"/command-outcomes/{identity}"))
        self.assertEqual(own.status_code, 200, own.text)

        self._switch_to_other_tenant()
        resp = self.client.get(self._url(f"/command-outcomes/{identity}"))
        self._assert_problem(resp, 404, "command_outcome_not_found")

    # -- ExportError ----------------------------------------------------------

    def test_an_export_of_a_missing_enrollment_is_enrollment_not_found(self):
        resp = self._post("/exports", {
            "enrollment_id": "senr_missing", "episode_id": "sep_missing", "export_kind": "evidence_summary",
        })
        self._assert_problem(resp, 404, "enrollment_not_found")

    def test_a_download_of_a_missing_export_is_export_not_found(self):
        resp = self._post("/exports/sexp_missing/download-intents")
        self._assert_problem(resp, 404, "export_not_found")


class TestTargetWithdrawEligibilityByRole(ScaleProblemAssertions, ScaleRouterTestBase):
    """#382: a Target's ``withdraw`` eligibility carries the caller's role,
    so a Member sees the button disabled instead of being refused on click.
    The command itself still refuses the Member."""

    def _url(self, path: str) -> str:
        return f"/api/v1/scale/{self.mode}{path}"

    def _post(self, path: str, body: dict) -> object:
        return self.client.post(
            self._url(path), json=body, headers={"Idempotency-Key": f"ci-only-ik-382-{new_id('k')}"},
        )

    def _published_target(self) -> str:
        preview = self._post("/target-publication-previews", {
            "product_version_id": f"pv_{self.workspace_id}",
            "offer_version_id": f"ov_{self.workspace_id}",
            "market_code": "US",
            "currency": "USD",
            "target_cpa": "20.00",
        })
        self.assertEqual(preview.status_code, 201, preview.text)
        target = self._post("/targets", {"publication_preview_id": preview.json()["id"]})
        self.assertEqual(target.status_code, 201, target.text)
        return target.json()["id"]

    def _as(self, context: TenantContext) -> None:
        self.app.dependency_overrides[request_tenant_context] = lambda: context

    def _withdraw_action(self, target_id: str) -> tuple[dict, dict]:
        listed = self.client.get(self._url("/targets"))
        self.assertEqual(listed.status_code, 200, listed.text)
        [row] = [t for t in listed.json()["items"] if t["id"] == target_id]
        detail = self.client.get(self._url(f"/targets/{target_id}"))
        self.assertEqual(detail.status_code, 200, detail.text)
        return tuple(
            {a["action"]: a for a in body["allowed_actions"]}["withdraw"]
            for body in (row, detail.json())
        )

    def test_a_member_sees_withdraw_denied_and_is_still_refused(self):
        target_id = self._published_target()

        self._as(TenantContext(
            workspace_id=self.workspace_id, role="member", initiator_id="user_member",
        ))
        for action in self._withdraw_action(target_id):
            self.assertEqual((action["allowed"], action["reason_code"]), (False, "owner_required"))
        resp = self._post(f"/targets/{target_id}/withdrawal-previews", {"reason": "#382"})
        self._assert_problem(resp, 403, "owner_required")

        self._as(self.ctx)
        for action in self._withdraw_action(target_id):
            self.assertEqual((action["allowed"], action["reason_code"]), (True, None))

    def test_a_withdrawn_target_reports_lifecycle_to_a_member(self):
        target_id = self._published_target()
        preview = self._post(f"/targets/{target_id}/withdrawal-previews", {"reason": "#382"})
        self.assertEqual(preview.status_code, 201, preview.text)
        withdrawn = self._post(f"/targets/{target_id}/withdraw", {"withdrawal_preview_id": preview.json()["id"]})
        self.assertEqual(withdrawn.status_code, 200, withdrawn.text)
        withdraw = {a["action"]: a for a in withdrawn.json()["allowed_actions"]}["withdraw"]
        self.assertEqual((withdraw["allowed"], withdraw["reason_code"]), (False, "invalid_lifecycle"))

        self._as(TenantContext(
            workspace_id=self.workspace_id, role="member", initiator_id="user_member",
        ))
        for action in self._withdraw_action(target_id):
            self.assertEqual((action["allowed"], action["reason_code"]), (False, "invalid_lifecycle"))

    def test_another_workspace_cannot_list_or_read_the_target(self):
        target_id = self._published_target()

        self._switch_to_other_tenant()
        listed = self.client.get(self._url("/targets"))
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertNotIn(target_id, [t["id"] for t in listed.json()["items"]])
        self._assert_problem(self.client.get(self._url(f"/targets/{target_id}")), 404, "target_not_found")


class TestScaleProblemResponsesOnAnEnrollment(ScaleProblemAssertions, EnrollmentChainTestBase):
    """The refusals that need a real active Enrollment and its Episode."""

    def _active_enrollment(self) -> dict:
        enrollment_id = self._create()["id"]
        resp = self.client.put(
            f"/api/v1/scale/{self.mode}/enrollments/{enrollment_id}/activate",
            json={"activation_preview_id": self._create_preview(enrollment_id, 1)},
            headers={"If-Match": '"1"', "Idempotency-Key": f"ik-activate-{new_id('t')}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _post(self, path: str, body: dict | None = None):
        return self.client.post(
            f"/api/v1/scale/{self.mode}{path}", json=body if body is not None else {},
            headers={"Idempotency-Key": f"ik-368-{new_id('k')}"},
        )

    def test_exports_and_breakdowns_answer_with_their_codes_and_stay_in_their_workspace(self):
        enrollment = self._active_enrollment()
        export_body = {
            "enrollment_id": enrollment["id"],
            "episode_id": enrollment["current_episode_id"],
            "export_kind": "evidence_summary",
        }
        breakdowns = (
            f"/api/v1/scale/{self.mode}/enrollments/{enrollment['id']}/breakdowns"
            "?snapshot_id=sesn_missing&breakdown_type=placement"
        )

        self._assert_problem(
            self._post("/exports", {**export_body, "episode_id": "sep_missing"}), 404, "episode_not_found",
        )
        created = self._post("/exports", export_body)
        self.assertEqual(created.status_code, 202, created.text)
        export_id = created.json()["id"]
        self._assert_problem(
            self._post(f"/exports/{export_id}/download-intents"), 409, "export_not_complete",
        )
        self._assert_problem(self.client.get(breakdowns), 404, "snapshot_not_found")

        self._switch_to_other_tenant()
        self._assert_problem(
            self._post(f"/exports/{export_id}/download-intents"), 404, "export_not_found",
        )
        self._assert_problem(self.client.get(breakdowns), 404, "enrollment_not_found")
        self._assert_problem(self._post("/exports", export_body), 404, "enrollment_not_found")



# ---------------------------------------------------------------------------
# Platform-scoped Scale router (Task 6, spec §3.7): /api/platform/v1/scale/*
# ---------------------------------------------------------------------------


class PlatformScaleRouterTestBase(unittest.TestCase):
    """HTTP fixture for ``/api/platform/v1/scale/*``.

    ``app.state.platform_operator_jwt_verifier``/``app.state.
    admission_reader`` are plain attributes ``app.scale.platform_router.
    _platform_context`` reads directly (not FastAPI dependencies), so tests
    set them directly rather than through ``app.dependency_overrides`` --
    the same pattern ``tests/test_platform_operator_identity.py``'s PO16-23
    tests use for ``app.state.platform_operator_jwt_verifier``."""

    @classmethod
    def setUpClass(cls) -> None:
        url = config.DATABASE_URL
        if url.startswith("sqlite:///"):
            Path(url.replace("sqlite:///", "", 1)).parent.mkdir(parents=True, exist_ok=True)
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")

    def setUp(self) -> None:
        from app.main import app

        self.app = app
        self.workspace_id = f"ws_platform_scale_{new_id('t')}"
        with platform_session() as session:
            session.add(WorkspaceRecord(id=self.workspace_id, name=self.workspace_id))

        self._original_verifier = app.state.platform_operator_jwt_verifier
        self._original_admission_reader = getattr(app.state, "admission_reader", None)
        self.addCleanup(self._restore_state)

        self.identity = PlatformOperatorIdentity(
            sub="operator-1", workspace_id=self.workspace_id, session_id="sess_1",
            iat=1, exp=2, jti="jti_1",
        )
        app.state.platform_operator_jwt_verifier = AsyncMock(verify=AsyncMock(return_value=self.identity))
        app.state.admission_reader = SimpleNamespace(
            read=AsyncMock(return_value=WorkspaceAdmission(
                workspace_id=self.workspace_id, authorization_epoch=0,
                entitlement_version=1, suspended=False, is_platform_workspace=True,
            )),
        )
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def _restore_state(self) -> None:
        self.app.state.platform_operator_jwt_verifier = self._original_verifier
        self.app.state.admission_reader = self._original_admission_reader

    def _headers(self, **overrides) -> dict:
        headers = {"Authorization": "Bearer good-platform-token"}
        headers.update(overrides)
        return headers


class TestPlatformScaleRouterAuth(PlatformScaleRouterTestBase):
    def test_platform_route_rejects_delegated_jwt(self):
        """PO20: a token the platform-operator verifier itself rejects
        (e.g. a delegated-shaped JWT, ``typ='delegated+jwt'``) -> 401, not
        200/500. The verifier's own typ/issuer/audience checks are unit
        tested in ``tests/test_platform_operator_identity.py``; this
        confirms the platform Scale router actually surfaces that
        rejection rather than bypassing it."""
        self.app.state.platform_operator_jwt_verifier = AsyncMock(
            verify=AsyncMock(
                side_effect=PlatformTokenError("platform_operator_token_invalid", "wrong typ"),
            )
        )
        resp = self.client.get(
            f"/api/platform/v1/scale/{ScaleRouterTestBase.mode}/enrollments",
            headers=self._headers(Authorization="Bearer delegated-shaped-jwt"),
        )
        self.assertEqual(resp.status_code, 401, resp.text)

    def test_missing_bearer_token_returns_401(self):
        resp = self.client.get(f"/api/platform/v1/scale/{ScaleRouterTestBase.mode}/enrollments")
        self.assertEqual(resp.status_code, 401, resp.text)

    def test_no_verifier_configured_returns_503(self):
        self.app.state.platform_operator_jwt_verifier = None
        resp = self.client.get(
            f"/api/platform/v1/scale/{ScaleRouterTestBase.mode}/enrollments",
            headers=self._headers(),
        )
        self.assertEqual(resp.status_code, 503, resp.text)

    def test_invalid_meta_mode_path_segment_returns_422(self):
        resp = self.client.get(
            "/api/platform/v1/scale/bogus_mode/enrollments", headers=self._headers(),
        )
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_internal_validation_requires_platform_workspace(self):
        """MA10/PO2: platform-operator identity + an ordinary (non-Platform)
        Workspace + ``internal_validation`` -> 403 at the ``require_meta_mode``
        domain gate."""
        self.app.state.admission_reader = SimpleNamespace(
            read=AsyncMock(return_value=WorkspaceAdmission(
                workspace_id=self.workspace_id, authorization_epoch=0,
                entitlement_version=1, suspended=False, is_platform_workspace=False,
            )),
        )
        resp = self.client.get(
            "/api/platform/v1/scale/internal_validation/enrollments", headers=self._headers(),
        )
        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertEqual(resp.json()["code"], "meta_mode_not_permitted")

    def test_platform_operator_customer_sandbox_is_permitted(self):
        resp = self.client.get(
            f"/api/platform/v1/scale/{ScaleRouterTestBase.mode}/enrollments", headers=self._headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_suspended_workspace_returns_403(self):
        self.app.state.admission_reader = SimpleNamespace(
            read=AsyncMock(return_value=WorkspaceAdmission(
                workspace_id=self.workspace_id, authorization_epoch=0,
                entitlement_version=1, suspended=True, is_platform_workspace=True,
            )),
        )
        resp = self.client.get(
            f"/api/platform/v1/scale/{ScaleRouterTestBase.mode}/enrollments", headers=self._headers(),
        )
        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertEqual(resp.json()["code"], "workspace_suspended")

    def test_admission_reader_not_configured_returns_503(self):
        self.app.state.admission_reader = None
        resp = self.client.get(
            f"/api/platform/v1/scale/{ScaleRouterTestBase.mode}/enrollments", headers=self._headers(),
        )
        self.assertEqual(resp.status_code, 503, resp.text)


class TestPlatformScaleRouterEnrollmentLifecycle(PlatformScaleRouterTestBase):
    """Smoke-tests the platform router's Enrollment create/list/activate
    path against ``internal_validation`` -- the mode a delegated user can
    never select (spec §3.7's whole reason for this router existing).

    Issue #86 Task 9: creation requires a genuine ``enrollment_candidate_id``
    here too -- built via a real ``evidence_observation``-sourced
    DeliveryTarget (``tests/support/scale_candidate_fixtures``, the same
    provenance shape a live ``internal_validation`` sync produces)."""

    def _new_candidate_id(self) -> str:
        fixture_ctx = TenantContext(
            workspace_id=self.workspace_id, role="system",
            executor="system:worker", initiator_id="operator-1",
        )
        ensure_governance_prereqs(fixture_ctx, f"prod_{self.workspace_id}")
        chain = build_ready_winner_and_target(
            fixture_ctx, f"prod_{self.workspace_id}", self.workspace_id,
            target_meta_mode="internal_validation",
        )
        candidate_ctx = TenantContext(
            workspace_id=self.workspace_id, role="system",
            executor="system:worker", initiator_id="operator-1",
            meta_mode="internal_validation",
        )
        with tenant_session(candidate_ctx) as session:
            candidate = create_enrollment_candidate(
                session, candidate_ctx,
                decision_id=chain["decision_id"],
                delivery_target_id=chain["delivery_target_id"],
                idempotency_key=new_id("candidate"),
            )
            candidate_id = candidate.id
        # `build_ready_winner_and_target`'s evidence_observation path (Task
        # 9's own fixture) already creates a valid attestation for this
        # contract generation -- only the metric-semantic-contract-set
        # membership `_build_episode_from_evidence` (Task 11) needs is
        # still missing.
        seed_activation_evidence_prereqs(
            candidate_ctx, delivery_target_id=chain["delivery_target_id"],
            meta_mode="internal_validation", tag=self.workspace_id,
        )
        return candidate_id

    def _create_preview(self, enrollment_id: str, expected_revision: int) -> str:
        resp = self.client.post(
            f"/api/platform/v1/scale/internal_validation/enrollments/{enrollment_id}/activation-previews",
            json={"expected_revision": expected_revision},
            headers=self._headers(**{"Idempotency-Key": f"ik-platform-preview-{new_id('t')}"}),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()["id"]

    def test_create_list_and_activate_enrollment_in_internal_validation(self):
        candidate_id = self._new_candidate_id()
        create_resp = self.client.post(
            "/api/platform/v1/scale/internal_validation/enrollments",
            json={"enrollment_candidate_id": candidate_id},
            headers=self._headers(**{"Idempotency-Key": f"ik-platform-create-{new_id('t')}"}),
        )
        self.assertEqual(create_resp.status_code, 201, create_resp.text)
        enrollment = create_resp.json()
        self.assertEqual(enrollment["lifecycle"], "draft")
        self.assertEqual(enrollment["meta_mode"], "internal_validation")

        list_resp = self.client.get(
            "/api/platform/v1/scale/internal_validation/enrollments", headers=self._headers(),
        )
        self.assertEqual(list_resp.status_code, 200, list_resp.text)
        self.assertEqual(len(list_resp.json()["items"]), 1)

        preview_id = self._create_preview(enrollment["id"], 1)
        activate_resp = self.client.put(
            f"/api/platform/v1/scale/internal_validation/enrollments/{enrollment['id']}/activate",
            json={"activation_preview_id": preview_id},
            headers=self._headers(**{"If-Match": '"1"', "Idempotency-Key": "ik-platform-activate-1"}),
        )
        self.assertEqual(activate_resp.status_code, 200, activate_resp.text)
        active = activate_resp.json()
        self.assertEqual(active["lifecycle"], "active")
        actions = {a["action"]: a["allowed"] for a in active["allowed_actions"]}
        self.assertEqual(actions, {"activate": False, "pause": True, "resume": False, "close": True})

        pause_resp = self.client.put(
            f"/api/platform/v1/scale/internal_validation/enrollments/{enrollment['id']}/pause",
            json={},
            headers=self._headers(**{"If-Match": '"2"', "Idempotency-Key": "ik-platform-pause-1"}),
        )
        self.assertEqual(pause_resp.status_code, 200, pause_resp.text)
        self.assertEqual(pause_resp.json()["lifecycle"], "paused")

    def test_platform_target_routes_are_real(self):
        """Issue #87: platform target list route is no longer a 501 stub."""
        resp = self.client.get(
            "/api/platform/v1/scale/internal_validation/targets", headers=self._headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_platform_target_preview_refuses_a_malformed_amount(self):
        """#381: the platform mirror shares the preview's request model."""
        from app.scale.models import ScaleCommandJournalRecord, ScaleTargetPublicationPreviewRecord

        resp = self.client.post(
            "/api/platform/v1/scale/internal_validation/target-publication-previews",
            json={
                "product_version_id": "pv_1", "offer_version_id": "ov_1",
                "market_code": "US", "currency": "USD", "target_cpa": "1,5",
            },
            headers=self._headers(**{"Idempotency-Key": f"ik-platform-target-{new_id('t')}"}),
        )
        self.assertEqual(resp.status_code, 422, resp.text)
        body = resp.json()
        self.assertEqual(body["code"], "validation_error")
        self.assertEqual(
            [(e["field"], e["code"]) for e in body["field_errors"]], [("body.target_cpa", "decimal_parsing")],
        )
        session = SessionLocal()
        self.addCleanup(session.close)
        for record in (ScaleTargetPublicationPreviewRecord, ScaleCommandJournalRecord):
            self.assertEqual(
                session.query(record).filter_by(workspace_id=self.workspace_id).count(), 0, record,
            )

    def test_platform_operator_may_withdraw_a_published_target(self):
        """#382: the platform routes serialize Targets with the operator's
        ``system`` context, which the Owner gate admits."""
        base = "/api/platform/v1/scale/internal_validation"
        preview = self.client.post(
            f"{base}/target-publication-previews",
            json={
                "product_version_id": "pv_platform", "offer_version_id": "ov_platform",
                "market_code": "US", "currency": "USD", "target_cpa": "20.00",
            },
            headers=self._headers(**{"Idempotency-Key": "ci-only-ik-382-platform-preview"}),
        )
        self.assertEqual(preview.status_code, 201, preview.text)
        published = self.client.post(
            f"{base}/targets", json={"publication_preview_id": preview.json()["id"]},
            headers=self._headers(**{"Idempotency-Key": "ci-only-ik-382-platform-publish"}),
        )
        self.assertEqual(published.status_code, 201, published.text)
        target_id = published.json()["id"]

        listed = self.client.get(f"{base}/targets", headers=self._headers())
        self.assertEqual(listed.status_code, 200, listed.text)
        detail = self.client.get(f"{base}/targets/{target_id}", headers=self._headers())
        self.assertEqual(detail.status_code, 200, detail.text)
        [row] = [t for t in listed.json()["items"] if t["id"] == target_id]
        for body in (published.json(), row, detail.json()):
            withdraw = {a["action"]: a for a in body["allowed_actions"]}["withdraw"]
            self.assertEqual((withdraw["allowed"], withdraw["reason_code"]), (True, None))


if __name__ == "__main__":
    unittest.main()
