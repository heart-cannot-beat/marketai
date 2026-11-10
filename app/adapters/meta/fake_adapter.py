"""Database-backed FakeMetaAdapter for customer_sandbox and controlled_mock.

All 'write' operations create/update FakeMetaObjectRecord rows in the
local database — no network call is ever made. Read operations query
FakeMetaObjectRecord rows. Idempotency is guaranteed within a scenario
version via deterministic remote_object_id generation.

``read_insights`` (#107) is the read-only ad-level insights read the
scheduled observation sync pages through. It writes nothing: the ads are the
scenario's ``ad`` objects, and every number is a pure function of
``(ad, local day)``, so the same request always returns the same page.
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

from app.adapters.persistence.models import new_id, utcnow
from app.meta.models import FakeMetaObjectRecord


class AdapterFaultTriggered(RuntimeError):
    """Raised when a controlled-mock fault injects uncertainty."""

    def __init__(self, effect: str, object_type: str, idempotency_key: str) -> None:
        self.effect = effect
        self.object_type = object_type
        self.idempotency_key = idempotency_key
        super().__init__(f"Fault triggered: {effect} on {object_type}/{idempotency_key}")


class AdapterReadFault(RuntimeError):
    """Raised when a controlled-mock fault fails an insights read (#107)."""

    def __init__(self, effect: str, page_order: int) -> None:
        self.effect = effect
        self.page_order = page_order
        super().__init__(f"Fault triggered: {effect} on insights page {page_order}")


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
    """One page of an insights read.

    ``payload`` is the Meta-shaped response body (``data`` plus ``paging``)
    the caller stores as raw evidence; ``next_cursor`` is ``None`` on the
    last page.
    """

    payload: dict[str, Any]
    row_count: int
    next_cursor: str | None


#: Insights fields the sandbox can answer. Anything else in a request is
#: left out of the rows rather than invented.
INSIGHTS_FIELDS = frozenset({
    "impressions", "clicks", "inline_link_clicks", "spend", "actions", "action_values",
})
INSIGHTS_PAGE_LIMIT = 25
_CURSOR_PREFIX = "fake-insights-offset:"


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

    # -- read-only insights (#107) -------------------------------------------

    def read_insights(
        self,
        *,
        ad_account_id: str,
        fields: Sequence[str],
        since: date,
        until: date,
        limit: int = INSIGHTS_PAGE_LIMIT,
        after: str | None = None,
    ) -> InsightsPage:
        """One page of ad-level daily insights (``time_increment=1``).

        Rows are ordered by local day, then ad id. A day with no row for an
        ad is a day the ad did not deliver -- the scenario's ads deliver
        every day, and a scenario without ads returns one empty last page.
        Reads only: no row is written, no network is touched.
        """
        if since > until:
            raise InsightsRequestError(f"since {since} is after until {until}")
        if limit < 1:
            raise InsightsRequestError("limit must be positive")
        offset = _decode_cursor(after)
        page_order = offset // limit + 1
        fault = self._match_read_fault(page_order)
        if fault is not None:
            raise AdapterReadFault(str(fault.get("effect")), page_order)

        ads = list(self._session.scalars(
            select(FakeMetaObjectRecord.remote_object_id).where(
                FakeMetaObjectRecord.workspace_id == self._workspace_id,
                FakeMetaObjectRecord.meta_mode == self._meta_mode,
                FakeMetaObjectRecord.scenario_version_id == self._scenario_version_id,
                FakeMetaObjectRecord.object_type == "ad",
            ).order_by(FakeMetaObjectRecord.remote_object_id)
        ))
        days = [since + timedelta(days=n) for n in range((until - since).days + 1)]
        total = len(days) * len(ads)
        if ads and offset >= total:
            raise InsightsRequestError(f"cursor is past the last row ({offset} >= {total})")
        selected = [
            (days[index // len(ads)], ads[index % len(ads)])
            for index in range(offset, min(offset + limit, total))
        ]
        wanted = [field for field in fields if field in INSIGHTS_FIELDS]
        rows = [_insights_row(ad_account_id, ad_id, day, wanted) for day, ad_id in selected]
        next_offset = offset + len(selected)
        next_cursor = _encode_cursor(next_offset) if next_offset < total else None
        paging: dict[str, Any] = {"cursors": {
            "before": _encode_cursor(offset), "after": _encode_cursor(next_offset),
        }}
        if next_cursor is not None:
            paging["next"] = next_cursor
        return InsightsPage(payload={"data": rows, "paging": paging}, row_count=len(rows),
                            next_cursor=next_cursor)

    def _match_read_fault(self, page_order: int) -> dict[str, Any] | None:
        """A controlled-mock rule ``{"operation": "read_insights", "effect":
        "read_error", "match_page": N}`` fails every read of page N. Only
        ``controlled_mock`` adapters carry rules (``resolve_adapter``)."""
        for rule in (self._fault_config or {}).get("rules", []):
            if rule.get("operation") == "read_insights" and rule.get("match_page") == page_order:
                return rule
        return None


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


def _insights_row(ad_account_id: str, ad_id: str, day: date, fields: Sequence[str]) -> dict[str, Any]:
    """Deterministic metrics for one ``(ad, local day)``, as Meta's strings."""
    digest = hashlib.sha256(f"fake-insights|{ad_id}|{day.isoformat()}".encode()).digest()

    def draw(index: int, modulus: int) -> int:
        return int.from_bytes(digest[index * 4:index * 4 + 4], "big") % modulus

    impressions = 500 + draw(0, 4500)
    clicks = draw(1, impressions // 20 + 1)
    link_clicks = draw(2, clicks + 1)
    spend_cents = 500 + draw(3, 9500)
    purchases = draw(4, link_clicks // 5 + 1)
    purchase_value_cents = purchases * (2000 + draw(5, 8000))
    metrics: dict[str, Any] = {
        "impressions": str(impressions),
        "clicks": str(clicks),
        "inline_link_clicks": str(link_clicks),
        "spend": f"{spend_cents // 100}.{spend_cents % 100:02d}",
        "actions": [{"action_type": "purchase", "value": str(purchases)}] if purchases else [],
        "action_values": (
            [{"action_type": "purchase",
              "value": f"{purchase_value_cents // 100}.{purchase_value_cents % 100:02d}"}]
            if purchases else []
        ),
    }
    row: dict[str, Any] = {
        "account_id": ad_account_id,
        "ad_id": ad_id,
        "date_start": day.isoformat(),
        "date_stop": day.isoformat(),
    }
    row.update({field: metrics[field] for field in fields})
    return row
