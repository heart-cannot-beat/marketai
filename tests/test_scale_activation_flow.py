"""Tests for the preview-only ``activate_enrollment``/``resume_enrollment``
protocol (issue #86 Task 11).

Spec: docs/superpowers/specs/2026-09-14-harden-scale-enrollment-lineage-design.md §5
Task: .superpowers/sdd/2026-09-14-harden-scale-enrollment-lineage/task-11-brief.md

Domain-level coverage (``ActivationFlowTestBase``, imported from
``tests.test_scale_enrollment`` rather than duplicated -- both files need
the exact same heavy candidate-gated fixture) exercises the brief's A1-A19
scenarios: a valid preview activates and creates the first Episode (A1), an
expired/already-consumed preview is rejected (A2/A3), a mid-transaction
failure leaves no partial Enrollment/Episode state (A10), an idempotent
replay after success returns the same result (A15), and a paused Enrollment
resumes only through a fresh preview (A19).

Router-level coverage (``TestActivateResumeRequestShape``) exercises A11/A20:
``EnrollmentActivateRequest``/``EnrollmentResumeRequest`` forbid every field
except ``activation_preview_id`` (Pydantic's ``extra="forbid"`` rejects the
pre-hardening ``delivery_config``/``episode_context``/``meta_mode`` shape
with a 422 before the route handler -- or any domain lookup -- ever runs,
so these need no candidate/preview fixture at all).
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="marketai-scale-actflow-"))
os.environ.setdefault("MARKETAI_TEST_ROOT", str(_TEST_ROOT))
os.environ.setdefault("MARKETAI_DATA_DIR", str(_TEST_ROOT / "data"))
os.environ.setdefault("MARKETAI_DATABASE_URL", f"sqlite:///{_TEST_ROOT / 'marketai.db'}")
os.environ.setdefault("MARKETAI_WORKSPACE_ID", "ws_test")

from sqlalchemy import select, update

from app.adapters.persistence.database import tenant_session
from app.platform.tenancy import TenantContext
from app.scale.activation_evidence import EvidenceStaleError
from app.scale.activation_preview import (
    PreviewAlreadyConsumedError,
    PreviewExpiredError,
)
from app.scale.enrollment import (
    activate_enrollment,
    pause_enrollment,
    resume_enrollment,
)
from app.scale.models import (
    ScaleActivationPreviewRecord,
    ScaleDeliveryEvidenceSnapshotRecord,
    ScaleEnrollmentRecord,
    ScaleEpisodeRecord,
)

from tests.test_scale_enrollment import ActivationFlowTestBase


# ---------------------------------------------------------------------------
# Domain-level: A1, A2, A3, A10, A15, A19
# ---------------------------------------------------------------------------


class TestActivateWithPreview(ActivationFlowTestBase):
    def test_activate_with_preview(self):
        """A1: valid preview -> activated, Episode created."""
        enrollment_id, revision = self._draft()
        enrollment, episode = self._activate(enrollment_id, revision, idempotency_key="idem_a1")

        self.assertEqual(enrollment.lifecycle, "active")
        self.assertIsNotNone(episode)
        self.assertEqual(episode.phase, "collecting")


class TestActivateExpiredPreview(ActivationFlowTestBase):
    def test_activate_expired_preview(self):
        """A2: preview expired -> PreviewExpiredError."""
        enrollment_id, revision = self._draft()
        preview_id = self._create_preview(enrollment_id, revision)
        with tenant_session(self.ctx) as session:
            session.execute(
                update(ScaleActivationPreviewRecord)
                .where(ScaleActivationPreviewRecord.id == preview_id)
                .values(expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
            )

        with tenant_session(self.ctx) as session:
            with self.assertRaises(PreviewExpiredError):
                activate_enrollment(
                    session, self.ctx,
                    enrollment_id=enrollment_id,
                    activation_preview_id=preview_id,
                    expected_revision=revision,
                    idempotency_key="idem_a2",
                )


class TestActivateConsumedPreview(ActivationFlowTestBase):
    def test_activate_consumed_preview(self):
        """A3: preview already consumed -> PreviewAlreadyConsumedError.

        Consumes one draft's preview via a real, successful activation,
        then tries to reuse that same (now-consumed) preview id against a
        *second*, still-draft Enrollment: ``load_and_consume_preview``'s
        CAS checks ``consumed_at`` before it ever compares
        ``enrollment_id``, so this is a clean way to reach
        ``PreviewAlreadyConsumedError`` specifically (reusing it against
        the *same*, now-active Enrollment would instead hit
        ``activate_enrollment``'s own ``invalid_transition`` lifecycle
        check first -- see ``test_rejects_activation_from_non_draft`` in
        ``tests/test_scale_enrollment.py``)."""
        consumed_enrollment_id, consumed_revision = self._draft()
        self._activate(consumed_enrollment_id, consumed_revision, idempotency_key="idem_a3_setup")
        with tenant_session(self.ctx) as session:
            consumed_preview_id = session.execute(
                select(ScaleActivationPreviewRecord.id).filter_by(
                    enrollment_id=consumed_enrollment_id,
                )
            ).scalar_one()

        other_enrollment_id, other_revision = self._draft()
        with tenant_session(self.ctx) as session:
            with self.assertRaises(PreviewAlreadyConsumedError):
                activate_enrollment(
                    session, self.ctx,
                    enrollment_id=other_enrollment_id,
                    activation_preview_id=consumed_preview_id,
                    expected_revision=other_revision,
                    idempotency_key="idem_a3",
                )


class TestActivateFailureLeavesNoPartialState(ActivationFlowTestBase):
    def test_failure_no_partial_state(self):
        """A10: a mid-transaction failure (evidence goes stale between
        preview-time and commit-time) leaves the Enrollment in `draft` --
        no partial lifecycle change, no orphaned Episode."""
        enrollment_id, revision = self._draft()
        preview_id = self._create_preview(enrollment_id, revision)

        # Back-date every snapshot for this chain's target so
        # `resolve_activation_evidence` (re-run at commit time) rejects it
        # as stale -- mirrors tests/test_scale_activation_preview.py's own
        # identical staleness test.
        with tenant_session(self.ctx) as session:
            session.execute(
                update(ScaleDeliveryEvidenceSnapshotRecord)
                .where(
                    ScaleDeliveryEvidenceSnapshotRecord.delivery_target_id
                    == self._chain["delivery_target_id"],
                )
                .values(observed_at=datetime.now(timezone.utc) - timedelta(hours=48))
            )

        with tenant_session(self.ctx) as session:
            with self.assertRaises(EvidenceStaleError):
                activate_enrollment(
                    session, self.ctx,
                    enrollment_id=enrollment_id,
                    activation_preview_id=preview_id,
                    expected_revision=revision,
                    idempotency_key="idem_a10",
                )

        with tenant_session(self.ctx) as session:
            refreshed = session.get(ScaleEnrollmentRecord, enrollment_id)
            self.assertEqual(refreshed.lifecycle, "draft")
            self.assertIsNone(refreshed.current_episode_id)

            episodes = session.execute(
                select(ScaleEpisodeRecord).filter_by(enrollment_id=enrollment_id)
            ).scalars().all()
            self.assertEqual(episodes, [])


class TestActivateIdempotentReplay(ActivationFlowTestBase):
    def test_idempotency_replay(self):
        """A15: replaying the same Idempotency-Key (and the same, already-
        consumed preview) after a successful activation returns the same
        result rather than erroring or re-running the transition."""
        enrollment_id, revision = self._draft()
        enrollment, episode = self._activate(enrollment_id, revision, idempotency_key="idem_a15")

        with tenant_session(self.ctx) as session:
            preview_id = session.execute(
                select(ScaleActivationPreviewRecord.id).filter_by(enrollment_id=enrollment_id)
            ).scalar_one()

        with tenant_session(self.ctx) as session:
            result_enrollment, result_episode = activate_enrollment(
                session, self.ctx,
                enrollment_id=enrollment_id,
                activation_preview_id=preview_id,
                expected_revision=revision,
                idempotency_key="idem_a15",
            )
        self.assertEqual(result_enrollment.lifecycle, "active")
        self.assertEqual(result_episode.id, episode.id)


class TestResumeThroughPreview(ActivationFlowTestBase):
    def test_resume_through_preview(self):
        """A19: a paused Enrollment resumes only through a fresh preview
        (transition_kind='resume'), continuing its still-open Episode."""
        enrollment_id, revision = self._draft()
        enrollment, episode = self._activate(enrollment_id, revision, idempotency_key="idem_a19_activate")
        with tenant_session(self.ctx) as session:
            paused = pause_enrollment(
                session, self.ctx, enrollment.id,
                expected_revision=enrollment.revision, idempotency_key="idem_a19_pause",
            )

        resume_preview_id = self._create_preview(enrollment.id, paused.revision)
        with tenant_session(self.ctx) as session:
            result_enrollment, result_episode = resume_enrollment(
                session, self.ctx,
                enrollment_id=enrollment.id,
                activation_preview_id=resume_preview_id,
                expected_revision=paused.revision,
                idempotency_key="idem_a19",
            )

        self.assertEqual(result_enrollment.lifecycle, "active")
        self.assertIsNotNone(result_episode)
        self.assertEqual(result_episode.id, episode.id)


# ---------------------------------------------------------------------------
# Router-level: A11, A20 -- request-shape rejection needs no fixture at all,
# since Pydantic's extra="forbid" rejects the body before any domain lookup.
# ---------------------------------------------------------------------------


class TestActivateResumeRequestShape(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from alembic import command
        from alembic.config import Config
        from app import config

        if config.DATABASE_URL.startswith("sqlite:///"):
            Path(config.DATABASE_URL.replace("sqlite:///", "", 1)).parent.mkdir(
                parents=True, exist_ok=True,
            )
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app
        from app.platform.http_identity import request_tenant_context

        self.ctx = TenantContext(
            workspace_id="ws_actflow_shape", role="owner", initiator_id="user_1",
        )
        self.app = app
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def test_meta_mode_in_activate_body_rejected(self):
        """A11: `meta_mode` in the activate body -> 422 (extra forbidden;
        meta_mode is exclusively a URL path segment, spec §3.10)."""
        resp = self.client.put(
            "/api/v1/scale/customer_sandbox/enrollments/does-not-exist/activate",
            json={"activation_preview_id": "does-not-matter", "meta_mode": "customer_sandbox"},
            headers={"Idempotency-Key": "idem_a11", "If-Match": '"1"'},
        )
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_resume_body_extra_fields_rejected(self):
        """A20: resume body carrying delivery_config -> 422 (extra
        forbidden; the pre-hardening shape is no longer accepted at all)."""
        resp = self.client.put(
            "/api/v1/scale/customer_sandbox/enrollments/does-not-exist/resume",
            json={"activation_preview_id": "does-not-matter", "delivery_config": {}},
            headers={"Idempotency-Key": "idem_a20", "If-Match": '"1"'},
        )
        self.assertEqual(resp.status_code, 422, resp.text)


if __name__ == "__main__":
    unittest.main()
