"""Issue #398 against an isolated migrated PostgreSQL with the real API role.

Publishing a Target into a context that already has an active one used to
reach the partial unique index ``ix_stgt_active_target`` and answer 500. It
now replaces that Target in one transaction: the old one becomes
``withdrawn`` with a ``withdraw`` event whose reason names its successor, and
the new one is published. Here the routes and the domain run as the role that
serves them in production, under RLS:

* a second publication answers 201, leaves the old Target's values and its
  ``publish`` event as they were, and leaves exactly one active Target;
* a retry of that publication returns the same Target;
* a preview made before another publication changed the context is refused
  with 409 ``preview_hash_mismatch``, whether the other publication commits
  first or while this one waits on it, and changes nothing;
* a withdrawal waiting on a replacement of the same Target is refused, so the
  Target is withdrawn once;
* another Workspace, another currency and a Member change nothing.

Run with ``DOMAIN_OWNER_DATABASE_URL``, ``DOMAIN_API_DATABASE_URL``,
``MARKETAI_DATABASE_URL`` (the owner) and ``MARKETAI_TENANT_CONTEXT_SECRET``
against a cluster released with ``make db-release``.
"""
from __future__ import annotations

import os
import threading
import time
import unittest
import uuid
from dataclasses import replace
from decimal import Decimal
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import WorkspaceRecord
from app.library.contracts import ROLE_OWNER
from app.main import app
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext
from app.scale.targets import TargetLifecycleError, publish_target, withdraw_target

URLS = [os.getenv(key) for key in ("DOMAIN_OWNER_DATABASE_URL", "DOMAIN_API_DATABASE_URL")]
MODE = "customer_sandbox"
BASE = f"/api/v1/scale/{MODE}"
_TIMEOUT = 30


@unittest.skipUnless(all(URLS) and os.getenv("MARKETAI_TENANT_CONTEXT_SECRET"),
                     "requires isolated PostgreSQL runtime roles")
class ScaleTargetReplacementPostgresTests(unittest.TestCase):
    def setUp(self):
        self.owner, self.api = create_engine(URLS[0]), create_engine(URLS[1])
        for engine in (self.owner, self.api):
            self.addCleanup(engine.dispose)
        self.owner_sessions = sessionmaker(bind=self.owner, expire_on_commit=False, autoflush=False)
        self.api_sessions = sessionmaker(bind=self.api, expire_on_commit=False, autoflush=False)
        self.current: TenantContext | None = None
        app.dependency_overrides[request_tenant_context] = lambda: self.current
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def _as_owner(self):
        return patch("app.adapters.persistence.database.SessionLocal", self.owner_sessions)

    def _as_api(self):
        return patch("app.adapters.persistence.database.SessionLocal", self.api_sessions)

    def _workspace(self, prefix: str, actor: str = "owner_a", role: str = ROLE_OWNER) -> TenantContext:
        """A Workspace the control plane provisioned, so the owner writes it."""
        workspace_id = f"{prefix}_{uuid.uuid4().hex[:10]}"
        with self._as_owner(), platform_session() as session:
            session.add(WorkspaceRecord(id=workspace_id, name=workspace_id))
        return TenantContext(workspace_id=workspace_id, role=role, initiator_id=actor,
                             initiator_role=role, request_id="test-req", trace_id="trace-test")

    def _post(self, caller: TenantContext, path: str, body: dict, key: str | None = None):
        self.current = caller
        with self._as_api():
            return self.client.post(f"{BASE}{path}", json=body,
                                    headers={"Idempotency-Key": key or f"ci-only-ik-398-{uuid.uuid4().hex}"})

    def _preview(self, caller: TenantContext, cpa: str, currency: str = "USD") -> str:
        response = self._post(caller, "/target-publication-previews", {
            "product_version_id": "pv_398", "offer_version_id": "ov_398", "market_code": "US",
            "currency": currency, "target_cpa": cpa, "minimum_economic_spend": "100.00",
        })
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def _publish(self, caller: TenantContext, preview_id: str, key: str | None = None):
        return self._post(caller, "/targets", {"publication_preview_id": preview_id}, key)

    def _published(self, caller: TenantContext, cpa: str, currency: str = "USD") -> str:
        response = self._publish(caller, self._preview(caller, cpa, currency))
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def _refused(self, response, status: int, code: str) -> None:
        self.assertEqual(response.status_code, status, response.text)
        body = response.json()
        self.assertEqual(body["code"], code)
        self.assertTrue({"type", "title", "status", "code", "trace_id", "retryable"} <= body.keys(), body)

    # -- reads, as the owner ------------------------------------------------

    def _targets(self, ctx: TenantContext) -> dict[str, tuple]:
        """id -> (currency, state, revision, cpa, spend, published_at, withdrawn_at, creator)."""
        with self.owner.connect() as connection:
            rows = connection.execute(text(
                "SELECT id, currency, lifecycle_state, revision, target_cpa, minimum_economic_spend, "
                "published_at, withdrawn_at, creator_id FROM scale_target_versions WHERE workspace_id = :w"
            ), {"w": ctx.workspace_id}).all()
        return {row[0]: tuple(row[1:]) for row in rows}

    def _active(self, ctx: TenantContext, currency: str = "USD") -> list[str]:
        return [i for i, row in self._targets(ctx).items() if row[0] == currency and row[1] == "active"]

    def _events(self, target_id: str) -> list[tuple]:
        with self.owner.connect() as connection:
            return [tuple(row) for row in connection.execute(text(
                "SELECT id, event_type, previous_lifecycle_state, current_lifecycle_state, reason, "
                "semantic_event_sequence, revision_at_event FROM scale_target_lifecycle_events "
                "WHERE target_version_id = :t ORDER BY semantic_event_sequence, created_at"
            ), {"t": target_id}).all()]

    def _consumed(self, preview_id: str) -> bool:
        with self.owner.connect() as connection:
            return connection.execute(text(
                "SELECT consumed_at IS NOT NULL FROM scale_target_publication_previews WHERE id = :id"
            ), {"id": preview_id}).scalar_one()

    def _journaled(self, ctx: TenantContext, key: str) -> int:
        with self.owner.connect() as connection:
            return connection.execute(text(
                "SELECT count(*) FROM scale_command_journal WHERE workspace_id = :w AND dedup_key = :k"
            ), {"w": ctx.workspace_id, "k": key}).scalar_one()

    def _state(self, ctx: TenantContext) -> tuple:
        targets = self._targets(ctx)
        return targets, {target_id: self._events(target_id) for target_id in targets}

    # -- replacement ----------------------------------------------------------

    def test_publishing_again_replaces_the_active_target_without_rewriting_it(self):
        ctx = self._workspace("ws_stgt_pg")
        old_id = self._published(ctx, "20.00")
        old_row, [old_publish] = self._targets(ctx)[old_id], self._events(old_id)

        key = "ci-only-ik-398-replace"
        replaced = self._publish(ctx, self._preview(ctx, "25.00"), key)
        self.assertEqual(replaced.status_code, 201, replaced.text)
        new_id = replaced.json()["id"]
        self.assertEqual((replaced.json()["lifecycle_state"], replaced.json()["target_cpa"]),
                         ("active", "25.000000000000"))

        targets = self._targets(ctx)
        currency, state, revision, cpa, spend, published_at, withdrawn_at, creator = targets[old_id]
        self.assertEqual((state, revision), ("withdrawn", 2))
        self.assertIsNotNone(withdrawn_at)
        # The values, the publication and its author are what they were.
        self.assertEqual((currency, cpa, spend, published_at, creator),
                         (old_row[0], old_row[3], old_row[4], old_row[5], old_row[7]))
        self.assertEqual((cpa, spend), (Decimal("20"), Decimal("100")))
        withdraw = self._events(old_id)
        self.assertEqual(withdraw[0], old_publish)
        self.assertEqual([event[1:] for event in withdraw], [
            ("publish", None, "active", "publication_confirmed", 1, 1),
            ("withdraw", "active", "withdrawn", f"replaced_by:{new_id}", 2, 2),
        ])
        self.assertEqual([event[1:] for event in self._events(new_id)],
                         [("publish", None, "active", "publication_confirmed", 1, 1)])
        self.assertEqual(self._active(ctx), [new_id])
        self.assertEqual(self._journaled(ctx, key), 1)

    def test_a_retry_of_a_replacement_returns_the_same_target(self):
        ctx = self._workspace("ws_stgt_pg_retry")
        old_id = self._published(ctx, "20.00")
        preview_id = self._preview(ctx, "25.00")
        key = "ci-only-ik-398-retry"
        first = self._publish(ctx, preview_id, key)
        self.assertEqual(first.status_code, 201, first.text)
        before = self._state(ctx)

        again = self._publish(ctx, preview_id, key)
        self.assertEqual(again.status_code, 201, again.text)
        self.assertEqual(again.json()["id"], first.json()["id"])
        self.assertEqual(self._state(ctx), before)
        self.assertEqual(self._active(ctx), [first.json()["id"]])
        self.assertEqual(len(self._events(old_id)), 2)

    def test_a_preview_made_before_another_publication_is_preview_hash_mismatch(self):
        for replacing in (False, True):
            with self.subTest(replacing=replacing):
                ctx = self._workspace("ws_stgt_pg_stale")
                if replacing:
                    self._published(ctx, "20.00")
                first, stale = self._preview(ctx, "25.00"), self._preview(ctx, "30.00")
                self.assertEqual(self._publish(ctx, first).status_code, 201)
                before = self._state(ctx)

                key = f"ci-only-ik-398-stale-{replacing}"
                self._refused(self._publish(ctx, stale, key), 409, "preview_hash_mismatch")
                self.assertEqual(self._state(ctx), before)
                self.assertFalse(self._consumed(stale))
                self.assertEqual(self._journaled(ctx, key), 0)
                self.assertEqual(len(self._active(ctx)), 1)

    # -- races on two real connections -----------------------------------------

    def _domain(self, ctx: TenantContext) -> TenantContext:
        return replace(ctx, meta_mode=MODE)

    def _http_previews(self, ctx: TenantContext, *amounts: str) -> list[str]:
        return [self._preview(ctx, amount) for amount in amounts]

    def _run(self, call) -> object:
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 -- captured for assertion
            return exc

    def _waiting_on_a_lock(self) -> bool:
        with self.owner.connect() as connection:
            return connection.execute(text("SELECT count(*) FROM pg_locks WHERE NOT granted")).scalar_one() > 0

    def _behind(self, ctx: TenantContext, first, second) -> tuple[object, object]:
        """Run ``first`` in an open API transaction, start ``second`` on another
        connection, commit ``first`` only once ``second`` waits on its locks,
        and return both outcomes."""
        outcome: list[object] = [None]

        def waiting() -> None:
            def call():
                with tenant_session(ctx) as session:
                    return second(session)
            outcome[0] = self._run(call)

        thread = threading.Thread(target=waiting)
        with self._as_api():
            with tenant_session(ctx) as session:
                result = first(session)
                thread.start()
                deadline = time.monotonic() + _TIMEOUT
                while not self._waiting_on_a_lock():
                    self.assertTrue(thread.is_alive(), outcome[0])
                    self.assertLess(time.monotonic(), deadline, "the second command never waited")
                    time.sleep(0.05)
            thread.join(timeout=_TIMEOUT)
        self.assertFalse(thread.is_alive())
        return result, outcome[0]

    def _publisher(self, ctx: TenantContext, preview_id: str):
        def publish(session):
            return publish_target(session, ctx, preview_id=preview_id,
                                  idempotency_key=f"ci-only-ik-398-{uuid.uuid4().hex}")
        return publish

    def _assert_mismatch(self, outcome: object) -> None:
        self.assertIsInstance(outcome, TargetLifecycleError, repr(outcome))
        self.assertEqual(outcome.code, "preview_hash_mismatch", repr(outcome))

    def test_a_publication_waiting_on_another_is_refused_and_changes_nothing(self):
        for replacing in (False, True):
            with self.subTest(replacing=replacing):
                http = self._workspace("ws_stgt_pg_wait")
                ctx = self._domain(http)
                old_id = self._published(http, "20.00") if replacing else None
                first, second = self._http_previews(http, "25.00", "30.00")

                # First publication: the second one's insert waits on the
                # unique index. Replacement: its lock on the old Target waits.
                winner, loser = self._behind(ctx, self._publisher(ctx, first), self._publisher(ctx, second))
                self._assert_mismatch(loser)
                self.assertEqual(self._active(http), [winner.id])
                self.assertFalse(self._consumed(second))
                if old_id:
                    self.assertEqual([e[1] for e in self._events(old_id)], ["publish", "withdraw"])

    def test_racing_publications_let_exactly_one_through(self):
        for replacing in (False, True):
            with self.subTest(replacing=replacing):
                http = self._workspace("ws_stgt_pg_race")
                ctx = self._domain(http)
                old_id = self._published(http, "20.00") if replacing else None
                previews = self._http_previews(http, "25.00", "30.00")
                barrier = threading.Barrier(len(previews))
                results: list[object] = [None] * len(previews)

                def publish(index: int) -> None:
                    def call():
                        barrier.wait(timeout=_TIMEOUT)
                        with tenant_session(ctx) as session:
                            return self._publisher(ctx, previews[index])(session)
                    results[index] = self._run(call)

                threads = [threading.Thread(target=publish, args=(i,)) for i in range(len(previews))]
                with self._as_api():
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join(timeout=_TIMEOUT * 2)

                published = [r for r in results if not isinstance(r, Exception)]
                refused = [r for r in results if isinstance(r, Exception)]
                self.assertEqual((len(published), len(refused)), (1, 1), results)
                self._assert_mismatch(refused[0])
                self.assertEqual(self._active(http), [published[0].id])
                if old_id:
                    self.assertEqual([e[1] for e in self._events(old_id)], ["publish", "withdraw"])

    def test_a_withdrawal_waiting_on_a_replacement_is_refused(self):
        http = self._workspace("ws_stgt_pg_wd")
        ctx = self._domain(http)
        old_id = self._published(http, "20.00")
        withdrawal = self._post(http, f"/targets/{old_id}/withdrawal-previews", {"reason": "#398"})
        self.assertEqual(withdrawal.status_code, 201, withdrawal.text)
        [replacement] = self._http_previews(http, "25.00")

        def withdraw(session):
            return withdraw_target(session, ctx, preview_id=withdrawal.json()["id"],
                                   idempotency_key="ci-only-ik-398-withdraw")

        winner, loser = self._behind(ctx, self._publisher(ctx, replacement), withdraw)
        self.assertIsInstance(loser, TargetLifecycleError, repr(loser))
        self.assertEqual(loser.code, "invalid_lifecycle")
        self.assertEqual([e[1:] for e in self._events(old_id)], [
            ("publish", None, "active", "publication_confirmed", 1, 1),
            ("withdraw", "active", "withdrawn", f"replaced_by:{winner.id}", 2, 2),
        ])
        self.assertEqual(self._targets(http)[old_id][1:3], ("withdrawn", 2))
        self.assertEqual(self._active(http), [winner.id])

    # -- boundaries ------------------------------------------------------------

    def test_another_workspace_neither_replaces_nor_consumes_a_target(self):
        ctx = self._workspace("ws_stgt_pg_a")
        stranger = self._workspace("ws_stgt_pg_b", "owner_b")
        mine = self._published(ctx, "20.00")
        before = self._state(ctx)

        # The same context in another Workspace is a context of its own.
        theirs = self._published(stranger, "25.00")
        self.assertEqual(self._state(ctx), before)
        self.assertEqual(self._active(stranger), [theirs])

        # Neither can publish the other's preview.
        for caller, owner in ((stranger, ctx), (ctx, stranger)):
            preview_id = self._preview(owner, "30.00")
            self._refused(self._publish(caller, preview_id), 404, "target_not_found")
            self.assertFalse(self._consumed(preview_id))
        self.assertEqual(self._state(ctx), before)
        self.assertEqual(self._active(ctx), [mine])
        self.assertEqual(self._active(stranger), [theirs])

    def test_another_currency_keeps_its_own_active_target(self):
        ctx = self._workspace("ws_stgt_pg_ccy")
        dollars = self._published(ctx, "20.00", "USD")
        euros = self._published(ctx, "18.00", "EUR")
        self.assertEqual((self._active(ctx, "USD"), self._active(ctx, "EUR")), ([dollars], [euros]))
        euro_events = self._events(euros)

        replacement = self._published(ctx, "22.00", "USD")
        self.assertEqual((self._active(ctx, "USD"), self._active(ctx, "EUR")), ([replacement], [euros]))
        self.assertEqual(self._events(euros), euro_events)

    def test_a_member_cannot_publish_and_nothing_changes(self):
        ctx = self._workspace("ws_stgt_pg_member")
        member = replace(ctx, role="member", initiator_id="member_a", initiator_role="member")
        active = self._published(ctx, "20.00")
        preview_id = self._preview(ctx, "25.00")
        before = self._state(ctx)

        key = "ci-only-ik-398-member"
        self._refused(self._publish(member, preview_id, key), 403, "owner_required")
        self.assertEqual(self._state(ctx), before)
        self.assertFalse(self._consumed(preview_id))
        self.assertEqual(self._journaled(ctx, key), 0)
        self.assertEqual(self._active(ctx), [active])


if __name__ == "__main__":
    unittest.main()
