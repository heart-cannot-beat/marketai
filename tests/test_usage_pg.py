"""Usage against an isolated migrated PostgreSQL with the real API, Worker and Readonly roles.

Issue #347: ``GET /api/v1/usage`` (``get_usage``) answered 500 ``permission
denied for table usage_events`` because ``marketai_domain_api`` held INSERT
on the table but no SELECT. Issue #8 (slice S1) adds the writer: provider
calls are recorded with ``INSERT ... ON CONFLICT DO NOTHING`` in the
settlement's own transaction, as the Worker or the API.

Here, as the roles that run in production:

* ``record_many`` writes a fact as the Worker, which holds INSERT but no
  SELECT, and as the API; writing the same key again is a counted duplicate,
  never an error, so the settlement around it still commits. Its
  ``ON CONFLICT DO NOTHING`` names no target: with one, PostgreSQL wants
  SELECT on the target's columns and refuses the Worker; without one, any
  unique key would be skipped, so the table keeps exactly two;
* inside Workspace A's context, a row for Workspace B is refused by RLS,
  with or without ``ON CONFLICT``;
* UPDATE and DELETE are refused by the append-only trigger, even for the
  owner that may bypass RLS;
* a Member and an Owner get 200 and one Workspace's ``provider_calls`` over
  ``[window_start, window_end)``, bucketed by execution mode, with Mock out
  of ``total``; without parameters it is ``provider_calls`` over the last
  24 hours; another Workspace's events stay out; an empty Workspace gets
  ``"0"``; a request with no verified identity gets 401;
* the API role without a tenant context sees no row.

Slice S2 adds AssetVersion and Operation facts, written as the role that
runs the domain change:

* ``transition_operation`` into a terminal state counts ``operations`` and
  ``processing_seconds`` as the API and as the Worker. Reading the
  Operation's execution mode reads every ``MODE_EVIDENCE`` table, so this
  also proves both roles may SELECT each of them under RLS; settling again
  is refused, recording again is a duplicate;
* ``asset_versions_generated`` and ``generation_items_failed`` are written
  as both roles, and a repeat is a duplicate;
* one Workspace's Operations never show in another's ``/usage``.

Slice S3 (#145) adds the CHECK constraints of spec §2.12 (migration
``zzk145a1b2c3``):

* the four checks are there, validated, and named as ``UsageEventRecord``
  names them;
* as the API and as the Worker, each check refuses the row that breaks it,
  through the statement ``record_many`` sends: ``ON CONFLICT DO NOTHING``
  skips unique violations only, so a bad row is never a counted duplicate;
* rows every S1 and S2 writer wrote, as both roles, before the checks
  existed pass ``VALIDATE``, unchanged; the append-only trigger, its function
  and the privilege matrix are the same afterwards;
* a row that breaks a check fails ``VALIDATE``, and the whole revision rolls
  back: no constraint is left behind ``NOT VALID``.

Issue #146: an Operation that ran against Meta is settled, as the API and as
the Worker, in a session bound to its Meta mode, which the settling path
passes on. ``controlled_mock`` counts as ``mock``, out of ``total``;
``customer_sandbox`` and ``internal_validation`` stay ``not_applicable``.

Then the privilege matrix of ``usage_events`` (the API reads and appends, the
Worker only appends, Readonly only reads, nobody updates or deletes) and the
migration's upgrade, downgrade and upgrade again.

Every Workspace a test opens, its usage rows and its Operations, are removed
afterwards by the owner with the append-only triggers disabled for that one
transaction.

Run with ``DOMAIN_OWNER_DATABASE_URL``, ``DOMAIN_API_DATABASE_URL``,
``DOMAIN_WORKER_DATABASE_URL``, ``MARKETAI_DATABASE_URL`` (the owner) and
``MARKETAI_TENANT_CONTEXT_SECRET`` against a cluster released with
``make db-release``.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import CheckConstraint, create_engine, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app import config
from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import WorkspaceRecord
from app.entitlements.operation_usage import record_operation_settled
from app.entitlements.usage import (
    METRICS,
    USAGE_EXECUTION_MODES,
    UsageFact,
    UsageRecorder,
    record_asset_version_generated,
    record_generation_item_failed,
    record_provider_call,
    usage_semantic_key,
)
from app.entitlements.usage_models import UsageEventRecord
from app.library.contracts import ROLE_MEMBER, ROLE_OWNER
from app.main import app
from app.platform.http_identity import request_tenant_context
from app.platform.operations import (
    InvalidOperationTransitionError,
    OperationRecord,
    OperationState,
    create_operation,
    transition_operation,
)
from app.platform.tenancy import TenantContext

URLS = [os.getenv(key) for key in ("DOMAIN_OWNER_DATABASE_URL", "DOMAIN_API_DATABASE_URL", "DOMAIN_WORKER_DATABASE_URL")]
ROOT = Path(__file__).resolve().parents[1]
#: The migration that grants the API SELECT (#347), and the head it follows.
REVISION, DOWN_REVISION = "zz7a8b9c0d1e", "zz6f7a8b9c0d"
#: role -> privileges it must hold on usage_events; every other DML privilege must be absent.
GRANTED = {
    "marketai_domain_api": {"SELECT", "INSERT"},
    "marketai_domain_worker": {"INSERT"},
    "marketai_domain_readonly": {"SELECT"},
}
DML = ("SELECT", "INSERT", "UPDATE", "DELETE")
OWNER, API, WORKER = 0, 1, 2
ROLE_NAMES = {API: "api", WORKER: "worker"}
METRIC = "provider_calls"
#: The migration that adds the CHECK constraints (#145), and the head it follows.
CHECKS_REVISION, CHECKS_DOWN_REVISION = "zzk145a1b2c3", "zzj113a1b2c3"
CHECK_NAMES = {c.name for c in UsageEventRecord.__table__.constraints if isinstance(c, CheckConstraint)}
#: (check it breaks, columns that break it). Each bad row breaks exactly one.
BAD_ROWS = (
    ("measurement_kind_valid", {"measurement_kind": "counter"}),
    ("direction_matches_kind", {"direction": "set"}),
    ("direction_matches_kind", {"measurement_kind": "gauge", "direction": "increase"}),
    ("quantity_nonnegative", {"quantity": Decimal("-1")}),
    ("execution_mode_valid", {"execution_mode": "controlled_mock"}),
)


def fact(source_id: str, mode: str = "byok", occurred_at: datetime | None = None) -> UsageFact:
    return UsageFact(
        metric_key=METRIC, quantity=Decimal("1"), unit="call", execution_mode=mode,
        semantic_key_hash=usage_semantic_key(METRIC, "provider_call_attempt", source_id),
        occurred_at=occurred_at or datetime.now(timezone.utc),
        source_resource_type="provider_call_attempt", source_resource_id=source_id,
    )


@unittest.skipUnless(all(URLS) and os.getenv("MARKETAI_TENANT_CONTEXT_SECRET"),
                     "requires isolated PostgreSQL runtime roles")
class UsagePostgresTests(unittest.TestCase):
    def setUp(self):
        self.owner, self.api, self.worker = [create_engine(url) for url in URLS]
        for engine in (self.owner, self.api, self.worker):
            self.addCleanup(engine.dispose)
        self.sessions = [sessionmaker(bind=e, expire_on_commit=False, autoflush=False)
                         for e in (self.owner, self.api, self.worker)]
        self.current: TenantContext | None = None
        app.dependency_overrides[request_tenant_context] = lambda: self.current
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.workspaces: list[str] = []
        self.addCleanup(self._remove_workspaces)

    def _remove_workspaces(self) -> None:
        """Leave the shared cluster as it was: the later CI steps count rows."""
        if not self.workspaces:
            return
        with self.owner.begin() as connection:
            connection.execute(text("ALTER TABLE usage_events DISABLE TRIGGER trg_usage_events_immutable"))
            connection.execute(text(
                "ALTER TABLE operation_state_events DISABLE TRIGGER operation_state_events_reject_mutation"))
            connection.execute(text("DELETE FROM usage_events WHERE workspace_id = ANY(:ids)"),
                               {"ids": self.workspaces})
            connection.execute(text("DELETE FROM operation_state_events WHERE workspace_id = ANY(:ids)"),
                               {"ids": self.workspaces})
            connection.execute(text("DELETE FROM operation_records WHERE workspace_id = ANY(:ids)"),
                               {"ids": self.workspaces})
            connection.execute(text(
                "ALTER TABLE operation_state_events ENABLE TRIGGER operation_state_events_reject_mutation"))
            connection.execute(text("ALTER TABLE usage_events ENABLE TRIGGER trg_usage_events_immutable"))
            connection.execute(text("DELETE FROM workspaces WHERE id = ANY(:ids)"), {"ids": self.workspaces})

    def _as(self, index: int):
        return patch("app.adapters.persistence.database.SessionLocal", self.sessions[index])

    def _workspace(self, prefix: str, role: str, actor: str) -> TenantContext:
        workspace_id = f"{prefix}_{uuid.uuid4().hex[:10]}"
        with self._as(OWNER), platform_session() as session:  # the control plane opens Workspaces
            session.add(WorkspaceRecord(id=workspace_id, name=workspace_id))
        self.workspaces.append(workspace_id)
        return TenantContext(workspace_id=workspace_id, role=role, initiator_id=actor,
                             request_id="test-req", trace_id="trace-test")

    def _as_role(self, ctx: TenantContext, role: str, actor: str) -> TenantContext:
        return TenantContext(workspace_id=ctx.workspace_id, role=role, initiator_id=actor,
                             request_id="test-req", trace_id="trace-test")

    def _record(self, index: int, ctx: TenantContext, *facts: UsageFact):
        """Record through the domain, as the API or the Worker, in a transaction of its own."""
        with self._as(index), tenant_session(ctx) as session:
            return UsageRecorder().record_many(session, ctx, facts)

    def _rows(self, ctx: TenantContext) -> list[tuple]:
        """What the API role sees of the Workspace (the Worker cannot read)."""
        with self._as(API), tenant_session(ctx) as session:
            return [(r.source_resource_id, r.execution_mode, r.measurement_kind, r.unit)
                    for r in session.query(UsageEventRecord).order_by(UsageEventRecord.source_resource_id)]

    def _usage(self, caller: TenantContext, params: dict | None = None) -> dict:
        self.current = caller
        with self._as(API):
            response = self.client.get("/api/v1/usage", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_first_and_duplicate_writes_succeed_as_the_worker_and_the_api(self):
        ctx = self._workspace("ws_usage_pg_write", ROLE_OWNER, "owner_w")
        for index, name in ROLE_NAMES.items():
            with self.subTest(role=name):
                source = f"pca_{name}_{uuid.uuid4().hex[:8]}"
                first = self._record(index, ctx, fact(source))
                again = self._record(index, ctx, fact(source))
                batch = self._record(index, ctx, fact(source), fact(f"{source}_b"), fact(f"{source}_b"))
                self.assertEqual((first.written, first.duplicates), (1, 0))
                self.assertEqual((again.written, again.duplicates), (0, 1))
                self.assertEqual((batch.written, batch.duplicates), (1, 2))

                attempt = SimpleNamespace(id=f"{source}_c", workspace_id=ctx.workspace_id,
                                          execution_mode="controlled_mock")
                with self._as(index), tenant_session(ctx) as session:
                    self.assertEqual(record_provider_call(session, ctx, attempt, occurred_at=datetime.now(timezone.utc)).written, 1)
                    self.assertEqual(record_provider_call(session, ctx, attempt, occurred_at=datetime.now(timezone.utc)).duplicates, 1)
        rows = self._rows(ctx)
        self.assertEqual(len(rows), 6)  # three keys per role, each once
        self.assertEqual({(kind, unit) for _, _, kind, unit in rows}, {("delta", "call")})
        self.assertEqual(sorted(mode for _, mode, _, _ in rows), ["byok"] * 4 + ["mock"] * 2)

    def test_a_named_conflict_target_would_need_select_the_worker_does_not_hold(self):
        """Why ``record_many`` names no conflict target: PostgreSQL wants SELECT on its columns."""
        ctx = self._workspace("ws_usage_pg_target", ROLE_OWNER, "owner_t")
        now = datetime.now(timezone.utc)
        statement = postgresql.insert(UsageEventRecord.__table__).values(
            id=f"ue_{uuid.uuid4().hex}", workspace_id=ctx.workspace_id, metric_key=METRIC,
            measurement_kind="delta", direction="increase", quantity=Decimal("1"), unit="call",
            execution_mode="byok", semantic_key_hash=uuid.uuid4().hex * 2, occurred_at=now, created_at=now,
        ).on_conflict_do_nothing(index_elements=["workspace_id", "semantic_key_hash"])
        with self.assertRaises(DBAPIError) as caught:
            with self._as(WORKER), tenant_session(ctx) as session:
                session.execute(statement)
        self.assertIn("permission denied for table usage_events", str(caught.exception))

    def test_the_only_unique_keys_are_the_id_and_the_semantic_key(self):
        """``ON CONFLICT DO NOTHING`` without a target skips any unique violation: keep it to these two."""
        with self.owner.connect() as connection:
            keys = connection.execute(text(
                "SELECT array_agg(a.attname::text ORDER BY k.ordinality) FROM pg_index i "
                "CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ordinality) "
                "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum "
                "WHERE i.indrelid = 'usage_events'::regclass AND i.indisunique GROUP BY i.indexrelid"
            )).scalars().all()
        self.assertEqual(sorted(tuple(key) for key in keys), [("id",), ("workspace_id", "semantic_key_hash")])

    def test_rls_refuses_a_row_for_another_workspace(self):
        alpha = self._workspace("ws_usage_pg_alpha", ROLE_OWNER, "owner_a")
        beta = self._workspace("ws_usage_pg_beta", ROLE_OWNER, "owner_b")
        now = datetime.now(timezone.utc)
        for index, name in ROLE_NAMES.items():
            for on_conflict in (False, True):
                with self.subTest(role=name, on_conflict=on_conflict):
                    statement = postgresql.insert(UsageEventRecord.__table__).values(
                        id=f"ue_{uuid.uuid4().hex}", workspace_id=beta.workspace_id, metric_key=METRIC,
                        measurement_kind="delta", direction="increase", quantity=Decimal("1"), unit="call",
                        execution_mode="byok", semantic_key_hash=uuid.uuid4().hex * 2,
                        occurred_at=now, created_at=now,
                    )
                    if on_conflict:  # the shape record_many sends
                        statement = statement.on_conflict_do_nothing()
                    with self.assertRaises(DBAPIError) as caught:
                        with self._as(index), tenant_session(alpha) as session:
                            session.execute(statement)
                    self.assertIn("row-level security", str(caught.exception))
        self.assertEqual(self._rows(alpha), [])
        self.assertEqual(self._rows(beta), [])

    def test_update_and_delete_are_refused_by_the_append_only_trigger(self):
        ctx = self._workspace("ws_usage_pg_append", ROLE_OWNER, "owner_t")
        self._record(WORKER, ctx, fact("pca_append"))
        for statement in ("UPDATE usage_events SET quantity = 2 WHERE workspace_id = :ws",
                          "DELETE FROM usage_events WHERE workspace_id = :ws"):
            with self.subTest(statement=statement.split()[0]):
                with self.assertRaises(DBAPIError) as caught:
                    with self.owner.begin() as connection:  # the owner may bypass RLS, not the trigger
                        connection.execute(text(statement), {"ws": ctx.workspace_id})
                self.assertIn("usage_events is append-only", str(caught.exception))
        self.assertEqual([source for source, *_ in self._rows(ctx)], ["pca_append"])

    def test_get_usage_buckets_one_workspace_s_provider_calls_under_the_api_role(self):
        member = self._workspace("ws_usage_pg", ROLE_MEMBER, "member_a")
        owner = self._as_role(member, ROLE_OWNER, "owner_a")
        stranger = self._workspace("ws_usage_pg_other", ROLE_MEMBER, "member_b")
        empty = self._workspace("ws_usage_pg_empty", ROLE_OWNER, "owner_c")

        now = datetime.now(timezone.utc).replace(microsecond=0)
        start, end = now - timedelta(hours=6), now - timedelta(hours=3)
        for index, source, mode, occurred_at in (
            (WORKER, "start", "byok", start),                          # the start is inside
            (WORKER, "sandbox", "sandbox", start + timedelta(hours=1)),
            (API, "late", "byok", end - timedelta(seconds=1)),         # the API records too
            (WORKER, "mock", "mock", start + timedelta(hours=1)),      # bucketed, never in total
            (WORKER, "end", "byok", end),                              # the end is outside
            (WORKER, "before", "byok", start - timedelta(seconds=1)),
            (WORKER, "recent", "byok", now - timedelta(hours=1)),
            (WORKER, "recent_mock", "mock", now - timedelta(hours=2)),
            (WORKER, "old", "byok", now - timedelta(hours=25)),        # before the default 24 hours
        ):
            self._record(index, member, fact(source, mode, occurred_at))
        self._record(WORKER, stranger, fact("stranger", "byok", start + timedelta(hours=1)))

        window = {"metric_key": METRIC, "window_start": start.isoformat(), "window_end": end.isoformat()}
        for caller in (member, owner):
            with self.subTest(role=caller.role):
                body = self._usage(caller, window)
                self.assertEqual(
                    (body["workspace_id"], body["metric_key"], body["window_start"], body["window_end"]),
                    (member.workspace_id, METRIC, start.isoformat(), end.isoformat()))
                self.assertEqual({k: Decimal(v) for k, v in body["by_execution_mode"].items()},
                                 {"byok": 2, "sandbox": 1, "mock": 1, "not_applicable": 0})
                self.assertEqual(Decimal(body["total"]), Decimal("3"))

                defaults = self._usage(caller)  # every row but "old" is in the last 24 hours
                self.assertEqual(defaults["metric_key"], METRIC)
                self.assertEqual({k: Decimal(v) for k, v in defaults["by_execution_mode"].items()},
                                 {"byok": 5, "sandbox": 1, "mock": 2, "not_applicable": 0})
                self.assertEqual(Decimal(defaults["total"]), Decimal("6"))
                self.assertEqual(datetime.fromisoformat(defaults["window_end"])
                                 - datetime.fromisoformat(defaults["window_start"]), timedelta(hours=24))

        other = self._usage(stranger, window)
        self.assertEqual((other["workspace_id"], Decimal(other["total"])), (stranger.workspace_id, Decimal("1")))
        self.assertEqual(self._usage(empty, window)["total"], "0")
        self.assertEqual(set(self._usage(empty)["by_execution_mode"].values()), {"0"})

        with self.api.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT count(*) FROM usage_events")).scalar(), 0)

        app.dependency_overrides.pop(request_tenant_context, None)  # no verified identity at all
        with self._as(API), patch.object(config, "LOCAL_IDENTITY_ENABLED", False):
            self.assertEqual(self.client.get("/api/v1/usage").status_code, 401)

    # -- Slice S2: Operations and AssetVersions ------------------------------

    def _operation(self, ctx: TenantContext) -> str:
        """A command the API admits. (The Worker opens Operations only for the
        scheduled Meta observation sync, which has no API command, #107.)"""
        with self._as(API), tenant_session(ctx) as session:
            return create_operation(session, command_type="usage.pg_test", context=ctx).id

    def _move(self, index: int, ctx: TenantContext, operation_id: str, state: OperationState) -> None:
        with self._as(index), tenant_session(ctx) as session:
            transition_operation(session, operation_id, state, reason="pg test", actor=ROLE_NAMES[index])

    def _metrics(self, ctx: TenantContext, source_id: str) -> list[tuple]:
        with self._as(API), tenant_session(ctx) as session:
            return sorted((r.metric_key, r.execution_mode, r.unit) for r in session.query(UsageEventRecord)
                          .filter(UsageEventRecord.source_resource_id == source_id))

    def test_settling_an_operation_counts_it_as_the_worker_and_the_api(self):
        ctx = self._workspace("ws_usage_pg_op", ROLE_OWNER, "owner_o")
        for index, name in ROLE_NAMES.items():
            with self.subTest(role=name):
                operation_id = self._operation(ctx)
                self._move(index, ctx, operation_id, OperationState.RUNNING)
                self.assertEqual(self._metrics(ctx, operation_id), [], "running is not terminal")
                self._move(index, ctx, operation_id, OperationState.SUCCEEDED)
                self.assertEqual(self._metrics(ctx, operation_id), [
                    ("operations", "not_applicable", "operation"),
                    ("processing_seconds", "not_applicable", "second"),
                ])
                with self.assertRaises(InvalidOperationTransitionError):
                    self._move(index, ctx, operation_id, OperationState.FAILED)
                with self._as(index), tenant_session(ctx) as session:
                    operation = session.get(OperationRecord, operation_id)
                    again = record_operation_settled(session, ctx, operation,
                                                     settled_at=datetime.now(timezone.utc))
                self.assertEqual((again.written, again.duplicates), (0, 2))
                self.assertEqual(len(self._metrics(ctx, operation_id)), 2)

    def test_a_meta_operation_counts_in_the_bucket_of_its_meta_mode_as_the_worker_and_the_api(self):
        """#146: the Meta mode the settling path passes decides the bucket; Mock stays out of ``total``."""
        ctx = self._workspace("ws_usage_pg_meta", ROLE_OWNER, "owner_m")
        buckets = {"controlled_mock": "mock", "customer_sandbox": "not_applicable",
                   "internal_validation": "not_applicable"}
        for index, name in ROLE_NAMES.items():
            for meta_mode, bucket in buckets.items():
                with self.subTest(role=name, meta_mode=meta_mode):
                    bound = replace(ctx, meta_mode=meta_mode)  # the binding a Meta handler holds
                    with self._as(API), tenant_session(bound) as session:
                        operation_id = create_operation(session, command_type="scale_export", context=bound).id
                    with self._as(index), tenant_session(bound) as session:
                        transition_operation(session, operation_id, OperationState.RUNNING,
                                             reason="pg test", actor=name)
                        transition_operation(session, operation_id, OperationState.SUCCEEDED,
                                             reason="pg test", actor=name, meta_mode=meta_mode)
                    self.assertEqual(self._metrics(ctx, operation_id), [
                        ("operations", bucket, "operation"),
                        ("processing_seconds", bucket, "second"),
                    ])
        member = self._as_role(ctx, ROLE_MEMBER, "member_m")
        for metric in ("operations", "processing_seconds"):
            with self.subTest(metric=metric):
                body = self._usage(member, {"metric_key": metric})
                if metric == "operations":
                    self.assertEqual((Decimal(body["by_execution_mode"]["mock"]),
                                      Decimal(body["by_execution_mode"]["not_applicable"])),
                                     (Decimal("2"), Decimal("4")))
                self.assertEqual(Decimal(body["total"]), Decimal(body["by_execution_mode"]["not_applicable"]),
                                 "Mock is never real use")

    def test_an_uncertain_operation_is_counted_once_when_the_worker_settles_it(self):
        ctx = self._workspace("ws_usage_pg_unc", ROLE_OWNER, "owner_u")
        operation_id = self._operation(ctx)
        for state in (OperationState.RUNNING, OperationState.UNCERTAIN):
            self._move(WORKER, ctx, operation_id, state)
        self.assertEqual(self._metrics(ctx, operation_id), [])
        self._move(WORKER, ctx, operation_id, OperationState.FAILED)
        self.assertEqual([metric for metric, *_ in self._metrics(ctx, operation_id)],
                         ["operations", "processing_seconds"])

    def test_asset_version_facts_are_written_as_the_worker_and_the_api(self):
        ctx = self._workspace("ws_usage_pg_av", ROLE_OWNER, "owner_v")
        for index, name in ROLE_NAMES.items():
            with self.subTest(role=name):
                version = SimpleNamespace(id=f"av_{name}_{uuid.uuid4().hex[:8]}", workspace_id=ctx.workspace_id,
                                          origin_kind="generation", execution_mode="byok",
                                          created_at=datetime.now(timezone.utc))
                item_id = f"ri_{name}_{uuid.uuid4().hex[:8]}"
                with self._as(index), tenant_session(ctx) as session:
                    self.assertEqual(record_asset_version_generated(session, ctx, version).written, 1)
                    self.assertEqual(record_asset_version_generated(session, ctx, version).duplicates, 1)
                    for _ in range(2):
                        failed = record_generation_item_failed(
                            session, ctx, run_item_id=item_id, revision=3, execution_mode="controlled_mock",
                            occurred_at=datetime.now(timezone.utc))
                    self.assertEqual((failed.written, failed.duplicates), (0, 1))
                self.assertEqual(self._metrics(ctx, version.id), [("asset_versions_generated", "byok", "asset_version")])
                self.assertEqual(self._metrics(ctx, item_id), [("generation_items_failed", "mock", "run_item")])

    def test_one_workspace_s_operations_stay_out_of_another_s_usage(self):
        alpha = self._workspace("ws_usage_pg_op_a", ROLE_MEMBER, "member_a")
        beta = self._workspace("ws_usage_pg_op_b", ROLE_MEMBER, "member_b")
        operation_id = self._operation(alpha)
        for state in (OperationState.RUNNING, OperationState.CANCELLED):
            self._move(WORKER, alpha, operation_id, state)
        for metric in ("operations", "processing_seconds"):
            with self.subTest(metric=metric):
                mine = self._usage(alpha, {"metric_key": metric})
                theirs = self._usage(beta, {"metric_key": metric})
                self.assertEqual(Decimal(mine["by_execution_mode"]["not_applicable"]) > 0, True)
                self.assertEqual(Decimal(mine["total"]), Decimal(mine["by_execution_mode"]["not_applicable"]))
                self.assertEqual((theirs["workspace_id"], Decimal(theirs["total"])), (beta.workspace_id, Decimal("0")))
                self.assertEqual({Decimal(v) for v in theirs["by_execution_mode"].values()}, {Decimal("0")})
        with self._as(API), tenant_session(beta) as session:  # nor through the table itself
            self.assertEqual(session.query(UsageEventRecord).filter(
                UsageEventRecord.source_resource_id == operation_id).count(), 0)

    # -- Slice S3: CHECK constraints (#145) ------------------------------------

    def _insert(self, ctx: TenantContext, **overrides):
        """The statement ``record_many`` sends, for one row of the Workspace."""
        now = datetime.now(timezone.utc)
        values = dict(
            id=f"ue_{uuid.uuid4().hex}", workspace_id=ctx.workspace_id, metric_key=METRIC,
            measurement_kind="delta", direction="increase", quantity=Decimal("1"), unit="call",
            execution_mode="byok", semantic_key_hash=uuid.uuid4().hex * 2, occurred_at=now, created_at=now,
        )
        values.update(overrides)
        return postgresql.insert(UsageEventRecord.__table__).values(**values).on_conflict_do_nothing()

    def _checks(self) -> dict[str, bool]:
        """Every CHECK on usage_events -> whether it is validated."""
        with self.owner.connect() as connection:
            return dict(connection.execute(text(
                "SELECT conname, convalidated FROM pg_constraint "
                "WHERE conrelid = 'usage_events'::regclass AND contype = 'c'")).all())

    def _append_only_trigger(self) -> list[tuple]:
        with self.owner.connect() as connection:
            return connection.execute(text(
                "SELECT t.tgname, t.tgenabled, pg_get_triggerdef(t.oid), p.prosrc, p.proconfig::text "
                "FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid "
                "WHERE t.tgrelid = 'usage_events'::regclass AND NOT t.tgisinternal ORDER BY t.tgname")).all()

    def _stored(self, ctx: TenantContext) -> list[tuple]:
        """Every column of the Workspace's rows, as the owner reads them."""
        with self.owner.connect() as connection:
            return connection.execute(text("SELECT * FROM usage_events WHERE workspace_id = :ws ORDER BY id"),
                                      {"ws": ctx.workspace_id}).all()

    def test_the_four_checks_are_validated_and_named_as_the_model_names_them(self):
        self.assertEqual(len(CHECK_NAMES), 4)
        self.assertEqual(self._checks(), dict.fromkeys(CHECK_NAMES, True))
        self.assertEqual([name for name, *_ in self._append_only_trigger()], ["trg_usage_events_immutable"])

    def test_each_check_refuses_the_row_that_breaks_it_as_the_api_and_the_worker(self):
        ctx = self._workspace("ws_usage_pg_check", ROLE_OWNER, "owner_k")
        for index, role in ROLE_NAMES.items():
            for name, overrides in BAD_ROWS:
                with self.subTest(role=role, check=name, row=overrides):
                    with self.assertRaises(DBAPIError) as caught:
                        with self._as(index), tenant_session(ctx) as session:
                            session.execute(self._insert(ctx, **overrides))
                    self.assertIn(f'violates check constraint "ck_usage_events_{name}"', str(caught.exception))
            with self.subTest(role=role, row="valid"):  # the same statement writes a good row
                with self._as(index), tenant_session(ctx) as session:
                    written = session.execute(self._insert(ctx), execution_options={"preserve_rowcount": True})
                    self.assertEqual(written.rowcount, 1)
        self.assertEqual(len(self._stored(ctx)), len(ROLE_NAMES))

    def test_rows_every_writer_wrote_before_the_checks_pass_validate(self):
        trigger, head = self._append_only_trigger(), self._alembic("current")
        self.assertIn("(head)", head)
        self.addCleanup(self._alembic, "upgrade", "head")  # never leave the cluster below head
        self._alembic("downgrade", CHECKS_DOWN_REVISION)
        self.assertEqual(self._checks(), {})

        ctx = self._workspace("ws_usage_pg_validate", ROLE_OWNER, "owner_v")
        now = datetime.now(timezone.utc)
        for index, name in ROLE_NAMES.items():
            with self._as(index), tenant_session(ctx) as session:
                for mode in ("byok", "sandbox", "mock", "controlled_mock"):  # S1 (#69)
                    attempt = SimpleNamespace(id=f"pca_{name}_{mode}", workspace_id=ctx.workspace_id,
                                              execution_mode=mode)
                    self.assertEqual(record_provider_call(session, ctx, attempt, occurred_at=now).written, 1)
                version = SimpleNamespace(id=f"av_{name}", workspace_id=ctx.workspace_id,  # S2 (#87)
                                          origin_kind="generation", execution_mode="byok", created_at=now)
                self.assertEqual(record_asset_version_generated(session, ctx, version).written, 1)
                self.assertEqual(record_generation_item_failed(
                    session, ctx, run_item_id=f"ri_{name}", revision=1, execution_mode="controlled_mock",
                    occurred_at=now).written, 1)
            operation_id = self._operation(ctx)
            for state in (OperationState.RUNNING, OperationState.SUCCEEDED):
                self._move(index, ctx, operation_id, state)
        before = self._stored(ctx)
        self.assertEqual(len(before), len(ROLE_NAMES) * (4 + 1 + 1 + 2))
        self.assertEqual({row.execution_mode for row in before}, set(USAGE_EXECUTION_MODES))
        self.assertEqual({row.metric_key for row in before}, set(METRICS))

        self._alembic("upgrade", CHECKS_REVISION)  # NOT VALID, then VALIDATE over these rows
        self.assertEqual(self._checks(), dict.fromkeys(CHECK_NAMES, True))
        self.assertEqual(self._stored(ctx), before)
        self._alembic("upgrade", "head")
        self.assertEqual(self._alembic("current"), head)
        self.assertEqual(self._append_only_trigger(), trigger)
        self.test_the_privilege_matrix_of_usage_events_holds()
        with self.assertRaises(DBAPIError) as caught:
            with self.owner.begin() as connection:
                connection.execute(text("UPDATE usage_events SET quantity = 2 WHERE workspace_id = :ws"),
                                   {"ws": ctx.workspace_id})
        self.assertIn("usage_events is append-only", str(caught.exception))

    def test_a_row_that_breaks_a_check_fails_validate_and_rolls_the_revision_back(self):
        self.addCleanup(self._alembic, "upgrade", "head")
        self._alembic("downgrade", CHECKS_DOWN_REVISION)
        ctx = self._workspace("ws_usage_pg_broken", ROLE_OWNER, "owner_x")
        self.addCleanup(self._remove_workspaces)  # runs before the upgrade above: the bad row goes first
        with self.owner.begin() as connection:  # the owner bypasses RLS; no writer sends this row
            connection.execute(self._insert(ctx, quantity=Decimal("-1")))

        failed = self._alembic_run("upgrade", CHECKS_REVISION)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('check constraint "ck_usage_events_quantity_nonnegative" of relation "usage_events" '
                      "is violated by some row", failed.stderr)
        self.assertIn(CHECKS_DOWN_REVISION, self._alembic("current"))
        self.assertEqual(self._checks(), {}, "the NOT VALID constraints rolled back with the revision")

    def test_the_privilege_matrix_of_usage_events_holds(self):
        with self.owner.connect() as connection:
            forced = connection.execute(text(
                "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE relname = 'usage_events'"
            )).scalar()
            self.assertTrue(forced)
            for role, expected in GRANTED.items():
                for privilege in DML:
                    held = connection.execute(text("SELECT has_table_privilege(:r, 'usage_events', :p)"),
                                              {"r": role, "p": privilege}).scalar()
                    self.assertEqual(held, privilege in expected, f"{role} {privilege} usage_events")

    def _alembic_run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "alembic", *arguments], cwd=ROOT, capture_output=True, text=True,
            env={**os.environ, "MARKETAI_DATABASE_URL": URLS[0]}, check=False,
        )

    def _alembic(self, *arguments: str) -> str:
        completed = self._alembic_run(*arguments)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout

    def _api_selects(self) -> bool:
        with self.owner.connect() as connection:
            return connection.execute(text(
                "SELECT has_table_privilege('marketai_domain_api', 'usage_events', 'SELECT')")).scalar()

    def test_the_migration_grants_select_and_its_downgrade_revokes_it(self):
        head = self._alembic("current")  # later migrations may follow REVISION
        self.assertIn("(head)", head)
        self.addCleanup(self._alembic, "upgrade", "head")  # never leave the cluster below head
        self._alembic("downgrade", DOWN_REVISION)
        self.assertIn(DOWN_REVISION, self._alembic("current"))
        self.assertFalse(self._api_selects())
        self._alembic("upgrade", REVISION)
        self.assertIn(REVISION, self._alembic("current"))
        self.assertTrue(self._api_selects())
        self._alembic("upgrade", "head")
        self.assertEqual(self._alembic("current"), head)
        self.test_the_privilege_matrix_of_usage_events_holds()


if __name__ == "__main__":
    unittest.main()
