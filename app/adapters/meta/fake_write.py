"""In-memory Meta write adapter used for Saga fault injection."""
from __future__ import annotations

from typing import Any

from .write_client import MetaWriteError, RemoteObject


class FakeMetaWriteAdapter:
    is_test_adapter = True

    def __init__(self, remote_state: dict | None = None, *, crash_after_create_once: str | None = None) -> None:
        self.remote_state = remote_state if remote_state is not None else {}
        self.crash_after_create_once = crash_after_create_once
        self.calls: list[str] = []

    async def preflight(self, preview: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("preflight")
        return {"account": preview["account"], "page": {"id": preview["creative"]["page_id"]}}

    async def ensure_step(self, step_key: str, marker: str, request: dict[str, Any]) -> RemoteObject:
        self.calls.append(step_key)
        status = request.get("status")
        if step_key in {"campaign_created", "adset_created", "ad_created"} and status != "PAUSED":
            raise MetaWriteError("unsafe_status", f"{step_key} must be PAUSED", retryable=False)
        key = (step_key, marker)
        if key in self.remote_state:
            item = self.remote_state[key]
            return RemoteObject(item["id"], item["kind"], True, dict(item))
        prefix = {
            "campaign_created": "cmp", "adset_created": "set", "image_uploaded": "img",
            "creative_created": "creative", "ad_created": "ad",
        }[step_key]
        item = {
            "id": f"{prefix}_{len(self.remote_state) + 1}",
            "kind": prefix, "name": marker,
            "configured_status": status or "N/A", "effective_status": status or "N/A",
            "request": dict(request),
        }
        self.remote_state[key] = item
        if self.crash_after_create_once == step_key:
            self.crash_after_create_once = None
            raise MetaWriteError("injected_after_remote_commit", "fault after remote commit", retryable=True)
        return RemoteObject(item["id"], item["kind"], False, dict(item))

    async def verify_hierarchy(self, remote_ids: dict[str, str]) -> dict[str, Any]:
        statuses = [item.get("configured_status") for item in self.remote_state.values()
                    if item.get("configured_status") != "N/A"]
        if any(status != "PAUSED" for status in statuses):
            raise MetaWriteError("remote_not_paused", "found non-PAUSED object", retryable=False)
        return {"remote_ids": dict(remote_ids), "all_delivery_objects_paused": True}
