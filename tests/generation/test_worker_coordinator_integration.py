"""Integration tests for the coordinated Generation handler (Issue #98 Task 1).

Exercises the three-phase ``ExternalCallCoordinator`` dispatch seam for
Generation RunItems in ``controlled_mock`` execution mode.  Follows the
same in-memory SQLite pattern as ``tests/test_provider_dispatch.py``.

The key properties tested:

1. The ``ProviderCallAttemptRecord`` is committed *before* the adapter's
   ``execute()`` method runs, so it survives a SIGKILL between phases.
2. A simulated crash between prepare and dispatch leaves a recoverable
   ``prepared`` attempt -- re-entry dispatches it without creating a
   duplicate.
3. A simulated crash during dispatch (``dispatching`` state) is classified
   ``uncertain`` on recovery -- never blindly re-dispatched.
4. The ``AssetVersionRecord`` and ``RunItemAssetVersionBindingRecord`` are
   created in the coordinator's completion transaction, atomically with the
   attempt's ``succeeded`` terminal state.
"""
from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterator

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import (
    AssetVersionRecord,
    GenerationRunRecord,
    JobRecord,
    OperationRecord,
    PlanRecord,
    ProductRecord,
    WorkspaceRecord,
    new_id,
)
from app.entitlements.cost_models import (
    ProviderCallAttemptRecord,
)
# Side-effect imports for FK resolution -- mirrors test_provider_dispatch.py.
from app.entitlements.credentials import (  # noqa: F401
    CredentialBindingRecord,
    CredentialVersionRecord,
)
from app.generation.controlled_adapter import (
    ControlledGenerationAdapter,
    GenerationOutput,
    GenerationRequest,
)
from app.generation.evidence import GenerationEvidenceRecorder
from app.generation.models import (
    GenerationRecipeSnapshotRecord,
    GenerationRunCommandRecord,
    RunItemAssetVersionBindingRecord,
    RunItemRecord,
)
from app.generation.worker import handle_run_item_coordinated
from app.platform.tenancy import TenantContext
from app.providers.adapter import ProviderCapabilities, ProviderDispatchError, ProviderResult
from app.providers.dispatch import (
    CoordinatedCallResult,
    ExternalCallCoordinator,
)

WORKSPACE_ID = "ws_gen_coord_test"
PRODUCT_ID = "prod_gen_coord_1"
PLAN_ID = "plan_gen_coord_1"
OPERATION_ID = "op_gen_coord_1"
JOB_ID = "job_gen_coord_1"
RUN_ID = "run_gen_coord_1"
RECIPE_SNAPSHOT_ID = "rs_gen_coord_1"
RUN_ITEM_ID = "ri_gen_coord_1"
SEMANTIC_FP = "abcdef1234567890abcdef1234567890"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_ctx() -> TenantContext:
    return TenantContext(
        workspace_id=WORKSPACE_ID,
        role="system",
        executor="system:worker",
        initiator_id="user_test",
        initiator_role="owner",
    )


class CoordinatedGenerationTestBase(unittest.IsolatedAsyncioTestCase):
    """In-memory SQLite database seeded with the minimum rows the
    coordinated generation handler needs: Workspace, Product, Plan,
    Operation, Job, GenerationRun, RecipeSnapshot, and a RunItem.
    """

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")

        @event.listens_for(self.engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(self.engine)
        self._SessionLocal = sessionmaker(
            bind=self.engine, expire_on_commit=False, autoflush=False,
        )

        @contextmanager
        def session_factory(ctx: TenantContext) -> Iterator[Session]:
            session = self._SessionLocal()
            session.info["tenant_context"] = ctx
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        self.session_factory = session_factory
        self.coordinator = ExternalCallCoordinator(session_factory=session_factory)
        self._seed_database()

    def _seed_database(self):
        now = _utcnow()
        session = self._SessionLocal()
        try:
            session.add(WorkspaceRecord(
                id=WORKSPACE_ID, name="Test Workspace", created_at=now,
            ))
            session.flush()

            session.add(ProductRecord(
                id=PRODUCT_ID,
                workspace_id=WORKSPACE_ID,
                name="Test Product",
                created_at=now,
                updated_at=now,
            ))
            session.flush()

            session.add(PlanRecord(
                id=PLAN_ID,
                workspace_id=WORKSPACE_ID,
                product_id=PRODUCT_ID,
                mode="controlled_mock",
                cap=10,
                total_full=1,
                snapshot_hash="snap_hash_1",
                created_at=now,
            ))
            session.flush()

            session.add(OperationRecord(
                id=OPERATION_ID,
                workspace_id=WORKSPACE_ID,
                command_type="generation.submit",
                state="running",
                created_at=now,
                updated_at=now,
            ))
            session.flush()

            session.add(JobRecord(
                id=JOB_ID,
                workspace_id=WORKSPACE_ID,
                operation_id=OPERATION_ID,
                type="generation.run_item",
                payload_json={"run_item_id": RUN_ITEM_ID},
                idempotency_key=f"gen_run_item:{RUN_ITEM_ID}",
                state="running",
                initiator_role="owner",
                lease_owner="worker-test",
                resource_type="run_item",
                resource_id=RUN_ITEM_ID,
                created_at=now,
            ))
            session.flush()

            session.add(GenerationRecipeSnapshotRecord(
                id=RECIPE_SNAPSHOT_ID,
                workspace_id=WORKSPACE_ID,
                canonicalization_version="v1",
                recipe_hash="recipe_hash_1",
                execution_mode="controlled_mock",
                canonical_json={
                    "truth_dependency": {
                        "truth_package_id": None,
                        "expected": True,
                        "policy_version": "v1",
                        "evidence": {
                            "kind": "controlled_mock",
                            "frozen_at": now.isoformat(),
                        },
                    },
                },
                created_at=now,
            ))
            session.flush()

            session.add(GenerationRunRecord(
                id=RUN_ID,
                workspace_id=WORKSPACE_ID,
                product_id=PRODUCT_ID,
                plan_id=PLAN_ID,
                provider="controlled_mock",
                model="mock-v1",
                aspect_ratio="1:1",
                image_size="1024x1024",
                variants=1,
                use_ref=False,
                status="running",
                total=1,
                done=0,
                failed=0,
                currency="USD",
                revision=1,
                recipe_snapshot_id=RECIPE_SNAPSHOT_ID,
                creation_operation_id=OPERATION_ID,
                submission_kind="confirmed_preview",
                execution_mode="controlled_mock",
                created_at=now,
                updated_at=now,
            ))
            session.flush()

            session.add(GenerationRunCommandRecord(
                id=new_id("grc"),
                workspace_id=WORKSPACE_ID,
                generation_run_id=RUN_ID,
                operation_id=OPERATION_ID,
                command_kind="submit",
                expected_run_revision=1,
                actor_id="user_test",
                created_at=now,
            ))
            session.flush()

            session.add(RunItemRecord(
                id=RUN_ITEM_ID,
                workspace_id=WORKSPACE_ID,
                generation_run_id=RUN_ID,
                ordinal=0,
                combination_identity_json={"a": "1"},
                combination_fingerprint="combo_fp_1",
                tags_json={},
                semantic_fingerprint=SEMANTIC_FP,
                execution_mode="controlled_mock",
                state="pending",
                revision=1,
                created_at=now,
                updated_at=now,
            ))
            session.flush()

            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- helpers -----------------------------------------------------------

    def _run_item(self) -> RunItemRecord:
        with self._SessionLocal() as session:
            return session.execute(
                select(RunItemRecord).where(
                    RunItemRecord.id == RUN_ITEM_ID,
                    RunItemRecord.workspace_id == WORKSPACE_ID,
                )
            ).scalar_one()

    def _attempt_rows(self) -> list[ProviderCallAttemptRecord]:
        with self._SessionLocal() as session:
            return (
                session.query(ProviderCallAttemptRecord)
                .filter_by(workspace_id=WORKSPACE_ID)
                .all()
            )

    def _asset_versions(self) -> list[AssetVersionRecord]:
        with self._SessionLocal() as session:
            return session.execute(
                select(AssetVersionRecord).where(
                    AssetVersionRecord.workspace_id == WORKSPACE_ID,
                )
            ).scalars().all()

    def _bindings(self) -> list[RunItemAssetVersionBindingRecord]:
        with self._SessionLocal() as session:
            return session.execute(
                select(RunItemAssetVersionBindingRecord).where(
                    RunItemAssetVersionBindingRecord.workspace_id == WORKSPACE_ID,
                )
            ).scalars().all()


class TestCoordinatedGenerationSuccess(CoordinatedGenerationTestBase):
    """The coordinated path completes successfully: prepare -> dispatch ->
    complete all commit, the RunItem reaches ``qc_pending``, and the
    AssetVersion is created."""

    async def test_full_success_path(self):
        ctx = _make_ctx()
        adapter = ControlledGenerationAdapter()
        evidence_recorder = GenerationEvidenceRecorder(RUN_ITEM_ID)

        await handle_run_item_coordinated(
            self.coordinator,
            ctx,
            RUN_ITEM_ID,
            adapter=adapter,
            evidence_recorder=evidence_recorder,
            session_factory=self.session_factory,
        )

        # RunItem should be qc_pending.
        item = self._run_item()
        self.assertEqual(item.state, "qc_pending")

        # Exactly one ProviderCallAttemptRecord should exist, in
        # ``succeeded`` state.
        attempts = self._attempt_rows()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].dispatch_state, "succeeded")
        self.assertEqual(attempts[0].execution_mode, "controlled_mock")
        self.assertEqual(attempts[0].chargeability, "free_sandbox")
        self.assertIsNone(attempts[0].spend_reservation_id)
        self.assertEqual(attempts[0].step_kind, "generation")
        self.assertEqual(attempts[0].run_item_id, RUN_ITEM_ID)
        self.assertEqual(len(attempts[0].call_semantic_key_hash), 64)

        # One AssetVersion should have been created.
        versions = self._asset_versions()
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0].origin_kind, "generation")
        self.assertEqual(versions[0].execution_mode, "controlled_mock")
        self.assertEqual(versions[0].provider_attempt_id, attempts[0].id)
        self.assertEqual(versions[0].producing_run_item_id, RUN_ITEM_ID)

        # One binding should link the RunItem to the AssetVersion.
        bindings = self._bindings()
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0].run_item_id, RUN_ITEM_ID)
        self.assertEqual(bindings[0].asset_version_id, versions[0].id)
        self.assertEqual(bindings[0].kind, "produced")
        self.assertEqual(bindings[0].producing_provider_attempt_id, attempts[0].id)


class TestAttemptRecordSurvivesSIGKILL(CoordinatedGenerationTestBase):
    """The prepare phase commits the ``ProviderCallAttemptRecord`` before
    the adapter's ``execute()`` runs, so a simulated SIGKILL after prepare
    but before dispatch leaves a recoverable ``prepared`` attempt."""

    async def test_attempt_committed_before_dispatch(self):
        """Simulate prepare completing and then a crash before the adapter
        is called. The attempt row must exist in the DB in ``prepared``
        state -- proving it would survive a SIGKILL."""
        ctx = _make_ctx()
        evidence_recorder = GenerationEvidenceRecorder(RUN_ITEM_ID)

        # Phase 1 only: prepare the attempt.
        prepared = self.coordinator._prepare_in_transaction(
            ctx,
            job_id=JOB_ID,
            operation_id=OPERATION_ID,
            call_semantic_key_hash=f"gen:{SEMANTIC_FP}",
            step_kind="generation",
            execution_mode="controlled_mock",
            chargeability="free_sandbox",
            provider="controlled_mock",
            evidence_recorder=evidence_recorder,
            spend_reservation_id=None,
            truth_package_id=None,
            run_item_id=RUN_ITEM_ID,
            model=None,
        )
        self.assertFalse(prepared["replay"])
        attempt_id = prepared["attempt_id"]

        # The attempt row must exist in the DB (committed by the prepare
        # phase), even though the adapter has not been called.
        attempts = self._attempt_rows()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].id, attempt_id)
        self.assertEqual(attempts[0].dispatch_state, "prepared")

        # The RunItem should be in ``running`` state (transitioned by the
        # evidence recorder's link_prepared_attempt).
        item = self._run_item()
        self.assertEqual(item.state, "running")

    async def test_recovery_after_prepare_crash_does_not_duplicate(self):
        """After a crash post-prepare, re-entering coordinator.execute()
        reuses the existing attempt -- it does NOT create a second one."""
        ctx = _make_ctx()

        # First pass: prepare only, then crash.
        rec1 = GenerationEvidenceRecorder(RUN_ITEM_ID)
        prepared = self.coordinator._prepare_in_transaction(
            ctx,
            job_id=JOB_ID,
            operation_id=OPERATION_ID,
            call_semantic_key_hash=f"gen:{SEMANTIC_FP}",
            step_kind="generation",
            execution_mode="controlled_mock",
            chargeability="free_sandbox",
            provider="controlled_mock",
            evidence_recorder=rec1,
            spend_reservation_id=None,
            truth_package_id=None,
            run_item_id=RUN_ITEM_ID,
            model=None,
        )
        attempt_id = prepared["attempt_id"]

        # Second pass: full execute reuses the prepared attempt.
        adapter = ControlledGenerationAdapter()
        rec2 = GenerationEvidenceRecorder(RUN_ITEM_ID)
        result = await self.coordinator.execute(
            ctx,
            job_id=JOB_ID,
            operation_id=OPERATION_ID,
            call_semantic_key_hash=f"gen:{SEMANTIC_FP}",
            step_kind="generation",
            execution_mode="controlled_mock",
            chargeability="free_sandbox",
            provider="controlled_mock",
            request=GenerationRequest(
                semantic_fingerprint=SEMANTIC_FP,
                execution_mode="controlled_mock",
            ),
            adapter=adapter,
            evidence_recorder=rec2,
            credential=None,
            spend_reservation_id=None,
            run_item_id=RUN_ITEM_ID,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.attempt_id, attempt_id)

        # Still exactly one attempt row -- no duplicate.
        attempts = self._attempt_rows()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].dispatch_state, "succeeded")


class TestDispatchingCrashClassifiesUncertain(CoordinatedGenerationTestBase):
    """A crash during dispatch (``dispatching`` state committed, no result
    evidence) is classified ``uncertain`` on re-entry -- never blindly
    re-dispatched."""

    async def test_dispatching_crash_becomes_uncertain(self):
        ctx = _make_ctx()

        # Phase 1: prepare.
        rec1 = GenerationEvidenceRecorder(RUN_ITEM_ID)
        prepared = self.coordinator._prepare_in_transaction(
            ctx,
            job_id=JOB_ID,
            operation_id=OPERATION_ID,
            call_semantic_key_hash=f"gen:{SEMANTIC_FP}",
            step_kind="generation",
            execution_mode="controlled_mock",
            chargeability="free_sandbox",
            provider="controlled_mock",
            evidence_recorder=rec1,
            spend_reservation_id=None,
            truth_package_id=None,
            run_item_id=RUN_ITEM_ID,
            model=None,
        )
        attempt_id = prepared["attempt_id"]

        # Phase 2 partially: commit ``dispatching`` but crash before
        # the adapter returns.
        self.coordinator._mark_dispatching(ctx, attempt_id=attempt_id)
        attempts = self._attempt_rows()
        self.assertEqual(attempts[0].dispatch_state, "dispatching")

        # Recovery re-entry via execute() must classify it uncertain
        # without calling the adapter.
        adapter = ControlledGenerationAdapter()
        rec2 = GenerationEvidenceRecorder(RUN_ITEM_ID)
        result = await self.coordinator.execute(
            ctx,
            job_id=JOB_ID,
            operation_id=OPERATION_ID,
            call_semantic_key_hash=f"gen:{SEMANTIC_FP}",
            step_kind="generation",
            execution_mode="controlled_mock",
            chargeability="free_sandbox",
            provider="controlled_mock",
            request=GenerationRequest(
                semantic_fingerprint=SEMANTIC_FP,
                execution_mode="controlled_mock",
            ),
            adapter=adapter,
            evidence_recorder=rec2,
            credential=None,
            spend_reservation_id=None,
            run_item_id=RUN_ITEM_ID,
        )

        # Must be classified uncertain, not re-dispatched.
        self.assertTrue(result.replayed)
        self.assertFalse(result.success)
        self.assertEqual(result.dispatch_certainty, "uncertain")

        # The attempt row must now be ``uncertain``.
        attempts = self._attempt_rows()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].dispatch_state, "uncertain")

        # No AssetVersion should have been created (dispatch was uncertain).
        versions = self._asset_versions()
        self.assertEqual(len(versions), 0)


class TestIdempotentReplay(CoordinatedGenerationTestBase):
    """A second call to execute() for the same job replays the stored
    outcome without re-dispatching or re-materializing."""

    async def test_replay_after_success(self):
        ctx = _make_ctx()
        adapter = ControlledGenerationAdapter()
        rec1 = GenerationEvidenceRecorder(RUN_ITEM_ID)

        first = await self.coordinator.execute(
            ctx,
            job_id=JOB_ID,
            operation_id=OPERATION_ID,
            call_semantic_key_hash=f"gen:{SEMANTIC_FP}",
            step_kind="generation",
            execution_mode="controlled_mock",
            chargeability="free_sandbox",
            provider="controlled_mock",
            request=GenerationRequest(
                semantic_fingerprint=SEMANTIC_FP,
                execution_mode="controlled_mock",
            ),
            adapter=adapter,
            evidence_recorder=rec1,
            credential=None,
            spend_reservation_id=None,
            run_item_id=RUN_ITEM_ID,
        )
        self.assertTrue(first.success)
        self.assertFalse(first.replayed)

        # Second call: should replay without re-dispatching.
        rec2 = GenerationEvidenceRecorder(RUN_ITEM_ID)
        second = await self.coordinator.execute(
            ctx,
            job_id=JOB_ID,
            operation_id=OPERATION_ID,
            call_semantic_key_hash=f"gen:{SEMANTIC_FP}",
            step_kind="generation",
            execution_mode="controlled_mock",
            chargeability="free_sandbox",
            provider="controlled_mock",
            request=GenerationRequest(
                semantic_fingerprint=SEMANTIC_FP,
                execution_mode="controlled_mock",
            ),
            adapter=ControlledGenerationAdapter(),
            evidence_recorder=rec2,
            credential=None,
            spend_reservation_id=None,
            run_item_id=RUN_ITEM_ID,
        )

        self.assertTrue(second.replayed)
        self.assertTrue(second.success)
        self.assertEqual(second.attempt_id, first.attempt_id)

        # Still exactly one attempt, one AssetVersion.
        self.assertEqual(len(self._attempt_rows()), 1)
        self.assertEqual(len(self._asset_versions()), 1)


if __name__ == "__main__":
    unittest.main()
