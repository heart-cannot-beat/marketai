"""Versioned, immutable read contracts for Meta Marketing API ingestion."""
from __future__ import annotations

import hashlib
import json
from datetime import date

from .. import config


ACCOUNT_FIELDS = ["id", "name", "currency", "timezone_name"]
STATUS_FIELDS = ["id", "name", "status", "effective_status", "updated_time"]
INSIGHT_FIELDS = [
    "ad_id",
    "ad_name",
    "adset_id",
    "adset_name",
    "spend",
    "impressions",
    "reach",
    "frequency",
    "inline_link_clicks",
    "inline_link_click_ctr",
    "cpc",
    "cpm",
    "actions",
    "action_values",
    "purchase_roas",
    "attribution_setting",
    "date_start",
    "date_stop",
]
INGESTION_KINDS = ("account", "insights", "ads", "adsets")
ACTION_ATTRIBUTION_WINDOWS = ("7d_click", "1d_view")


def metric_contract() -> dict:
    """Everything that can change the meaning of a metric row."""
    return {
        "schema_version": 2,
        "api_version": config.META_API_VERSION,
        "level": "ad",
        "time_increment": 1,
        "action_report_time": "conversion",
        "action_attribution_windows": list(ACTION_ATTRIBUTION_WINDOWS),
        "purchase_action_type": config.META_PURCHASE_ACTION_TYPE,
        "fields": INSIGHT_FIELDS,
    }


def contract_hash(contract: dict | None = None) -> str:
    raw = json.dumps(contract or metric_contract(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def request_snapshot(kind: str, date_start: date, date_stop: date) -> dict:
    """Return a token-free request snapshot safe to persist and replay."""
    account = config.META_AD_ACCOUNT_ID
    common = {"limit": config.META_PAGE_LIMIT}
    if kind == "account":
        return {"path": account, "params": {"fields": ",".join(ACCOUNT_FIELDS)}}
    if kind in {"ads", "adsets"}:
        return {"path": f"{account}/{kind}", "params": {**common, "fields": ",".join(STATUS_FIELDS)}}
    if kind == "insights":
        contract = metric_contract()
        return {
            "path": f"{account}/insights",
            "params": {
                **common,
                "fields": ",".join(INSIGHT_FIELDS),
                "level": contract["level"],
                "time_increment": contract["time_increment"],
                "action_report_time": contract["action_report_time"],
                "action_attribution_windows": json.dumps(contract["action_attribution_windows"]),
                "time_range": json.dumps({"since": date_start.isoformat(), "until": date_stop.isoformat()}),
            },
        }
    raise ValueError(f"未知 Meta ingestion kind: {kind}")
