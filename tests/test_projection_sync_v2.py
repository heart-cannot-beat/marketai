"""Tests for v2 signed-envelope projection sync (Task 19).

Covers the three new components:
- ControlPlaneV2SnapshotHttpAdapter (HTTP fetcher)
- V2WorkspaceProjectionStore (ingest bridge)
- WorkspaceProjectionSyncer with v2 wiring
"""
from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from unittest.mock import AsyncMock, MagicMock

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import WorkspaceRecord
from app.entitlements.models import (
    EntitlementProjectionRecord,
    EntitlementSnapshotInboxRecord,
)
from app.entitlements.projection import EntitlementProjectionModule
from app.entitlements.types import InboxOutcome
from app.platform.projection_sync import (
    AuthorizationSnapshot,
    ControlPlaneV2SnapshotHttpAdapter,
    ProjectionUpstreamUnavailableError,
    SyncResult,
    V2WorkspaceProjectionStore,
    WorkspaceNotFoundUpstreamError,
    WorkspaceProjectionSyncer,
)

# ---------------------------------------------------------------------------
# Helpers: signed envelope construction
# ---------------------------------------------------------------------------

KEY_ID = "test-key-1"
KEY_BYTES = b"test-secret-key-for-hmac-verification-minimum-32-bytes!"
WS_ID = "ws_v2_test"


def _payload(
    *,
    snapshot_id: str = "snap_v2_001",
    workspace_id: str = WS_ID,
    entitlement_version: int = 1,
    lease_sequence: int = 1,
    projection_version: int = 1,
    semantic_hash: str = "abc123def456",
    **overrides: object,
) -> dict:
    base: dict = {
        "snapshot_id": snapshot_id,
        "workspace_id": workspace_id,
        "workspace_name": "Test Workspace",
        "authorization_epoch": 1,
        "entitlement_version": entitlement_version,
        "lease_sequence": lease_sequence,
        "projection_version": projection_version,
        "lifecycle": "active",
        "suspended": False,
        "entitlement_snapshot": {
            "semantic_hash": semantic_hash,
            "plan_version_id": "plan_v1",
            "source_refs": {},
            "period_start": "2026-01-01T00:00:00+00:00",
            "period_end": "2026-12-31T23:59:59+00:00",
            "period_anchor": "2026-01-01T00:00:00+00:00",
            "resolved_entitlements": {"features": ["gen_text"]},
            "restriction_summary": {},
            "entitlement_registry_version": 1,
            "resolution_policy_version": 1,
            "platform_limits_version": 1,
            "issued_at": "2026-01-01T00:00:00+00:00",
            "valid_until": "2026-12-31T23:59:59+00:00",
            "audit_event_id": "audit_v2_001",
        },
    }
    base.update(overrides)
    return base


def _sign(payload: dict, *, key_id: str = KEY_ID, key: bytes = KEY_BYTES) -> bytes:
    """Build a valid signed envelope as raw bytes."""
    header = {
        "schema_version": 2,
        "signature_key_id": key_id,
        "signature_algorithm": "hmac-sha256",
    }
    signed_content = json.dumps(
        {"protected_header": header, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
    )
    sig = hmac.new(key, signed_content.encode(), hashlib.sha256).hexdigest()
    envelope = {"protected_header": header, "payload": payload, "signature": sig}
    return json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()


# ---------------------------------------------------------------------------
# ControlPlaneV2SnapshotHttpAdapter tests
# ---------------------------------------------------------------------------


class TestV2FetcherCallsCorrectEndpoint(unittest.IsolatedAsyncioTestCase):
    """test_v2_fetcher_calls_correct_endpoint"""

    def _adapter(self, http_client: AsyncMock) -> ControlPlaneV2SnapshotHttpAdapter:
        return ControlPlaneV2SnapshotHttpAdapter(
            base_url="https://control-plane.internal",
            service_token="d2cp-token",
            timeout_ms=3000,
            http_client=http_client,
        )

    async def test_v2_fetcher_calls_correct_endpoint(self) -> None:
        envelope = _sign(_payload())
        response = MagicMock()
        response.status_code = 200
        response.content = envelope

        http_client = AsyncMock()
        http_client.get.return_value = response
        adapter = self._adapter(http_client)

        result = await adapter.fetch("ws_v2_test")

        self.assertEqual(result, envelope)

        called_url = http_client.get.call_args.args[0]
        self.assertEqual(
            called_url,
            "https://control-plane.internal/api/internal/domain/workspaces/"
            "ws_v2_test/workspace-projection-snapshot",
        )
        self.assertEqual(
            http_client.get.call_args.kwargs["headers"]["Authorization"],
            "Bearer d2cp-token",
        )
        self.assertEqual(
            http_client.get.call_args.kwargs["headers"]["Accept"],
            "application/json",
        )


class TestV2FetcherHandles404(unittest.IsolatedAsyncioTestCase):
    """test_v2_fetcher_handles_404"""

    async def test_v2_fetcher_handles_404(self) -> None:
        response = MagicMock()
        response.status_code = 404

        http_client = AsyncMock()
        http_client.get.return_value = response
        adapter = ControlPlaneV2SnapshotHttpAdapter(
            base_url="https://control-plane.internal",
            service_token="d2cp-token",
            timeout_ms=3000,
            http_client=http_client,
        )

        with self.assertRaises(WorkspaceNotFoundUpstreamError):
            await adapter.fetch("ws_missing")


class TestV2FetcherHandlesTimeout(unittest.IsolatedAsyncioTestCase):
    """test_v2_fetcher_handles_timeout"""

    async def test_v2_fetcher_handles_timeout(self) -> None:
        http_client = AsyncMock()
        http_client.get.side_effect = httpx.ConnectTimeout("timed out")
        adapter = ControlPlaneV2SnapshotHttpAdapter(
            base_url="https://control-plane.internal",
            service_token="d2cp-token",
            timeout_ms=3000,
            http_client=http_client,
        )

        with self.assertRaises(ProjectionUpstreamUnavailableError):
            await adapter.fetch("ws_v2_test")

    async def test_v2_fetcher_handles_5xx(self) -> None:
        response = MagicMock()
        response.status_code = 503

        http_client = AsyncMock()
        http_client.get.return_value = response
        adapter = ControlPlaneV2SnapshotHttpAdapter(
            base_url="https://control-plane.internal",
            service_token="d2cp-token",
            timeout_ms=3000,
            http_client=http_client,
        )

        with self.assertRaises(ProjectionUpstreamUnavailableError):
            await adapter.fetch("ws_v2_test")


# ---------------------------------------------------------------------------
# V2WorkspaceProjectionStore tests
# ---------------------------------------------------------------------------


class TestV2StoreCallsIngest(unittest.IsolatedAsyncioTestCase):
    """test_v2_store_calls_ingest"""

    async def test_v2_store_calls_ingest(self) -> None:
        projection_module = AsyncMock(spec=EntitlementProjectionModule)
        projection_module.ingest.return_value = InboxOutcome.APPLIED

        store = V2WorkspaceProjectionStore(projection_module)
        envelope = _sign(_payload())

        outcome = await store.apply(envelope)

        self.assertEqual(outcome, InboxOutcome.APPLIED)
        projection_module.ingest.assert_awaited_once_with(
            envelope,
            delivery_kind="full_repair",
        )

    async def test_v2_store_propagates_duplicate_outcome(self) -> None:
        projection_module = AsyncMock(spec=EntitlementProjectionModule)
        projection_module.ingest.return_value = InboxOutcome.DUPLICATE

        store = V2WorkspaceProjectionStore(projection_module)
        envelope = _sign(_payload())

        outcome = await store.apply(envelope)

        self.assertEqual(outcome, InboxOutcome.DUPLICATE)


# ---------------------------------------------------------------------------
# End-to-end: v2 syncer with real SQLite DB
# ---------------------------------------------------------------------------


class TestV2SyncerEndToEnd(unittest.IsolatedAsyncioTestCase):
    """test_v2_syncer_end_to_end"""

    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", echo=False)
        Base.metadata.create_all(self.engine)
        self.SessionFactory = sessionmaker(
            bind=self.engine, expire_on_commit=False,
        )
        with self.SessionFactory() as s:
            s.add(WorkspaceRecord(id=WS_ID, name="Test Workspace"))
            s.commit()

        self.projection_module = EntitlementProjectionModule(
            session_factory=self.SessionFactory,
            verification_key_ring={KEY_ID: KEY_BYTES},
        )
        self.v2_store = V2WorkspaceProjectionStore(self.projection_module)

    async def test_v2_syncer_end_to_end(self) -> None:
        envelope = _sign(_payload())

        # Mock the v2 fetcher to return our signed envelope
        v2_fetcher = AsyncMock(spec=ControlPlaneV2SnapshotHttpAdapter)
        v2_fetcher.fetch.return_value = envelope

        # v1 fetcher/store are mocks (should not be called)
        v1_fetcher = AsyncMock()
        v1_store = AsyncMock()

        syncer = WorkspaceProjectionSyncer(
            v1_fetcher,
            v1_store,
            v2_fetcher=v2_fetcher,
            v2_store=self.v2_store,
        )

        self.assertTrue(syncer.v2_enabled)

        result = await syncer.sync(WS_ID)

        self.assertEqual(result.version, "v2")
        self.assertTrue(result.applied)
        self.assertEqual(result.v2_outcome, InboxOutcome.APPLIED)

        # v1 path should not have been called
        v1_fetcher.fetch.assert_not_awaited()
        v1_store.apply.assert_not_awaited()

        # Verify the projection was written to the database
        with self.SessionFactory() as s:
            proj = s.get(EntitlementProjectionRecord, WS_ID)
            self.assertIsNotNone(proj)
            self.assertEqual(proj.entitlement_version, 1)
            self.assertEqual(proj.lease_sequence, 1)
            self.assertEqual(proj.current_snapshot_id, "snap_v2_001")

    async def test_v2_syncer_duplicate_is_not_applied(self) -> None:
        """A duplicate envelope returns applied=False with DUPLICATE outcome."""
        envelope = _sign(_payload())

        v2_fetcher = AsyncMock(spec=ControlPlaneV2SnapshotHttpAdapter)
        v2_fetcher.fetch.return_value = envelope

        syncer = WorkspaceProjectionSyncer(
            AsyncMock(),
            AsyncMock(),
            v2_fetcher=v2_fetcher,
            v2_store=self.v2_store,
        )

        # First call: applied
        result1 = await syncer.sync(WS_ID)
        self.assertTrue(result1.applied)

        # Second call: duplicate
        result2 = await syncer.sync(WS_ID)
        self.assertEqual(result2.version, "v2")
        self.assertFalse(result2.applied)
        self.assertEqual(result2.v2_outcome, InboxOutcome.DUPLICATE)


# ---------------------------------------------------------------------------
# v1 fallback when v2 is not configured
# ---------------------------------------------------------------------------


class TestV1FallbackWhenV2NotConfigured(unittest.IsolatedAsyncioTestCase):
    """test_v1_fallback_when_v2_not_configured"""

    async def test_v1_fallback_when_v2_not_configured(self) -> None:
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

        # No v2 components
        syncer = WorkspaceProjectionSyncer(fetcher, store)

        self.assertFalse(syncer.v2_enabled)

        result = await syncer.sync("ws_1")

        self.assertIsInstance(result, SyncResult)
        self.assertEqual(result.version, "v1")
        self.assertTrue(result.applied)
        self.assertIsNone(result.v2_outcome)
        fetcher.fetch.assert_awaited_once_with("ws_1")
        store.apply.assert_awaited_once_with(snapshot)

    async def test_v1_still_works_with_only_v2_fetcher_set(self) -> None:
        """v2 requires BOTH fetcher and store; only one means v1 fallback."""
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

        v2_fetcher = AsyncMock(spec=ControlPlaneV2SnapshotHttpAdapter)

        syncer = WorkspaceProjectionSyncer(
            fetcher, store, v2_fetcher=v2_fetcher,
        )

        self.assertFalse(syncer.v2_enabled)

        result = await syncer.sync("ws_1")

        self.assertEqual(result.version, "v1")
        fetcher.fetch.assert_awaited_once()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
