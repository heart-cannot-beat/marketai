"""Tests for activation evidence + activation preview (spec §4.5/§6.11,
issue #86 Task 10).

Exercises:
  - ``resolve_activation_evidence()`` freshness enforcement, snapshot reuse,
    and error paths (no attestation, stale evidence, future evidence).
  - ``create_activation_preview()`` lifecycle validation, revision gating,
    lineage hash computation, and idempotent replay.
  - ``load_and_consume_preview()`` atomic CAS consumption, expiry enforcement,
    mismatch detection.

Reuses the ``CandidateTestBase`` fixture infrastructure from
``test_scale_enrollment_candidates`` (same heavy Winner + DeliveryTarget
chain) -- this is the established duplication-per-file convention in this
repo's test suite.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config

from app.adapters.persistence.database import engine, tenant_session
from app.adapters.persistence.models import new_id, utcnow
from app.meta.evidence import create_attestation, publish_evidence
from app.adapters.persistence.models import OperationRecord
from app.meta.models import MetaObservationSyncRunRecord
from app.platform.tenancy import TenantContext
from app.scale.activation_evidence import (
    EvidenceFromFutureError,
    EvidenceStaleError,
    NoValidAttestationError,
    resolve_activation_evidence,
)
from app.scale.activation_preview import (
    EnrollmentLifecycleInvalidError,
    EnrollmentNotFoundError,
    PreviewAlreadyConsumedError,
    PreviewExpiredError,
    PreviewRevisionMismatchError,
    create_activation_preview,
    load_and_consume_preview,
)
from app.scale.candidates import create_enrollment_candidate
from app.scale.enrollment import create_draft_enrollment
from app.scale.enrollment_lineage import lock_and_validate_enrollment_candidate
from app.scale.models import (
    ScaleActivationPreviewRecord,
    ScaleDeliveryEvidenceSnapshotRecord,
)

from tests.test_scale_enrollment_candidates import (
    CandidateTestBase,
    CTX_SANDBOX,
    _get_shared_ready_chain,
)

CTX = TenantContext(
    workspace_id="ws_secand",
    role="owner",
    initiator_id="user_secand",
    request_id="req_ap_test",
    trace_id="trace_ap_test",
)


def _seed_attestation_for_target(chain: dict, tag: str) -> str:
    """Seed an evidence publication + attestation for the delivery target's
    contract generation so ``resolve_activation_evidence`` can find one."""
    with tenant_session(CTX_SANDBOX) as s:
        from app.scale.models import ScaleDeliveryTargetRecord
        target = s.get(ScaleDeliveryTargetRecord, chain["delivery_target_id"])
        contract_generation_id = target.contract_generation_id

        now = utcnow()
        op = OperationRecord(
            id=new_id("op"), workspace_id=CTX_SANDBOX.workspace_id,
            command_type="evidence_publish", state="completed",
        )
        s.add(op)
        s.flush()

        sync_run = MetaObservationSyncRunRecord(
            id=new_id("msync"), workspace_id=CTX_SANDBOX.workspace_id,
            meta_mode=CTX_SANDBOX.meta_mode,
            contract_generation_id=contract_generation_id,
            occurrence_id=new_id("occ"), run_generation=1,
            state="succeeded", operation_id=op.id,
            executor="test", completed_at=now,
        )
        s.add(sync_run)
        s.flush()

        pub = publish_evidence(s, sync_run_id=sync_run.id, operation_id=op.id)
        att = create_attestation(
            s, contract_generation_id=contract_generation_id,
            publication_id=pub.id, evidence_watermark="[2026-09-01,2026-09-07]",
        )
        return att.id


# ---------------------------------------------------------------------------
# resolve_activation_evidence tests
# ---------------------------------------------------------------------------


class TestResolveActivationEvidence(CandidateTestBase):

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._chain = cls._build_chain_with_attestation()

    @classmethod
    def _build_chain_with_attestation(cls) -> dict:
        from tests.test_scale_enrollment_candidates import PaidOperationAdmission, EntitlementPolicy, CostQuoteEngine, ReservationBook, PRICE_REGISTRY
        admission = PaidOperationAdmission(
            policy=EntitlementPolicy(),
            quote_engine=CostQuoteEngine(price_registry=PRICE_REGISTRY, price_rule_version="test-v1"),
            reservation_book=ReservationBook(),
        )
        chain = _get_shared_ready_chain(admission)
        chain = dict(chain)
        chain["attestation_id"] = _seed_attestation_for_target(chain, "ap_shared")
        return chain

    def _make_lineage(self):
        chain = self._chain
        with tenant_session(CTX_SANDBOX) as s:
            candidate = create_enrollment_candidate(
                s, CTX_SANDBOX,
                decision_id=chain["decision_id"],
                delivery_target_id=chain["delivery_target_id"],
                idempotency_key=f"idem_ae_{new_id('k')}",
            )
            lineage = lock_and_validate_enrollment_candidate(
                s, CTX_SANDBOX, candidate_id=candidate.id,
            )
            return s, lineage

    def test_resolve_creates_snapshot(self) -> None:
        """Happy path: attestation exists, snapshot is created."""
        chain = self._chain
        with tenant_session(CTX_SANDBOX) as s:
            candidate = create_enrollment_candidate(
                s, CTX_SANDBOX,
                decision_id=chain["decision_id"],
                delivery_target_id=chain["delivery_target_id"],
                idempotency_key=f"idem_ae_snap_{new_id('k')}",
            )
            lineage = lock_and_validate_enrollment_candidate(
                s, CTX_SANDBOX, candidate_id=candidate.id,
            )
            now = datetime.now(timezone.utc)
            snapshot = resolve_activation_evidence(
                s, CTX_SANDBOX, lineage=lineage, reference_time=now,
            )
            self.assertIsNotNone(snapshot)
            self.assertTrue(snapshot.id.startswith("sdes_") or "sdes" in snapshot.id)
            self.assertEqual(snapshot.delivery_target_id, chain["delivery_target_id"])
            self.assertEqual(snapshot.workspace_id, CTX_SANDBOX.workspace_id)

    def test_resolve_reuses_existing_snapshot(self) -> None:
        """Second call with same target/attestation returns existing snapshot."""
        chain = self._chain
        with tenant_session(CTX_SANDBOX) as s:
            candidate = create_enrollment_candidate(
                s, CTX_SANDBOX,
                decision_id=chain["decision_id"],
                delivery_target_id=chain["delivery_target_id"],
                idempotency_key=f"idem_ae_reuse_{new_id('k')}",
            )
            lineage = lock_and_validate_enrollment_candidate(
                s, CTX_SANDBOX, candidate_id=candidate.id,
            )
            now = datetime.now(timezone.utc)
            snap1 = resolve_activation_evidence(
                s, CTX_SANDBOX, lineage=lineage, reference_time=now,
            )
            snap2 = resolve_activation_evidence(
                s, CTX_SANDBOX, lineage=lineage, reference_time=now,
            )
            self.assertEqual(snap1.id, snap2.id)

    def test_no_attestation_raises(self) -> None:
        """No valid attestation -> NoValidAttestationError."""
        chain = self._chain
        with tenant_session(CTX_SANDBOX) as s:
            candidate = create_enrollment_candidate(
                s, CTX_SANDBOX,
                decision_id=chain["decision_id"],
                delivery_target_id=chain["delivery_target_id"],
                idempotency_key=f"idem_ae_noatt_{new_id('k')}",
            )
            lineage = lock_and_validate_enrollment_candidate(
                s, CTX_SANDBOX, candidate_id=candidate.id,
            )
            # Use a savepoint so invalidation does not affect other tests
            from app.meta.evidence import invalidate_attestation
            from app.meta.models import EvidenceAttestationRecord
            from sqlalchemy import select
            target = lineage.target
            nested = s.begin_nested()
            atts = s.scalars(
                select(EvidenceAttestationRecord).where(
                    EvidenceAttestationRecord.contract_generation_id == target.contract_generation_id,
                    EvidenceAttestationRecord.state == "valid",
                )
            ).all()
            for att in atts:
                invalidate_attestation(s, attestation_id=att.id, reason="test")

            with self.assertRaises(NoValidAttestationError):
                resolve_activation_evidence(
                    s, CTX_SANDBOX, lineage=lineage,
                    reference_time=datetime.now(timezone.utc),
                )
            nested.rollback()

    def test_stale_evidence_raises(self) -> None:
        """Snapshot observed_at > 36 hours ago -> EvidenceStaleError."""
        chain = self._chain
        with tenant_session(CTX_SANDBOX) as s:
            candidate = create_enrollment_candidate(
                s, CTX_SANDBOX,
                decision_id=chain["decision_id"],
                delivery_target_id=chain["delivery_target_id"],
                idempotency_key=f"idem_ae_stale_{new_id('k')}",
            )
            lineage = lock_and_validate_enrollment_candidate(
                s, CTX_SANDBOX, candidate_id=candidate.id,
            )
            # Manually back-date all existing snapshots for this target to
            # 48h ago so the staleness check fires.
            from sqlalchemy import update
            target = lineage.target
            s.execute(
                update(ScaleDeliveryEvidenceSnapshotRecord)
                .where(
                    ScaleDeliveryEvidenceSnapshotRecord.delivery_target_id == target.id,
                )
                .values(observed_at=datetime.now(timezone.utc) - timedelta(hours=48))
            )
            s.flush()
            with self.assertRaises(EvidenceStaleError):
                resolve_activation_evidence(
                    s, CTX_SANDBOX, lineage=lineage,
                    reference_time=datetime.now(timezone.utc),
                )

    def test_future_evidence_raises(self) -> None:
        """Snapshot observed_at > 30s in the future -> EvidenceFromFutureError."""
        chain = self._chain
        with tenant_session(CTX_SANDBOX) as s:
            candidate = create_enrollment_candidate(
                s, CTX_SANDBOX,
                decision_id=chain["decision_id"],
                delivery_target_id=chain["delivery_target_id"],
                idempotency_key=f"idem_ae_future_{new_id('k')}",
            )
            lineage = lock_and_validate_enrollment_candidate(
                s, CTX_SANDBOX, candidate_id=candidate.id,
            )
            from sqlalchemy import update
            target = lineage.target
            # Create snapshot then shift observed_at 5 minutes into future.
            resolve_activation_evidence(
                s, CTX_SANDBOX, lineage=lineage,
                reference_time=datetime.now(timezone.utc),
            )
            nested = s.begin_nested()
            s.execute(
                update(ScaleDeliveryEvidenceSnapshotRecord)
                .where(
                    ScaleDeliveryEvidenceSnapshotRecord.delivery_target_id == target.id,
                )
                .values(observed_at=datetime.now(timezone.utc) + timedelta(minutes=5))
            )
            s.flush()
            with self.assertRaises(EvidenceFromFutureError):
                resolve_activation_evidence(
                    s, CTX_SANDBOX, lineage=lineage,
                    reference_time=datetime.now(timezone.utc),
                )
            nested.rollback()

    def test_snapshot_bound_to_exact_target(self) -> None:
        """Resolved snapshot matches exact delivery_target_id and fingerprint."""
        chain = self._chain
        with tenant_session(CTX_SANDBOX) as s:
            candidate = create_enrollment_candidate(
                s, CTX_SANDBOX,
                decision_id=chain["decision_id"],
                delivery_target_id=chain["delivery_target_id"],
                idempotency_key=f"idem_ae_exact_{new_id('k')}",
            )
            lineage = lock_and_validate_enrollment_candidate(
                s, CTX_SANDBOX, candidate_id=candidate.id,
            )
            now = datetime.now(timezone.utc)
            snapshot = resolve_activation_evidence(
                s, CTX_SANDBOX, lineage=lineage, reference_time=now,
            )
            self.assertEqual(
                snapshot.delivery_target_id, chain["delivery_target_id"],
            )
            self.assertEqual(
                snapshot.delivery_target_fingerprint,
                lineage.target.target_fingerprint,
            )
            self.assertEqual(
                snapshot.attestation_id, chain["attestation_id"],
            )


# ---------------------------------------------------------------------------
# create_activation_preview tests
# ---------------------------------------------------------------------------


class TestCreateActivationPreview(CandidateTestBase):

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._chain = TestResolveActivationEvidence._build_chain_with_attestation()

    def _make_draft_enrollment(self) -> str:
        chain = self._chain
        idem = f"idem_prev_{new_id('k')}"
        with tenant_session(CTX_SANDBOX) as s:
            candidate = create_enrollment_candidate(
                s, CTX_SANDBOX,
                decision_id=chain["decision_id"],
                delivery_target_id=chain["delivery_target_id"],
                idempotency_key=f"cand_{idem}",
            )
            candidate_id = candidate.id
        with tenant_session(CTX_SANDBOX) as s:
            enrollment = create_draft_enrollment(
                s, CTX_SANDBOX,
                enrollment_candidate_id=candidate_id,
                idempotency_key=idem,
            )
            return enrollment.id, enrollment.revision

    def test_create_preview_happy_path(self) -> None:
        """Draft enrollment -> preview created with correct fields."""
        enrollment_id, revision = self._make_draft_enrollment()
        with tenant_session(CTX_SANDBOX) as s:
            preview = create_activation_preview(
                s, CTX_SANDBOX,
                enrollment_id=enrollment_id,
                expected_revision=revision,
                idempotency_key=f"prev_{new_id('k')}",
            )
            self.assertIsNotNone(preview)
            self.assertEqual(preview.enrollment_id, enrollment_id)
            self.assertEqual(preview.enrollment_revision, revision)
            self.assertEqual(preview.transition_kind, "activate")
            self.assertIsNotNone(preview.lineage_hash)
            self.assertIsNotNone(preview.candidate_fingerprint)
            self.assertIsNone(preview.consumed_at)
            self.assertIsNotNone(preview.expires_at)
            # Expires in ~15 min
            ttl = preview.expires_at.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)
            self.assertGreater(ttl.total_seconds(), 800)
            self.assertLess(ttl.total_seconds(), 1000)

    def test_preview_wrong_revision(self) -> None:
        """Expected revision mismatch -> PreviewRevisionMismatchError."""
        enrollment_id, revision = self._make_draft_enrollment()
        with tenant_session(CTX_SANDBOX) as s:
            with self.assertRaises(PreviewRevisionMismatchError):
                create_activation_preview(
                    s, CTX_SANDBOX,
                    enrollment_id=enrollment_id,
                    expected_revision=revision + 999,
                    idempotency_key=f"prev_{new_id('k')}",
                )

    def test_preview_enrollment_not_found(self) -> None:
        """Non-existent enrollment -> EnrollmentNotFoundError."""
        with tenant_session(CTX_SANDBOX) as s:
            with self.assertRaises(EnrollmentNotFoundError):
                create_activation_preview(
                    s, CTX_SANDBOX,
                    enrollment_id="nonexistent_enrollment_id",
                    expected_revision=1,
                    idempotency_key=f"prev_{new_id('k')}",
                )

    def test_preview_idempotent_replay(self) -> None:
        """Same idempotency key returns the same preview."""
        enrollment_id, revision = self._make_draft_enrollment()
        idem = f"prev_idem_{new_id('k')}"
        with tenant_session(CTX_SANDBOX) as s:
            p1 = create_activation_preview(
                s, CTX_SANDBOX,
                enrollment_id=enrollment_id,
                expected_revision=revision,
                idempotency_key=idem,
            )
        with tenant_session(CTX_SANDBOX) as s:
            p2 = create_activation_preview(
                s, CTX_SANDBOX,
                enrollment_id=enrollment_id,
                expected_revision=revision,
                idempotency_key=idem,
            )
            self.assertEqual(p1.id, p2.id)


# ---------------------------------------------------------------------------
# load_and_consume_preview tests
# ---------------------------------------------------------------------------


class TestLoadAndConsumePreview(CandidateTestBase):

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._chain = TestResolveActivationEvidence._build_chain_with_attestation()

    def _make_preview(self) -> tuple[str, str, int]:
        """Returns (preview_id, enrollment_id, enrollment_revision)."""
        chain = self._chain
        idem = f"idem_cons_{new_id('k')}"
        with tenant_session(CTX_SANDBOX) as s:
            candidate = create_enrollment_candidate(
                s, CTX_SANDBOX,
                decision_id=chain["decision_id"],
                delivery_target_id=chain["delivery_target_id"],
                idempotency_key=f"cand_{idem}",
            )
            candidate_id = candidate.id
        with tenant_session(CTX_SANDBOX) as s:
            enrollment = create_draft_enrollment(
                s, CTX_SANDBOX,
                enrollment_candidate_id=candidate_id,
                idempotency_key=idem,
            )
            enrollment_id = enrollment.id
            revision = enrollment.revision
        with tenant_session(CTX_SANDBOX) as s:
            preview = create_activation_preview(
                s, CTX_SANDBOX,
                enrollment_id=enrollment_id,
                expected_revision=revision,
                idempotency_key=f"prev_{idem}",
            )
            return preview.id, enrollment_id, revision

    def test_consume_preview_happy_path(self) -> None:
        """Consume a fresh preview -> sets consumed_at."""
        preview_id, enrollment_id, revision = self._make_preview()
        with tenant_session(CTX_SANDBOX) as s:
            consumed = load_and_consume_preview(
                s, CTX_SANDBOX,
                preview_id=preview_id,
                enrollment_id=enrollment_id,
                expected_enrollment_revision=revision,
            )
            self.assertIsNotNone(consumed.consumed_at)
            self.assertEqual(consumed.id, preview_id)

    def test_consume_already_consumed(self) -> None:
        """Double-consume -> PreviewAlreadyConsumedError."""
        preview_id, enrollment_id, revision = self._make_preview()
        with tenant_session(CTX_SANDBOX) as s:
            load_and_consume_preview(
                s, CTX_SANDBOX,
                preview_id=preview_id,
                enrollment_id=enrollment_id,
                expected_enrollment_revision=revision,
            )
        with tenant_session(CTX_SANDBOX) as s:
            with self.assertRaises(PreviewAlreadyConsumedError):
                load_and_consume_preview(
                    s, CTX_SANDBOX,
                    preview_id=preview_id,
                    enrollment_id=enrollment_id,
                    expected_enrollment_revision=revision,
                )

    def test_consume_expired_preview(self) -> None:
        """Expired preview -> PreviewExpiredError."""
        preview_id, enrollment_id, revision = self._make_preview()
        with tenant_session(CTX_SANDBOX) as s:
            row = s.get(ScaleActivationPreviewRecord, preview_id)
            row.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
            s.flush()
        with tenant_session(CTX_SANDBOX) as s:
            with self.assertRaises(PreviewExpiredError):
                load_and_consume_preview(
                    s, CTX_SANDBOX,
                    preview_id=preview_id,
                    enrollment_id=enrollment_id,
                    expected_enrollment_revision=revision,
                )

    def test_consume_revision_mismatch(self) -> None:
        """Wrong enrollment revision -> PreviewRevisionMismatchError."""
        preview_id, enrollment_id, revision = self._make_preview()
        with tenant_session(CTX_SANDBOX) as s:
            with self.assertRaises(PreviewRevisionMismatchError):
                load_and_consume_preview(
                    s, CTX_SANDBOX,
                    preview_id=preview_id,
                    enrollment_id=enrollment_id,
                    expected_enrollment_revision=revision + 999,
                )


if __name__ == "__main__":
    unittest.main()
