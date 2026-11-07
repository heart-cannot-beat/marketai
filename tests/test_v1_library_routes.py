"""HTTP-level tests for the Library v1 sub-router (Issue #27 Task 2).

Exercises the real FastAPI app via ``TestClient`` with ``request_tenant_context``
overridden to a fixed ``TenantContext`` -- the same pattern
``tests/test_generation_api.py`` uses -- so requests hit the actual route
bodies, ``tenant_session`` and the RFC 9457 problem-handler wiring registered
in ``app/main.py`` (``register_problem_handlers``), rather than calling
``app.library.service`` directly.

20 routes exist under ``/api/v1/library`` but they all follow one of two thin
adapter shapes (read, or command with If-Match + Idempotency-Key), so this
suite exercises a representative set per contracts.py entry type rather than
one test per route: entry-types, create, save-draft + If-Match, validate,
publish, archive, bind, fork, clone-product, withdraw, pagination, tenant
isolation, and the production-backed reference/template routes.
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

# Import order matters: tests.support.library_fixtures sets
# MARKETAI_DATABASE_URL/MARKETAI_DATA_DIR to an isolated per-process temp dir
# via os.environ.setdefault. app.config reads those env vars at import time
# (falling back to the real repo-local data/marketai.db otherwise), so this
# import must land before `from app.main import app` -- else running this
# file standalone (outside the full suite, where an alphabetically earlier
# test module has already set the keys) pollutes the real dev database.
from tests.support.library_fixtures import LibraryFixtures

from app.library import command_outcomes, service
from app.library.contracts import ENTRY_DATA_SOURCE, ENTRY_PRODUCT, ENTRY_TYPES, ROLE_OWNER
from app.main import app
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import system_context

#: Every entry lists these; ``bind_reference`` is added for bindable types only.
_ENTRY_ACTIONS = frozenset({"save_draft", "validate_draft", "publish", "archive"})
_BINDABLE_TYPES = frozenset({"product", "person"})
#: Contract §4 Owner-only publish types, written out rather than read from
#: ``OWNER_ONLY_PUBLISH`` so a change to that set has to change this test too.
_OWNER_ONLY_PUBLISH_TYPES = frozenset({
    "offer", "prompt_policy", "data_source", "manual_fact_set",
    "truth_resolution_policy", "brand_localization_policy",
})
_DATA_SOURCE_CONTENT = {
    "adapter_kind": "manual",
    "capabilities": ["price"],
    "market_context": ["US"],
    "config": {},
    "config_schema_version": "manual/1.0",
    "execution_mode": "real",
}


class TestLibraryV1Routes(LibraryFixtures):
    def setUp(self):
        self.ctx = self._make_context()
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def _owner_client(self):
        owner_ctx = self.owner_ctx(self.ctx)
        client = TestClient(app, raise_server_exceptions=False)
        app.dependency_overrides[request_tenant_context] = lambda: owner_ctx
        self.addCleanup(client.close)
        return client, owner_ctx

    def _create_entry(self, client=None, entry_type=ENTRY_PRODUCT, name="Widget", content=None):
        client = client or self.client
        resp = client.post(
            "/api/v1/library/entries",
            json={"entry_type": entry_type, "display_name": name, "content": content or {}},
            headers={
                "Idempotency-Key": f"ik-create-{name}-{id(client)}",
                "X-Command-Identity": f"command.create.{id(client)}.{name}",
            },
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def test_list_entry_types_returns_sorted_type_keys(self):
        resp = self.client.get("/api/v1/library/entry-types")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("entry_types", body)
        self.assertIsInstance(body["entry_types"], list)
        self.assertIn(ENTRY_PRODUCT, body["entry_types"])
        self.assertEqual(body["entry_types"], sorted(body["entry_types"]))

    def test_list_entries_paginates_with_page_envelope(self):
        for i in range(3):
            self._create_entry(name=f"Product{i}")
        resp = self.client.get("/api/v1/library/entries?limit=2")
        self.assertEqual(resp.status_code, 200, resp.text)
        page = resp.json()
        self.assertEqual(len(page["items"]), 2)
        self.assertIsNotNone(page["next_cursor"])

        next_resp = self.client.get(f"/api/v1/library/entries?limit=2&cursor={page['next_cursor']}")
        self.assertEqual(next_resp.status_code, 200)
        next_page = next_resp.json()
        self.assertEqual(len(next_page["items"]), 1)
        self.assertIsNone(next_page["next_cursor"])

    def test_get_entry_returns_revision_etag_and_allowed_actions(self):
        entry = self._create_entry()
        resp = self.client.get(f"/api/v1/library/entries/{entry['id']}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["ETag"], '"1"')
        body = resp.json()
        self.assertEqual(body["revision"], 1)
        codes = {a["action"] for a in body["allowed_actions"]}
        # A Product is bindable, so it lists Bind Reference too.
        self.assertEqual(codes, _ENTRY_ACTIONS | {"bind_reference"})
        # Member role cannot archive.
        archive_action = next(a for a in body["allowed_actions"] if a["action"] == "archive")
        self.assertFalse(archive_action["allowed"])
        self.assertEqual(archive_action["reason_code"], "owner_required")

    def test_get_entry_unknown_id_returns_404(self):
        resp = self.client.get("/api/v1/library/entries/does-not-exist")
        self.assertEqual(resp.status_code, 404)

    def test_cross_tenant_entry_is_not_found(self):
        entry = self._create_entry()
        other_ctx = self._make_context()
        app.dependency_overrides[request_tenant_context] = lambda: other_ctx
        resp = self.client.get(f"/api/v1/library/entries/{entry['id']}")
        self.assertEqual(resp.status_code, 404)

    # ------------------------------------------------------------------
    # Entry eligibility applies the commands' own rules (#24)
    # ------------------------------------------------------------------

    def _as(self, context):
        app.dependency_overrides[request_tenant_context] = lambda: context
        return self.client

    def _entry_actions(self, context, entry_id: str) -> dict[str, dict]:
        resp = self._as(context).get(f"/api/v1/library/entries/{entry_id}")
        self.assertEqual(resp.status_code, 200, resp.text)
        return {a["action"]: a for a in resp.json()["allowed_actions"]}

    def _publish_entry(self, context, entry_id: str, key: str):
        return self._as(context).post(
            f"/api/v1/library/entries/{entry_id}/publish",
            json={},
            headers={"Idempotency-Key": key, "If-Match": "1"},
        )

    def _draft_of_every_type(self) -> dict[str, str]:
        return {
            entry_type: service.create_entry(
                self.ctx, entry_type=entry_type, display_name=f"Draft {entry_type}", content={},
            )["id"]
            for entry_type in sorted(ENTRY_TYPES)
        }

    def test_only_products_and_people_list_bind_reference(self):
        for entry_type, entry_id in self._draft_of_every_type().items():
            with self.subTest(entry_type=entry_type):
                actions = self._entry_actions(self.ctx, entry_id)
                if entry_type in _BINDABLE_TYPES:
                    self.assertEqual(set(actions), _ENTRY_ACTIONS | {"bind_reference"})
                    bind = actions["bind_reference"]
                    self.assertEqual((bind["allowed"], bind["reason_code"]), (True, None))
                else:
                    self.assertEqual(set(actions), _ENTRY_ACTIONS)

    def test_publish_eligibility_refuses_a_member_only_the_owner_only_types(self):
        entries = self._draft_of_every_type()
        owner = self.owner_ctx(self.ctx)
        worker = system_context(self.ctx.workspace_id, initiator_id="owner_a", initiator_role=ROLE_OWNER)
        refused: dict[str, set[tuple[str, str]]] = {}
        for context in (self.ctx, owner, worker):
            for entry_type, entry_id in entries.items():
                publish = self._entry_actions(context, entry_id)["publish"]
                self.assertEqual(publish["requirements"], ["impact_confirmation"])
                if not publish["allowed"]:
                    refused.setdefault(context.role, set()).add((entry_type, publish["reason_code"]))
        # A Member keeps Publish on Product, Person, Dimension Template and the
        # rest; the Owner and a system context are refused nothing.
        self.assertEqual(
            refused,
            {"member": {(entry_type, "owner_required") for entry_type in _OWNER_ONLY_PUBLISH_TYPES}},
        )

    def test_member_refused_publish_is_still_refused_when_sent(self):
        entry = service.create_entry(
            self.ctx, entry_type=ENTRY_DATA_SOURCE, display_name="Member source", content=_DATA_SOURCE_CONTENT,
        )
        publish = self._entry_actions(self.ctx, entry["id"])["publish"]
        self.assertEqual((publish["allowed"], publish["reason_code"]), (False, "owner_required"))

        resp = self._publish_entry(self.ctx, entry["id"], "ik-24-member-publish")

        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertEqual(resp.json()["code"], "library_forbidden")
        self.assertIsNone(service.get_entry(self.ctx, entry["id"])["current_version_id"])

    def test_owner_and_system_still_publish_an_owner_only_type(self):
        owner = self.owner_ctx(self.ctx)
        entry = service.create_entry(
            self.ctx, entry_type=ENTRY_DATA_SOURCE, display_name="Owner source", content=_DATA_SOURCE_CONTENT,
        )
        publish = self._entry_actions(owner, entry["id"])["publish"]
        self.assertEqual((publish["allowed"], publish["reason_code"]), (True, None))

        resp = self._publish_entry(owner, entry["id"], "ik-24-owner-publish")

        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(resp.json()["state"], "effective")

        worker = system_context(self.ctx.workspace_id, initiator_id="owner_a", initiator_role=ROLE_OWNER)
        handed_off = service.create_entry(
            self.ctx, entry_type=ENTRY_DATA_SOURCE, display_name="Worker source", content=_DATA_SOURCE_CONTENT,
        )
        version = service.publish(worker, handed_off["id"], handed_off["draft"]["revision"])
        self.assertEqual(version["state"], "effective")

    def test_another_workspace_owner_gets_404_for_an_owner_only_entry(self):
        entry = service.create_entry(
            self.ctx, entry_type=ENTRY_DATA_SOURCE, display_name="Hidden source", content=_DATA_SOURCE_CONTENT,
        )
        outsider = self._make_context(role=ROLE_OWNER, actor="owner_elsewhere")

        read = self._as(outsider).get(f"/api/v1/library/entries/{entry['id']}")
        publish = self._publish_entry(outsider, entry["id"], "ik-24-outsider-publish")

        self.assertEqual(read.status_code, 404, read.text)
        self.assertNotIn("allowed_actions", read.json())
        self.assertEqual(publish.status_code, 404, publish.text)
        self.assertIsNone(service.get_entry(self.ctx, entry["id"])["current_version_id"])

    # ------------------------------------------------------------------
    # Commands: create / save-draft / validate / publish
    # ------------------------------------------------------------------

    def test_create_entry_requires_idempotency_key(self):
        resp = self.client.post(
            "/api/v1/library/entries",
            json={"entry_type": ENTRY_PRODUCT, "display_name": "Test"},
        )
        self.assertEqual(resp.status_code, 422)

    def test_create_entry_returns_draft_and_revision_one(self):
        entry = self._create_entry(name="Alpha")
        self.assertEqual(entry["revision"], 1)
        self.assertIsNotNone(entry["draft"])
        self.assertEqual(entry["draft"]["revision"], 1)
        self.assertEqual(entry["lifecycle"], "active")

    def test_command_outcome_lookup_recovers_the_exact_created_entry(self):
        identity = "route.command.create.recover.001"
        resp = self.client.post(
            "/api/v1/library/entries",
            json={"entry_type": ENTRY_PRODUCT, "display_name": "Recovered", "content": {}},
            headers={
                "Idempotency-Key": "ik-route-recover-create",
                "X-Command-Identity": identity,
            },
        )
        self.assertEqual(resp.status_code, 201, resp.text)

        outcome = self.client.get(
            f"/api/v1/library/command-outcomes/{identity}?command_kind=create_entry"
        )
        self.assertEqual(outcome.status_code, 200, outcome.text)
        body = outcome.json()
        self.assertEqual(body["outcome"], "succeeded")
        self.assertEqual(body["result"]["id"], resp.json()["id"])
        self.assertEqual(body["command_identity"], identity)

    def test_missing_command_outcome_requires_reconciliation(self):
        resp = self.client.get(
            "/api/v1/library/command-outcomes/route.command.missing.001?command_kind=fork"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["outcome"], "reconciliation_required")

    def test_reconcile_proves_a_reserved_command_was_not_applied(self):
        identity = "route.command.pending.001"
        fingerprint = service.compute_fingerprint({"route": "pending"})
        service._ensure_idempotency_reservation(
            self.ctx,
            "ik-route-pending",
            "fork",
            fingerprint,
            command_outcomes.hash_command_identity(identity),
        )
        resp = self.client.post(
            f"/api/v1/library/command-outcomes/{identity}/reconcile?command_kind=fork"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["outcome"], "not_applied")

    def test_command_outcome_rejects_malformed_identity(self):
        resp = self.client.get("/api/v1/library/command-outcomes/short")
        self.assertEqual(resp.status_code, 422)

    def test_command_outcome_wrong_actor_is_not_found(self):
        identity = "route.command.actor.bound.001"
        resp = self.client.post(
            "/api/v1/library/entries",
            json={"entry_type": ENTRY_PRODUCT, "display_name": "Actor-bound", "content": {}},
            headers={
                "Idempotency-Key": "ik-route-actor-bound",
                "X-Command-Identity": identity,
            },
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        attacker = self._make_context(
            workspace_id=self.ctx.workspace_id,
            actor="different-actor",
        )
        app.dependency_overrides[request_tenant_context] = lambda: attacker
        outcome = self.client.get(f"/api/v1/library/command-outcomes/{identity}")
        self.assertEqual(outcome.status_code, 404)

    def test_save_draft_requires_if_match(self):
        entry = self._create_entry()
        resp = self.client.put(
            f"/api/v1/library/entries/{entry['id']}/draft",
            json={"content": {"name": "Updated"}},
            headers={"Idempotency-Key": "ik-save-1"},
        )
        self.assertEqual(resp.status_code, 428)

    def test_save_draft_rejects_malformed_if_match(self):
        entry = self._create_entry()
        resp = self.client.put(
            f"/api/v1/library/entries/{entry['id']}/draft",
            json={"content": {"name": "Updated"}},
            headers={"Idempotency-Key": "ik-save-2", "If-Match": "not-an-int"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_save_draft_with_stale_if_match_returns_409(self):
        entry = self._create_entry()
        resp = self.client.put(
            f"/api/v1/library/entries/{entry['id']}/draft",
            json={"content": {"name": "Updated"}},
            headers={"Idempotency-Key": "ik-save-3", "If-Match": "99"},
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "revision_conflict")

    def test_save_draft_advances_revision(self):
        entry = self._create_entry()
        resp = self.client.put(
            f"/api/v1/library/entries/{entry['id']}/draft",
            json={"content": {"name": "Updated"}},
            headers={"Idempotency-Key": "ik-save-4", "If-Match": "1"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["revision"], 2)
        self.assertEqual(body["draft"]["content"]["name"], "Updated")

    def test_validate_draft_reports_blocking_findings_for_empty_product(self):
        entry = self._create_entry(content={})
        resp = self.client.post(
            f"/api/v1/library/entries/{entry['id']}/validate",
            headers={"Idempotency-Key": "ik-validate-1", "If-Match": "1"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertGreater(len(body["blocking"]), 0)
        self.assertIn("entry_type", body)

    def test_validate_draft_without_if_match_returns_428(self):
        entry = self._create_entry()
        resp = self.client.post(
            f"/api/v1/library/entries/{entry['id']}/validate",
            headers={"Idempotency-Key": "ik-validate-2"},
        )
        self.assertEqual(resp.status_code, 428)

    # ------------------------------------------------------------------
    # Full lifecycle: create -> save draft -> validate -> publish -> withdraw
    # ------------------------------------------------------------------

    def _publish_reference(self, client, ctx):
        content = {
            "object_id": "obj_test",
            "source": "upload",
            "file": {"content_sha256": "a" * 64, "mime_type": "image/png", "byte_size": 100,
                      "width": 80, "height": 60},
            "rights": {"state": "granted", "scope": "global_paid_social",
                       "declared_by": ctx.initiator_id, "valid_to": "2099-01-01"},
        }
        from app.library import service
        facts = service.upload_reference_object(ctx, raw=self.png_bytes(), declared_mime="image/png", filename="r.png")
        content["object_id"] = facts["object_id"]
        content["file"] = {k: facts[k] for k in ("content_sha256", "mime_type", "byte_size", "width", "height")}
        content["gate_checks"] = facts.get("checks", [])
        content["derivative"] = facts.get("derivative", {})
        entry, version = self._publish(ctx, "reference_asset", "Ref", content)
        return entry, version

    def png_bytes(self):
        from tests.support.library_fixtures import png_bytes
        return png_bytes()

    def test_publish_returns_version_detail_with_allowed_actions(self):
        owner_client, owner_ctx = self._owner_client()
        _ref_entry, ref_version = self._publish_reference(owner_client, owner_ctx)
        _group_entry, group_version = self._group_version(owner_ctx, ref_version["id"])
        content = self._product_content(ref_version["id"], group_version["id"])
        entry = self._create_entry(client=owner_client, content=content)
        validate_resp = owner_client.post(
            f"/api/v1/library/entries/{entry['id']}/validate",
            headers={"Idempotency-Key": "ik-pub-validate", "If-Match": "1"},
        )
        self.assertEqual(validate_resp.status_code, 200, validate_resp.text)
        self.assertTrue(validate_resp.json()["ok"], validate_resp.json())

        publish_resp = owner_client.post(
            f"/api/v1/library/entries/{entry['id']}/publish",
            json={},
            headers={"Idempotency-Key": "ik-publish-1", "If-Match": "1"},
        )
        self.assertEqual(publish_resp.status_code, 201, publish_resp.text)
        version = publish_resp.json()
        self.assertEqual(version["state"], "effective")
        self.assertEqual(version["version_no"], 1)
        codes = {a["action"] for a in version["allowed_actions"]}
        self.assertEqual(codes, {"withdraw", "fork", "clone_product"})
        withdraw_action = next(a for a in version["allowed_actions"] if a["action"] == "withdraw")
        self.assertTrue(withdraw_action["allowed"])

        # get_version round-trips with an ETag from version_no.
        get_resp = owner_client.get(f"/api/v1/library/versions/{version['id']}")
        self.assertEqual(get_resp.status_code, 200)
        self.assertEqual(get_resp.headers["ETag"], '"1"')

        # withdraw takes no If-Match -- versions have no revision counter --
        # and returns the same impact-analysis shape as GET .../impact.
        withdraw_resp = owner_client.post(
            f"/api/v1/library/versions/{version['id']}/withdraw",
            json={"reason": "recall for legal review"},
            headers={"Idempotency-Key": "ik-withdraw-1"},
        )
        self.assertEqual(withdraw_resp.status_code, 200, withdraw_resp.text)
        impact = withdraw_resp.json()
        self.assertEqual(impact["state"], "withdrawn")
        self.assertEqual(impact["version_id"], version["id"])
        self.assertEqual(impact["reason"], "recall for legal review")

        # The version itself now reflects withdrawn/blocked with no eligible actions.
        post_withdraw = owner_client.get(f"/api/v1/library/versions/{version['id']}")
        self.assertEqual(post_withdraw.status_code, 200)
        post_body = post_withdraw.json()
        self.assertEqual(post_body["state"], "withdrawn")
        self.assertEqual(post_body["eligibility"], "blocked")
        self.assertFalse(any(a["allowed"] for a in post_body["allowed_actions"]))

    def test_archive_requires_owner_role(self):
        entry = self._create_entry()
        resp = self.client.post(
            f"/api/v1/library/entries/{entry['id']}/archive",
            headers={"Idempotency-Key": "ik-archive-1", "If-Match": "1"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_archive_as_owner_succeeds(self):
        owner_client, _owner_ctx = self._owner_client()
        entry = self._create_entry(client=owner_client)
        resp = owner_client.post(
            f"/api/v1/library/entries/{entry['id']}/archive",
            headers={"Idempotency-Key": "ik-archive-2", "If-Match": "1"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["lifecycle"], "archived")

    # ------------------------------------------------------------------
    # Production-backed routes (formerly 501 stubs)
    # ------------------------------------------------------------------

    def test_apply_template_returns_404_for_unknown_version(self):
        resp = self.client.post(
            "/api/v1/library/versions/some-version/apply",
            json={"product_entry_id": "lib_x", "display_name": "Applied"},
            headers={"Idempotency-Key": "ik-apply-1"},
        )
        self.assertEqual(resp.status_code, 404)

    def test_create_reference_object_returns_404_for_unknown_intent(self):
        resp = self.client.post(
            "/api/v1/library/reference-objects",
            json={"upload_intent_id": "ui_x"},
            headers={"Idempotency-Key": "ik-ref-1"},
        )
        self.assertEqual(resp.status_code, 404)

    def test_create_reference_download_intent_returns_404_for_unknown_version(self):
        resp = self.client.post(
            "/api/v1/library/versions/some-version/download-intent",
            headers={"Idempotency-Key": "ik-dl-1"},
        )
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
