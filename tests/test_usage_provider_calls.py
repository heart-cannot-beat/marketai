"""Usage ``provider_calls`` (Issue #8, slice S1).

The registry refuses unknown metrics, wrong units and negative quantities;
``record_many`` writes a semantic key once and reports duplicates; and every
provider-call settlement point counts a call exactly when the provider is
known to have been called, in the settlement's own transaction:

| Settlement point                          | Counted                            |
|-------------------------------------------|------------------------------------|
| ``ExternalCallCoordinator`` success       | yes                                |
| ``ExternalCallCoordinator`` billed failure| yes                                |
| ``ExternalCallCoordinator`` not_dispatched| no                                 |
| ``ExternalCallCoordinator`` uncertain     | no, until ``reconcile()`` settles  |
| competitor scan ``settle_uncertain_call`` | charged: yes; not charged: no      |
| marketing planner                         | BYOK answer: yes; Mock/timeout: no |
| marketing AI QC                           | BYOK answer: yes; Mock/timeout: no |
| Mock generation ``handle_run_item``       | success: yes (mock); crash: no     |

Marketing generation (``paid_execution``) is covered in
``tests/test_generation_selection.py``; the PostgreSQL roles, RLS and the
append-only trigger in ``tests/test_usage_pg.py``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import config
from app.adapters.persistence.database import Base, tenant_session
from app.adapters.persistence.models import JobRecord, WorkspaceRecord
from app.competitors.scan_reconcile import settle_uncertain_call
from app.entitlements.cost_models import ProviderCallAttemptRecord, ProviderCostLedgerEntryRecord
from app.entitlements.usage import (
    METRICS,
    PROVIDER_CALLS,
    UsageFact,
    UsageFactError,
    UsageQuery,
    UsageRecorder,
    record_provider_call,
    usage_execution_mode,
    usage_semantic_key,
)
from app.entitlements.usage_models import UsageEventRecord
from app.main import app
from app.marketing.ai_qc_models import MarketingAIQCRecord
from app.marketing.models import PlanningCallRecord
from app.marketing.planner import example_result
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext
from app.providers.adapter import ProviderDispatchError
from tests.support.library_fixtures import LibraryFixtures
from tests.test_marketing_ai_qc import MarketingAIQCFixtures
from tests.test_marketing_planning import MarketingPlanningFixtures
from tests.test_provider_dispatch import (
    CALL_KEY_HASH,
    JOB_ID,
    OPERATION_ID,
    RESERVATION_ID,
    WORKSPACE_ID,
    DispatchCoordinatorTestBase,
    FakeAdapter,
    FakeEvidenceRecorder,
    _make_ctx,
    _Request,
)

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
CTX_A = TenantContext(workspace_id="ws_usage_a", role="owner", initiator_id="owner_a",
                      request_id="req", trace_id="trace")
CTX_B = TenantContext(workspace_id="ws_usage_b", role="owner", initiator_id="owner_b",
                      request_id="req", trace_id="trace")
#: What ``MarketingPlanningFixtures.create`` submits.
SOURCE_TEXT = "Alpha 充电器\n65W，多口，面向差旅办公用户。"
PLANNING_ENV = {"MARKETAI_PLANNING_PROVIDER": "openai", "MARKETAI_PLANNING_MODEL": "text-test",
                "MARKETAI_PLANNING_MAX_COST_USD": "0.10"}


def fact(source_id: str = "pca_1", **overrides) -> UsageFact:
    values = dict(
        metric_key="provider_calls",
        quantity=Decimal("1"),
        unit="call",
        execution_mode="byok",
        semantic_key_hash=usage_semantic_key("provider_calls", "provider_call_attempt", source_id),
        occurred_at=NOW,
        source_resource_type="provider_call_attempt",
        source_resource_id=source_id,
    )
    values.update(overrides)
    return UsageFact(**values)


def usage_rows(session, workspace_id: str) -> list[UsageEventRecord]:
    return list(session.scalars(select(UsageEventRecord).where(UsageEventRecord.workspace_id == workspace_id)))


# ---------------------------------------------------------------------------
# Registry, record_many and summarize (SQLite)
# ---------------------------------------------------------------------------

class UsageRecorderTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.session = sessionmaker(bind=engine)()
        self.addCleanup(self.session.close)
        for ctx in (CTX_A, CTX_B):
            self.session.add(WorkspaceRecord(id=ctx.workspace_id, name=ctx.workspace_id))
        self.session.commit()

    def record(self, *facts, ctx=CTX_A):
        return UsageRecorder().record_many(self.session, ctx, facts)

    def refused(self, code: str, *facts) -> None:
        with self.assertRaises(UsageFactError) as caught:
            self.record(*facts)
        self.assertEqual(caught.exception.code, code)
        self.session.commit()
        self.assertEqual(usage_rows(self.session, CTX_A.workspace_id), [])

    def test_the_registry_holds_provider_calls_only(self):
        self.assertEqual(set(METRICS), {"provider_calls"})
        self.assertEqual(
            (PROVIDER_CALLS.measurement_kind, PROVIDER_CALLS.direction, PROVIDER_CALLS.unit, PROVIDER_CALLS.aggregation),
            ("delta", "increase", "call", "sum"))

    def test_the_registry_refuses_unknown_metrics(self):
        for metric_key in ("provider_call", "storage_bytes", "active_members", ""):
            with self.subTest(metric_key=metric_key):
                self.refused("usage_metric_unknown", fact(metric_key=metric_key))

    def test_the_registry_refuses_a_wrong_unit(self):
        for unit in ("calls", "count", "image", ""):
            with self.subTest(unit=unit):
                self.refused("usage_unit_mismatch", fact(unit=unit))

    def test_the_registry_refuses_negative_and_non_finite_quantities(self):
        self.refused("usage_quantity_negative", fact(quantity=Decimal("-1")))
        self.refused("usage_quantity_negative", fact(quantity=Decimal("-0.000001")))
        for quantity in (Decimal("NaN"), Decimal("Infinity"), 1.0, 1):
            with self.subTest(quantity=quantity):
                self.refused("usage_quantity_invalid", fact(quantity=quantity))
        self.assertEqual(self.record(fact(quantity=Decimal("0"))).written, 1)

    def test_the_registry_refuses_unknown_modes_bad_keys_and_naive_times(self):
        for mode in ("controlled_mock", "production", "free_sandbox"):
            with self.subTest(mode=mode):
                self.refused("usage_execution_mode_unknown", fact(execution_mode=mode))
        self.refused("usage_semantic_key_invalid", fact(semantic_key_hash="pca_1"))
        self.refused("usage_semantic_key_invalid", fact(semantic_key_hash="A" * 64))
        self.refused("usage_occurred_at_naive", fact(occurred_at=NOW.replace(tzinfo=None)))

    def test_a_refused_fact_leaves_its_whole_batch_unwritten(self):
        self.refused("usage_unit_mismatch", fact("pca_ok"), fact("pca_bad", unit="calls"))

    def test_the_only_unique_keys_are_the_id_and_the_semantic_key(self):
        """``record_many`` skips any unique violation (it names no conflict target,
        which would need SELECT the Worker lacks); keep the table to these two keys."""
        from sqlalchemy import PrimaryKeyConstraint, UniqueConstraint

        table = UsageEventRecord.__table__
        keys = sorted(tuple(c.name for c in constraint.columns) for constraint in table.constraints
                      if isinstance(constraint, (PrimaryKeyConstraint, UniqueConstraint)))
        keys += [tuple(c.name for c in index.columns) for index in table.indexes if index.unique]
        self.assertEqual(sorted(keys), [("id",), ("workspace_id", "semantic_key_hash")])

    def test_the_same_key_is_written_once_and_counted_as_a_duplicate(self):
        # Alembic's ``fileConfig`` (run in-process by ``LibraryFixtures``) disables
        # every logger imported before it; switch this one back on to observe it.
        logger = logging.getLogger("app.entitlements.usage")
        self.addCleanup(setattr, logger, "disabled", logger.disabled)
        logger.disabled = False
        with self.assertLogs(logger, "INFO") as logs:
            first = self.record(fact("pca_1"))
            again = self.record(fact("pca_1"))
            batch = self.record(fact("pca_2"), fact("pca_2"), fact("pca_1"))
        self.session.commit()
        self.assertEqual((first.written, first.duplicates), (1, 0))
        self.assertEqual((again.written, again.duplicates), (0, 1))
        self.assertEqual((batch.written, batch.duplicates), (1, 2))
        rows = usage_rows(self.session, CTX_A.workspace_id)
        self.assertEqual(sorted(row.source_resource_id for row in rows), ["pca_1", "pca_2"])
        duplicates = [r for r in logs.records if r.getMessage() == "usage_event_duplicate"]
        self.assertEqual(len(duplicates), 3)
        self.assertEqual((duplicates[0].workspace_id, duplicates[0].metric_key, duplicates[0].source_resource_id),
                         (CTX_A.workspace_id, "provider_calls", "pca_1"))

    def test_the_key_is_scoped_to_the_workspace(self):
        self.assertEqual(self.record(fact("pca_1"), ctx=CTX_A).written, 1)
        self.assertEqual(self.record(fact("pca_1"), ctx=CTX_B).written, 1)

    def test_the_row_takes_kind_direction_and_unit_from_the_registry(self):
        self.record(fact("pca_1", execution_mode="mock"))
        self.session.commit()
        row = usage_rows(self.session, CTX_A.workspace_id)[0]
        self.assertEqual(
            (row.metric_key, row.measurement_kind, row.direction, row.unit, row.quantity, row.execution_mode),
            ("provider_calls", "delta", "increase", "call", Decimal("1"), "mock"))

    def test_a_rolled_back_transaction_leaves_no_usage(self):
        self.record(fact("pca_1"))
        self.session.rollback()
        self.assertEqual(usage_rows(self.session, CTX_A.workspace_id), [])

    def test_the_semantic_key_is_the_v1_attempt_hash(self):
        expected = hashlib.sha256(b"usage:v1:provider_calls:provider_call_attempt:pca_42").hexdigest()
        self.assertEqual(usage_semantic_key("provider_calls", "provider_call_attempt", "pca_42"), expected)
        self.assertNotEqual(usage_semantic_key("provider_calls", "provider_call_attempt", "pca_42", "x"), expected)

    def test_controlled_mock_is_a_mock_call_not_a_sandbox_call(self):
        self.assertEqual(
            {mode: usage_execution_mode(mode) for mode in ("byok", "sandbox", "mock", "controlled_mock")},
            {"byok": "byok", "sandbox": "sandbox", "mock": "mock", "controlled_mock": "mock"})
        with self.assertRaises(UsageFactError):
            usage_execution_mode("production")

    def test_record_provider_call_keys_the_attempt_and_refuses_another_workspace(self):
        attempt = SimpleNamespace(id="pca_7", workspace_id=CTX_A.workspace_id, execution_mode="controlled_mock")
        self.assertEqual(record_provider_call(self.session, CTX_A, attempt, occurred_at=NOW).written, 1)
        self.assertEqual(record_provider_call(self.session, CTX_A, attempt, occurred_at=NOW).duplicates, 1)
        with self.assertRaises(UsageFactError) as caught:
            record_provider_call(self.session, CTX_B, attempt, occurred_at=NOW)
        self.assertEqual(caught.exception.code, "usage_workspace_mismatch")
        self.session.commit()
        row = usage_rows(self.session, CTX_A.workspace_id)[0]
        self.assertEqual(row.semantic_key_hash, usage_semantic_key("provider_calls", "provider_call_attempt", "pca_7"))
        self.assertEqual((row.execution_mode, row.source_resource_type), ("mock", "provider_call_attempt"))
        self.assertEqual(usage_rows(self.session, CTX_B.workspace_id), [])

    def test_summarize_buckets_the_window_and_keeps_mock_out_of_the_total(self):
        start, end = NOW - timedelta(hours=3), NOW
        for source, mode, at in (
            ("in_byok_1", "byok", start),                       # the start is inside
            ("in_byok_2", "byok", end - timedelta(seconds=1)),
            ("in_sandbox", "sandbox", start + timedelta(hours=1)),
            ("in_mock_1", "mock", start + timedelta(hours=1)),
            ("in_mock_2", "mock", start + timedelta(hours=2)),
            ("in_na", "not_applicable", start + timedelta(hours=1)),
            ("at_end", "byok", end),                            # the end is outside
            ("before", "byok", start - timedelta(seconds=1)),
        ):
            self.record(fact(source, execution_mode=mode, occurred_at=at))
        self.record(fact("other_ws", occurred_at=start), ctx=CTX_B)
        self.session.commit()

        summary = UsageQuery().summarize(self.session, CTX_A, metric_key="provider_calls",
                                         window_start=start, window_end=end)
        self.assertEqual(dict(summary.by_execution_mode), {
            "byok": Decimal("2"), "sandbox": Decimal("1"), "mock": Decimal("2"), "not_applicable": Decimal("1")})
        self.assertEqual(summary.total, Decimal("4"))

        other_metric = UsageQuery().summarize(self.session, CTX_A, metric_key="storage_bytes",
                                              window_start=start, window_end=end)
        self.assertEqual(other_metric.total, Decimal("0"))
        self.assertEqual(set(other_metric.by_execution_mode.values()), {Decimal("0")})


# ---------------------------------------------------------------------------
# ExternalCallCoordinator (dispatch.py) and scan_reconcile
# ---------------------------------------------------------------------------

class BilledFailureAdapter(FakeAdapter):
    """The provider answered with an unusable result and billed for it."""

    async def execute(self, request, *, credential, idempotency_key):
        self.calls.append((request, credential, idempotency_key))
        raise ProviderDispatchError(dispatch_certainty="dispatched", message="unusable", error_code="bad_output")


class CoordinatorUsageTests(DispatchCoordinatorTestBase):
    def _rows(self) -> list[UsageEventRecord]:
        with self._SessionLocal() as session:
            return usage_rows(session, WORKSPACE_ID)

    async def _execute(self, adapter, **overrides):
        return await self.coordinator.execute(**self._execute_kwargs(
            request=_Request(payload="hello"), adapter=adapter, evidence_recorder=FakeEvidenceRecorder(),
            **overrides))

    async def test_a_byok_success_counts_one_call_when_it_settles(self):
        result = await self._execute(FakeAdapter(outcomes=["success"]))
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        attempt = self._attempt_row(result.attempt_id)
        self.assertEqual(
            (rows[0].metric_key, rows[0].execution_mode, rows[0].source_resource_type, rows[0].source_resource_id),
            ("provider_calls", "byok", "provider_call_attempt", result.attempt_id))
        self.assertEqual(rows[0].occurred_at, attempt.completed_at)

    async def test_a_billed_failure_counts_one_call(self):
        result = await self._execute(BilledFailureAdapter())
        self.assertEqual((result.success, result.dispatch_certainty), (False, "dispatched"))
        self.assertEqual(self._reservation_row(RESERVATION_ID).state, "settled")
        self.assertEqual([r.source_resource_id for r in self._rows()], [result.attempt_id])

    async def test_a_call_that_never_left_counts_nothing(self):
        result = await self._execute(FakeAdapter(outcomes=["terminal_failure"]))
        self.assertEqual(result.dispatch_certainty, "not_dispatched")
        self.assertEqual(self._rows(), [])

    async def test_an_uncertain_call_counts_once_reconcile_settles_it(self):
        first = await self._execute(FakeAdapter(outcomes=["uncertain_failure"]))
        self.assertEqual(first.dispatch_certainty, "uncertain")
        self.assertEqual(self._rows(), [])
        reconciled = await self.coordinator.reconcile(
            _make_ctx(), attempt_id=first.attempt_id,
            adapter=FakeAdapter(accepts_idempotency_key=True, outcomes=["success"]),
            evidence_recorder=FakeEvidenceRecorder(), request=_Request(payload="hello"))
        self.assertTrue(reconciled.success)
        self.assertEqual([r.source_resource_id for r in self._rows()], [first.attempt_id])

    async def test_replaying_execute_after_completion_still_counts_one(self):
        """Crash window 4: the outcome is committed; a redelivered Job replays it."""
        first = await self._execute(FakeAdapter(outcomes=["success"]))
        replay = await self._execute(FakeAdapter(outcomes=["success"]))
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.attempt_id, first.attempt_id)
        self.assertEqual(len(self._rows()), 1)

    async def test_a_failure_after_the_usage_write_rolls_back_the_ledger_and_the_usage(self):
        import app.providers.dispatch as dispatch

        def record_then_fail(*args, **kwargs):
            result = record_provider_call(*args, **kwargs)
            self.assertEqual(result.written, 1)
            raise RuntimeError("injected after settlement")

        with patch.object(dispatch, "record_provider_call", side_effect=record_then_fail):
            with self.assertRaisesRegex(RuntimeError, "injected after settlement"):
                await self._execute(FakeAdapter(outcomes=["success"]))
        self.assertEqual(self._ledger_entries_for(RESERVATION_ID), [])
        self.assertEqual(self._reservation_row(RESERVATION_ID).state, "reserved")
        self.assertEqual(self._rows(), [])

    async def test_a_controlled_mock_call_counts_in_the_mock_bucket(self):
        await self._execute(FakeAdapter(outcomes=["success"]), execution_mode="controlled_mock",
                            chargeability="free_sandbox", spend_reservation_id=None)
        self.assertEqual([r.execution_mode for r in self._rows()], ["mock"])
        with self._SessionLocal() as session:
            summary = UsageQuery().summarize(session, _make_ctx(), metric_key="provider_calls",
                                             window_start=NOW - timedelta(days=3650),
                                             window_end=datetime.now(timezone.utc) + timedelta(minutes=1))
        self.assertEqual((summary.total, summary.by_execution_mode["mock"]), (Decimal("0"), Decimal("1")))

    async def _uncertain_attempt(self) -> str:
        result = await self._execute(FakeAdapter(outcomes=["uncertain_failure"]))
        self.assertEqual(result.dispatch_certainty, "uncertain")
        return result.attempt_id

    def _settle(self, attempt_id: str, charged: bool) -> bool:
        with self.session_factory(_make_ctx()) as session:
            return settle_uncertain_call(session, _make_ctx(), attempt_id=attempt_id, charged=charged,
                                         prices=None, pricing_version="test", source="owner")

    async def test_a_scan_page_reconciled_as_charged_counts_one_call_once(self):
        attempt_id = await self._uncertain_attempt()
        self.assertTrue(self._settle(attempt_id, charged=True))
        self.assertFalse(self._settle(attempt_id, charged=True))  # already settled
        self.assertEqual([r.source_resource_id for r in self._rows()], [attempt_id])

    async def test_a_scan_page_reconciled_as_not_charged_counts_nothing(self):
        attempt_id = await self._uncertain_attempt()
        self.assertTrue(self._settle(attempt_id, charged=False))
        self.assertEqual(self._reservation_row(RESERVATION_ID).state, "released")
        self.assertEqual(self._rows(), [])


# ---------------------------------------------------------------------------
# Marketing planner and AI QC
# ---------------------------------------------------------------------------

def provider_call_rows(ctx, job_ids) -> list[UsageEventRecord]:
    """Usage rows of the attempts made for these Jobs."""
    with tenant_session(ctx) as session:
        attempt_ids = set(session.scalars(select(ProviderCallAttemptRecord.id).where(
            ProviderCallAttemptRecord.workspace_id == ctx.workspace_id,
            ProviderCallAttemptRecord.job_id.in_(list(job_ids)))))
        return [row for row in usage_rows(session, ctx.workspace_id) if row.source_resource_id in attempt_ids]


class PlannerUsageTests(MarketingPlanningFixtures):
    def paid_call(self):
        self.paid_setup()
        project = self.create()
        with patch.dict("os.environ", PLANNING_ENV):
            quote = self.client.get("/api/v1/marketing/capabilities").json()["paid"]
            return project, self.plan_call(project, mode="byok", quote_hash=quote["quote_hash"])

    def job_of(self, call) -> str:
        with tenant_session(self.context) as session:
            return session.get(PlanningCallRecord, call["id"]).job_id

    def test_a_byok_answer_counts_one_call_and_a_replay_counts_nothing(self):
        project, call = self.paid_call()
        answer = example_result("understanding", {"source_text": SOURCE_TEXT})
        complete = AsyncMock(return_value=json.dumps(answer))
        self.execute(call, complete)
        self.execute(call, complete)
        complete.assert_awaited_once()
        self.assertEqual(self.read(project)["latest_call"]["state"], "succeeded")
        rows = provider_call_rows(self.context, [self.job_of(call)])
        self.assertEqual([r.execution_mode for r in rows], ["byok"])

    def test_an_invalid_byok_answer_still_counts_the_call(self):
        project, call = self.paid_call()
        self.execute(call, AsyncMock(return_value="not JSON"))
        self.assertEqual(self.read(project)["latest_call"]["state"], "failed")
        self.assertEqual(len(provider_call_rows(self.context, [self.job_of(call)])), 1)

    def test_a_timed_out_byok_call_counts_nothing(self):
        project, call = self.paid_call()
        complete = AsyncMock(side_effect=TimeoutError())
        self.execute(call, complete)
        self.execute(call, complete)
        self.assertEqual(self.read(project)["latest_call"]["state"], "uncertain")
        self.assertEqual(provider_call_rows(self.context, [self.job_of(call)]), [])

    def test_a_mock_plan_creates_no_attempt_and_counts_nothing(self):
        project = self.create()
        self.execute(self.plan_call(project))
        self.assertEqual(self.read(project)["latest_call"]["state"], "succeeded")
        with tenant_session(self.context) as session:
            self.assertEqual(usage_rows(session, self.context.workspace_id), [])


class AIQCUsageTests(MarketingAIQCFixtures):
    def qc_jobs(self, ids) -> list[str]:
        with tenant_session(self.context) as session:
            return [session.get(MarketingAIQCRecord, call_id).job_id for call_id in ids]

    def answer(self, provider, model, key, inputs, images):
        return self.report(inputs)

    def test_byok_answers_count_one_call_each_valid_or_not(self):
        _, row = self.setup_round(paid=True)
        row, ids = self.run_kind(row, "single", AsyncMock(side_effect=self.answer))
        self.assertGreater(len(ids), 1)
        rows = provider_call_rows(self.context, self.qc_jobs(ids))
        self.assertEqual(len(rows), len(ids))
        self.assertEqual({r.execution_mode for r in rows}, {"byok"})

        row = self.client.get(f"/api/v1/marketing/rounds/{row['id']}").json()["data"]
        _, invalid = self.run_kind(row, "paired", AsyncMock(return_value={"summary": "Everything passes", "findings": []}))
        self.assertTrue(invalid)
        with tenant_session(self.context) as session:
            self.assertEqual({session.get(MarketingAIQCRecord, i).state for i in invalid}, {"failed"})
        self.assertEqual(len(provider_call_rows(self.context, self.qc_jobs(invalid))), len(invalid))

    def test_a_timed_out_byok_check_counts_nothing(self):
        _, row = self.setup_round(paid=True)
        row = self.command(row, "ai-qc", {"quote_hash": self.quote(row)["quote_hash"]}, 202)
        ids = self.execute_checks(row, AsyncMock(side_effect=TimeoutError("no response")))
        self.assertEqual(provider_call_rows(self.context, self.qc_jobs(ids)), [])

    def test_a_mock_check_creates_no_attempt_and_counts_nothing(self):
        _, row = self.setup_round()
        row = self.command(row, "ai-qc", {"quote_hash": self.quote(row)["quote_hash"]}, 202)
        ids = self.execute_checks(row, AsyncMock())
        self.assertEqual(provider_call_rows(self.context, self.qc_jobs(ids)), [])


# ---------------------------------------------------------------------------
# Mock generation (handle_run_item) and GET /api/v1/usage
# ---------------------------------------------------------------------------

class MockGenerationAndEndpointTests(LibraryFixtures):
    def _run_item(self, ctx):
        from app.generation.draft import create_draft
        from app.generation.models import RunItemRecord
        from app.generation.preview import create_preview
        from app.generation.submission import submit_generation
        from app.adapters.persistence.models import new_id

        with tenant_session(ctx) as session:
            draft = create_draft(session, ctx, product_id=self._product_id(ctx, session), provider="mock",
                                 model="mock-v1", execution_mode="mock",
                                 combination_strategy={"kind": "full", "planner_version": "v1"})
            preview = create_preview(session, ctx, draft.id, draft.revision)
            result = submit_generation(session, ctx, preview_id=preview.id, preview_hash=preview.preview_hash,
                                       expected_draft_revision=draft.revision, idempotency_key=new_id("ik"))
            return session.scalars(select(RunItemRecord.id).where(
                RunItemRecord.generation_run_id == result["generation_run_id"])).first()

    def test_a_mock_generation_counts_one_mock_call(self):
        from app.generation.worker import handle_run_item

        ctx = self._make_context()
        item_id = self._run_item(ctx)
        with tenant_session(ctx) as session:
            handle_run_item(session, ctx, item_id)
        with tenant_session(ctx) as session:
            handle_run_item(session, ctx, item_id)  # no longer dispatchable: a no-op
            rows = usage_rows(session, ctx.workspace_id)
            attempt = session.scalar(select(ProviderCallAttemptRecord).where(
                ProviderCallAttemptRecord.workspace_id == ctx.workspace_id,
                ProviderCallAttemptRecord.run_item_id == item_id))
        self.assertEqual([(r.execution_mode, r.source_resource_id) for r in rows], [("mock", attempt.id)])

    def test_a_mock_generation_that_crashed_after_dispatch_counts_nothing(self):
        from app.generation.worker import handle_run_item_dispatch_only

        ctx = self._make_context()
        item_id = self._run_item(ctx)
        with tenant_session(ctx) as session:
            handle_run_item_dispatch_only(session, ctx, item_id)
        with tenant_session(ctx) as session:
            self.assertEqual(usage_rows(session, ctx.workspace_id), [])

    def _seed_usage(self, ctx, source, mode, occurred_at):
        with tenant_session(ctx) as session:
            UsageRecorder().record_many(session, ctx, [fact(source, execution_mode=mode, occurred_at=occurred_at)])

    def _get(self, ctx, params=None):
        app.dependency_overrides[request_tenant_context] = lambda: ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(client.close)
        return client.get("/api/v1/usage", params=params)

    def test_get_usage_defaults_to_provider_calls_and_buckets_by_mode(self):
        member = self._make_context()  # a Member reads Usage
        other = self._make_context()
        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        for source, mode in (("a1", "byok"), ("a2", "byok"), ("a3", "sandbox"), ("a4", "mock")):
            self._seed_usage(member, source, mode, recent)
        self._seed_usage(member, "old", "byok", recent - timedelta(days=2))
        self._seed_usage(other, "b1", "byok", recent)

        response = self._get(member)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["workspace_id"], body["metric_key"]), (member.workspace_id, "provider_calls"))
        self.assertEqual({k: Decimal(v) for k, v in body["by_execution_mode"].items()},
                         {"byok": 2, "sandbox": 1, "mock": 1, "not_applicable": 0})
        self.assertEqual(Decimal(body["total"]), Decimal("3"))  # Mock is not real use

        other_body = self._get(other).json()
        self.assertEqual((other_body["workspace_id"], Decimal(other_body["total"])), (other.workspace_id, 1))
        empty = self._get(self._make_context()).json()
        self.assertEqual((empty["total"], set(empty["by_execution_mode"].values())), ("0", {"0"}))

    def test_get_usage_without_a_verified_identity_is_401(self):
        client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(client.close)
        with patch.object(config, "LOCAL_IDENTITY_ENABLED", False):
            response = client.get("/api/v1/usage")
        self.assertEqual(response.status_code, 401, response.text)


if __name__ == "__main__":
    unittest.main()
