"""Draft Experiment commands on the SQLite path (#340).

The four schema-2 draft commands (add, edit and remove an entry; edit the
draft context) go through the interface here:

* a second live entry with the same AssetVersion is 409
  ``experiment_asset_duplicate`` and writes nothing, where it used to be an
  ``IntegrityError`` and a 500; a removed entry's version is free again;
* two commands that read the same revision cannot both commit: the second
  finds the revision taken and answers 409, its change lost to nobody;
* every accepted command appends exactly one audit event, carrying ids, the
  revision and changed field names only; a refused one appends none;
* another Workspace's Member and Owner (non-members here) get 404 and change
  nothing.

``tests/test_experiment_entries_pg.py`` runs the same cases as the real API
role on PostgreSQL.
"""
from __future__ import annotations

import json
import unittest
import uuid
from collections import Counter
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    AuditEventRecord,
    ExperimentEntryRecord,
    ExperimentRecord,
    ExperimentStateEventRecord,
    WorkspaceRecord,
)
from app.library.contracts import ROLE_MEMBER, ROLE_OWNER
from app.main import app
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext
from tests.support.draft_race import race_on_one_revision
from tests.test_experiment_lifecycle import CTX, PROD_ID, _draft_context, _seed_asset_versions

#: Everything a draft command's audit event may carry: ids, the revision, field names.
AUDIT_KEYS = {"entry_id", "asset_version_id", "revision", "changed_fields"}
SECRET = "content that must never reach the audit trail"


def _ctx(workspace_id: str, role: str, actor: str) -> TenantContext:
    return TenantContext(workspace_id=workspace_id, role=role, initiator_id=actor,
                         request_id=f"req-{actor}", trace_id=f"trace-{actor}")


MEMBER = _ctx(CTX.workspace_id, ROLE_MEMBER, "member_340")
STRANGERS = (_ctx("ws_dc340_other", ROLE_MEMBER, "member_340_other"),
             _ctx("ws_dc340_other", ROLE_OWNER, "owner_340_other"))


class DraftCommandFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")
        with tenant_session(STRANGERS[0]) as session:
            if session.get(WorkspaceRecord, STRANGERS[0].workspace_id) is None:
                session.add(WorkspaceRecord(id=STRANGERS[0].workspace_id, name="Another Workspace"))

    def setUp(self) -> None:
        self.current: TenantContext = MEMBER
        app.dependency_overrides[request_tenant_context] = lambda: self.current
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    # --- interface ----------------------------------------------------------
    def _send(self, method: str, path: str, body: dict):
        return self.client.request(method, path, json=body,
                                   headers={"Idempotency-Key": f"ik-{uuid.uuid4().hex}"})

    def _call(self, method: str, path: str, expect: int, body: dict | None = None) -> dict:
        response = self._send(method, path, body or {})
        self.assertEqual(response.status_code, expect, f"{method} {path}: {response.text}")
        return response.json()

    def _add(self, experiment_id: str, asset_version_id: str, revision: int, *, marketing_vars=None):
        return self._send("POST", f"/api/v1/experiments/{experiment_id}/entries",
                          {"asset_version_id": asset_version_id, "expected_revision": revision,
                           "marketing_vars": marketing_vars or {}})

    def _edit(self, experiment_id: str, entry_id: str, revision: int, **fields):
        return self._send("PUT", f"/api/v1/experiments/{experiment_id}/entries/{entry_id}",
                          {**fields, "expected_revision": revision})

    def _remove(self, experiment_id: str, entry_id: str, revision: int):
        # Removal stays on the legacy interface (#284); the web removes nothing.
        return self._send("DELETE", f"/api/experiments/{experiment_id}/entries/{entry_id}",
                          {"expected_revision": revision})

    def _context(self, experiment_id: str, revision: int, **overrides):
        return self._send("PUT", f"/api/v1/experiments/{experiment_id}/context",
                          {"draft_context": _draft_context(**overrides), "expected_revision": revision})

    # --- state --------------------------------------------------------------
    def _assets(self, tag: str, count: int) -> list[str]:
        with tenant_session(CTX) as session:
            return _seed_asset_versions(session, f"{tag}_{uuid.uuid4().hex[:6]}", count)

    def _draft(self, *asset_version_ids: str) -> tuple[str, list[dict]]:
        created = self._call("POST", "/api/v1/experiments", 201, {
            "product_id": PROD_ID, "name": "Draft commands", "draft_context": _draft_context()})
        entries = []
        for revision, asset_version_id in enumerate(asset_version_ids, start=1):
            response = self._add(created["id"], asset_version_id, revision)
            self.assertEqual(response.status_code, 201, response.text)
            entries.append(response.json())
        return created["id"], entries

    def _state(self, experiment_id: str) -> dict:
        """Every entry row, the revision, the context and the audit trail."""
        with tenant_session(MEMBER) as session:
            experiment = session.get(ExperimentRecord, experiment_id)
            rows = session.scalars(select(ExperimentEntryRecord).where(
                ExperimentEntryRecord.experiment_id == experiment_id).order_by(ExperimentEntryRecord.id)).all()
            audit = session.scalars(select(AuditEventRecord).where(
                AuditEventRecord.resource_type == "experiment",
                AuditEventRecord.resource_id == experiment_id)).all()
            state_events = session.scalar(select(func.count()).select_from(ExperimentStateEventRecord).where(
                ExperimentStateEventRecord.experiment_id == experiment_id))
            return {
                "revision": experiment.revision,
                "context": experiment.config_json,
                "entries": [(row.id, row.asset_version_id, row.position, row.marketing_vars_json,
                             row.removed_at is not None) for row in rows],
                "audit": {event.id: (event.event_type, event.details_json, event.initiator_id)
                          for event in audit},
                "state_events": state_events,
            }

    def _new_events(self, before: dict, after: dict) -> list[tuple[str, dict, str]]:
        return [event for event_id, event in after["audit"].items() if event_id not in before["audit"]]

    def _assert_one_event(self, before: dict, after: dict, event_type: str, **details) -> dict:
        events = self._new_events(before, after)
        self.assertEqual([event[0] for event in events], [event_type], events)
        recorded, actor = events[0][1], events[0][2]
        self.assertLessEqual(set(recorded), AUDIT_KEYS, recorded)
        self.assertNotIn(SECRET, json.dumps(recorded))
        self.assertEqual({key: recorded[key] for key in details}, details)
        self.assertEqual(actor, self.current.initiator_id)
        # A draft edit is no state transition.
        self.assertEqual(after["state_events"], before["state_events"])
        return recorded


class DuplicateAssetTests(DraftCommandFixtures):
    def test_a_second_live_entry_with_the_same_asset_version_is_409_and_writes_nothing(self):
        first, second, spare = self._assets("dup", 3)
        experiment_id, (kept, other) = self._draft(first, second)
        before = self._state(experiment_id)

        added = self._add(experiment_id, first, 3, marketing_vars={"headline": SECRET})
        self.assertEqual((added.status_code, added.json()["code"]), (409, "experiment_asset_duplicate"),
                         added.text)
        edited = self._edit(experiment_id, other["id"], 3, asset_version_id=first)
        self.assertEqual((edited.status_code, edited.json()["code"]), (409, "experiment_asset_duplicate"),
                         edited.text)
        # The legacy interface keeps its single conflict code, and is no 500 either.
        legacy = self._send("PUT", f"/api/experiments/{experiment_id}/entries/{other['id']}",
                            {"asset_version_id": first, "expected_revision": 3})
        self.assertEqual((legacy.status_code, legacy.json()["code"]), (409, "experiment_conflict"), legacy.text)
        self.assertEqual(self._state(experiment_id), before)

        # An entry keeping its own version is no duplicate of itself.
        same = self._edit(experiment_id, kept["id"], 3, asset_version_id=first,
                          marketing_vars={"headline": SECRET})
        self.assertEqual((same.status_code, same.json()["revision"]), (200, 4), same.text)
        after = self._state(experiment_id)
        self._assert_one_event(before, after, "experiment.entry_updated", entry_id=kept["id"],
                               asset_version_id=first, revision=4, changed_fields=["marketing_vars"])
        swapped = self._edit(experiment_id, kept["id"], 4, asset_version_id=spare)
        self.assertEqual((swapped.status_code, swapped.json()["asset_version_id"]), (200, spare), swapped.text)

    def test_a_removed_entrys_asset_version_can_be_taken_again(self):
        first, second = self._assets("readd", 2)
        experiment_id, (gone, kept) = self._draft(first, second)
        self.assertEqual(self._remove(experiment_id, gone["id"], 3).json(), {"removed": gone["id"], "revision": 4})

        again = self._add(experiment_id, first, 4)
        self.assertEqual((again.status_code, again.json()["revision"]), (201, 5), again.text)
        # Now live again, the version is taken; once removed, an edit may take it.
        self.assertEqual(self._edit(experiment_id, kept["id"], 5, asset_version_id=first).status_code, 409)
        self.assertEqual(self._remove(experiment_id, again.json()["id"], 5).status_code, 200)
        moved = self._edit(experiment_id, kept["id"], 6, asset_version_id=first)
        self.assertEqual((moved.status_code, moved.json()["asset_version_id"]), (200, first), moved.text)
        live = [row for row in self._state(experiment_id)["entries"] if not row[4]]
        self.assertEqual([(row[0], row[1]) for row in live], [(kept["id"], first)])


class RevisionRaceTests(DraftCommandFixtures):
    """Two real connections read one revision before either writes.

    On SQLite the loser waits for the database write lock (``busy_timeout``)
    and then finds the revision taken. ``tests/test_experiment_entries_pg.py``
    reproduces the same race on PostgreSQL, where the loser waits on the
    Experiment row instead.
    """

    def _assert_one_winner(self, results, winner_status: int, loser_code: str = "revision_conflict"):
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        statuses = sorted(response.status_code for _, response in results)
        self.assertEqual(statuses, sorted([winner_status, 409]), [r.text for _, r in results])
        loser = next(response for _, response in results if response.status_code == 409)
        self.assertEqual(loser.json()["code"], loser_code, loser.text)
        return next(response for _, response in results if response.status_code == winner_status)

    def test_two_adds_on_one_revision(self):
        first, second, third = self._assets("race_add", 3)
        experiment_id, _ = self._draft(first)
        before = self._state(experiment_id)
        winner = self._assert_one_winner(race_on_one_revision(
            lambda: self._add(experiment_id, second, 2), lambda: self._add(experiment_id, third, 2)), 201)
        after = self._state(experiment_id)
        self.assertEqual(after["revision"], 3)
        self.assertEqual(len(after["entries"]), 2)
        self._assert_one_event(before, after, "experiment.entry_added", entry_id=winner.json()["id"], revision=3)

    def test_two_edits_on_one_revision(self):
        first, second = self._assets("race_edit", 2)
        experiment_id, (entry, _) = self._draft(first, second)
        before = self._state(experiment_id)
        winner = self._assert_one_winner(race_on_one_revision(
            lambda: self._edit(experiment_id, entry["id"], 3, marketing_vars={"headline": "a"}),
            lambda: self._edit(experiment_id, entry["id"], 3, marketing_vars={"headline": "b"})), 200)
        after = self._state(experiment_id)
        self.assertEqual(after["revision"], 4)
        stored = next(row for row in after["entries"] if row[0] == entry["id"])
        self.assertEqual(stored[3], winner.json()["marketing_vars"])
        self._assert_one_event(before, after, "experiment.entry_updated", revision=4,
                               changed_fields=["marketing_vars"])

    def test_two_removals_on_one_revision(self):
        first, second = self._assets("race_remove", 2)
        experiment_id, entries = self._draft(first, second)
        before = self._state(experiment_id)
        # The legacy interface answers every conflict as ``experiment_conflict``.
        winner = self._assert_one_winner(race_on_one_revision(
            *(lambda entry=entry: self._remove(experiment_id, entry["id"], 3) for entry in entries)),
            200, loser_code="experiment_conflict")
        after = self._state(experiment_id)
        self.assertEqual(after["revision"], 4)
        self.assertEqual([row[0] for row in after["entries"] if row[4]], [winner.json()["removed"]])
        self._assert_one_event(before, after, "experiment.entry_removed", entry_id=winner.json()["removed"],
                               revision=4)

    def test_two_context_edits_on_one_revision(self):
        experiment_id, _ = self._draft()
        before = self._state(experiment_id)
        winner = self._assert_one_winner(race_on_one_revision(
            lambda: self._context(experiment_id, 1, market="GB", currency="GBP"),
            lambda: self._context(experiment_id, 1, placement="story")), 200)
        after = self._state(experiment_id)
        self.assertEqual((after["revision"], after["context"]), (2, winner.json()["draft_context"]))
        self._assert_one_event(before, after, "experiment.context_updated", revision=2)


class AuditTrailTests(DraftCommandFixtures):
    def test_each_accepted_command_appends_exactly_one_event_and_a_refused_one_none(self):
        first, second = self._assets("audit", 2)
        experiment_id, _ = self._draft()

        steps = [
            (lambda: self._add(experiment_id, first, 1, marketing_vars={"headline": SECRET}), 201,
             "experiment.entry_added"),
            (lambda: self._add(experiment_id, second, 2), 201, "experiment.entry_added"),
            (lambda: self._add(experiment_id, second, 3), 409, None),  # duplicate
            (lambda: self._add(experiment_id, second, 2), 409, None),  # stale revision
            (lambda: self._context(experiment_id, 3, market="GB", currency="GBP"), 200,
             "experiment.context_updated"),
        ]
        recorded = []
        for send, status, event_type in steps:
            before = self._state(experiment_id)
            response = send()
            self.assertEqual(response.status_code, status, response.text)
            after = self._state(experiment_id)
            if event_type is None:
                self.assertEqual(after, before)
            else:
                recorded.append(self._assert_one_event(before, after, event_type,
                                                       revision=response.json()["revision"]))
        self.assertEqual(recorded[0]["asset_version_id"], first)
        self.assertEqual(recorded[2]["changed_fields"], ["currency", "market"])

        entry_id = next(row[0] for row in self._state(experiment_id)["entries"] if row[1] == first)
        before = self._state(experiment_id)
        self.assertEqual(self._edit(experiment_id, entry_id, 4, asset_version_id=first,
                                    marketing_vars={"headline": SECRET + "!"}).status_code, 200)
        after = self._state(experiment_id)
        self._assert_one_event(before, after, "experiment.entry_updated", entry_id=entry_id, revision=5,
                               changed_fields=["marketing_vars"])
        self.assertEqual(self._remove(experiment_id, entry_id, 5).status_code, 200)
        self._assert_one_event(after, self._state(experiment_id), "experiment.entry_removed",
                               entry_id=entry_id, asset_version_id=first, revision=6)

        counts = Counter(event[0] for event in self._state(experiment_id)["audit"].values())
        self.assertEqual(counts, Counter({"experiment.created": 1, "experiment.entry_added": 2,
                                          "experiment.context_updated": 1, "experiment.entry_updated": 1,
                                          "experiment.entry_removed": 1}))


class TenantBoundaryTests(DraftCommandFixtures):
    def test_another_workspaces_member_and_owner_change_nothing(self):
        first, second, spare = self._assets("tenant", 3)
        experiment_id, (entry, _) = self._draft(first, second)
        before = self._state(experiment_id)
        for stranger in STRANGERS:
            self.current = stranger
            for response in (
                self._add(experiment_id, spare, 3),
                self._add(experiment_id, first, 3),  # would be a duplicate: still no oracle
                self._edit(experiment_id, entry["id"], 3, marketing_vars={"headline": SECRET}),
                self._edit(experiment_id, entry["id"], 3, asset_version_id=second),
                self._context(experiment_id, 3, market="GB", currency="GBP"),
                self._remove(experiment_id, entry["id"], 3),
            ):
                self.assertEqual((response.status_code, response.json()["code"]), (404, "experiment_not_found"),
                                 response.text)
        self.current = MEMBER
        self.assertEqual(self._state(experiment_id), before)


if __name__ == "__main__":
    unittest.main()
