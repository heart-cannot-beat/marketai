"""GET /api/v1/operations/{operation_id} -- ETag polling and Retry-After
(spec §5.6, Task 13).

Flat in ``tests/`` (not ``tests/api/``) -- there is no ``tests/api/``
package in this repo; every test module lives flat here, matching the note
on ``tests/test_operations.py`` about a nested package shadowing stdlib
modules for whatever imports after it.

This drives the real FastAPI app through ``TestClient`` and the real
``tenant_session``/``platform_session`` units of work -- not a throwaway
engine like ``tests/test_operations.py`` -- because the behavior under
test (the ETag computed from the handler's own response body,
``Retry-After`` keyed off ``OperationState``, and 404 scoped to the
caller's Workspace) lives in the ``app/main.py`` HTTP handler, not in
``app.platform.operations`` alone.

``app.adapters.persistence.database.engine`` is a process-wide singleton
bound once, at whichever module first imports ``app.config``, to that
process's ``MARKETAI_DATABASE_URL`` -- setting the environment variable
here would be too late if any earlier test module already imported it
(inside the full ``python -m unittest discover`` run, ``tests/test_foundation.py``
sorts before this module and does exactly that, then deletes its own
scratch directory in ``tearDownClass``). Rather than race that ordering,
``setUpClass`` below recreates whatever directory the already-bound engine
points at and (re)applies every migration to it -- safe whether that
database is missing (recreated from empty), already current (no-op), or
this module happens to run standalone (first to bind it).
"""
from __future__ import annotations

import unittest
from pathlib import Path

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.adapters.persistence.database import (  # noqa: E402
    engine,
    platform_session,
    tenant_session,
)
from app.adapters.persistence.models import (  # noqa: E402
    OperationRecord,
    WorkspaceRecord,
    new_id,
    utcnow,
)
from app.main import app  # noqa: E402
from app.platform.http_identity import request_tenant_context  # noqa: E402
from app.platform.operations import (  # noqa: E402
    OperationResultRef,
    OperationState,
    create_operation,
    transition_operation,
)
from app.platform.tenancy import TenantContext  # noqa: E402

WORKSPACE_ID = "ws_operation_polling"
OTHER_WORKSPACE_ID = "ws_operation_polling_other"

OWNER = TenantContext(workspace_id=WORKSPACE_ID, role="owner", initiator_id="owner_poll")
OTHER_OWNER = TenantContext(
    workspace_id=OTHER_WORKSPACE_ID, role="owner", initiator_id="owner_poll_other"
)


def _ensure_workspace(workspace_id: str) -> None:
    with platform_session() as session:
        if session.get(WorkspaceRecord, workspace_id) is None:
            session.add(
                WorkspaceRecord(id=workspace_id, name=workspace_id, created_at=utcnow())
            )


class OperationPollingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        db_file = engine.url.database
        if db_file and db_file != ":memory:":
            Path(db_file).parent.mkdir(parents=True, exist_ok=True)
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")
        _ensure_workspace(WORKSPACE_ID)
        _ensure_workspace(OTHER_WORKSPACE_ID)

    def setUp(self) -> None:
        app.dependency_overrides[request_tenant_context] = lambda: OWNER
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def _create_operation(self, command_type: str = "test_command") -> str:
        with tenant_session(OWNER) as session:
            operation = create_operation(session, command_type=command_type)
            return operation.id

    def _create_admitted_operation(self) -> str:
        """Mirror the durable state written by PaidOperationAdmission."""
        with tenant_session(OWNER) as session:
            operation = OperationRecord(
                id=new_id("op"),
                workspace_id=WORKSPACE_ID,
                initiator_id=OWNER.initiator_id,
                command_type="test_admitted_command",
                state="admitted",
                semantic_key_hash=new_id("semantic"),
                request_fingerprint_hash=new_id("request"),
                admission_contract_version=1,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            session.add(operation)
            session.flush()
            return operation.id

    def _transition(self, operation_id: str, to_state: OperationState, **kwargs) -> None:
        with tenant_session(OWNER) as session:
            transition_operation(
                session,
                operation_id,
                to_state,
                reason=kwargs.pop("reason", None),
                actor=kwargs.pop("actor", "system:worker"),
                **kwargs,
            )

    def test_returns_200_with_etag_and_body_shape(self):
        operation_id = self._create_operation()

        response = self.client.get(f"/api/v1/operations/{operation_id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn("etag", response.headers)
        body = response.json()
        self.assertEqual(body["operation_id"], operation_id)
        self.assertEqual(body["command_type"], "test_command")
        self.assertEqual(body["state"], "queued")
        self.assertIsNone(body["result"])
        self.assertIn("created_at", body)
        self.assertIn("updated_at", body)

    def test_non_terminal_state_sets_retry_after(self):
        operation_id = self._create_operation()

        response = self.client.get(f"/api/v1/operations/{operation_id}")

        self.assertEqual(response.headers.get("retry-after"), "5")

    def test_admitted_operation_is_a_pollable_non_terminal_response(self):
        operation_id = self._create_admitted_operation()

        response = self.client.get(f"/api/v1/operations/{operation_id}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "admitted")
        self.assertEqual(response.headers.get("retry-after"), "5")

    def test_terminal_state_has_no_retry_after(self):
        operation_id = self._create_operation()
        self._transition(operation_id, OperationState.RUNNING)
        self._transition(operation_id, OperationState.SUCCEEDED)

        response = self.client.get(f"/api/v1/operations/{operation_id}")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("retry-after", response.headers)
        self.assertEqual(response.json()["state"], "succeeded")

    def test_uncertain_state_still_sets_retry_after(self):
        """`uncertain` is non-terminal (spec §5.2) -- only the reconciler
        may leave it, so a poller must keep retrying."""
        operation_id = self._create_operation()
        self._transition(operation_id, OperationState.RUNNING)
        self._transition(operation_id, OperationState.UNCERTAIN)

        response = self.client.get(f"/api/v1/operations/{operation_id}")

        self.assertEqual(response.headers.get("retry-after"), "5")
        self.assertEqual(response.json()["state"], "uncertain")

    def test_if_none_match_with_current_etag_returns_304(self):
        operation_id = self._create_operation()
        first = self.client.get(f"/api/v1/operations/{operation_id}")
        etag = first.headers["etag"]

        second = self.client.get(
            f"/api/v1/operations/{operation_id}", headers={"If-None-Match": etag}
        )

        self.assertEqual(second.status_code, 304)
        self.assertEqual(second.headers["etag"], etag)
        self.assertEqual(second.content, b"")

    def test_if_none_match_with_stale_etag_returns_fresh_200(self):
        operation_id = self._create_operation()
        first = self.client.get(f"/api/v1/operations/{operation_id}")
        stale_etag = first.headers["etag"]

        self._transition(operation_id, OperationState.RUNNING)

        second = self.client.get(
            f"/api/v1/operations/{operation_id}",
            headers={"If-None-Match": stale_etag},
        )

        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(second.headers["etag"], stale_etag)
        self.assertEqual(second.json()["state"], "running")

    def test_result_ref_is_surfaced_once_the_operation_has_one(self):
        operation_id = self._create_operation()
        self._transition(operation_id, OperationState.RUNNING)
        self._transition(
            operation_id,
            OperationState.SUCCEEDED,
            result_ref=OperationResultRef(
                resource_type="asset_version", resource_id="av_123"
            ),
        )

        response = self.client.get(f"/api/v1/operations/{operation_id}")

        self.assertEqual(
            response.json()["result"],
            {"resource_type": "asset_version", "resource_id": "av_123"},
        )

    def test_unknown_operation_id_returns_404_problem_details(self):
        response = self.client.get("/api/v1/operations/op_does_not_exist")

        self.assertEqual(response.status_code, 404)
        self.assertTrue(
            response.headers["content-type"].startswith("application/problem+json")
        )
        body = response.json()
        self.assertEqual(body["code"], "operation_not_found")
        self.assertEqual(body["status"], 404)
        self.assertEqual(body["type"], "urn:marketai:error:operation-not-found")
        self.assertIn("trace_id", body)
        self.assertFalse(body["retryable"])

    def test_operation_from_another_workspace_is_reported_not_found(self):
        with tenant_session(OTHER_OWNER) as session:
            operation = create_operation(session, command_type="test_command")
            operation_id = operation.id

        response = self.client.get(f"/api/v1/operations/{operation_id}")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "operation_not_found")


if __name__ == "__main__":
    unittest.main()
