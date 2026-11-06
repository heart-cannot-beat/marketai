"""Authorization snapshot propagation adapters (spec §3.10)."""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

import httpx

from app.platform.projection_sync import (
    AuthorizationSnapshot,
    ControlPlaneAuthorizationSnapshotHttpAdapter,
    PostgresWorkspaceProjectionStore,
    ProjectionUpstreamUnavailableError,
    WorkspaceNotFoundUpstreamError,
    WorkspaceProjectionSyncer,
)

SNAPSHOT_BODY = {
    "workspace_id": "ws_1",
    "workspace_name": "Acme",
    "authorization_epoch": 2,
    "entitlement_version": 4,
    "suspended": False,
    "projection_version": 7,
}


def make_response(status_code=200, json_body=None, raise_error=None):
    response = MagicMock()
    response.status_code = status_code
    if raise_error is not None:
        response.json.side_effect = raise_error
    else:
        response.json.return_value = json_body
    return response


class ControlPlaneAuthorizationSnapshotHttpAdapterTests(unittest.IsolatedAsyncioTestCase):
    def _adapter(self, http_client) -> ControlPlaneAuthorizationSnapshotHttpAdapter:
        return ControlPlaneAuthorizationSnapshotHttpAdapter(
            base_url="https://control-plane.internal",
            service_token="svc-token",
            timeout_ms=3000,
            http_client=http_client,
        )

    async def test_fetch_returns_a_snapshot_on_200(self) -> None:
        http_client = AsyncMock()
        http_client.get.return_value = make_response(200, SNAPSHOT_BODY)
        adapter = self._adapter(http_client)

        snapshot = await adapter.fetch("ws_1")

        self.assertEqual(
            snapshot,
            AuthorizationSnapshot(
                workspace_id="ws_1",
                workspace_name="Acme",
                authorization_epoch=2,
                entitlement_version=4,
                suspended=False,
                projection_version=7,
            ),
        )
        called_url = http_client.get.call_args.args[0]
        self.assertEqual(
            called_url,
            "https://control-plane.internal/api/internal/domain/workspaces/ws_1/authorization-snapshot",
        )
        self.assertEqual(
            http_client.get.call_args.kwargs["headers"]["Authorization"], "Bearer svc-token"
        )

    async def test_fetch_maps_404_to_workspace_not_found(self) -> None:
        http_client = AsyncMock()
        http_client.get.return_value = make_response(404)
        adapter = self._adapter(http_client)

        with self.assertRaises(WorkspaceNotFoundUpstreamError):
            await adapter.fetch("ws_missing")

    async def test_fetch_maps_a_5xx_to_upstream_unavailable(self) -> None:
        http_client = AsyncMock()
        http_client.get.return_value = make_response(503)
        adapter = self._adapter(http_client)

        with self.assertRaises(ProjectionUpstreamUnavailableError):
            await adapter.fetch("ws_1")

    async def test_fetch_keeps_a_non_200_status_on_the_error(self) -> None:
        for status in (302, 401, 403, 500, 503):
            with self.subTest(status=status):
                http_client = AsyncMock()
                http_client.get.return_value = make_response(status)

                with self.assertRaises(ProjectionUpstreamUnavailableError) as caught:
                    await self._adapter(http_client).fetch("ws_1")

                self.assertEqual(caught.exception.status_code, status)

    async def test_fetch_errors_without_an_upstream_status_carry_none(self) -> None:
        http_client = AsyncMock()
        http_client.get.return_value = make_response(404)
        with self.assertRaises(WorkspaceNotFoundUpstreamError) as not_found:
            await self._adapter(http_client).fetch("ws_missing")
        self.assertIsNone(getattr(not_found.exception, "status_code", None))

        http_client = AsyncMock()
        http_client.get.side_effect = httpx.ConnectTimeout("timed out")
        with self.assertRaises(ProjectionUpstreamUnavailableError) as transport:
            await self._adapter(http_client).fetch("ws_1")
        self.assertIsNone(transport.exception.status_code)

        http_client = AsyncMock()
        http_client.get.return_value = make_response(200, raise_error=ValueError("bad json"))
        with self.assertRaises(ProjectionUpstreamUnavailableError) as malformed:
            await self._adapter(http_client).fetch("ws_1")
        self.assertIsNone(malformed.exception.status_code)

    async def test_fetch_maps_a_transport_error_to_upstream_unavailable(self) -> None:
        http_client = AsyncMock()
        http_client.get.side_effect = httpx.ConnectTimeout("timed out")
        adapter = self._adapter(http_client)

        with self.assertRaises(ProjectionUpstreamUnavailableError):
            await adapter.fetch("ws_1")

    async def test_fetch_rejects_malformed_json(self) -> None:
        http_client = AsyncMock()
        http_client.get.return_value = make_response(200, raise_error=ValueError("bad json"))
        adapter = self._adapter(http_client)

        with self.assertRaises(ProjectionUpstreamUnavailableError):
            await adapter.fetch("ws_1")

    async def test_fetch_rejects_a_response_missing_fields(self) -> None:
        http_client = AsyncMock()
        incomplete = {k: v for k, v in SNAPSHOT_BODY.items() if k != "projection_version"}
        http_client.get.return_value = make_response(200, incomplete)
        adapter = self._adapter(http_client)

        with self.assertRaises(ProjectionUpstreamUnavailableError):
            await adapter.fetch("ws_1")

    async def test_fetch_rejects_a_mismatched_workspace_id(self) -> None:
        http_client = AsyncMock()
        wrong_id = {**SNAPSHOT_BODY, "workspace_id": "ws_other"}
        http_client.get.return_value = make_response(200, wrong_id)
        adapter = self._adapter(http_client)

        with self.assertRaises(ProjectionUpstreamUnavailableError):
            await adapter.fetch("ws_1")

    async def test_fetch_rejects_invalid_field_types(self) -> None:
        http_client = AsyncMock()
        bad_types = {**SNAPSHOT_BODY, "authorization_epoch": "not-a-number"}
        http_client.get.return_value = make_response(200, bad_types)
        adapter = self._adapter(http_client)

        with self.assertRaises(ProjectionUpstreamUnavailableError):
            await adapter.fetch("ws_1")


class PostgresWorkspaceProjectionStoreTests(unittest.IsolatedAsyncioTestCase):
    def _snapshot(self) -> AuthorizationSnapshot:
        return AuthorizationSnapshot(
            workspace_id="ws_1",
            workspace_name="Acme",
            authorization_epoch=2,
            entitlement_version=4,
            suspended=False,
            projection_version=7,
        )

    async def test_apply_calls_the_security_definer_function_and_commits(self) -> None:
        session = MagicMock()
        session.execute.return_value.scalar.return_value = True
        store = PostgresWorkspaceProjectionStore(lambda: session, default_currency="USD")

        applied = await store.apply(self._snapshot())

        self.assertTrue(applied)
        sql_text = str(session.execute.call_args.args[0])
        self.assertIn("marketai_apply_workspace_projection", sql_text)
        params = session.execute.call_args.args[1]
        self.assertEqual(params["ws_id"], "ws_1")
        self.assertEqual(params["currency"], "USD")
        session.commit.assert_called_once()
        session.close.assert_called_once()

    async def test_apply_returns_false_for_a_stale_replay(self) -> None:
        session = MagicMock()
        session.execute.return_value.scalar.return_value = False
        store = PostgresWorkspaceProjectionStore(lambda: session, default_currency="USD")

        applied = await store.apply(self._snapshot())

        self.assertFalse(applied)
        session.commit.assert_called_once()

    async def test_apply_rolls_back_and_closes_on_failure(self) -> None:
        session = MagicMock()
        session.execute.side_effect = RuntimeError("db down")
        store = PostgresWorkspaceProjectionStore(lambda: session, default_currency="USD")

        with self.assertRaises(RuntimeError):
            await store.apply(self._snapshot())

        session.rollback.assert_called_once()
        session.close.assert_called_once()
        session.commit.assert_not_called()


class WorkspaceProjectionSyncerTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_fetches_then_applies(self) -> None:
        snapshot = AuthorizationSnapshot(
            workspace_id="ws_1",
            workspace_name="Acme",
            authorization_epoch=1,
            entitlement_version=1,
            suspended=False,
            projection_version=1,
        )
        fetcher = AsyncMock()
        fetcher.fetch.return_value = snapshot
        store = AsyncMock()
        store.apply.return_value = True
        syncer = WorkspaceProjectionSyncer(fetcher, store)

        result = await syncer.sync("ws_1")

        self.assertTrue(result)
        fetcher.fetch.assert_awaited_once_with("ws_1")
        store.apply.assert_awaited_once_with(snapshot)

    async def test_sync_propagates_a_not_found_fetch_error_without_calling_apply(self) -> None:
        fetcher = AsyncMock()
        fetcher.fetch.side_effect = WorkspaceNotFoundUpstreamError("ws_1")
        store = AsyncMock()
        syncer = WorkspaceProjectionSyncer(fetcher, store)

        with self.assertRaises(WorkspaceNotFoundUpstreamError):
            await syncer.sync("ws_1")
        store.apply.assert_not_awaited()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
