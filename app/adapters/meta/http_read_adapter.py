"""Read-only Meta adapter for ``internal_validation`` (#112, #101 S6; ADR 0009).

``internal_validation`` is the one Meta evidence mode that reads the real
Graph API, and only in the platform Workspace. :class:`HttpMetaReadAdapter`
answers the two reads the scheduled executor makes
(``app.meta.observation_sync.MetaObservationReadPort``) -- ad-level insights
and the delivery-configuration edges -- and nothing else: it has no write
method, and it reaches Meta only through
:class:`app.adapters.meta.client.HttpMetaReadGateway`, which sends GET and
nothing else. ADR 0017's tripwire pins both facts.

Credentials. The adapter holds no secret. Every read opens the binding's two
CredentialVersions (``meta_access_token``, ``meta_app_secret``) through the
credential protection layer (``app.entitlements.credential_protector``),
inside the read's own Workspace, and only while they are ``active``; the
plaintext lives for that one request and the protector zeroes its buffers
afterwards (best effort: the header the request sends is a ``str``). The
process-wide ``META_ACCESS_TOKEN``, ``META_APP_SECRET`` and
``META_WRITE_ACCESS_TOKEN`` are never read. Each read also re-checks that
its Workspace is the platform Workspace.

Evidence. The page a read returns is Meta's response with the credentials
taken out: ``paging.next`` and ``paging.previous`` are dropped (Meta puts the
access token in those URLs), only the opaque ``paging.cursors`` stay, and
every string that still carries the token, the app secret or the proof is
redacted. The account edge is a node, not a list; it is kept as a one-row
``data`` list, the shape ``app.meta.scale_read_parsers`` reads.

Usage. Each page carries Meta's usage reading
(:class:`app.adapters.meta.client.MetaUsage`); ``defer_seconds`` is the
back-off the executor must wait before the next read once it crosses the
threshold. A rate-limit error is a ``throttled`` :class:`MetaReadError`
with its own back-off.

The configuration edges are read as they stand when the read runs. The
schedule reads at 04:00 account-local time after D, so this is the
configuration at the end of D (contract §6.2, ``read time (D)``); ``as_of``
names that day in the request identity and is not sent.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, ContextManager, Iterator, Mapping, Sequence

from cryptography.exceptions import InvalidTag
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.adapters.meta.client import (
    HttpMetaReadGateway,
    MetaReadCredentials,
    MetaReadError,
    MetaUsage,
    redact,
)
from app.adapters.persistence.models import WorkspaceRecord
from app.entitlements.credential_protector import (
    CredentialEncryptionContext,
    CredentialProtector,
    EncryptedEnvelope,
    LocalKmsAdapter,
)
from app.entitlements.credentials import CredentialVersionRecord
from app.meta.scale_read_contracts import META_GRAPH_API_VERSION, PAGE_LIMIT

__all__ = [
    "CONFIGURATION_EDGES",
    "HttpMetaReadAdapter",
    "MetaReadPage",
    "build_internal_validation_adapter",
    "credential_opener",
    "read_gateway",
]

#: The edges of the delivery-configuration read: the account node, then its ads,
#: ad sets and campaigns (``app.meta.scale_read_contracts.CONFIGURATION_EDGES``).
CONFIGURATION_EDGES = ("account", "ads", "adsets", "campaigns")
ACCESS_TOKEN_KEY = "meta_access_token"
APP_SECRET_KEY = "meta_app_secret"
CREDENTIAL_PROVIDER = "meta"

CredentialOpener = Callable[[], ContextManager[MetaReadCredentials]]


@dataclass(frozen=True)
class MetaReadPage:
    """One page of a read, ready to keep as evidence.

    ``payload`` is Meta's response without credentials; ``next_cursor`` is
    ``None`` on the last page; ``defer_seconds`` is set when the usage
    reading says to wait before the next read.
    """

    payload: dict[str, Any]
    row_count: int
    next_cursor: str | None
    http_status: int
    usage: MetaUsage

    @property
    def defer_seconds(self) -> float | None:
        return self.usage.backoff_seconds()

    def outcome_summary(self) -> dict[str, Any]:
        return {"usage": self.usage.summary()}


def read_gateway() -> HttpMetaReadGateway:
    """The gateway every read uses: the read contracts' frozen API version."""
    return HttpMetaReadGateway(api_version=META_GRAPH_API_VERSION, timeout=config.META_HTTP_TIMEOUT)


def default_protector() -> CredentialProtector:
    """The protector that sealed the platform's credentials at bootstrap
    (``app.meta.platform_api._get_protector``): the same envelope seam."""
    return LocalKmsAdapter.local_only()


class HttpMetaReadAdapter:
    """GET-only Graph API reads for ``internal_validation``; no write method."""

    is_test_adapter = False

    def __init__(self, *, open_credentials: CredentialOpener, gateway: HttpMetaReadGateway) -> None:
        self._open_credentials = open_credentials
        self._gateway = gateway

    def __repr__(self) -> str:
        return "HttpMetaReadAdapter()"

    def read_insights(
        self,
        *,
        ad_account_id: str,
        fields: Sequence[str],
        since: date,
        until: date,
        limit: int = PAGE_LIMIT,
        after: str | None = None,
        time_increment: str = "1",
        breakdowns: Sequence[str] = (),
        action_attribution_windows: Sequence[str] = (),
        action_report_time: str | None = None,
    ) -> MetaReadPage:
        """One page of ad-level insights for ``[since, until]`` (account-local days)."""
        if since > until:
            raise MetaReadError("meta_read_request_invalid", f"since {since} is after until {until}",
                                kind="failed", network_attempted=False)
        params: dict[str, str] = {
            "level": "ad",
            "fields": ",".join(fields),
            "time_range": json.dumps({"since": since.isoformat(), "until": until.isoformat()},
                                     separators=(",", ":")),
            "time_increment": str(time_increment),
            "limit": str(_limit(limit)),
        }
        if breakdowns:
            params["breakdowns"] = ",".join(breakdowns)
        if action_attribution_windows:
            params["action_attribution_windows"] = json.dumps(list(action_attribution_windows))
        if action_report_time:
            params["action_report_time"] = action_report_time
        if after:
            params["after"] = after
        return self._read(f"/{ad_account_id}/insights", params, node=False)

    def read_delivery_configuration(
        self,
        *,
        ad_account_id: str,
        edge: str,
        fields: Sequence[str],
        as_of: date,
        limit: int = PAGE_LIMIT,
        after: str | None = None,
    ) -> MetaReadPage:
        """One page of one configuration edge, as it stands when read."""
        del as_of  # the read is of the current configuration; see the module docstring
        if edge not in CONFIGURATION_EDGES:
            raise MetaReadError("meta_read_request_invalid", f"unknown configuration edge {edge!r}",
                                kind="failed", network_attempted=False)
        params = {"fields": ",".join(fields)}
        if edge == "account":
            if after:
                raise MetaReadError("meta_read_request_invalid", "the account node has one page",
                                    kind="failed", network_attempted=False)
            return self._read(f"/{ad_account_id}", params, node=True)
        params["limit"] = str(_limit(limit))
        if after:
            params["after"] = after
        return self._read(f"/{ad_account_id}/{edge}", params, node=False)

    def _read(self, path: str, params: Mapping[str, str], *, node: bool) -> MetaReadPage:
        with self._open_credentials() as credentials:
            response = self._gateway.get(path, params, credentials)
            payload, next_cursor = _evidence_payload(response.body, node=node, secrets=credentials.secrets())
        return MetaReadPage(
            payload=payload,
            row_count=len(payload["data"]),
            next_cursor=next_cursor,
            http_status=response.http_status,
            usage=response.usage,
        )


def _limit(limit: int) -> int:
    if limit < 1:
        raise MetaReadError("meta_read_request_invalid", "limit must be positive", kind="failed",
                            network_attempted=False)
    return limit


def _evidence_payload(body: Mapping[str, Any], *, node: bool, secrets: tuple[str, ...]) -> tuple[dict, str | None]:
    """The response as evidence: rows, opaque cursors, no credentials."""
    if node:
        row = {key: value for key, value in body.items() if key != "paging"}
        return redact({"data": [row]}, secrets), None
    data = body.get("data")
    if not isinstance(data, list) or not all(isinstance(row, Mapping) for row in data):
        raise MetaReadError("meta_read_response_invalid", "Meta answered without a list of rows", kind="failed")
    paging = body.get("paging") if isinstance(body.get("paging"), Mapping) else {}
    cursors = paging.get("cursors") if isinstance(paging.get("cursors"), Mapping) else {}
    after = cursors.get("after")
    # Meta's last page still names an `after` cursor; only `next` says there is more.
    next_cursor = str(after) if paging.get("next") and after else None
    if paging.get("next") and not after:
        raise MetaReadError("meta_read_response_invalid", "Meta named a next page without a cursor", kind="failed")
    payload: dict[str, Any] = {"data": list(data)}
    if cursors:
        payload["paging"] = {"cursors": {key: cursors[key] for key in ("before", "after") if key in cursors}}
    return redact(payload, secrets), (redact(next_cursor, secrets) if next_cursor else None)


# ---------------------------------------------------------------------------
# Credentials: decrypted per call, inside the read's Workspace
# ---------------------------------------------------------------------------


def _require_platform_workspace(session: Session, workspace_id: str) -> None:
    workspace = session.get(WorkspaceRecord, workspace_id)
    if workspace is None or not workspace.is_platform_workspace:
        raise MetaReadError(
            "meta_platform_workspace_required",
            "internal_validation reads Meta only in the platform Workspace",
            kind="auth", network_attempted=False,
        )


def _unavailable(detail: str) -> MetaReadError:
    return MetaReadError("meta_read_credential_unavailable", detail, kind="auth", network_attempted=False)


@contextmanager
def _open_version(
    session: Session, workspace_id: str, version_id: str | None, expected_key: str, protector: CredentialProtector,
) -> Iterator[str]:
    """One active CredentialVersion of this Workspace, decrypted for one read."""
    if not version_id:
        raise _unavailable(f"the binding names no {expected_key} version")
    version = session.scalar(
        select(CredentialVersionRecord).where(
            CredentialVersionRecord.id == version_id,
            CredentialVersionRecord.workspace_id == workspace_id,
        )
    )
    if version is None:
        raise _unavailable(f"{expected_key} version {version_id} is not in this Workspace")
    if version.credential_key != expected_key or version.provider != CREDENTIAL_PROVIDER:
        raise _unavailable(f"version {version_id} is not a Meta {expected_key}")
    if version.state != "active":
        raise _unavailable(f"{expected_key} version {version_id} is {version.state}")
    envelope = EncryptedEnvelope(
        encrypted_value=version.encrypted_secret,
        encrypted_data_key=version.encrypted_data_key,
        nonce=version.nonce,
        encryption_key_id=version.encryption_key_id,
        encryption_algorithm=version.encryption_algorithm,
        aad_schema_version=version.aad_schema_version,
    )
    context = CredentialEncryptionContext(
        workspace_id=workspace_id,
        credential_version_id=version.id,
        credential_key=version.credential_key,
        provider=version.provider,
        version_no=version.version_no,
        aad_schema_version=version.aad_schema_version,
    )
    try:
        scope = protector.open(envelope, context)
    except (InvalidTag, ValueError):
        raise _unavailable(f"{expected_key} version {version_id} could not be decrypted") from None
    with scope as view:
        try:
            plaintext = bytes(view).decode("utf-8")
        except UnicodeDecodeError:
            raise _unavailable(f"{expected_key} version {version_id} is not text") from None
        yield plaintext


def credential_opener(
    session: Session,
    *,
    workspace_id: str,
    access_token_version_id: str | None,
    app_secret_version_id: str | None,
    protector: CredentialProtector | None = None,
) -> CredentialOpener:
    """A context manager factory that decrypts the pair afresh on every read."""
    protector = protector or default_protector()

    @contextmanager
    def open_credentials() -> Iterator[MetaReadCredentials]:
        _require_platform_workspace(session, workspace_id)
        with _open_version(session, workspace_id, access_token_version_id, ACCESS_TOKEN_KEY, protector) as token, \
                _open_version(session, workspace_id, app_secret_version_id, APP_SECRET_KEY, protector) as secret:
            yield MetaReadCredentials(token, secret)

    return open_credentials


def build_internal_validation_adapter(
    session: Session,
    *,
    workspace_id: str,
    access_token_version_id: str | None,
    app_secret_version_id: str | None,
) -> HttpMetaReadAdapter:
    """The adapter ``resolve_adapter`` hands out for ``internal_validation``."""
    return HttpMetaReadAdapter(
        open_credentials=credential_opener(
            session,
            workspace_id=workspace_id,
            access_token_version_id=access_token_version_id,
            app_secret_version_id=app_secret_version_id,
        ),
        gateway=read_gateway(),
    )
