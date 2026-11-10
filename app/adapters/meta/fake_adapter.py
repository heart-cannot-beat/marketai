"""Database-backed FakeMetaAdapter for customer_sandbox and controlled_mock.

All 'write' operations create/update FakeMetaObjectRecord rows in the
local database — no network call is ever made. Read operations query
FakeMetaObjectRecord rows. Idempotency is guaranteed within a scenario
version via deterministic remote_object_id generation.

``read_insights`` (#107) and ``read_delivery_configuration`` (#101 S2) are
the read-only reads the scheduled observation sync pages through. They write
nothing: the ads are the scenario's ``ad`` objects, and every number and
status is a pure function of ``(ad, local day)`` from
:mod:`app.adapters.meta.sandbox_timeline`, so the same request always returns
the same page. The account's currency and time zone come from the scenario
(``app.meta.scale_read_contracts.sandbox_account_binding``).
"""
from __future__ import annotations

import base64
import hashlib
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Protocol, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.meta import sandbox_timeline as timeline
from app.adapters.persistence.models import new_id, utcnow
from app.meta.models import FakeMetaObjectRecord, ScenarioVersionRecord
from app.meta.scale_read_contracts import AccountBinding, sandbox_account_binding


class AdapterFaultTriggered(RuntimeError):
    """Raised when a controlled-mock fault injects uncertainty."""

    def __init__(self, effect: str, object_type: str, idempotency_key: str) -> None:
        self.effect = effect
        self.object_type = object_type
        self.idempotency_key = idempotency_key
        super().__init__(f"Fault triggered: {effect} on {object_type}/{idempotency_key}")


class AdapterReadFault(RuntimeError):
    """Raised when a controlled-mock fault fails a read (#107, #101 S2)."""

    def __init__(self, effect: str, page_order: int, operation: str = "read_insights") -> None:
        self.effect = effect
        self.page_order = page_order
        self.operation = operation
        super().__init__(f"Fault triggered: {effect} on {operation} page {page_order}")


class InsightsRequestError(ValueError):
    """An insights request the sandbox cannot answer (bad cursor or range)."""


@dataclass(frozen=True)
class FakeAdapterResult:
    remote_object_id: str
    object_type: str
    recovered: bool
    state: dict[str, Any]


@dataclass(frozen=True)
class InsightsPage:
    """One page of a read (insights or a configuration edge).

    ``payload`` is the Meta-shaped response body (``data`` plus ``paging``)
    the caller stores as raw evidence; ``next_cursor`` is ``None`` on the
    last page.
    """

    payload: dict[str, Any]
    row_count: int
    next_cursor: str | None


#: Insights fields the sandbox can answer. Anything else in a request is
#: left out of the rows rather than invented. ``reach`` and ``frequency``
#: only exist for an exact window (``time_increment="all_days"``).
INSIGHTS_FIELDS = frozenset({
    "impressions", "clicks", "inline_link_clicks", "spend", "actions", "action_values",
    "account_currency", "reach", "frequency",
})
_WINDOW_ONLY_FIELDS = frozenset({"reach", "frequency"})
_METRIC_FIELDS = INSIGHTS_FIELDS - {"account_currency"}
INSIGHTS_PAGE_LIMIT = 25
CONFIGURATION_EDGES = frozenset({"account", "ads", "adsets", "campaigns"})
_CURSOR_PREFIX = "fake-insights-offset:"
_ATTRIBUTION_SPEC = [
    {"event_type": "CLICK_THROUGH", "window_days": 7},
    {"event_type": "VIEW_THROUGH", "window_days": 1},
]


class MetaAdapterPort(Protocol):
    is_test_adapter: bool

    def ensure_object(
        self,
        object_type: str,
        idempotency_key: str,
        state: dict[str, Any],
    ) -> FakeAdapterResult: ...

    def read_object(
        self,
        object_type: str,
        remote_object_id: str,
    ) -> dict[str, Any] | None: ...


_TYPE_PREFIX = {
    "campaign": "cmp_fake_",
    "adset": "set_fake_",
    "ad": "ad_fake_",
    "creative": "creative_fake_",
    "image": "img_fake_",
}


class FakeMetaAdapter:
    is_test_adapter = True

    def __init__(
        self,
        session: Session,
        workspace_id: str,
        meta_mode: str,
        scenario_version_id: str,
        *,
        fault_config: dict[str, Any] | None = None,
    ) -> None:
        self._session = session
        self._workspace_id = workspace_id
        self._meta_mode = meta_mode
        self._scenario_version_id = scenario_version_id
        self._fault_config = fault_config or {}

    def _deterministic_id(self, object_type: str, idempotency_key: str) -> str:
        prefix = _TYPE_PREFIX.get(object_type, f"{object_type}_fake_")
        digest = hashlib.sha256(
            f"{self._scenario_version_id}:{object_type}:{idempotency_key}".encode()
        ).hexdigest()[:12]
        return f"{prefix}{digest}"

    def deterministic_object_id(self, object_type: str, idempotency_key: str) -> str:
        """Public, read-only recomputation of the id ``ensure_object`` would use.

        Pure hash arithmetic over ``(scenario_version_id, object_type,
        idempotency_key)`` -- no session access, no write. This is what lets
        Operation recovery (``app.launch.recovery``) ask "does the object this
        write would have created already exist?" via :meth:`read_object`
        without ever calling :meth:`ensure_object` again -- recovery reads
        adapter state, it never re-executes write effects.
        """
        return self._deterministic_id(object_type, idempotency_key)

    def _match_fault(self, object_type: str) -> dict[str, Any] | None:
        """Check if a fault rule matches this ensure_object call.

        Rules use match_ordinal (1-based) among ensure_object calls of the
        same object_type within this scenario version. The ordinal is derived
        from the scenario version's durable objects, not from this instance:
        callers such as the Launch saga resolve a fresh adapter per step.
        Idempotent replays return before this check, so they consume no
        ordinal; nor does a call faulted before apply, which leaves no object
        behind -- retrying its key hits the same rule again.
        """
        rules = (self._fault_config or {}).get("rules", [])
        if not rules:
            return None

        created = self._session.execute(
            select(func.count()).select_from(FakeMetaObjectRecord).where(
                FakeMetaObjectRecord.workspace_id == self._workspace_id,
                FakeMetaObjectRecord.meta_mode == self._meta_mode,
                FakeMetaObjectRecord.scenario_version_id == self._scenario_version_id,
                FakeMetaObjectRecord.object_type == object_type,
            )
        ).scalar_one()
        current = created + 1

        for rule in rules:
            if (
                rule.get("object_type") == object_type
                and rule.get("match_ordinal") == current
            ):
                return rule
        return None

    def ensure_object(
        self,
        object_type: str,
        idempotency_key: str,
        state: dict[str, Any],
    ) -> FakeAdapterResult:
        remote_id = self._deterministic_id(object_type, idempotency_key)

        # Idempotent return for already-created objects
        existing = self._session.execute(
            select(FakeMetaObjectRecord).where(
                FakeMetaObjectRecord.workspace_id == self._workspace_id,
                FakeMetaObjectRecord.meta_mode == self._meta_mode,
                FakeMetaObjectRecord.scenario_version_id == self._scenario_version_id,
                FakeMetaObjectRecord.remote_object_id == remote_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return FakeAdapterResult(
                remote_object_id=remote_id,
                object_type=object_type,
                recovered=True,
                state=existing.state,
            )

        fault = self._match_fault(object_type)
        effect = fault.get("effect") if fault else None

        if effect == "uncertain_before_apply":
            raise AdapterFaultTriggered(effect, object_type, idempotency_key)

        if effect == "pause_before_apply":
            duration_ms = fault.get("duration_ms", 0)
            if not isinstance(duration_ms, (int, float)) or duration_ms <= 0:
                raise ValueError("pause_before_apply requires positive duration_ms")
            time.sleep(duration_ms / 1000.0)

        applied_state = dict(state)
        if effect == "return_state_patch":
            patch = fault.get("state_patch", {})
            if "status" in patch:
                raise ValueError("state_patch may not modify status")
            applied_state.update(patch)

        obj = FakeMetaObjectRecord(
            id=new_id("fmo"),
            workspace_id=self._workspace_id,
            meta_mode=self._meta_mode,
            scenario_version_id=self._scenario_version_id,
            object_type=object_type,
            remote_object_id=remote_id,
            state=applied_state,
        )
        self._session.add(obj)
        self._session.flush()

        if effect == "uncertain_after_apply":
            raise AdapterFaultTriggered(effect, object_type, idempotency_key)

        return FakeAdapterResult(
            remote_object_id=remote_id,
            object_type=object_type,
            recovered=False,
            state=applied_state,
        )

    def read_object(
        self,
        object_type: str,
        remote_object_id: str,
    ) -> dict[str, Any] | None:
        obj = self._session.execute(
            select(FakeMetaObjectRecord).where(
                FakeMetaObjectRecord.workspace_id == self._workspace_id,
                FakeMetaObjectRecord.meta_mode == self._meta_mode,
                FakeMetaObjectRecord.scenario_version_id == self._scenario_version_id,
                FakeMetaObjectRecord.remote_object_id == remote_object_id,
                FakeMetaObjectRecord.object_type == object_type,
            )
        ).scalar_one_or_none()
        return obj.state if obj is not None else None

    # -- read-only reads (#107, #101 S2) -------------------------------------

    def _ads(self) -> list[FakeMetaObjectRecord]:
        return list(self._session.scalars(
            select(FakeMetaObjectRecord).where(
                FakeMetaObjectRecord.workspace_id == self._workspace_id,
                FakeMetaObjectRecord.meta_mode == self._meta_mode,
                FakeMetaObjectRecord.scenario_version_id == self._scenario_version_id,
                FakeMetaObjectRecord.object_type == "ad",
            ).order_by(FakeMetaObjectRecord.remote_object_id)
        ))

    def _object_state(self, object_type: str, remote_object_id: str) -> dict[str, Any]:
        return self.read_object(object_type, remote_object_id) or {}

    def account(self, ad_account_id: str) -> AccountBinding:
        """The simulated account: currency and time zone from the scenario."""
        scenario = self._session.get(ScenarioVersionRecord, self._scenario_version_id)
        return sandbox_account_binding(ad_account_id, scenario.scenario_config if scenario else None)

    def read_insights(
        self,
        *,
        ad_account_id: str,
        fields: Sequence[str],
        since: date,
        until: date,
        limit: int = INSIGHTS_PAGE_LIMIT,
        after: str | None = None,
        time_increment: str = "1",
        breakdowns: Sequence[str] = (),
        action_attribution_windows: Sequence[str] = (),
        action_report_time: str | None = None,
    ) -> InsightsPage:
        """One page of ad-level insights.

        ``time_increment="1"`` gives one row per delivering ``(local day,
        ad)``, ordered by day, then ad id; ``"all_days"`` gives one row per
        ad that delivered in the range. A day without a row is a day the ad
        did not deliver -- the timeline pauses it or it spent nothing -- and a
        read without rows returns one empty last page. ``breakdowns`` splits
        each row into the sandbox's segments; a privacy-suppressed segment
        keeps its segment values and loses every metric. The attribution
        arguments are part of the request identity only: the sandbox's
        purchases are already the contract's. Reads only: no row is written,
        no network is touched.
        """
        if since > until:
            raise InsightsRequestError(f"since {since} is after until {until}")
        if limit < 1:
            raise InsightsRequestError("limit must be positive")
        if time_increment not in ("1", "all_days"):
            raise InsightsRequestError(f"unsupported time_increment {time_increment!r}")
        breakdowns = tuple(breakdowns)
        if breakdowns and breakdowns not in timeline.BREAKDOWN_SEGMENTS:
            raise InsightsRequestError(f"unsupported breakdowns {list(breakdowns)!r}")
        offset = _decode_cursor(after)
        page_order = offset // limit + 1
        fault = self._match_read_fault("read_insights", page_order, {
            "match_time_increment": time_increment, "match_breakdowns": list(breakdowns),
            "match_window_days": (until - since).days + 1,
        })
        if fault is not None:
            raise AdapterReadFault(str(fault.get("effect")), page_order)

        account = self.account(ad_account_id)
        ads = [ad.remote_object_id for ad in self._ads()]
        days = [since + timedelta(days=n) for n in range((until - since).days + 1)]
        wanted = [field for field in fields if field in INSIGHTS_FIELDS]
        if time_increment == "1":
            wanted = [field for field in wanted if field not in _WINDOW_ONLY_FIELDS]
        rows = (
            _window_rows(account, ads, days, wanted) if time_increment == "all_days"
            else _daily_rows(account, ads, days, wanted, breakdowns)
        )
        return _page(rows, offset, limit)

    def read_delivery_configuration(
        self,
        *,
        ad_account_id: str,
        edge: str,
        fields: Sequence[str],
        as_of: date,
        limit: int = INSIGHTS_PAGE_LIMIT,
        after: str | None = None,
    ) -> InsightsPage:
        """One page of one configuration edge, as the account stood on ``as_of``.

        ``account`` is one row with the account's currency and time zone;
        ``ads``, ``adsets`` and ``campaigns`` list the scenario's ads and the
        ad sets and campaigns they belong to. An ad's status follows the
        timeline (``PAUSED`` on its paused day); settings come from the
        object the sandbox Launch wrote, or are drawn per ad when there is
        none. Targeting is returned as the account would return it; the
        parser keeps only its hash. Reads only.
        """
        if edge not in CONFIGURATION_EDGES:
            raise InsightsRequestError(f"unknown configuration edge {edge!r}")
        if limit < 1:
            raise InsightsRequestError("limit must be positive")
        offset = _decode_cursor(after)
        page_order = offset // limit + 1
        fault = self._match_read_fault("read_delivery_configuration", page_order, {"match_edge": edge})
        if fault is not None:
            raise AdapterReadFault(str(fault.get("effect")), page_order, "read_delivery_configuration")
        rows = self._configuration_rows(ad_account_id, edge, as_of)
        wanted = set(fields) | {"id"}
        return _page([{key: value for key, value in row.items() if key in wanted} for row in rows], offset, limit)

    def _configuration_rows(self, ad_account_id: str, edge: str, as_of: date) -> list[dict[str, Any]]:
        account = self.account(ad_account_id)
        if edge == "account":
            return [{"id": ad_account_id, "account_id": _account_number(ad_account_id),
                     "currency": account.currency, "timezone_name": account.timezone_name}]
        ads = []
        for ad in self._ads():
            state = ad.state or {}
            ad_id = ad.remote_object_id
            adset_id = str(state.get("adset_id") or self._deterministic_id("adset", f"sandbox:{ad_id}"))
            adset_state = self._object_state("adset", adset_id)
            campaign_id = str(adset_state.get("campaign_id") or state.get("campaign_id")
                              or self._deterministic_id("campaign", f"sandbox:{adset_id}"))
            creative_id = str((state.get("creative") or {}).get("creative_id")
                              or self._deterministic_id("creative", f"sandbox:{ad_id}"))
            ads.append((ad_id, adset_id, campaign_id, creative_id, adset_state))
        if edge == "ads":
            return [
                {"id": ad_id, "account_id": _account_number(ad_account_id), "adset_id": adset_id,
                 "campaign_id": campaign_id, "creative": {"id": creative_id},
                 "status": timeline.configuration_for_ad(ad_id, as_of)["status"],
                 "effective_status": timeline.configuration_for_ad(ad_id, as_of)["status"]}
                for ad_id, adset_id, campaign_id, creative_id, _state in ads
            ]
        if edge == "adsets":
            rows: dict[str, dict[str, Any]] = {}
            for ad_id, adset_id, campaign_id, _creative, state in ads:
                if adset_id in rows:
                    continue
                drawn = timeline.configuration_for_ad(ad_id, as_of)
                rows[adset_id] = {
                    "id": adset_id, "campaign_id": campaign_id, "status": "ACTIVE", "effective_status": "ACTIVE",
                    "daily_budget": str(state.get("daily_budget") or drawn["daily_budget"]),
                    "optimization_goal": str(state.get("optimization_goal") or "OFFSITE_CONVERSIONS"),
                    "bid_strategy": str(state.get("bid_strategy") or "LOWEST_COST_WITHOUT_CAP"),
                    "attribution_spec": list(_ATTRIBUTION_SPEC),
                    "targeting": state.get("targeting") or {"geo_locations": {"countries": ["US"]}},
                    "learning_stage_info": {"status": drawn["learning_stage"]},
                }
            return [rows[key] for key in sorted(rows)]
        campaigns = sorted({campaign_id for _ad, _adset, campaign_id, _creative, _state in ads})
        return [{"id": campaign_id, "status": "ACTIVE", "effective_status": "ACTIVE",
                 "bid_strategy": "LOWEST_COST_WITHOUT_CAP"} for campaign_id in campaigns]

    def _match_read_fault(self, operation: str, page_order: int, request: dict[str, Any]) -> dict[str, Any] | None:
        """A controlled-mock rule ``{"operation": "read_insights" |
        "read_delivery_configuration", "effect": "read_error", "match_page":
        N}`` fails every read of page N. Optional ``match_time_increment``,
        ``match_breakdowns``, ``match_window_days`` and ``match_edge`` narrow
        it to one query instance's reads. Only ``controlled_mock`` adapters carry rules
        (``resolve_adapter``)."""
        for rule in (self._fault_config or {}).get("rules", []):
            if rule.get("operation") != operation or rule.get("match_page") != page_order:
                continue
            if all(rule[key] == value for key, value in request.items() if key in rule):
                return rule
        return None


def _account_number(ad_account_id: str) -> str:
    """Meta reports an account id without its ``act_`` prefix in rows."""
    return ad_account_id.removeprefix("act_")


def _money(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def _metrics(values: timeline.DayValues) -> dict[str, Any]:
    return {
        "impressions": str(values.impressions),
        "clicks": str(values.clicks),
        "inline_link_clicks": str(values.link_clicks),
        "spend": _money(values.spend_cents),
        "actions": [{"action_type": "purchase", "value": str(values.purchases)}] if values.purchases else [],
        "action_values": (
            [{"action_type": "purchase", "value": _money(values.purchase_value_cents)}]
            if values.purchases else []
        ),
    }


def _row(account: AccountBinding, ad_id: str, since: date, until: date, wanted: Sequence[str],
         metrics: dict[str, Any] | None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "account_id": _account_number(account.ad_account_id),
        "ad_id": ad_id,
        "date_start": since.isoformat(),
        "date_stop": until.isoformat(),
    }
    if "account_currency" in wanted:
        row["account_currency"] = account.currency
    if metrics is not None:
        row.update({field: metrics[field] for field in wanted if field in metrics})
    return row


def _daily_rows(account: AccountBinding, ads: list[str], days: list[date], wanted: list[str],
                breakdowns: tuple[str, ...]) -> list[dict[str, Any]]:
    rows = []
    for day in days:
        for ad_id in ads:
            if not breakdowns:
                values = timeline.day_values(ad_id, day)
                if values is not None:
                    rows.append(_row(account, ad_id, day, day, wanted, _metrics(values)))
                continue
            for segment in timeline.BREAKDOWN_SEGMENTS[breakdowns]:
                values = timeline.segment_values(ad_id, day, breakdowns, segment)
                if values is None:
                    continue
                suppressed = timeline.segment_suppressed(ad_id, day, segment)
                row = _row(account, ad_id, day, day, wanted, None if suppressed else _metrics(values))
                row.update(dict(zip(breakdowns, segment)))
                rows.append(row)
    return rows


def _window_rows(account: AccountBinding, ads: list[str], days: list[date], wanted: list[str]) -> list[dict[str, Any]]:
    rows = []
    for ad_id in ads:
        window = timeline.window_values(ad_id, days)
        if window is None:
            continue
        values, reach = window
        metrics = _metrics(values)
        metrics["reach"] = str(reach)
        metrics["frequency"] = f"{values.impressions / reach:.6f}"
        rows.append(_row(account, ad_id, days[0], days[-1], wanted, metrics))
    return rows


def _page(rows: list[dict[str, Any]], offset: int, limit: int) -> InsightsPage:
    if offset and offset >= len(rows):
        raise InsightsRequestError(f"cursor is past the last row ({offset} >= {len(rows)})")
    selected = rows[offset:offset + limit]
    next_offset = offset + len(selected)
    next_cursor = _encode_cursor(next_offset) if next_offset < len(rows) else None
    paging: dict[str, Any] = {"cursors": {"before": _encode_cursor(offset), "after": _encode_cursor(next_offset)}}
    if next_cursor is not None:
        paging["next"] = next_cursor
    return InsightsPage(payload={"data": selected, "paging": paging}, row_count=len(selected), next_cursor=next_cursor)


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"{_CURSOR_PREFIX}{offset}".encode()).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        text = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise InsightsRequestError("unreadable cursor") from exc
    if not text.startswith(_CURSOR_PREFIX) or not text[len(_CURSOR_PREFIX):].isdigit():
        raise InsightsRequestError("unreadable cursor")
    return int(text[len(_CURSOR_PREFIX):])
