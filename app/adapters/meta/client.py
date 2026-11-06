"""Small, testable Meta Graph API client restricted to GET requests."""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any, Protocol
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
