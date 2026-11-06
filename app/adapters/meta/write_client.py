"""Meta write port and guarded HTTP adapter for PAUSED canary creation."""
from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from ... import config


@dataclass(frozen=True)
class RemoteObject:
    id: str
    kind: str
    recovered: bool
    snapshot: dict[str, Any]


class MetaWriteError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class MetaWritePort(Protocol):
    is_test_adapter: bool

    async def preflight(self, preview: dict[str, Any]) -> dict[str, Any]: ...
    async def ensure_step(self, step_key: str, marker: str, request: dict[str, Any]) -> RemoteObject: ...
    async def verify_hierarchy(self, remote_ids: dict[str, str]) -> dict[str, Any]: ...


class HttpMetaWriteAdapter:
    """True external adapter. It refuses to initialize unless the kill switch is explicitly open."""

    is_test_adapter = False
    _EDGE = {
        "campaign_created": "campaigns", "adset_created": "adsets",
        "image_uploaded": "adimages", "creative_created": "adcreatives", "ad_created": "ads",
    }

    def __init__(self) -> None:
        if not config.meta_write_ready():
            raise MetaWriteError("meta_write_disabled", "Meta 写入未显式启用或配置不完整", retryable=False)

    def _proof(self) -> str:
        return hmac.new(
            config.META_APP_SECRET.encode(), config.META_WRITE_ACCESS_TOKEN.encode(), hashlib.sha256
        ).hexdigest()

    async def _request(self, method: str, path: str, *, params: dict | None = None,
                       data: dict | None = None, files: dict | None = None) -> dict:
        url = f"https://graph.facebook.com/{config.META_API_VERSION}/{path.lstrip('/')}"
        headers = {"Authorization": f"Bearer {config.META_WRITE_ACCESS_TOKEN}"}
        values = dict(params or data or {})
        values["appsecret_proof"] = self._proof()
        async with httpx.AsyncClient(timeout=config.META_HTTP_TIMEOUT) as client:
            response = await client.request(
                method, url, headers=headers, params=values if method == "GET" else None,
                data=values if method != "GET" else None, files=files,
            )
        try:
            body = response.json()
        except ValueError:
            body = {"error": {"message": "Meta 返回非 JSON"}}
        if response.status_code >= 400 or "error" in body:
            error = body.get("error") or {}
            graph_code = str(error.get("code") or response.status_code)
            retryable = response.status_code == 429 or response.status_code >= 500 or graph_code in {
                "1", "2", "4", "17", "32", "613",
            }
            raise MetaWriteError(f"meta_{graph_code}", str(error.get("message") or "Meta write failed"),
                                 retryable=retryable)
        return body

    async def _find_named(self, edge: str, marker: str) -> dict | None:
        after = None
        fields = {
            "campaigns": "id,name,status,configured_status,effective_status",
            "adsets": "id,name,status,configured_status,effective_status",
            "ads": "id,name,status,configured_status,effective_status",
            "adcreatives": "id,name",
        }[edge]
        for _ in range(100):
            params = {"fields": fields, "limit": 100}
            if after:
                params["after"] = after
            body = await self._request("GET", f"{config.META_AD_ACCOUNT_ID}/{edge}", params=params)
            for item in body.get("data") or []:
                if item.get("name") == marker:
                    return item
            after = ((body.get("paging") or {}).get("cursors") or {}).get("after")
            if not after:
                return None
        raise MetaWriteError("pagination_guard", "Meta 对账分页超过 100 页", retryable=False)

    @staticmethod
    def _assert_paused(kind: str, snapshot: dict) -> None:
        if kind in {"campaign", "adset", "ad"}:
            status = str(snapshot.get("configured_status") or snapshot.get("status") or "").upper()
            if status != "PAUSED":
                raise MetaWriteError("remote_not_paused", f"远端 {kind} 不是 PAUSED", retryable=False)

    async def _read_object(self, object_id: str, kind: str) -> dict:
        fields = {
            "campaign": "id,name,status,configured_status,effective_status",
            "adset": "id,name,status,configured_status,effective_status,campaign_id",
            "ad": "id,name,status,configured_status,effective_status,adset_id,campaign_id,creative",
        }[kind]
        snapshot = await self._request(
            "GET", object_id,
            params={"fields": fields},
        )
        self._assert_paused(kind, snapshot)
        return snapshot

    async def preflight(self, preview: dict[str, Any]) -> dict[str, Any]:
        account = await self._request(
            "GET", config.META_AD_ACCOUNT_ID, params={"fields": "id,currency,timezone_name"}
        )
        expected = preview["account"]
        if str(account.get("currency") or "").upper() != expected["currency"]:
            raise MetaWriteError("account_currency_drift", "账户币种与 Preview 不一致", retryable=False)
        if str(account.get("timezone_name") or "") != expected["timezone"]:
            raise MetaWriteError("account_timezone_drift", "账户时区与 Preview 不一致", retryable=False)
        page = await self._request(
            "GET", preview["creative"]["page_id"], params={"fields": "id,name"}
        )
        if str(page.get("id") or "") != preview["creative"]["page_id"]:
            raise MetaWriteError("page_permission_failed", "Page 权限探针失败", retryable=False)
        return {"account": account, "page": page}

    async def ensure_step(self, step_key: str, marker: str, request: dict[str, Any]) -> RemoteObject:
        if not config.META_WRITE_ENABLED:
            raise MetaWriteError("kill_switch_closed", "Meta 写入 kill switch 已关闭", retryable=False)
        edge = self._EDGE[step_key]
        # Ad images are content-addressed by Meta's returned hash. Re-uploading
        # identical bytes is safe and does not create a deliverable object.
        found = None if step_key == "image_uploaded" else await self._find_named(edge, marker)
        kind = step_key.removesuffix("_created").removesuffix("_uploaded")
        if found:
            object_id = str(found.get("id") or found.get("hash") or "")
            if step_key != "image_uploaded":
                found = await self._read_object(object_id, kind)
            return RemoteObject(object_id, kind, True, found)

        payload = dict(request)
        if step_key in {"campaign_created", "adset_created", "ad_created"}:
            payload["status"] = "PAUSED"
        if step_key == "image_uploaded":
            path = Path(str(payload.pop("asset_path")))
            payload.pop("content_sha256", None)
            if not path.is_file():
                raise MetaWriteError("asset_missing", "待上传 AssetVersion 文件不存在", retryable=False)
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            body = await self._request(
                "POST", f"{config.META_AD_ACCOUNT_ID}/{edge}", data={},
                files={"filename": (marker, path.read_bytes(), mime)},
            )
            images = body.get("images") or {}
            first = next(iter(images.values()), body)
            object_id = str(first.get("hash") or first.get("id") or "")
            return RemoteObject(object_id, "image", False, first)

        encoded = {
            key: json.dumps(value, ensure_ascii=False, separators=(",", ":")) if isinstance(value, (dict, list)) else value
            for key, value in payload.items()
        }
        encoded["name"] = marker
        body = await self._request("POST", f"{config.META_AD_ACCOUNT_ID}/{edge}", data=encoded)
        object_id = str(body.get("id") or "")
        if not object_id:
            raise MetaWriteError("missing_remote_id", f"Meta {step_key} 未返回 id", retryable=False)
        snapshot = await self._read_object(object_id, kind) if step_key != "creative_created" else body
        return RemoteObject(object_id, kind, False, snapshot)

    async def verify_hierarchy(self, remote_ids: dict[str, str]) -> dict[str, Any]:
        snapshots = {}
        for key, kind in (("campaign_id", "campaign"), ("adset_id", "adset"), ("ad_id", "ad")):
            snapshots[key] = await self._read_object(remote_ids[key], kind)
        if str(snapshots["adset_id"].get("campaign_id") or "") != remote_ids["campaign_id"]:
            raise MetaWriteError("hierarchy_mismatch", "AdSet 不属于预期 Campaign", retryable=False)
        if str(snapshots["ad_id"].get("adset_id") or "") != remote_ids["adset_id"]:
            raise MetaWriteError("hierarchy_mismatch", "Ad 不属于预期 AdSet", retryable=False)
        creative = snapshots["ad_id"].get("creative") or {}
        if str(creative.get("id") or "") != remote_ids["creative_id"]:
            raise MetaWriteError("hierarchy_mismatch", "Ad 不引用预期 Creative", retryable=False)
        return snapshots
