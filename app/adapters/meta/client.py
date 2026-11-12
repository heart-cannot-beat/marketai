"""Small, testable Meta Graph API client restricted to GET requests.

Two read paths address the Graph API from this file, and both send GET only
(ADR 0017's tripwire pins that this file and the write client are the only
ones that do):

* :class:`MetaClient` and :class:`HttpMetaGateway` -- the legacy async read
  ingestion, authenticated with the process-wide ``META_ACCESS_TOKEN``.
* :class:`HttpMetaReadGateway` -- the ``internal_validation`` read (#112,
  #101 S6). It holds no credential: every call is handed the plaintext pair
  the caller just decrypted from the binding's CredentialVersions
  (:class:`MetaReadCredentials`), sends the token as a header and the
  ``appsecret_proof`` as a query parameter, follows no redirect, and raises
  :class:`MetaReadError` with Meta's message scrubbed of both secrets and the
  proof. It also reads Meta's usage headers into :class:`MetaUsage`, which
  decides when a sync must stop and wait (:meth:`MetaUsage.backoff_seconds`).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol
from urllib.parse import parse_qs, urlparse

import httpx

from ... import config


@dataclass(frozen=True)
class MetaPage:
    body: dict[str, Any]
    data: list[dict[str, Any]]
    after_cursor: str | None
    usage_headers: dict[str, str]


class MetaApiError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after


class MetaGateway(Protocol):
    async def get(self, path: str, params: dict[str, Any]) -> tuple[int, dict[str, Any], dict[str, str]]: ...


class HttpMetaGateway:
    """HTTP boundary. Access tokens are headers, never persisted request params."""

    async def get(self, path: str, params: dict[str, Any]) -> tuple[int, dict[str, Any], dict[str, str]]:
        url = f"https://graph.facebook.com/{config.META_API_VERSION}/{path.lstrip('/')}"
        headers = {"Authorization": f"Bearer {config.META_ACCESS_TOKEN}"}
        async with httpx.AsyncClient(timeout=config.META_HTTP_TIMEOUT) as client:
            response = await client.get(url, params=params, headers=headers)
        try:
            body = response.json()
        except ValueError:
            body = {"error": {"message": "Meta 返回了非 JSON 响应"}}
        return response.status_code, body, dict(response.headers)


def _next_after_cursor(body: dict[str, Any]) -> str | None:
    paging = body.get("paging") or {}
    cursor = (paging.get("cursors") or {}).get("after")
    if cursor:
        return str(cursor)
    # Some Graph responses only expose `next`. Persist only its opaque cursor,
    # never the URL because it can contain an access token.
    next_url = paging.get("next")
    if next_url:
        values = parse_qs(urlparse(str(next_url)).query).get("after")
        if values:
            return values[0]
    return None


class MetaClient:
    def __init__(self, gateway: MetaGateway | None = None) -> None:
        self.gateway = gateway or HttpMetaGateway()

    async def fetch_page(self, request: dict[str, Any], after: str | None = None) -> MetaPage:
        params = dict(request["params"])
        if after:
            params["after"] = after
        if config.META_APP_SECRET:
            params["appsecret_proof"] = hmac.new(
                config.META_APP_SECRET.encode("utf-8"),
                config.META_ACCESS_TOKEN.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        try:
            status, body, headers = await self.gateway.get(str(request["path"]), params)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise MetaApiError("network_error", str(exc), retryable=True) from exc

        if status >= 400 or "error" in body:
            error = body.get("error") or {}
            graph_code = str(error.get("code") or status)
            subcode = error.get("error_subcode")
            code = f"meta_{graph_code}" + (f"_{subcode}" if subcode else "")
            retryable = status == 429 or status >= 500 or graph_code in {"1", "2", "4", "17", "32", "613"}
            retry_after = None
            try:
                retry_after = float(headers.get("retry-after", ""))
            except ValueError:
                pass
            raise MetaApiError(code, str(error.get("message") or f"Meta HTTP {status}"), retryable=retryable,
                               retry_after=retry_after)

        raw_data = body.get("data")
        if isinstance(raw_data, list):
            data = [item for item in raw_data if isinstance(item, dict)]
        elif isinstance(body, dict):
            data = [body]
        else:
            data = []
        usage = {
            key: value for key, value in headers.items()
            if key.lower() in {
                "x-app-usage", "x-ad-account-usage", "x-business-use-case-usage",
                "x-fb-trace-id", "x-fb-request-id",
            }
        }
        return MetaPage(body=body, data=data, after_cursor=_next_after_cursor(body), usage_headers=usage)


# ---------------------------------------------------------------------------
# internal_validation reads (#112, #101 S6): synchronous, GET only, the
# credentials handed in per call
# ---------------------------------------------------------------------------

GRAPH_API_BASE_URL = "https://graph.facebook.com"
#: ``/act_<id>`` or ``/act_<id>/<edge>``: the only paths a read may name.
_READ_PATH = re.compile(r"/act_[A-Za-z0-9_]+(?:/(?:insights|ads|adsets|campaigns))?")
_REDACTED = "[redacted]"
#: Credentials Meta may echo inside a URL it returns (``paging.next``).
_URL_SECRET = re.compile(r"(access_token|appsecret_proof|client_secret)=[^&\s\"']+", re.IGNORECASE)

#: Usage thresholds and back-off (#112). Meta throttles a caller once a usage
#: header reaches 100 % of its rolling one-hour window, and every header
#: decays continuously over that hour. A sync stops paging at 75 %: one batch
#: is about ten pages, so the headroom covers the page that crossed the line
#: plus other callers of the same app and account. It waits 15 minutes below
#: 90 % and 30 minutes from 90 %, or as long as Meta says regaining access
#: takes when that is longer; a throttling error with no estimate waits the
#: whole window. Every wait is clamped to 5-60 minutes (a Job's delay is
#: capped at an hour).
USAGE_DEFER_THRESHOLD_PCT = 75.0
USAGE_HIGH_PCT = 90.0
BACKOFF_ELEVATED_SECONDS = 15 * 60
BACKOFF_HIGH_SECONDS = 30 * 60
BACKOFF_THROTTLED_SECONDS = 60 * 60
MIN_BACKOFF_SECONDS = 5 * 60
MAX_BACKOFF_SECONDS = 60 * 60

#: Usage headers and the utilisation fields each reports, in percent.
USAGE_HEADERS = (
    "x-app-usage", "x-ad-account-usage", "x-business-use-case-usage", "x-fb-ads-insights-throttle",
)
_UTILISATION_FIELDS = frozenset({"call_count", "total_cputime", "total_time", "acc_id_util_pct", "app_id_util_pct"})

#: Graph error codes (https://developers.facebook.com/docs/graph-api/guides/error-handling).
_AUTH_CODES = frozenset({102, 190})
_THROTTLE_CODES = frozenset({4, 17, 32, 613, *range(80000, 80015)})
_THROTTLE_SUBCODES = frozenset({1504022, 1504039})
_TRANSIENT_CODES = frozenset({1, 2})

#: ``MetaReadError.kind``: what the executor does about it.
#: ``auth`` -- the credentials cannot read (expired, revoked, no permission):
#: the run fails now, no retry helps. ``throttled`` -- wait and resume.
#: ``transient`` -- retry. ``failed`` -- anything else: retry, then fail.
READ_ERROR_KINDS = ("auth", "throttled", "transient", "failed")


class MetaReadCredentials:
    """The plaintext pair one read needs. Never printed, never persisted."""

    __slots__ = ("_access_token", "_app_secret")

    def __init__(self, access_token: str, app_secret: str) -> None:
        if not access_token or not app_secret:
            raise MetaReadError(
                "meta_read_credential_unavailable", "Meta credential material is empty",
                kind="auth", network_attempted=False,
            )
        self._access_token = access_token
        self._app_secret = app_secret

    def __repr__(self) -> str:
        return "MetaReadCredentials(<redacted>)"

    __str__ = __repr__

    @property
    def access_token(self) -> str:
        return self._access_token

    def appsecret_proof(self) -> str:
        return hmac.new(
            self._app_secret.encode("utf-8"), self._access_token.encode("utf-8"), hashlib.sha256,
        ).hexdigest()

    def secrets(self) -> tuple[str, ...]:
        """Every string a response, a message or a log line must not carry."""
        return (self._access_token, self._app_secret, self.appsecret_proof())


def redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """``value`` with every secret, and any credential a URL carries, replaced.

    Walks dicts and lists; only strings change. Applied to everything a read
    keeps: the page payload, Meta's error message, the httpx log line.
    """
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, _REDACTED)
        return _URL_SECRET.sub(lambda match: f"{match.group(1)}={_REDACTED}", value)
    if isinstance(value, Mapping):
        return {key: redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    return value


class MetaReadError(RuntimeError):
    """A read that did not return a page. The message is already scrubbed.

    ``kind`` is one of :data:`READ_ERROR_KINDS`; ``permanent`` (``auth``)
    tells the executor no retry can help. ``retry_after`` is the back-off of
    a ``throttled`` error. ``network_attempted`` is ``False`` when the read
    stopped before any request left the process.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        kind: str,
        http_status: int | None = None,
        retry_after: float | None = None,
        network_attempted: bool = True,
        usage: "MetaUsage | None" = None,
    ) -> None:
        if kind not in READ_ERROR_KINDS:
            raise ValueError(f"unknown Meta read error kind {kind!r}")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.kind = kind
        self.http_status = http_status
        self.retry_after = retry_after
        self.network_attempted = network_attempted
        self.usage = usage

    @property
    def permanent(self) -> bool:
        return self.kind == "auth"

    @property
    def error_category(self) -> str:
        """The outcome's ``error_category`` (spec §3.6)."""
        return {"auth": "auth_failure", "throttled": "rate_limit", "transient": "transient"}.get(
            self.kind, "read_failed",
        )


def _clamp_backoff(seconds: float) -> float:
    return float(max(MIN_BACKOFF_SECONDS, min(MAX_BACKOFF_SECONDS, seconds)))


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class MetaUsage:
    """What Meta's usage headers said about one response.

    ``max_utilisation_pct`` is the highest percentage any header reported
    (``None``: no usage header at all); ``regain_after_seconds`` the longest
    time Meta estimated until a throttled caller regains access;
    ``reported_by`` the headers that carried a reading.
    """

    max_utilisation_pct: float | None = None
    regain_after_seconds: float | None = None
    reported_by: tuple[str, ...] = field(default_factory=tuple)

    def backoff_seconds(self) -> float | None:
        """How long to wait before the next read, or ``None`` to go on."""
        if self.max_utilisation_pct is None or self.max_utilisation_pct < USAGE_DEFER_THRESHOLD_PCT:
            return None
        base = BACKOFF_HIGH_SECONDS if self.max_utilisation_pct >= USAGE_HIGH_PCT else BACKOFF_ELEVATED_SECONDS
        return _clamp_backoff(max(base, self.regain_after_seconds or 0))

    def throttled_backoff_seconds(self, retry_after_header: float | None) -> float:
        """The wait after Meta refused a read for its rate limits."""
        if retry_after_header:
            return _clamp_backoff(retry_after_header)
        return _clamp_backoff(self.regain_after_seconds or BACKOFF_THROTTLED_SECONDS)

    def summary(self) -> dict[str, Any]:
        """The reading an outcome keeps: numbers and header names, no ids."""
        return {
            "max_utilisation_pct": self.max_utilisation_pct,
            "regain_after_seconds": self.regain_after_seconds,
            "reported_by": list(self.reported_by),
            "backoff_seconds": self.backoff_seconds(),
        }


def parse_usage_headers(headers: Mapping[str, str]) -> MetaUsage:
    """Read the four usage headers; a header that does not parse is ignored.

    ``x-app-usage`` and ``x-fb-ads-insights-throttle`` are one object;
    ``x-ad-account-usage`` adds ``reset_time_duration`` (seconds);
    ``x-business-use-case-usage`` maps each business to a list of objects
    with ``estimated_time_to_regain_access`` (minutes).
    """
    lowered = {str(key).lower(): value for key, value in headers.items()}
    peak: float | None = None
    regain: float | None = None
    reported: list[str] = []
    for name in USAGE_HEADERS:
        raw = lowered.get(name)
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            continue
        readings = [item for entries in parsed.values() if isinstance(entries, list) for item in entries] \
            if name == "x-business-use-case-usage" and isinstance(parsed, dict) else [parsed]
        seen = False
        for reading in readings:
            if not isinstance(reading, Mapping):
                continue
            for key, value in reading.items():
                number = _number(value)
                if number is None:
                    continue
                if key in _UTILISATION_FIELDS:
                    peak = number if peak is None else max(peak, number)
                    seen = True
                elif key == "estimated_time_to_regain_access" and number > 0:
                    regain = max(regain or 0.0, number * 60)
                elif key == "reset_time_duration" and number > 0:
                    regain = max(regain or 0.0, number)
        if seen:
            reported.append(name)
    return MetaUsage(max_utilisation_pct=peak, regain_after_seconds=regain, reported_by=tuple(reported))


@dataclass(frozen=True)
class MetaReadResponse:
    """A 2xx JSON object from the Graph API, with its usage reading.

    ``body`` is Meta's response as received; the caller scrubs it before
    keeping any of it (:func:`redact`).
    """

    http_status: int
    body: dict[str, Any]
    usage: MetaUsage


def _read_error(
    status: int, body: Any, headers: Mapping[str, str], usage: MetaUsage, secrets: tuple[str, ...],
) -> MetaReadError:
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        if status < 400:
            return MetaReadError("meta_read_response_invalid", f"Meta answered HTTP {status} without a JSON object",
                                 kind="failed", http_status=status, usage=usage)
        error = {}
    graph_code = _number(error.get("code"))
    subcode = _number(error.get("error_subcode"))
    code_text = "" if graph_code is None else f" (code {int(graph_code)}" + (
        f", subcode {int(subcode)})" if subcode is not None else ")")
    trace = error.get("fbtrace_id")
    message = redact(str(error.get("message") or f"Meta HTTP {status}"), secrets)[:300] + code_text + (
        f" trace {redact(str(trace), secrets)[:64]}" if trace else "")
    if (graph_code is not None and int(graph_code) in _THROTTLE_CODES) or status == 429 or (
            subcode is not None and int(subcode) in _THROTTLE_SUBCODES):
        retry_after = _number(headers.get("retry-after"))
        return MetaReadError("meta_read_rate_limited", message, kind="throttled", http_status=status,
                             retry_after=usage.throttled_backoff_seconds(retry_after), usage=usage)
    if (graph_code is not None and int(graph_code) in _AUTH_CODES) or status == 401:
        return MetaReadError("meta_read_auth_failed", message, kind="auth", http_status=status, usage=usage)
    if (graph_code is not None and (int(graph_code) == 10 or 200 <= int(graph_code) <= 299)) or status == 403:
        return MetaReadError("meta_read_permission_denied", message, kind="auth", http_status=status, usage=usage)
    if (graph_code is not None and int(graph_code) in _TRANSIENT_CODES) or status >= 500 \
            or error.get("is_transient") is True:
        return MetaReadError("meta_read_unavailable", message, kind="transient", http_status=status, usage=usage)
    return MetaReadError("meta_read_failed", message, kind="failed", http_status=status, usage=usage)


class HttpMetaReadGateway:
    """The one way an ``internal_validation`` read reaches Meta: a GET.

    It keeps no credential. :meth:`get` takes the plaintext pair the caller
    just decrypted, sends the token in the ``Authorization`` header and its
    ``appsecret_proof`` as a query parameter, and never follows a redirect.
    Every failure is a :class:`MetaReadError` whose message names no secret.
    ``transport`` exists for tests (``httpx.MockTransport``): nothing else
    may route a read anywhere but the Graph API.
    """

    def __init__(self, *, api_version: str, timeout: float, transport: httpx.BaseTransport | None = None) -> None:
        if not re.fullmatch(r"v[0-9]+\.[0-9]+", api_version or ""):
            raise ValueError(f"not a Graph API version: {api_version!r}")
        self.api_version = api_version
        self.timeout = timeout
        self._transport = transport

    def get(self, path: str, params: Mapping[str, str], credentials: MetaReadCredentials) -> MetaReadResponse:
        if not _READ_PATH.fullmatch(path or ""):
            raise MetaReadError("meta_read_request_invalid", f"not a read path: {path!r}"[:200],
                                kind="failed", network_attempted=False)
        if "access_token" in params or "appsecret_proof" in params:
            raise MetaReadError("meta_read_request_invalid", "credentials are not request parameters",
                                kind="failed", network_attempted=False)
        secrets = credentials.secrets()
        url = f"{GRAPH_API_BASE_URL}/{self.api_version}{path}"
        query = {**{key: str(value) for key, value in params.items()}, "appsecret_proof": credentials.appsecret_proof()}
        try:
            with httpx.Client(timeout=self.timeout, transport=self._transport, follow_redirects=False) as client:
                response = client.get(url, params=query, headers={"Authorization": f"Bearer {credentials.access_token}"})
                status = response.status_code
                headers = {key.lower(): value for key, value in response.headers.items()}
                try:
                    body: Any = response.json()
                except ValueError:
                    body = None
        except httpx.TimeoutException:
            raise MetaReadError("meta_read_unavailable", "the Meta read timed out", kind="transient") from None
        except httpx.HTTPError as exc:
            # The exception's text can carry the URL and so the proof: only its type is kept.
            raise MetaReadError("meta_read_unavailable", f"the Meta read failed ({type(exc).__name__})",
                                kind="transient") from None
        usage = parse_usage_headers(headers)
        if status >= 400 or not isinstance(body, dict) or "error" in body:
            raise _read_error(status, body, headers, usage, secrets)
        return MetaReadResponse(http_status=status, body=body, usage=usage)


class _RedactCredentialsInUrls(logging.Filter):
    """httpx logs each request's URL at INFO; the proof is in its query."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _URL_SECRET.sub(lambda match: f"{match.group(1)}={_REDACTED}", message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


for _logger_name in ("httpx", "httpcore"):
    _logger = logging.getLogger(_logger_name)
    if not any(isinstance(existing, _RedactCredentialsInUrls) for existing in _logger.filters):
        _logger.addFilter(_RedactCredentialsInUrls())
