"""A simulated Graph API for the ``internal_validation`` read tests (#112).

No network and no real token: :class:`GraphSimulator` is the handler of an
``httpx.MockTransport``. It answers the reads ``HttpMetaReadAdapter`` makes
-- the account node, the ``ads``/``adsets``/``campaigns`` edges and ad-level
``insights`` -- for one fictional account (Acme Charge), pages with Meta's
cursor shape, and, like Meta, puts the caller's access token into every
``paging.next`` URL, so the tests can prove none of it is kept.

Every request is recorded (method, path, query, ``Authorization`` header).
``usage`` gives the usage headers of each answer and ``fault`` can replace
any answer, e.g. an expired token (code 190) or a rate-limit error.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable
from urllib.parse import urlencode

import httpx

from app.adapters.meta.http_read_adapter import read_gateway
from app.adapters.meta.client import HttpMetaReadGateway

AD_ACCOUNT = "act_acme_charge_validation"
CURRENCY = "USD"
TIMEZONE = "America/Los_Angeles"
#: Fictional credentials. Long enough to be found if anything kept them.
ACCESS_TOKEN = "ci-only-meta-access-token-acme-charge-0123456789"
APP_SECRET = "ci-only-meta-app-secret-acme-charge-9876543210"


@dataclass
class RecordedRequest:
    method: str
    path: str
    params: dict[str, str]
    authorization: str | None
    url: str


def _cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"acme-offset:{offset}".encode()).decode().rstrip("=")


def _offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    text = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
    return int(text.removeprefix("acme-offset:"))


@dataclass
class GraphSimulator:
    ads: tuple[str, ...] = ("acme_ad_1", "acme_ad_2", "acme_ad_3")
    ad_account_id: str = AD_ACCOUNT
    currency: str = CURRENCY
    timezone_name: str = TIMEZONE
    usage: Callable[[int, RecordedRequest], dict[str, str]] | None = None
    fault: Callable[[int, RecordedRequest], httpx.Response | None] | None = None
    requests: list[RecordedRequest] = field(default_factory=list)

    # -- transport ----------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def gateway(self) -> HttpMetaReadGateway:
        gateway = read_gateway()
        return HttpMetaReadGateway(api_version=gateway.api_version, timeout=5.0, transport=self.transport())

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        recorded = RecordedRequest(
            method=request.method, path=request.url.path, params=params,
            authorization=request.headers.get("authorization"), url=str(request.url),
        )
        self.requests.append(recorded)
        index = len(self.requests)
        headers = dict(self.usage(index, recorded)) if self.usage else {}
        if self.fault is not None:
            response = self.fault(index, recorded)
            if response is not None:
                response.headers.update(headers)
                return response
        token = (recorded.authorization or "").removeprefix("Bearer ")
        body = self._answer(recorded, token)
        return httpx.Response(200, json=body, headers=headers)

    # -- answers ------------------------------------------------------------

    @property
    def account_number(self) -> str:
        return self.ad_account_id.removeprefix("act_")

    def _answer(self, request: RecordedRequest, token: str) -> dict[str, Any]:
        parts = request.path.split("/")  # ["", "v21.0", "act_...", edge?]
        edge = parts[3] if len(parts) > 3 else None
        if parts[2] != self.ad_account_id:
            return {"error": {"message": "Unsupported get request.", "type": "GraphMethodException", "code": 100}}
        if edge is None:
            return {"id": self.ad_account_id, "account_id": self.account_number,
                    "currency": self.currency, "timezone_name": self.timezone_name}
        rows = self._insights(request.params) if edge == "insights" else self._edge(edge)
        return self._page(request, rows, token)

    def _edge(self, edge: str) -> list[dict[str, Any]]:
        if edge == "ads":
            return [{"id": ad, "account_id": self.account_number, "adset_id": f"set_{ad}",
                     "campaign_id": "cmp_acme_charge", "creative": {"id": f"cr_{ad}"},
                     "status": "ACTIVE", "effective_status": "ACTIVE"} for ad in self.ads]
        if edge == "adsets":
            return [{"id": f"set_{ad}", "campaign_id": "cmp_acme_charge", "status": "ACTIVE",
                     "effective_status": "ACTIVE", "daily_budget": "2500", "optimization_goal": "OFFSITE_CONVERSIONS",
                     "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                     "attribution_spec": [{"event_type": "CLICK_THROUGH", "window_days": 7},
                                          {"event_type": "VIEW_THROUGH", "window_days": 1}],
                     "targeting": {"geo_locations": {"countries": ["US"]}},
                     "learning_stage_info": {"status": "SUCCESS"}} for ad in self.ads]
        if edge == "campaigns":
            return [{"id": "cmp_acme_charge", "status": "ACTIVE", "effective_status": "ACTIVE",
                     "bid_strategy": "LOWEST_COST_WITHOUT_CAP"}]
        raise AssertionError(f"unexpected edge {edge!r}")

    def _insights(self, params: dict[str, str]) -> list[dict[str, Any]]:
        window = json.loads(params["time_range"])
        since, until = date.fromisoformat(window["since"]), date.fromisoformat(window["until"])
        fields = params["fields"].split(",")
        days = [since + timedelta(days=n) for n in range((until - since).days + 1)]
        rows = []
        if params.get("time_increment") == "all_days":
            for ad_index, ad in enumerate(self.ads, start=1):
                impressions = 1000 * ad_index * len(days)
                row = self._row(ad, since, until, ad_index * len(days), fields)
                row.update({"reach": str(impressions // 2), "frequency": "2.000000"})
                rows.append({key: value for key, value in row.items() if key in fields or key in _IDENTITY})
            return rows
        for day in days:
            for ad_index, ad in enumerate(self.ads, start=1):
                rows.append(self._row(ad, day, day, ad_index, fields))
        return rows

    def _row(self, ad: str, since: date, until: date, scale: int, fields: list[str]) -> dict[str, Any]:
        metrics = {
            "impressions": str(1000 * scale), "clicks": str(30 * scale), "inline_link_clicks": str(20 * scale),
            "spend": f"{12 * scale}.50",
            "actions": [{"action_type": "purchase", "value": str(scale)}],
            "action_values": [{"action_type": "purchase", "value": f"{40 * scale}.00"}],
        }
        row = {"account_id": self.account_number, "ad_id": ad,
               "date_start": since.isoformat(), "date_stop": until.isoformat()}
        if "account_currency" in fields:
            row["account_currency"] = self.currency
        row.update({key: value for key, value in metrics.items() if key in fields})
        return row

    def _page(self, request: RecordedRequest, rows: list[dict[str, Any]], token: str) -> dict[str, Any]:
        limit = int(request.params.get("limit") or 25)
        offset = _offset(request.params.get("after"))
        selected = rows[offset:offset + limit]
        body: dict[str, Any] = {"data": selected}
        if not selected:
            return body
        end = offset + len(selected)
        paging: dict[str, Any] = {"cursors": {"before": _cursor(offset), "after": _cursor(end)}}
        query = {key: value for key, value in request.params.items() if key != "appsecret_proof"}
        if end < len(rows):
            # Meta's next link carries the caller's access token.
            paging["next"] = (f"https://graph.facebook.com{request.path}?"
                              + urlencode({**query, "access_token": token, "after": _cursor(end)}))
        if offset:
            paging["previous"] = (f"https://graph.facebook.com{request.path}?"
                                  + urlencode({**query, "access_token": token, "before": _cursor(offset)}))
        body["paging"] = paging
        return body


_IDENTITY = frozenset({"account_id", "ad_id", "date_start", "date_stop", "account_currency"})


def graph_error(status: int, code: int, message: str, *, subcode: int | None = None,
                headers: dict[str, str] | None = None) -> httpx.Response:
    error: dict[str, Any] = {"message": message, "type": "OAuthException", "code": code, "fbtrace_id": "AcmeTrace1"}
    if subcode is not None:
        error["error_subcode"] = subcode
    return httpx.Response(status, json={"error": error}, headers=headers or {})


def business_usage(call_count: float, *, regain_minutes: float = 0) -> dict[str, str]:
    """An ``x-business-use-case-usage`` reading for the fictional business."""
    return {"x-business-use-case-usage": json.dumps({"acme_business": [{
        "type": "ads_insights", "call_count": call_count, "total_cputime": 1, "total_time": 1,
        "estimated_time_to_regain_access": regain_minutes,
    }]})}


# ---------------------------------------------------------------------------
# The platform Workspace's internal_validation connection
# ---------------------------------------------------------------------------


def platform_context(workspace_id: str, *, role: str = "owner") -> "TenantContext":
    """A platform operator acting in the platform Workspace's internal_validation mode."""
    from app.platform.tenancy import TenantContext

    return TenantContext(
        workspace_id=workspace_id, role=role, initiator_id="usr_acme_platform_operator",
        meta_mode="internal_validation", is_platform_workspace=True,
        request_id="req-acme-validation", trace_id="trace-acme-validation",
    )


def provision_connection(context, *, ad_account_id: str = AD_ACCOUNT, access_token: str = ACCESS_TOKEN,
                         app_secret: str = APP_SECRET) -> dict[str, str]:
    """What the platform bootstrap leaves: two sealed credential versions, an
    internal_validation connection and its binding naming both."""
    from app.adapters.persistence.database import tenant_session
    from app.entitlements.credential_admin import CredentialAdmin
    from app.entitlements.credential_protector import LocalKmsAdapter
    from app.meta.connections import create_binding, create_connection

    admin = CredentialAdmin(LocalKmsAdapter.local_only())
    with tenant_session(context) as session:
        token = admin.create(session, context, credential_key="meta_access_token", provider="meta",
                             plaintext=access_token.encode(), display_label="Acme Charge validation token")
        secret = admin.create(session, context, credential_key="meta_app_secret", provider="meta",
                              plaintext=app_secret.encode(), display_label="Acme Charge validation secret")
        connection = create_connection(session, context=context, ad_account_id=ad_account_id)
        binding = create_binding(
            session, context=context, connection_id=connection.id, ad_account_id=ad_account_id,
            access_token_version_id=token.credential_version_id,
            app_secret_version_id=secret.credential_version_id,
        )
        return {
            "connection_id": connection.id,
            "binding_id": binding.id,
            "access_token_version_id": token.credential_version_id,
            "app_secret_version_id": secret.credential_version_id,
        }
