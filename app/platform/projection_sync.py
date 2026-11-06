"""Authorization snapshot propagation from control plane to domain (spec §3.10).

The domain `workspaces` table is a *projection* of the control-plane `Team`.
The control plane is the source of truth for workspace existence,
`authorization_epoch`, `entitlement_version`, and `suspended`; the domain
never invents or mutates these values on its own -- it only accepts
monotonic updates fetched over HTTP.

`app/` and `worker/` never connect to or read the control-plane database,
even when both databases share one PostgreSQL cluster. The only production
fetch path is `ControlPlaneAuthorizationSnapshotHttpAdapter`, which calls the
BFF's internal, service-token-authenticated snapshot endpoint. There is no
direct-database production Adapter.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Protocol

import anyio
import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.entitlements.projection import EntitlementProjectionModule
from app.entitlements.types import InboxOutcome

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthorizationSnapshot:
    workspace_id: str
    workspace_name: str
    authorization_epoch: int
    entitlement_version: int
    suspended: bool
    projection_version: int


class WorkspaceNotFoundUpstreamError(Exception):
    """The control plane authoritatively reports this workspace does not exist (404)."""


class ProjectionUpstreamUnavailableError(Exception):
    """Timeout, auth failure, malformed response, or a non-404 upstream error.

    `status_code` is the upstream HTTP status when the BFF answered with
    something other than 200 or 404, and None for every other failure.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AuthorizationSnapshotFetcher(Protocol):
    async def fetch(self, workspace_id: str) -> AuthorizationSnapshot:
        """Fetch the latest authorization snapshot from the control plane.

        Raises:
            WorkspaceNotFoundUpstreamError: The control plane authoritatively
                says this workspace does not exist (HTTP 404).
            ProjectionUpstreamUnavailableError: timeout, authentication
                failure, malformed response, or non-404 upstream error.
        """
        ...


class WorkspaceProjectionStore(Protocol):
    async def apply(self, snapshot: AuthorizationSnapshot) -> bool:
        """Apply a newer snapshot; return False for a replay/stale snapshot."""
        ...


@dataclass(frozen=True)
class SyncResult:
    """Outcome of a workspace projection sync attempt."""

    version: str  # "v1" or "v2"
    applied: bool  # True when the projection was materially updated
    v2_outcome: InboxOutcome | None = None  # present only for v2 path


class WorkspaceProjectionSyncer:
    """The single seam `DelegatedIdentityMiddleware` calls for a repair.

    The fetch and persistence seams are internal dependencies: production
    wires an HTTP fetch Adapter and a PostgreSQL `SECURITY DEFINER` store
    Adapter below; tests use in-memory doubles of the two Protocols.

    When v2 components are supplied, ``sync()`` uses the signed-envelope
    path first. If v2 is not configured the v1 path is used as a fallback.
    """

    def __init__(
        self,
        snapshot_fetcher: AuthorizationSnapshotFetcher,
        projection_store: WorkspaceProjectionStore,
        *,
        v2_fetcher: ControlPlaneV2SnapshotHttpAdapter | None = None,
        v2_store: V2WorkspaceProjectionStore | None = None,
    ) -> None:
        self._snapshot_fetcher = snapshot_fetcher
        self._projection_store = projection_store
        self._v2_fetcher = v2_fetcher
        self._v2_store = v2_store

    @property
    def v2_enabled(self) -> bool:
        return self._v2_fetcher is not None and self._v2_store is not None

    async def sync(self, workspace_id: str) -> SyncResult:
        if self.v2_enabled:
            assert self._v2_fetcher is not None
            assert self._v2_store is not None
            envelope_bytes = await self._v2_fetcher.fetch(workspace_id)
            outcome = await self._v2_store.apply(envelope_bytes)
            return SyncResult(
                version="v2",
                applied=(outcome == InboxOutcome.APPLIED),
                v2_outcome=outcome,
            )
        # v1 fallback
        snapshot = await self._snapshot_fetcher.fetch(workspace_id)
        applied = await self._projection_store.apply(snapshot)
        return SyncResult(version="v1", applied=applied)


_REQUIRED_SNAPSHOT_FIELDS = (
    "workspace_id",
    "workspace_name",
    "authorization_epoch",
    "entitlement_version",
    "suspended",
    "projection_version",
)


class ControlPlaneAuthorizationSnapshotHttpAdapter:
    """HTTP-only fetch of one `AuthorizationSnapshot` from the BFF.

    Calls ``GET {base_url}/api/internal/domain/workspaces/{workspace_id}/authorization-snapshot``
    with the shared service token. Only a 404 is treated as an authoritative
    "does not exist"; every other failure (timeout, non-2xx, malformed body,
    a workspace_id that does not match what was asked for) is a transient
    upstream problem the caller should retry.
    """

    def __init__(
        self,
        *,
        base_url: str,
        service_token: str,
        timeout_ms: int,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._service_token = service_token
        self._timeout_seconds = timeout_ms / 1000
        self._http_client = http_client

    async def fetch(self, workspace_id: str) -> AuthorizationSnapshot:
        url = (
            f"{self._base_url}/api/internal/domain/workspaces/"
            f"{workspace_id}/authorization-snapshot"
        )
        try:
            response = await self._http_client.get(
                url,
                headers={
                    "Authorization": f"Bearer {self._service_token}",
                    "Accept": "application/json",
                },
                timeout=self._timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise ProjectionUpstreamUnavailableError(
                f"authorization-snapshot request failed: {exc!r}"
            ) from exc

        if response.status_code == 404:
            raise WorkspaceNotFoundUpstreamError(workspace_id)
        if response.status_code != 200:
            raise ProjectionUpstreamUnavailableError(
                f"authorization-snapshot returned status {response.status_code}",
                status_code=response.status_code,
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise ProjectionUpstreamUnavailableError(
                "authorization-snapshot response is not valid JSON"
            ) from exc

        if not isinstance(data, dict) or any(
            field not in data for field in _REQUIRED_SNAPSHOT_FIELDS
        ):
            raise ProjectionUpstreamUnavailableError(
                "authorization-snapshot response is missing required fields"
            )

        if data["workspace_id"] != workspace_id:
            # Never trust a snapshot for a different workspace than the one
            # requested -- a proxy or upstream bug here is cross-tenant data.
            raise ProjectionUpstreamUnavailableError(
                "authorization-snapshot workspace_id does not match the request"
            )

        try:
            return AuthorizationSnapshot(
                workspace_id=str(data["workspace_id"]),
                workspace_name=str(data["workspace_name"]),
                authorization_epoch=int(data["authorization_epoch"]),
                entitlement_version=int(data["entitlement_version"]),
                suspended=bool(data["suspended"]),
                projection_version=int(data["projection_version"]),
            )
        except (TypeError, ValueError) as exc:
            raise ProjectionUpstreamUnavailableError(
                "authorization-snapshot response has an invalid field type"
            ) from exc


class PostgresWorkspaceProjectionStore:
    """Applies a snapshot via the `marketai_apply_workspace_projection`
    `SECURITY DEFINER` function (migration `g1a2b3c4d5e6`).

    Never issues a direct `INSERT`/`UPDATE` on `workspaces` -- the runtime
    role has no such grant, only `EXECUTE` on this one narrowly scoped
    function, which enforces the monotonic `projection_version` guard.
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        default_currency: str,
    ) -> None:
        self._session_factory = session_factory
        self._default_currency = default_currency

    async def apply(self, snapshot: AuthorizationSnapshot) -> bool:
        return await anyio.to_thread.run_sync(lambda: self._apply_sync(snapshot))

    def _apply_sync(self, snapshot: AuthorizationSnapshot) -> bool:
        session = self._session_factory()
        try:
            applied = session.execute(
                text(
                    "SELECT marketai_apply_workspace_projection("
                    ":ws_id, :ws_name, :epoch, :ent_version, :suspended, "
                    ":proj_version, :currency"
                    ") AS applied"
                ),
                {
                    "ws_id": snapshot.workspace_id,
                    "ws_name": snapshot.workspace_name,
                    "epoch": snapshot.authorization_epoch,
                    "ent_version": snapshot.entitlement_version,
                    "suspended": snapshot.suspended,
                    "proj_version": snapshot.projection_version,
                    "currency": self._default_currency,
                },
            ).scalar()
            session.commit()
            return bool(applied)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


# ---------------------------------------------------------------------------
# v2: signed-envelope projection (EntitlementProjectionModule)
# ---------------------------------------------------------------------------


class ControlPlaneV2SnapshotHttpAdapter:
    """Fetches a signed entitlement envelope from the v2 snapshot endpoint.

    Calls ``GET {base_url}/api/internal/domain/workspaces/{workspace_id}/workspace-projection-snapshot``
    using the domain-to-control-plane service token as Bearer auth.

    Returns the raw response body as ``bytes`` so that
    ``EntitlementProjectionModule.ingest()`` can verify the HMAC signature
    against the exact bytes the control plane produced.
    """

    def __init__(
        self,
        *,
        base_url: str,
        service_token: str,
        timeout_ms: int,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._service_token = service_token
        self._timeout_seconds = timeout_ms / 1000
        self._http_client = http_client

    async def fetch(self, workspace_id: str) -> bytes:
        """Return the raw signed-envelope bytes for *workspace_id*.

        Raises:
            WorkspaceNotFoundUpstreamError: 404 from upstream.
            ProjectionUpstreamUnavailableError: any other transport or
                HTTP error.
        """
        url = (
            f"{self._base_url}/api/internal/domain/workspaces/"
            f"{workspace_id}/workspace-projection-snapshot"
        )
        try:
            response = await self._http_client.get(
                url,
                headers={
                    "Authorization": f"Bearer {self._service_token}",
                    "Accept": "application/json",
                },
                timeout=self._timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise ProjectionUpstreamUnavailableError(
                f"workspace-projection-snapshot request failed: {exc!r}"
            ) from exc

        if response.status_code == 404:
            raise WorkspaceNotFoundUpstreamError(workspace_id)
        if response.status_code != 200:
            raise ProjectionUpstreamUnavailableError(
                f"workspace-projection-snapshot returned status {response.status_code}"
            )

        return response.content


class V2WorkspaceProjectionStore:
    """Applies a signed entitlement envelope via ``EntitlementProjectionModule``.

    This store does not own a session factory: the
    ``EntitlementProjectionModule`` already carries one and manages its own
    transaction boundary internally.
    """

    def __init__(
        self,
        projection_module: EntitlementProjectionModule,
    ) -> None:
        self._projection_module = projection_module

    async def apply(self, envelope_bytes: bytes) -> InboxOutcome:
        """Verify and apply *envelope_bytes*; return the inbox outcome."""
        return await self._projection_module.ingest(
            envelope_bytes,
            delivery_kind="full_repair",
        )

