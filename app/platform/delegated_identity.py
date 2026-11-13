"""Pure ASGI middleware that verifies delegated BFF JWTs (spec §3).

`DelegatedIdentityMiddleware` is deliberately not
`starlette.middleware.base.BaseHTTPMiddleware`: that base class can break
`contextvars` propagation and forces async I/O (JWT verification, JWKS
refresh, the workspace admission read) through a sync-looking `dispatch`
that blocks the event loop. This middleware speaks the ASGI protocol
directly instead.

It protects only `/api/v1/*` paths -- the legacy `/api/*` surface (health,
readiness, OpenAPI docs, and the pre-#17 product/run endpoints) is outside
its scope by construction, not by an exemption list that has to be kept in
sync as routes are added.

On success it writes a `TenantContext` to `scope["state"]["tenant_context"]`,
which `app.platform.http_identity.request_tenant_context` reads via
`request.state.tenant_context`. On failure it writes a Problem Details
(RFC 9457) response itself, with headers built from an explicit list -- never
by forwarding whatever the client sent.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from enum import StrEnum

import jwt
from sqlalchemy.exc import StatementError
from starlette.types import ASGIApp, Receive, Scope, Send

from .jwks_store import (
    JwksKeyStore,
    JwksNotYetLoadedError,
    JwksRefreshError,
    JwksStaleError,
    UnknownKidError,
)
from .projection_sync import (
    ProjectionUpstreamUnavailableError,
    WorkspaceNotFoundUpstreamError,
    WorkspaceProjectionSyncer,
)
from .tenancy import DEFAULT_UI_LANGUAGE, TenantContext, new_request_id, ui_language_from_header
from .workspace_admission import WorkspaceAdmission, WorkspaceAdmissionReader, WorkspaceNotFoundError

logger = logging.getLogger(__name__)

JTI_PATTERN = re.compile(r"^jti_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

#: Exactly these 13 claims -- no more, no fewer. The middleware rejects a
#: token carrying anything else rather than silently ignoring it.
EXPECTED_CLAIMS = frozenset(
    {
        "iss",
        "aud",
        "sub",
        "workspace_id",
        "membership_id",
        "role",
        "session_id",
        "authorization_epoch",
        "entitlement_version",
        "jti",
        "iat",
        "nbf",
        "exp",
    }
)

#: Correlation identifiers accepted from the BFF (`X-Request-Id`,
#: `X-Trace-Id`) must look like the values the BFF itself generates
#: (`req_<uuid-hex>` / an opaque browser-supplied token) before they are
#: trusted into a Problem Details body or a TenantContext -- an unbounded or
#: control-character value from a misbehaving upstream must not reach logs.
_CORRELATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")

#: 403 codes the client should retry: `authorization_epoch_stale` resolves as
#: soon as the BFF reissues a token with the current epoch (no fixed delay
#: needed); the two `*_sync_pending` codes resolve once the domain's
#: projection repair lands, so they additionally carry a `retry_after` hint
#: (spec §3.5, §3.6).
_RETRYABLE_403_CODES = frozenset(
    {"authorization_epoch_stale", "authorization_sync_pending", "entitlement_sync_pending"}
)
_RETRY_AFTER_403_CODES = frozenset({"authorization_sync_pending", "entitlement_sync_pending"})

#: One `projection_sync` warning per on-demand sync; a real attempt adds the
#: second half. The fields are in the message itself because the API has no
#: logging config of its own: under Uvicorn's defaults a warning prints as
#: its bare message, and `extra=` fields never show. Only class names and the
#: upstream status go in -- an exception's message can carry the snapshot
#: URL or the driver's SQL.
_SYNC_LOG = "projection_sync trigger=%s result=%s workspace_id=%s trace_id=%s"
_SYNC_ATTEMPT_LOG = (
    _SYNC_LOG + " elapsed_ms=%d error_class=%s cause_class=%s http_status=%s rate_limited_since_last=%d"
)


class SyncResult(StrEnum):
    SYNCED = "synced"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    NOT_FOUND = "not_found"


class SyncTrigger(StrEnum):
    MISSING_ROW = "missing_row"
    EPOCH_AHEAD = "epoch_ahead"
    ENTITLEMENT_AHEAD = "entitlement_ahead"


class TokenVerificationError(Exception):
    def __init__(self, code: str, description: str):
        self.code = code
        self.description = description
        super().__init__(description)


@dataclass(frozen=True)
class VerifiedDelegatedIdentity:
    sub: str
    workspace_id: str
    membership_id: str
    role: str
    session_id: str
    authorization_epoch: int
    entitlement_version: int
    jti: str


@dataclass
class IdentityConfig:
    issuer: str
    audience: str
    max_clock_skew_seconds: int
    max_token_bytes: int
    max_token_ttl_seconds: int
    projection_repair_interval_seconds: int


class PerKeyRateLimiter:
    """At most one permit per key per `interval_seconds`, process-wide.

    Used to keep a flood of concurrent requests with a stale projection from
    each triggering their own projection-repair round trip to the control
    plane.
    """

    def __init__(self, interval_seconds: int) -> None:
        self._interval = interval_seconds
        self._last: dict[str, float] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        last = self._last.get(key)
        if last is not None and now - last < self._interval:
            return False
        self._last[key] = now
        return True


def _extract_header(scope: Scope, name: bytes) -> str | None:
    for header_name, header_value in scope.get("headers", []):
        if header_name == name:
            return header_value.decode("latin-1")
    return None


def _validated_correlation_id(value: str | None) -> str | None:
    if value is None or not _CORRELATION_ID_PATTERN.match(value):
        return None
    return value


def _sync_error_fields(error: Exception | None) -> tuple[str, str, str]:
    """`error_class`, `cause_class` and `http_status` for a sync failure."""
    if error is None:
        return "-", "-", "-"
    # SQLAlchemy keeps the driver's error (e.g. InsufficientPrivilege) on `orig`.
    cause = error.orig if isinstance(error, StatementError) else error.__cause__
    status = error.status_code if isinstance(error, ProjectionUpstreamUnavailableError) else None
    return (
        type(error).__name__,
        type(cause).__name__ if cause is not None else "-",
        str(status) if status is not None else "-",
    )


class DelegatedIdentityMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        jwks_store: JwksKeyStore,
        admission_reader: WorkspaceAdmissionReader,
        projection_syncer: WorkspaceProjectionSyncer,
        config: IdentityConfig,
    ) -> None:
        self.app = app
        self.jwks_store = jwks_store
        self.admission_reader = admission_reader
        self.projection_syncer = projection_syncer
        self.config = config
        self._projection_repair_limiter = PerKeyRateLimiter(
            interval_seconds=config.projection_repair_interval_seconds,
        )
        # Syncs the limiter refused since each workspace's last real attempt.
        self._rate_limited_since_last: dict[str, int] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        scope.setdefault("state", {})

        path = scope.get("path", "")
        if self._is_exempt(path):
            await self.app(scope, receive, send)
            return

        # Correlation identifiers are established before anything can fail,
        # so every error response -- including "no token at all" -- carries
        # a trace_id and the eventual TenantContext carries the same one.
        request_id = _validated_correlation_id(_extract_header(scope, b"x-request-id")) or new_request_id()
        trace_id = _validated_correlation_id(_extract_header(scope, b"x-trace-id")) or request_id
        scope["state"]["request_id"] = request_id
        scope["state"]["trace_id"] = trace_id

        token = self._extract_bearer_token(scope)
        if token is None:
            await self._send_401(send, "delegated_identity_required", "Bearer token required", trace_id)
            return

        try:
            identity = await self._verify_token(token)
        except (JwksRefreshError, JwksStaleError, JwksNotYetLoadedError):
            await self._send_503(
                send, "jwks_unavailable", "Signing keys are temporarily unavailable", trace_id
            )
            return
        except TokenVerificationError as e:
            await self._send_401(send, e.code, e.description, trace_id)
            return

        # Read workspace projection — on miss, attempt sync then retry.
        try:
            admission = await self.admission_reader.read(identity.workspace_id)
        except WorkspaceNotFoundError:
            result = await self._try_projection_sync(
                identity.workspace_id, trigger=SyncTrigger.MISSING_ROW, trace_id=trace_id
            )
            if result == SyncResult.NOT_FOUND:
                await self._send_401(send, "workspace_not_found", "Workspace does not exist", trace_id)
                return
            if result not in (SyncResult.SYNCED, SyncResult.RATE_LIMITED):
                await self._send_503(
                    send,
                    "projection_sync_unavailable",
                    "Cannot verify workspace existence — upstream unavailable",
                    trace_id,
                )
                return
            # RATE_LIMITED reads again too: the sync that took this window, or
            # the control plane's push, may have created the row since the
            # first read.
            try:
                admission = await self.admission_reader.read(identity.workspace_id)
            except WorkspaceNotFoundError:
                await self._send_503(
                    send,
                    "projection_sync_unavailable",
                    (
                        "Projection store reported success but no row is readable"
                        if result == SyncResult.SYNCED
                        else "Cannot verify workspace existence — upstream unavailable"
                    ),
                    trace_id,
                )
                return

        # Epoch/entitlement gates — on claim-ahead, attempt repair then re-read.
        epoch_error = self._check_authorization_epoch(identity, admission)
        entitlement_error = self._check_entitlement_version(identity, admission, scope)

        if epoch_error == "authorization_sync_pending" or entitlement_error == "entitlement_sync_pending":
            trigger = (
                SyncTrigger.EPOCH_AHEAD
                if epoch_error == "authorization_sync_pending"
                else SyncTrigger.ENTITLEMENT_AHEAD
            )
            repair = await self._try_projection_sync(identity.workspace_id, trigger=trigger, trace_id=trace_id)
            if repair == SyncResult.SYNCED:
                try:
                    admission = await self.admission_reader.read(identity.workspace_id)
                except WorkspaceNotFoundError:
                    await self._send_503(
                        send,
                        "projection_sync_unavailable",
                        "Projection store reported success but no row is readable",
                        trace_id,
                    )
                    return
                epoch_error = self._check_authorization_epoch(identity, admission)
                entitlement_error = self._check_entitlement_version(identity, admission, scope)
            elif repair == SyncResult.NOT_FOUND:
                await self._send_401(send, "workspace_not_found", "Workspace no longer exists", trace_id)
                return
            elif repair == SyncResult.UPSTREAM_UNAVAILABLE:
                await self._send_503(
                    send,
                    "projection_sync_unavailable",
                    "Authorization projection repair is unavailable",
                    trace_id,
                )
                return
            # RATE_LIMITED falls through to the existing fail-closed epoch or
            # write-entitlement result below.

        if epoch_error is not None:
            await self._send_403(send, epoch_error, trace_id)
            return

        if entitlement_error is not None:
            await self._send_403(send, entitlement_error, trace_id)
            return

        if admission.suspended:
            await self._send_403(send, "workspace_suspended", trace_id)
            return

        # A display preference only; it never selects or widens the tenant.
        ui_language = ui_language_from_header(_extract_header(scope, b"x-ui-language"))
        tenant_context = self._build_tenant_context(identity, admission, request_id, trace_id, ui_language)
        scope["state"]["tenant_context"] = tenant_context

        await self.app(scope, receive, send)

    def _is_exempt(self, path: str) -> bool:
        return not path.startswith("/api/v1/")

    def _extract_bearer_token(self, scope: Scope) -> str | None:
        value = _extract_header(scope, b"authorization")
        if value is not None and value.startswith("Bearer "):
            return value[len("Bearer "):]
        return None

    async def _verify_token(self, raw_token: str) -> VerifiedDelegatedIdentity:
        if len(raw_token.encode("utf-8")) > self.config.max_token_bytes:
            raise TokenVerificationError("token_too_large", "Token exceeds maximum size")

        try:
            unverified_header = jwt.get_unverified_header(raw_token)
        except jwt.exceptions.DecodeError:
            raise TokenVerificationError("malformed_token", "Token is malformed")

        if unverified_header.get("alg") != "ES256":
            raise TokenVerificationError("algorithm_not_allowed", "Only ES256 is allowed")
        if unverified_header.get("typ") != "delegated+jwt":
            raise TokenVerificationError("invalid_token_type", "Token type must be delegated+jwt")

        kid = unverified_header.get("kid")
        if not kid or not isinstance(kid, str):
            raise TokenVerificationError("unknown_signing_key", "Missing kid in header")

        try:
            key = await self.jwks_store.get_key(kid)
        except UnknownKidError:
            raise TokenVerificationError("unknown_signing_key", f"Unknown kid: {kid}")

        try:
            payload = jwt.decode(
                raw_token,
                key=key,
                algorithms=["ES256"],
                issuer=self.config.issuer,
                audience=self.config.audience,
                leeway=self.config.max_clock_skew_seconds,
                options={"require": ["exp", "nbf", "iat", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError:
            raise TokenVerificationError("token_expired", "Token has expired")
        except jwt.ImmatureSignatureError:
            raise TokenVerificationError("token_not_yet_valid", "Token not yet valid")
        except jwt.InvalidIssuerError:
            raise TokenVerificationError("invalid_issuer", "Invalid issuer")
        except jwt.InvalidAudienceError:
            raise TokenVerificationError("invalid_audience", "Invalid audience")
        except jwt.InvalidSignatureError:
            raise TokenVerificationError("invalid_signature", "Invalid signature")
        except jwt.MissingRequiredClaimError:
            raise TokenVerificationError("malformed_token", "Token is missing a required claim")
        except jwt.DecodeError:
            raise TokenVerificationError("malformed_token", "Token decode failed")

        actual_claims = set(payload.keys())
        if actual_claims != EXPECTED_CLAIMS:
            extra = actual_claims - EXPECTED_CLAIMS
            if extra:
                raise TokenVerificationError("unexpected_claims", f"Unexpected claims: {sorted(extra)}")
            missing = EXPECTED_CLAIMS - actual_claims
            raise TokenVerificationError("malformed_token", f"Missing claims: {sorted(missing)}")

        iat = payload.get("iat")
        exp = payload.get("exp")
        if not isinstance(iat, (int, float)) or isinstance(iat, bool):
            raise TokenVerificationError("malformed_token", "iat must be numeric")
        if not isinstance(exp, (int, float)) or isinstance(exp, bool):
            raise TokenVerificationError("malformed_token", "exp must be numeric")
        if exp - iat > self.config.max_token_ttl_seconds:
            raise TokenVerificationError("token_ttl_exceeded", "Token TTL too long")

        jti = payload.get("jti", "")
        if not isinstance(jti, str) or not JTI_PATTERN.match(jti):
            raise TokenVerificationError("invalid_jti_format", "Invalid jti format")

        sub = payload.get("sub", "")
        if not isinstance(sub, str) or not sub or len(sub) > 255:
            raise TokenVerificationError("invalid_sub", "Invalid sub claim")

        ws_id = payload.get("workspace_id", "")
        if not isinstance(ws_id, str) or not ws_id or len(ws_id) > 255:
            raise TokenVerificationError("invalid_workspace_id", "Invalid workspace_id")

        mem_id = payload.get("membership_id", "")
        if not isinstance(mem_id, str) or not mem_id or len(mem_id) > 255:
            raise TokenVerificationError("invalid_membership_id", "Invalid membership_id")

        role = payload.get("role", "")
        if role not in ("owner", "member"):
            raise TokenVerificationError("invalid_role", "Invalid role")

        sess_id = payload.get("session_id", "")
        if not isinstance(sess_id, str) or not sess_id or len(sess_id) > 255:
            raise TokenVerificationError("invalid_session_id", "Invalid session_id")

        auth_epoch = payload.get("authorization_epoch")
        if not isinstance(auth_epoch, int) or isinstance(auth_epoch, bool) or auth_epoch < 0:
            raise TokenVerificationError("invalid_authorization_epoch", "Invalid authorization_epoch")

        ent_version = payload.get("entitlement_version")
        if not isinstance(ent_version, int) or isinstance(ent_version, bool) or ent_version < 0:
            raise TokenVerificationError("invalid_entitlement_version", "Invalid entitlement_version")

        return VerifiedDelegatedIdentity(
            sub=sub,
            workspace_id=ws_id,
            membership_id=mem_id,
            role=role,
            session_id=sess_id,
            authorization_epoch=auth_epoch,
            entitlement_version=ent_version,
            jti=jti,
        )

    def _check_authorization_epoch(
        self,
        identity: VerifiedDelegatedIdentity,
        admission: WorkspaceAdmission,
    ) -> str | None:
        claim = identity.authorization_epoch
        local = admission.authorization_epoch
        if claim == local:
            return None
        if claim < local:
            return "authorization_epoch_stale"
        # claim > local: domain projection is behind the control plane.
        return "authorization_sync_pending"

    def _check_entitlement_version(
        self,
        identity: VerifiedDelegatedIdentity,
        admission: WorkspaceAdmission,
        scope: Scope,
    ) -> str | None:
        claim = identity.entitlement_version
        local = admission.entitlement_version
        if claim == local:
            return None
        if claim > local:
            method = scope.get("method", "GET").upper()
            if method in ("POST", "PUT", "PATCH", "DELETE"):
                return "entitlement_sync_pending"
            return None
        # claim < local: the local (newer) projection wins. No error.
        return None

    def _build_tenant_context(
        self,
        identity: VerifiedDelegatedIdentity,
        admission: WorkspaceAdmission,
        request_id: str,
        trace_id: str,
        ui_language: str = DEFAULT_UI_LANGUAGE,
    ) -> TenantContext:
        from .tenancy import IdentityKind

        return TenantContext(
            workspace_id=identity.workspace_id,
            role=identity.role,
            initiator_id=identity.sub,
            authorization_epoch=admission.authorization_epoch,
            executor="user",
            request_id=request_id,
            trace_id=trace_id,
            identity_kind=IdentityKind.DELEGATED_USER,
            ui_language=ui_language,
        )

    async def _try_projection_sync(
        self, workspace_id: str, *, trigger: SyncTrigger, trace_id: str
    ) -> SyncResult:
        if not self._projection_repair_limiter.allow(workspace_id):
            # Only the first refusal in a limiter window is logged; the next
            # real attempt reports how many there were.
            refused = self._rate_limited_since_last.get(workspace_id, 0) + 1
            self._rate_limited_since_last[workspace_id] = refused
            if refused == 1:
                logger.warning(_SYNC_LOG, trigger, SyncResult.RATE_LIMITED, workspace_id, trace_id)
            return SyncResult.RATE_LIMITED
        rate_limited_since_last = self._rate_limited_since_last.pop(workspace_id, 0)
        error: Exception | None = None
        started = time.monotonic()
        try:
            await self.projection_syncer.sync(workspace_id)
            result = SyncResult.SYNCED
        except WorkspaceNotFoundUpstreamError as exc:
            error, result = exc, SyncResult.NOT_FOUND
        except Exception as exc:
            error, result = exc, SyncResult.UPSTREAM_UNAVAILABLE
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.warning(
            _SYNC_ATTEMPT_LOG,
            trigger,
            result,
            workspace_id,
            trace_id,
            elapsed_ms,
            *_sync_error_fields(error),
            rate_limited_since_last,
        )
        return result

    def _problem_body(self, status: int, code: str, title: str, trace_id: str, retryable: bool) -> bytes:
        body: dict[str, object] = {
            "type": f"urn:marketai:error:{code.replace('_', '-')}",
            "title": title,
            "status": status,
            "code": code,
            "trace_id": trace_id,
            "retryable": retryable,
        }
        if code in _RETRY_AFTER_403_CODES:
            body["retry_after"] = 5
        return json.dumps(body).encode()

    async def _send_401(self, send: Send, code: str, description: str, trace_id: str) -> None:
        body = self._problem_body(401, code, description, trace_id, retryable=False)
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    [b"content-type", b"application/problem+json"],
                    [
                        b"www-authenticate",
                        f'Bearer error="{code}", error_description="{description}"'.encode(),
                    ],
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def _send_403(self, send: Send, code: str, trace_id: str) -> None:
        title = code.replace("_", " ").title()
        body = self._problem_body(403, code, title, trace_id, retryable=code in _RETRYABLE_403_CODES)
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [[b"content-type", b"application/problem+json"]],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def _send_503(self, send: Send, code: str, description: str, trace_id: str) -> None:
        body = self._problem_body(503, code, description, trace_id, retryable=True)
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    [b"content-type", b"application/problem+json"],
                    [b"retry-after", b"5"],
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
