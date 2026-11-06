"""Issue #284 against an isolated migrated PostgreSQL with the real API role.

SQLite does not check grants, so editing a draft Experiment entry answered
500 ``permission denied for table experiment_entries`` on PostgreSQL while
``make verify`` stayed green. Here both draft entry commands run as the role
that serves them in production:

* edit (``PUT /api/v1/experiments/{id}/entries/{entry_id}``) and remove (the
  legacy ``DELETE``, a soft delete through UPDATE: the domain database grants
  no DELETE) succeed and bump the revision;
* a stale revision is the existing 409, never a 500;
* a removed entry is gone from the detail, the duplicate analysis and the
  frozen candidates, answers 404, and its asset and position can be taken
  again; the row itself stays;
* a frozen Experiment refuses both commands;
* another Workspace's Member and Owner (non-members here) get 404 and change
  nothing.

Then the privilege matrix of ``experiment_entries``: the API may UPDATE, no
role may DELETE, and both unique indexes cover live entries only.

And the draft commands' hardening (#340), under the same role:

* a second live entry with an AssetVersion is 409 ``experiment_asset_duplicate``
  and writes nothing, where the live-only unique index used to answer 500; a
  removed entry's version can be added again;
* two commands that read one revision run on two real connections: the
  loser waits on the Experiment row, then finds the revision taken and
  answers 409, for each of the four commands;
* each accepted command appends exactly one audit event (ids, revision and
  field names only), a refused one none;
* another Workspace's Member and Owner get 404 and change nothing.

Run with ``DOMAIN_OWNER_DATABASE_URL``, ``DOMAIN_API_DATABASE_URL``,
``DOMAIN_WORKER_DATABASE_URL``, ``MARKETAI_DATABASE_URL`` (the owner) and
``MARKETAI_TENANT_CONTEXT_SECRET`` against a cluster released with
``make db-release``.
"""
from __future__ import annotations

import importlib.util
import json
import os
import unittest
import uuid
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import (
    AuditEventRecord,
    ExperimentEntryRecord,
    ExperimentRecord,
    ExperimentStateEventRecord,
    ExperimentVersionCandidateRecord,
    ExperimentVersionRecord,
    WorkspaceRecord,
)
from app.library.contracts import ROLE_MEMBER, ROLE_OWNER
from app.main import app
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext
from tests.support.draft_race import race_on_one_revision

URLS = [os.getenv(key) for key in ("DOMAIN_OWNER_DATABASE_URL", "DOMAIN_API_DATABASE_URL", "DOMAIN_WORKER_DATABASE_URL")]
SEED_SCRIPT = Path(__file__).resolve().parents[1] / "web" / "e2e" / "experiments" / "seed_prerequisites.py"
#: role -> privileges it must hold on experiment_entries; every other DML privilege must be absent.
GRANTED = {
    "marketai_domain_api": {"SELECT", "INSERT", "UPDATE"},
    "marketai_domain_worker": {"SELECT"},
    "marketai_domain_readonly": {"SELECT"},
}
DML = ("SELECT", "INSERT", "UPDATE", "DELETE")
#: Everything a draft command's audit event may carry (#340): ids, the revision, field names.
AUDIT_KEYS = {"entry_id", "asset_version_id", "revision", "changed_fields"}
SECRET = "content that must never reach the audit trail"


def _seed_module():
    spec = importlib.util.spec_from_file_location("experiments_e2e_seed_prerequisites_pg", SEED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _draft_context(seed: dict) -> dict:
    """The Experiments workspace's draft context (``duplicate-resolution.spec.ts``)."""
    start, end = date.today() + timedelta(days=1), date.today() + timedelta(days=30)
    return {
        "market": "US", "currency": "USD", "placement": "feed",
        "product_version_id": seed["productVersionId"], "offer_version_id": seed["offerVersionId"],
        "budget": {"daily_amount": "5.00", "total_amount": "150.00"},
        "schedule": {"start_at": f"{start.isoformat()}T00:00:00+00:00",
                     "end_at": f"{end.isoformat()}T23:59:59+00:00", "timezone": "UTC"},
        "audience": {"schema_version": "1", "content": {"targeting": "broad"}},
        "attribution": {"click_window_days": 7, "view_window_days": 1},
        "statistical_plan": {"primary_metric": "ctr", "ctr_threshold": "0.01", "alpha": "0.05",
                             "bootstrap_samples": 2000},
    }


@unittest.skipUnless(all(URLS) and os.getenv("MARKETAI_TENANT_CONTEXT_SECRET"),
                     "requires isolated PostgreSQL runtime roles")
class ExperimentEntryPostgresTests(unittest.TestCase):
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

    def _as(self, index: int):
        return patch("app.adapters.persistence.database.SessionLocal", self.sessions[index])

    def _workspace(self, prefix: str, role: str, actor: str) -> TenantContext:
        workspace_id = f"{prefix}_{uuid.uuid4().hex[:10]}"
        with self._as(0), platform_session() as session:  # the control plane opens Workspaces
            session.add(WorkspaceRecord(id=workspace_id, name=workspace_id))
        return TenantContext(workspace_id=workspace_id, role=role, initiator_id=actor,
                             request_id="test-req", trace_id="trace-test")

    def _as_role(self, ctx: TenantContext, role: str, actor: str) -> TenantContext:
        return TenantContext(workspace_id=ctx.workspace_id, role=role, initiator_id=actor,
                             request_id="test-req", trace_id="trace-test")

    def _call(self, method: str, path: str, expect: int, body: dict | None = None) -> dict:
        response = self.client.request(method, path, json=body,
                                       headers={"Idempotency-Key": f"ik-{uuid.uuid4().hex}"})
        self.assertEqual(response.status_code, expect, f"{method} {path}: {response.text}")
        return response.json()

    def _put(self, experiment_id: str, entry_id: str, body: dict, expect: int = 200) -> dict:
        return self._call("PUT", f"/api/v1/experiments/{experiment_id}/entries/{entry_id}", expect, body)

    def _remove(self, experiment_id: str, entry_id: str, revision: int, expect: int = 200) -> dict:
        return self._call("DELETE", f"/api/experiments/{experiment_id}/entries/{entry_id}", expect,
                          {"expected_revision": revision})

    def _rows(self, ctx: TenantContext, experiment_id: str) -> dict:
        """Every entry row and the revision, read as the schema owner."""
        with self._as(0), tenant_session(ctx) as session:
            rows = session.scalars(select(ExperimentEntryRecord).where(
                ExperimentEntryRecord.experiment_id == experiment_id).order_by(ExperimentEntryRecord.id)).all()
            revision = session.get(ExperimentRecord, experiment_id).revision
            return {"revision": revision, "entries": [
                (row.id, row.asset_version_id, row.position, row.marketing_vars_json, row.removed_at is not None)
                for row in rows]}

    def test_draft_entry_commands_run_under_the_api_role(self):
        member = self._workspace("ws_entries_pg", ROLE_MEMBER, "member_a")
        owner = self._as_role(member, ROLE_OWNER, "owner_a")
        with self._as(0):
            seed = _seed_module().seed_workspace(owner, workspace_slug="entries-pg")
        stranger = self._workspace("ws_entries_pg_other", ROLE_MEMBER, "member_b")
        first, second = seed["duplicatePairAssetVersionIds"]
        independent = seed["independentAssetVersionId"]

        with self._as(1):
            self.current = member
            created = self._call("POST", "/api/v1/experiments", 201, {
                "product_id": seed["productId"], "name": "Entries under the API role",
                "draft_context": _draft_context(seed)})
            experiment_id = created["id"]
            base = f"/api/v1/experiments/{experiment_id}"
            kept = self._call("POST", f"{base}/entries", 201,
                              {"asset_version_id": first, "expected_revision": 1})
            removed = self._call("POST", f"{base}/entries", 201,
                                 {"asset_version_id": independent, "expected_revision": 2})
            self.assertEqual((kept["position"], removed["position"], removed["revision"]), (0, 1, 3))

            # Edit: marketing variables, then the asset version itself.
            edited = self._put(experiment_id, kept["id"], {"marketing_vars": {"headline": "edited"},
                                                           "expected_revision": 3})
            self.assertEqual((edited["revision"], edited["marketing_vars"]), (4, {"headline": "edited"}))
            stale = self._put(experiment_id, kept["id"], {"marketing_vars": {"headline": "lost"},
                                                          "expected_revision": 3}, expect=409)
            self.assertEqual(stale["code"], "revision_conflict")
            swapped = self._put(experiment_id, kept["id"], {"asset_version_id": second, "expected_revision": 4})
            self.assertEqual((swapped["revision"], swapped["asset_version_id"]), (5, second))

            # Remove: the legacy DELETE soft-deletes through UPDATE.
            self.assertEqual(self._remove(experiment_id, removed["id"], 4, expect=409)["code"], "experiment_conflict")
            self.assertEqual(self._remove(experiment_id, removed["id"], 5),
                             {"removed": removed["id"], "revision": 6})
            # A removed entry is gone for both commands.
            self._remove(experiment_id, removed["id"], 6, expect=404)
            self._put(experiment_id, removed["id"], {"marketing_vars": {}, "expected_revision": 6}, expect=404)
            detail = self._call("GET", base, 200)
            self.assertEqual((detail["revision"], [entry["id"] for entry in detail["entries"]]),
                             (6, [kept["id"]]))

        rows = self._rows(member, experiment_id)["entries"]
        self.assertIn((removed["id"], independent, 1, {}, True), rows)  # the row stays, marked removed

        with self._as(1):
            # The removed entry's asset and position can be taken again.
            again = self._call("POST", f"{base}/entries", 201,
                               {"asset_version_id": independent, "expected_revision": 6})
            self.assertEqual((again["position"], again["revision"]), (1, 7))

        # Another Workspace's Member and Owner (non-members here) change nothing.
        before = self._rows(member, experiment_id)
        with self._as(1):
            for caller in (stranger, self._as_role(stranger, ROLE_OWNER, "owner_b")):
                self.current = caller
                self.assertEqual(self._put(experiment_id, kept["id"], {"marketing_vars": {"x": 1},
                                                                       "expected_revision": 7},
                                           expect=404)["code"], "experiment_not_found")
                self.assertEqual(self._remove(experiment_id, kept["id"], 7, expect=404)["code"],
                                 "experiment_not_found")
                self._call("GET", base, 404)
        self.assertEqual(self._rows(member, experiment_id), before)

        with self._as(1):
            self.current = member
            analysis = self._call("GET", f"{base}/duplicate-analysis", 200)
            self.assertFalse(analysis["has_duplicates"], analysis)
            frozen = self._call("POST", f"{base}/freeze", 200, {
                "expected_revision": 7, "analyzed_candidate_set_hash": analysis["candidate_set_hash"]})
            self.assertEqual((frozen["state"], frozen["revision"]), ("frozen", 8))
            self.assertEqual([entry["id"] for entry in frozen["entries"]], [kept["id"], again["id"]])

        with self._as(0), tenant_session(member) as session:
            version = session.scalars(select(ExperimentVersionRecord).where(
                ExperimentVersionRecord.experiment_id == experiment_id)).one()
            candidates = session.scalars(select(ExperimentVersionCandidateRecord.asset_version_id).where(
                ExperimentVersionCandidateRecord.experiment_version_id == version.id)).all()
        self.assertEqual(sorted(candidates), sorted([second, independent]))

        # Entries are immutable after freeze.
        before = self._rows(member, experiment_id)
        with self._as(1):
            for caller in (member, owner):
                self.current = caller
                self.assertEqual(self._put(experiment_id, kept["id"], {"marketing_vars": {"x": 1},
                                                                       "expected_revision": 8},
                                           expect=409)["code"], "experiment_state_conflict")
                self.assertEqual(self._remove(experiment_id, kept["id"], 8, expect=409)["code"],
                                 "experiment_conflict")
        self.assertEqual(self._rows(member, experiment_id), before)

        with self.api.connect() as connection:
            visible = connection.execute(text("SELECT count(*) FROM experiment_entries")).scalar()
            self.assertEqual(visible, 0, "experiment_entries leaked without a tenant context")

    def _send(self, method: str, path: str, body: dict):
        return self.client.request(method, path, json=body,
                                   headers={"Idempotency-Key": f"ik-{uuid.uuid4().hex}"})

    def _state(self, ctx: TenantContext, experiment_id: str) -> dict:
        """Entry rows, revision, context and audit trail, read as the schema owner."""
        with self._as(0), tenant_session(ctx) as session:
            experiment = session.get(ExperimentRecord, experiment_id)
            rows = session.scalars(select(ExperimentEntryRecord).where(
                ExperimentEntryRecord.experiment_id == experiment_id).order_by(ExperimentEntryRecord.id)).all()
            audit = session.scalars(select(AuditEventRecord).where(
                AuditEventRecord.resource_type == "experiment",
                AuditEventRecord.resource_id == experiment_id)).all()
            return {
                "revision": experiment.revision,
                "context": experiment.config_json,
                "entries": [(row.id, row.asset_version_id, row.position, row.marketing_vars_json,
                             row.removed_at is not None) for row in rows],
                "audit": {event.id: (event.event_type, event.details_json, event.initiator_id)
                          for event in audit},
                "state_events": session.scalar(select(func.count()).select_from(ExperimentStateEventRecord).where(
                    ExperimentStateEventRecord.experiment_id == experiment_id)),
            }

    def _one_event(self, before: dict, after: dict, event_type: str, actor: str, **details) -> dict:
        events = [event for event_id, event in after["audit"].items() if event_id not in before["audit"]]
        self.assertEqual([event[0] for event in events], [event_type], events)
        recorded = events[0][1]
        self.assertLessEqual(set(recorded), AUDIT_KEYS, recorded)
        self.assertNotIn(SECRET, json.dumps(recorded))
        self.assertEqual({key: recorded[key] for key in details}, details)
        self.assertEqual(events[0][2], actor)
        self.assertEqual(after["state_events"], before["state_events"])  # a draft edit is no transition
        return recorded

    def _one_winner(self, results, status: int, loser_code: str = "revision_conflict"):
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        self.assertEqual(sorted(response.status_code for _, response in results), sorted([status, 409]),
                         [response.text for _, response in results])
        loser = next(response for _, response in results if response.status_code == 409)
        self.assertEqual(loser.json()["code"], loser_code, loser.text)
        return next(response for _, response in results if response.status_code == status).json()

    def test_draft_commands_refuse_duplicates_and_lost_updates_and_audit_once(self):
        member = self._workspace("ws_draft340_pg", ROLE_MEMBER, "member_c")
        owner = self._as_role(member, ROLE_OWNER, "owner_c")
        with self._as(0):
            seed = _seed_module().seed_workspace(owner, workspace_slug="draft340-pg")
        stranger = self._workspace("ws_draft340_pg_other", ROLE_MEMBER, "member_d")
        first, second = seed["duplicatePairAssetVersionIds"]
        independent = seed["independentAssetVersionId"]

        self.current = member
        with self._as(1):
            experiment_id = self._call("POST", "/api/v1/experiments", 201, {
                "product_id": seed["productId"], "name": "Draft hardening under the API role",
                "draft_context": _draft_context(seed)})["id"]
        base = f"/api/v1/experiments/{experiment_id}"

        def add(asset_version_id: str, revision: int, **extra):
            return self._send("POST", f"{base}/entries",
                              {"asset_version_id": asset_version_id, "expected_revision": revision, **extra})

        def edit(entry_id: str, revision: int, **fields):
            return self._send("PUT", f"{base}/entries/{entry_id}", {**fields, "expected_revision": revision})

        def remove(entry_id: str, revision: int):
            return self._send("DELETE", f"/api/experiments/{experiment_id}/entries/{entry_id}",
                              {"expected_revision": revision})

        def context(revision: int, **overrides):
            return self._send("PUT", f"{base}/context", {
                "draft_context": {**_draft_context(seed), **overrides}, "expected_revision": revision})

        with self._as(1):
            kept = add(first, 1, marketing_vars={"headline": SECRET}).json()
            other = add(second, 2).json()
            self.assertEqual((kept["revision"], other["revision"]), (2, 3))

        # A second live entry with an AssetVersion: 409 and nothing written.
        before = self._state(member, experiment_id)
        with self._as(1):
            for response in (add(first, 3), edit(other["id"], 3, asset_version_id=first)):
                self.assertEqual((response.status_code, response.json()["code"]),
                                 (409, "experiment_asset_duplicate"), response.text)
        self.assertEqual(self._state(member, experiment_id), before)

        # A removed entry's version can be added again.
        with self._as(1):
            self.assertEqual(remove(kept["id"], 3).json(), {"removed": kept["id"], "revision": 4})
        before = self._state(member, experiment_id)
        with self._as(1):
            again = add(first, 4)
            self.assertEqual((again.status_code, again.json()["revision"]), (201, 5), again.text)
        again = again.json()
        after = self._state(member, experiment_id)
        self._one_event(before, after, "experiment.entry_added", "member_c", entry_id=again["id"],
                        asset_version_id=first, revision=5)

        # Each command, twice at once on one revision: one wins, one is told.
        races = [
            ("experiment.entry_updated", 200, "revision_conflict", lambda: race_on_one_revision(
                lambda: edit(again["id"], 5, marketing_vars={"headline": "a"}),
                lambda: edit(again["id"], 5, marketing_vars={"headline": SECRET}))),
            ("experiment.context_updated", 200, "revision_conflict", lambda: race_on_one_revision(
                lambda: context(6, market="GB", currency="GBP"), lambda: context(6, placement="story"))),
            # The legacy interface answers every conflict as ``experiment_conflict``.
            ("experiment.entry_removed", 200, "experiment_conflict", lambda: race_on_one_revision(
                lambda: remove(again["id"], 7), lambda: remove(other["id"], 7))),
        ]
        for event_type, status, loser_code, race in races:
            before = self._state(member, experiment_id)
            with self._as(1):
                winner = self._one_winner(race(), status, loser_code)
            after = self._state(member, experiment_id)
            self.assertEqual(after["revision"], before["revision"] + 1)
            self._one_event(before, after, event_type, "member_c", revision=after["revision"])
            if event_type == "experiment.entry_updated":
                stored = next(row for row in after["entries"] if row[0] == again["id"])
                self.assertEqual(stored[3], winner["marketing_vars"])
            if event_type == "experiment.context_updated":
                self.assertEqual(after["context"], winner["draft_context"])
            if event_type == "experiment.entry_removed":
                freed = first if winner["removed"] == again["id"] else second

        before = self._state(member, experiment_id)
        with self._as(1):
            winner = self._one_winner(race_on_one_revision(lambda: add(independent, 8), lambda: add(freed, 8)), 201)
        after = self._state(member, experiment_id)
        self.assertEqual((after["revision"], sum(not row[4] for row in after["entries"])), (9, 2))
        self._one_event(before, after, "experiment.entry_added", "member_c", entry_id=winner["id"], revision=9)

        counts = Counter(event[0] for event in after["audit"].values())
        self.assertEqual(counts, Counter({"experiment.created": 1, "experiment.entry_added": 4,
                                          "experiment.entry_updated": 1, "experiment.context_updated": 1,
                                          "experiment.entry_removed": 2}))

        # Another Workspace's Member and Owner (non-members here) change nothing.
        live = next(row[0] for row in after["entries"] if not row[4])
        with self._as(1):
            for caller in (stranger, self._as_role(stranger, ROLE_OWNER, "owner_d")):
                self.current = caller
                for response in (add(second, 9), add(independent, 9),
                                 edit(live, 9, marketing_vars={"headline": SECRET}),
                                 context(9, market="GB", currency="GBP"), remove(live, 9)):
                    self.assertEqual((response.status_code, response.json()["code"]),
                                     (404, "experiment_not_found"), response.text)
        self.assertEqual(self._state(member, experiment_id), after)

    def test_the_privilege_matrix_and_live_only_unique_indexes_hold(self):
        with self.owner.connect() as connection:
            forced = connection.execute(text(
                "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE relname = 'experiment_entries'"
            )).scalar()
            self.assertTrue(forced)
            for role, expected in GRANTED.items():
                for privilege in DML:
                    held = connection.execute(text("SELECT has_table_privilege(:r, 'experiment_entries', :p)"),
                                              {"r": role, "p": privilege}).scalar()
                    self.assertEqual(held, privilege in expected, f"{role} {privilege} experiment_entries")
            indexes = dict(connection.execute(text(
                "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'experiment_entries' "
                "AND indexname IN ('uq_experiment_entry_position', 'uq_experiment_entry_asset_version')"
            )).all())
            self.assertEqual(set(indexes), {"uq_experiment_entry_position", "uq_experiment_entry_asset_version"})
            for name, definition in indexes.items():
                self.assertIn("UNIQUE", definition, name)
                self.assertIn("WHERE (removed_at IS NULL)", definition, name)
            constraints = connection.execute(text(
                "SELECT count(*) FROM pg_constraint WHERE conname IN "
                "('uq_experiment_entry_position', 'uq_experiment_entry_asset_version')")).scalar()
            self.assertEqual(constraints, 0)


if __name__ == "__main__":
    unittest.main()
