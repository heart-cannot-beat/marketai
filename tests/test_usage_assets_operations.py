"""Usage of AssetVersions and Operations (Issue #8, slice S2).

Four metrics join ``provider_calls``. Each is written in the transaction of
the fact it records, keyed so that writing the same fact again counts
nothing new:

| Metric                     | Written where                                   | Key                     |
|----------------------------|-------------------------------------------------|-------------------------|
| ``asset_versions_generated`` | Generation creates the AssetVersion           | AssetVersion id         |
| ``generation_items_failed``  | ``generation.worker._transition_item`` -> failed | RunItem id + revision |
| ``operations``               | ``transition_operation`` -> terminal state    | Operation id            |
| ``processing_seconds``       | ``transition_operation`` -> terminal state    | Operation id            |

An Operation's execution mode is read from the rows that record the mode of
what it ran (``operation_usage.MODE_EVIDENCE``). A Mock Operation lands in
the ``mock`` bucket of ``operations`` and never in ``total``.

The paid marketing path is covered in ``tests/test_generation_selection.py``,
BYOK dispatch in ``tests/test_generation_byok.py``, every legacy fold in
``tests/test_legacy_operations.py``, and the PostgreSQL roles in
``tests/test_usage_pg.py``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.adapters.persistence.database import Base, tenant_session
from app.adapters.persistence.models import (
    AssetVersionRecord,
    GenerationRunRecord,
    JobRecord,
    OperationStateEventRecord,
    new_id,
)
from app.entitlements import operation_usage
from app.entitlements.operation_usage import (
    MODE_EVIDENCE,
    NOT_MODE_EVIDENCE,
    operation_usage_mode,
    record_operation_settled,
    strongest_mode,
)
from app.entitlements.usage import (
    ASSET_VERSIONS_GENERATED,
    GENERATION_ITEMS_FAILED,
    METRICS,
    OPERATIONS,
    PROCESSING_SECONDS,
    UsageFactError,
    as_utc,
    record_asset_version_generated,
    record_generation_item_failed,
    usage_semantic_key,
)
from app.entitlements.usage_models import UsageEventRecord
from app.generation.execution_policy import StepRejection
from app.generation.models import RunItemRecord
from app.main import app
from app.platform.http_identity import request_tenant_context
from app.platform.operations import (
    InvalidOperationTransitionError,
    OperationRecord,
    OperationState,
    aggregate_operation_from_jobs,
    create_operation,
    transition_operation,
)
from tests.support.library_fixtures import LibraryFixtures

T0 = datetime(2026, 11, 9, 8, 0, tzinfo=timezone.utc)
S = OperationState


def rows(ctx, *, metric: str | None = None, source_id: str | None = None) -> list[UsageEventRecord]:
    with tenant_session(ctx) as session:
        query = select(UsageEventRecord).where(UsageEventRecord.workspace_id == ctx.workspace_id)
        if metric is not None:
            query = query.where(UsageEventRecord.metric_key == metric)
        if source_id is not None:
            query = query.where(UsageEventRecord.source_resource_id == source_id)
        found = list(session.scalars(query.order_by(UsageEventRecord.metric_key)))
        for row in found:
            session.expunge(row)
        return found


def at(seconds: float):
    """Pin the clock ``transition_operation`` reads."""
    return patch("app.platform.operations.utcnow", return_value=T0 + timedelta(seconds=seconds))


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class RegistryTests(LibraryFixtures):
    def test_the_registry_holds_the_s2_metrics(self):
        self.assertEqual(
            set(METRICS),
            {"provider_calls", "asset_versions_generated", "generation_items_failed",
             "operations", "processing_seconds"},
        )
        for metric, unit in ((ASSET_VERSIONS_GENERATED, "asset_version"), (GENERATION_ITEMS_FAILED, "run_item"),
                             (OPERATIONS, "operation"), (PROCESSING_SECONDS, "second")):
            with self.subTest(metric=metric.key):
                self.assertEqual(
                    (metric.measurement_kind, metric.direction, metric.unit, metric.aggregation),
                    ("delta", "increase", unit, "sum"))


# ---------------------------------------------------------------------------
# Operations: transition_operation into a terminal state
# ---------------------------------------------------------------------------

class OperationUsageTests(LibraryFixtures):
    def setUp(self):
        self.ctx = self._make_context()

    def operation(self, *, initial=S.QUEUED, version: int = 0, created=T0) -> str:
        with tenant_session(self.ctx) as session:
            return create_operation(session, command_type="test.command", context=self.ctx,
                                    initial_state=initial, admission_contract_version=version, now=created).id

    def move(self, operation_id: str, *path: tuple[OperationState, float]) -> None:
        for state, seconds in path:
            with at(seconds), tenant_session(self.ctx) as session:
                transition_operation(session, operation_id, state, reason="test", actor="tester")

    def settled(self, operation_id: str) -> dict[str, UsageEventRecord]:
        found = rows(self.ctx, source_id=operation_id)
        self.assertEqual(len(found), len({r.metric_key for r in found}), "one row per metric")
        return {r.metric_key: r for r in found}

    def test_a_terminal_transition_counts_the_operation_and_its_seconds(self):
        operation_id = self.operation()
        self.move(operation_id, (S.RUNNING, 30), (S.SUCCEEDED, 90.5))
        facts = self.settled(operation_id)
        self.assertEqual(set(facts), {"operations", "processing_seconds"})
        count, seconds = facts["operations"], facts["processing_seconds"]
        self.assertEqual((count.quantity, count.unit), (Decimal("1"), "operation"))
        self.assertEqual((seconds.quantity, seconds.unit), (Decimal("90.5"), "second"))
        for metric, row in facts.items():
            with self.subTest(metric=metric):
                self.assertEqual((row.measurement_kind, row.direction), ("delta", "increase"))
                self.assertEqual((row.source_resource_type, row.execution_mode), ("operation", "not_applicable"))
                self.assertEqual(row.semantic_key_hash, usage_semantic_key(metric, "operation", operation_id))
                self.assertEqual(as_utc(row.occurred_at), T0 + timedelta(seconds=90.5))

    def test_non_terminal_transitions_count_nothing(self):
        operation_id = self.operation()
        self.move(operation_id, (S.RUNNING, 1), (S.UNCERTAIN, 2), (S.RUNNING, 3), (S.UNCERTAIN, 4))
        self.assertEqual(rows(self.ctx), [])

    def test_every_terminal_state_counts_once_whichever_path_reaches_it(self):
        paths = {
            "running -> succeeded": (S.QUEUED, [(S.RUNNING, 1), (S.SUCCEEDED, 2)]),
            "running -> partially_succeeded": (S.QUEUED, [(S.RUNNING, 1), (S.PARTIALLY_SUCCEEDED, 2)]),
            "running -> failed": (S.QUEUED, [(S.RUNNING, 1), (S.FAILED, 2)]),
            "running -> cancelled": (S.QUEUED, [(S.RUNNING, 1), (S.CANCELLED, 2)]),
            "queued -> cancelled": (S.QUEUED, [(S.CANCELLED, 2)]),
            "queued -> failed (legacy fold, #22)": (S.QUEUED, [(S.FAILED, 2)]),
            "admitted -> failed (stale epoch)": (S.ADMITTED, [(S.FAILED, 2)]),
            "admitted -> cancelled": (S.ADMITTED, [(S.CANCELLED, 2)]),
            "uncertain -> failed": (S.QUEUED, [(S.RUNNING, 1), (S.UNCERTAIN, 1.5), (S.FAILED, 2)]),
        }
        for name, (initial, path) in paths.items():
            with self.subTest(path=name):
                operation_id = self.operation(initial=initial)
                self.move(operation_id, *path)
                facts = self.settled(operation_id)
                self.assertEqual((facts["operations"].quantity, facts["processing_seconds"].quantity),
                                 (Decimal("1"), Decimal("2")))
                # A terminal state has no way out: a second settlement is refused
                # before anything is written.
                with self.assertRaises(InvalidOperationTransitionError):
                    self.move(operation_id, (S.FAILED, 3))
                self.assertEqual(len(rows(self.ctx, source_id=operation_id)), 2)

    def test_an_uncertain_operation_is_counted_once_when_it_settles(self):
        operation_id = self.operation()
        self.move(operation_id, (S.RUNNING, 10), (S.UNCERTAIN, 20))
        self.assertEqual(rows(self.ctx), [], "uncertain is not a terminal state")
        self.move(operation_id, (S.SUCCEEDED, 600))
        facts = self.settled(operation_id)
        self.assertEqual(facts["operations"].quantity, Decimal("1"))
        # The seconds run to the settlement, the time spent uncertain included.
        self.assertEqual(facts["processing_seconds"].quantity, Decimal("600"))

    def test_recording_a_settled_operation_again_counts_nothing_new(self):
        operation_id = self.operation()
        self.move(operation_id, (S.RUNNING, 1), (S.FAILED, 5))
        with tenant_session(self.ctx) as session:
            operation = session.get(OperationRecord, operation_id)
            again = record_operation_settled(session, self.ctx, operation, settled_at=T0 + timedelta(seconds=9))
        self.assertEqual((again.written, again.duplicates), (0, 2))
        facts = self.settled(operation_id)
        self.assertEqual(facts["processing_seconds"].quantity, Decimal("5"))

    def test_a_usage_write_that_fails_fails_the_transition(self):
        operation_id = self.operation()
        self.move(operation_id, (S.RUNNING, 1))
        with patch.object(operation_usage.UsageRecorder, "record_many", side_effect=RuntimeError("usage down")):
            with self.assertRaises(RuntimeError):
                self.move(operation_id, (S.SUCCEEDED, 2))
        with tenant_session(self.ctx) as session:
            self.assertEqual(session.get(OperationRecord, operation_id).state, "running")
            events = session.scalars(select(OperationStateEventRecord.to_state).where(
                OperationStateEventRecord.operation_id == operation_id)).all()
        self.assertEqual(events, ["queued", "running"])
        self.assertEqual(rows(self.ctx), [])

    def test_a_clock_behind_the_creation_counts_zero_seconds(self):
        operation_id = self.operation(created=T0 + timedelta(seconds=30))
        with patch.object(operation_usage, "logger") as log:
            self.move(operation_id, (S.CANCELLED, 10))
        self.assertEqual(log.warning.call_args.args[0], "usage_processing_seconds_clock_skew")
        self.assertEqual(self.settled(operation_id)["processing_seconds"].quantity, Decimal("0"))

    def test_another_workspace_s_operation_is_refused(self):
        operation_id = self.operation()
        other = self._make_context()
        with tenant_session(self.ctx) as session:
            operation = session.get(OperationRecord, operation_id)
            session.expunge(operation)
        with tenant_session(other) as session:
            with self.assertRaises(UsageFactError) as caught:
                record_operation_settled(session, other, operation, settled_at=T0)
        self.assertEqual(caught.exception.code, "usage_workspace_mismatch")
        self.assertEqual(rows(other), [])

    def test_v1_aggregation_counts_the_operation_once_however_often_it_runs(self):
        operation_id = self.operation(initial=S.ADMITTED, version=1)
        with tenant_session(self.ctx) as session:
            for index in range(2):
                session.add(JobRecord(
                    id=new_id("job"), workspace_id=self.ctx.workspace_id, initiator_id="user_a",
                    initiator_role="owner", type="localization.edit_attempt", payload_json={},
                    idempotency_key=new_id("idem"), state=("succeeded", "failed")[index],
                    operation_id=operation_id, entitlement_version_at_enqueue=1, execution_contract_version=1,
                ))
        for _ in range(3):  # every sibling Job's terminal transaction calls the fold
            with tenant_session(self.ctx) as session:
                aggregate_operation_from_jobs(session, operation_id, actor="system:worker", context=self.ctx)
        with tenant_session(self.ctx) as session:
            self.assertEqual(session.get(OperationRecord, operation_id).state, "partially_succeeded")
        self.assertEqual(set(self.settled(operation_id)), {"operations", "processing_seconds"})


# ---------------------------------------------------------------------------
# The execution mode of an Operation
# ---------------------------------------------------------------------------

class OperationModeTests(LibraryFixtures):
    def test_every_table_that_records_an_operation_s_mode_is_classified(self):
        """A new table with an Operation id and a mode must be read or excluded on purpose."""
        import app.main  # noqa: F401 -- loads every mapped model
        import app.worker_runtime  # noqa: F401

        mode_columns = {"execution_mode", "provider_execution_mode", "meta_mode"}
        read = {e.table for e in MODE_EVIDENCE}
        unclassified = []
        for name, table in Base.metadata.tables.items():
            columns = set(table.c.keys())
            if any(c == "operation_id" or c.endswith("_operation_id") for c in columns) and columns & mode_columns:
                if name not in read and name not in NOT_MODE_EVIDENCE:
                    unclassified.append(name)
        self.assertEqual(unclassified, [])
        for evidence in MODE_EVIDENCE:
            with self.subTest(table=evidence.table):
                columns = Base.metadata.tables[evidence.table].c
                self.assertIn(evidence.operation_column, columns)
                self.assertIn("workspace_id", columns)
                if evidence.via_table is None:
                    self.assertIn(evidence.mode_column, columns)
                else:
                    self.assertIn(evidence.via_column, columns)
                    self.assertIn(evidence.mode_column, Base.metadata.tables[evidence.via_table].c)
        self.assertFalse(read & set(NOT_MODE_EVIDENCE))

    def test_the_strongest_recorded_mode_wins(self):
        cases = {
            (): "not_applicable",
            ("mock",): "mock",
            ("controlled_mock",): "mock",
            ("sandbox", "mock"): "sandbox",
            ("byok", "mock", "sandbox"): "byok",
            ("legacy_platform", "mock"): "mock",
        }
        for modes, expected in cases.items():
            with self.subTest(modes=modes):
                self.assertEqual(strongest_mode(modes, workspace_id="ws", operation_id="op"), expected)

    def test_an_unrecognised_mode_is_logged_and_ignored(self):
        with patch.object(operation_usage, "logger") as log:
            self.assertEqual(strongest_mode(["legacy_platform"], workspace_id="ws", operation_id="op"),
                             "not_applicable")
        self.assertEqual(log.warning.call_args.args[0], "usage_operation_mode_unrecognized")
        self.assertEqual(log.warning.call_args.kwargs["extra"]["mode"], "legacy_platform")

    def test_the_evidence_query_runs_against_the_migrated_schema(self):
        """Every evidence table and column exists in the migrated database, not only in the models."""
        ctx = self._make_context()
        with tenant_session(ctx) as session:
            self.assertEqual(operation_usage_mode(session, ctx.workspace_id, "op_none"), "not_applicable")


# ---------------------------------------------------------------------------
# Mock generation end to end: AssetVersion, Operation and the Mock bucket
# ---------------------------------------------------------------------------

class GenerationUsageTests(LibraryFixtures):
    def submit(self, ctx, variants: int = 1) -> tuple[str, str, list[str]]:
        from app.generation.draft import create_draft
        from app.generation.preview import create_preview
        from app.generation.submission import submit_generation

        with tenant_session(ctx) as session:
            draft = create_draft(session, ctx, product_id=self._product_id(ctx, session), provider="mock",
                                 model="mock-v1", execution_mode="mock",
                                 combination_strategy={"kind": "full", "planner_version": "v1"},
                                 format_config={"aspect_ratio": "1:1", "variants_per_combination": variants})
            preview = create_preview(session, ctx, draft.id, draft.revision)
            result = submit_generation(session, ctx, preview_id=preview.id, preview_hash=preview.preview_hash,
                                       expected_draft_revision=draft.revision, idempotency_key=new_id("ik"))
            items = session.scalars(select(RunItemRecord.id).where(
                RunItemRecord.generation_run_id == result["generation_run_id"]).order_by(RunItemRecord.ordinal)).all()
        return result["generation_run_id"], result["operation_id"], list(items)

    def generate(self, ctx, item_id: str) -> str:
        from app.generation.worker import handle_run_item

        with tenant_session(ctx) as session:
            handle_run_item(session, ctx, item_id)
        with tenant_session(ctx) as session:
            return session.scalar(select(AssetVersionRecord.id).where(
                AssetVersionRecord.workspace_id == ctx.workspace_id,
                AssetVersionRecord.producing_run_item_id == item_id))

    def item(self, ctx, item_id: str) -> RunItemRecord:
        with tenant_session(ctx) as session:
            item = session.get(RunItemRecord, item_id)
            session.expunge(item)
            return item

    def test_a_mock_generation_counts_its_asset_version_its_operation_and_keeps_both_out_of_total(self):
        from app.generation.qc import run_qc

        ctx = self._make_context()
        run_id, operation_id, (item_id,) = self.submit(ctx)
        version_id = self.generate(ctx, item_id)
        self.generate(ctx, item_id)  # redelivered Job: the item is past dispatch, a no-op

        generated = rows(ctx, metric="asset_versions_generated")
        self.assertEqual([(r.source_resource_type, r.source_resource_id, r.execution_mode, r.quantity)
                          for r in generated], [("asset_version", version_id, "mock", Decimal("1"))])
        with tenant_session(ctx) as session:
            version = session.get(AssetVersionRecord, version_id)
            self.assertEqual(as_utc(generated[0].occurred_at), as_utc(version.created_at))
        self.assertEqual(rows(ctx, metric="operations"), [], "the Run is still in quality check")

        with tenant_session(ctx) as session:
            run_qc(session, ctx, version_id, item_id)
        with tenant_session(ctx) as session:
            self.assertEqual(session.get(GenerationRunRecord, run_id).status, "succeeded")
            self.assertEqual(session.get(OperationRecord, operation_id).state, "succeeded")
        self.assertEqual([(r.metric_key, r.execution_mode) for r in rows(ctx, source_id=operation_id)],
                         [("operations", "mock"), ("processing_seconds", "mock")])
        self.assertEqual(rows(ctx, metric="generation_items_failed"), [])

        for metric in ("asset_versions_generated", "operations"):
            with self.subTest(metric=metric):
                body = self.usage(ctx, metric)
                self.assertEqual(Decimal(body["by_execution_mode"]["mock"]), Decimal("1"))
                self.assertEqual(Decimal(body["total"]), Decimal("0"), "Mock is never real use")

    def test_a_failed_generation_counts_one_failed_item_and_no_asset_version(self):
        from app.generation.worker import _fail_or_pause_item, fail_run_item_after_handler_fault

        ctx = self._make_context()
        _, _, (paused_id, failed_id) = self.submit(ctx, variants=2)
        with tenant_session(ctx) as session:
            # A recoverable rejection pauses the item: nothing failed yet.
            _fail_or_pause_item(session, ctx, session.get(RunItemRecord, paused_id),
                                StepRejection(code="credential_unavailable", detail="", recoverable=True))
            _fail_or_pause_item(session, ctx, session.get(RunItemRecord, failed_id),
                                StepRejection(code="mode_unavailable", detail="", recoverable=False))
        with tenant_session(ctx) as session:  # the handler fault path finds it failed already: a no-op
            fail_run_item_after_handler_fault(session, ctx, failed_id, error_summary="late fault")

        failed = rows(ctx, metric="generation_items_failed")
        item = self.item(ctx, failed_id)
        self.assertEqual([(r.source_resource_type, r.source_resource_id, r.execution_mode, r.unit) for r in failed],
                         [("run_item", failed_id, "mock", "run_item")])
        self.assertEqual(failed[0].semantic_key_hash, usage_semantic_key(
            "generation_items_failed", "run_item", failed_id, f"revision:{item.revision}"))
        self.assertEqual(as_utc(failed[0].occurred_at), as_utc(item.updated_at))
        self.assertEqual(rows(ctx, metric="asset_versions_generated"), [])

    def test_a_retried_item_that_fails_again_is_a_second_failure(self):
        from app.generation.worker import _transition_item, fail_run_item_after_handler_fault

        ctx = self._make_context()
        _, _, (item_id,) = self.submit(ctx)
        with tenant_session(ctx) as session:
            fail_run_item_after_handler_fault(session, ctx, item_id, error_summary="crashed")
        with tenant_session(ctx) as session:  # Recovery re-queues it, and it fails again
            _transition_item(session, ctx, session.get(RunItemRecord, item_id), state="queued")
        with tenant_session(ctx) as session:
            fail_run_item_after_handler_fault(session, ctx, item_id, error_summary="crashed again")
        failed = rows(ctx, metric="generation_items_failed")
        self.assertEqual(len(failed), 2)
        self.assertEqual(len({r.semantic_key_hash for r in failed}), 2)

    def test_a_blocking_quality_check_is_not_a_failed_generation(self):
        from app.generation.qc import run_qc_with_blocking_failure

        ctx = self._make_context()
        _, _, (item_id,) = self.submit(ctx)
        version_id = self.generate(ctx, item_id)
        with tenant_session(ctx) as session:
            run_qc_with_blocking_failure(session, ctx, version_id, item_id)
        self.assertEqual(self.item(ctx, item_id).state, "failed")
        self.assertEqual(rows(ctx, metric="generation_items_failed"), [])
        self.assertEqual(len(rows(ctx, metric="asset_versions_generated")), 1)

    def test_a_usage_write_that_fails_rolls_the_asset_version_back(self):
        from app.generation.worker import handle_run_item

        from app.entitlements.usage import UsageRecorder

        ctx = self._make_context()
        _, _, (item_id,) = self.submit(ctx)
        record_many = UsageRecorder.record_many

        def refuse_versions(recorder, session, context, facts):
            facts = list(facts)
            if any(fact.metric_key == "asset_versions_generated" for fact in facts):
                raise RuntimeError("usage down")
            return record_many(recorder, session, context, facts)

        with patch.object(UsageRecorder, "record_many", refuse_versions):
            with self.assertRaises(RuntimeError):
                with tenant_session(ctx) as session:
                    handle_run_item(session, ctx, item_id)
        with tenant_session(ctx) as session:
            self.assertIsNone(session.scalar(select(AssetVersionRecord.id).where(
                AssetVersionRecord.producing_run_item_id == item_id)))
        self.assertEqual(self.item(ctx, item_id).state, "pending")
        self.assertEqual(rows(ctx), [])

    def test_the_same_fact_written_again_counts_nothing_new(self):
        ctx = self._make_context()
        _, _, (item_id,) = self.submit(ctx)
        version_id = self.generate(ctx, item_id)
        with tenant_session(ctx) as session:
            version = session.get(AssetVersionRecord, version_id)
            self.assertEqual(record_asset_version_generated(session, ctx, version).duplicates, 1)
            for _ in range(2):
                result = record_generation_item_failed(session, ctx, run_item_id=item_id, revision=7,
                                                       execution_mode="controlled_mock", occurred_at=T0)
            self.assertEqual((result.written, result.duplicates), (0, 1))
        self.assertEqual(len(rows(ctx, metric="asset_versions_generated")), 1)
        self.assertEqual([r.execution_mode for r in rows(ctx, metric="generation_items_failed")], ["mock"])

    def test_only_a_generated_version_of_the_context_s_workspace_is_counted(self):
        ctx = self._make_context()
        _, _, (item_id,) = self.submit(ctx)
        version_id = self.generate(ctx, item_id)
        other = self._make_context()
        with tenant_session(ctx) as session:
            version = session.get(AssetVersionRecord, version_id)
            session.expunge(version)
        with tenant_session(other) as session:
            with self.assertRaises(UsageFactError) as caught:
                record_asset_version_generated(session, other, version)
        self.assertEqual(caught.exception.code, "usage_workspace_mismatch")
        version.origin_kind = "upload"
        with tenant_session(ctx) as session:
            with self.assertRaises(UsageFactError) as caught:
                record_asset_version_generated(session, ctx, version)
        self.assertEqual(caught.exception.code, "usage_source_invalid")

    # -- GET /api/v1/usage --------------------------------------------------

    def usage(self, ctx, metric: str) -> dict:
        app.dependency_overrides[request_tenant_context] = lambda: ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(client.close)
        response = client.get("/api/v1/usage", params={"metric_key": metric})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_one_workspace_s_operations_never_appear_in_another_s_usage(self):
        alpha, beta = self._make_context(), self._make_context()
        with tenant_session(alpha) as session:
            operation_id = create_operation(session, command_type="test.command", context=alpha).id
        for state in (S.RUNNING, S.SUCCEEDED):
            with tenant_session(alpha) as session:
                transition_operation(session, operation_id, state, reason="test", actor="tester")
        _, _, (item_id,) = self.submit(alpha)
        self.generate(alpha, item_id)

        for metric in ("operations", "processing_seconds", "asset_versions_generated"):
            with self.subTest(metric=metric):
                mine, theirs = self.usage(alpha, metric), self.usage(beta, metric)
                self.assertGreater(sum(Decimal(v) for v in mine["by_execution_mode"].values()), 0)
                self.assertEqual(theirs["workspace_id"], beta.workspace_id)
                self.assertEqual(Decimal(theirs["total"]), 0)
                self.assertEqual({Decimal(v) for v in theirs["by_execution_mode"].values()}, {Decimal("0")})
        self.assertEqual(Decimal(self.usage(alpha, "operations")["by_execution_mode"]["not_applicable"]), 1)
