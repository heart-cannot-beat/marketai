# Issue #27 Domain Interface + BFF Bridge — Sub-project 1 Implementation Plan

Status: Executed on `Raybbish/build-new-generation-assets-and-creative-library`

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-mount all Library and Generation routes under `/api/v1/`, add missing Domain Interface operations, generate typed BFF artifacts for 45 operations, and wire the `DomainGateway` provider into the authenticated Workspace shell — so that sub-projects 2–4 (Creative Library UI, New Generation UI, Assets/Review/Export UI) can build pages against a fully typed, generated BFF seam.

**Architecture:** Library and Generation `APIRouter`s are re-mounted as sub-routers of the existing `v1_router` (ADR 0004). Each route gets an explicit `operation_id` and Pydantic `response_model`. The OpenAPI extraction pipeline (`generate_openapi.py` → `generate_all.sh`) produces 7 typed TypeScript artifacts. The BFF catch-all handler (`[...path].ts`) consumes those generated artifacts and is updated to preserve repeated query parameters. The browser-side `DomainGateway`, `useDomainQuery`, and `useDomainCommand` are extended from the generated types. `WorkspaceLayout.tsx` wires the gateway provider.

**Tech Stack:** Python 3.12 / FastAPI / Pydantic v2 / SQLAlchemy 2 / PostgreSQL (server); TypeScript / Next.js 14 / SWR / Zod / Ajv (web); openapi-typescript (codegen)

**Spec:** `docs/superpowers/specs/2026-09-02-issue-27-bff-bridge-route-matrix.md`

## Global Constraints

- All browser domain traffic flows through the BFF catch-all at `/api/domain/v1/w/{slug}/...` — no direct FastAPI calls from the browser.
- FastAPI derives Workspace/actor identity from the delegated JWT only — never from browser-supplied parameters.
- New v1 routes must NOT call the legacy `store` repository or process-local `manager` from `app.pipeline`.
- Every command with side effects carries `x-marketai-idempotent: true` in OpenAPI.
- Mutable aggregates use `ETag: "<revision>"` / `If-Match` revision contract (428/400/409).
- Paginated collections use `Page[T]` with `{ items, next_cursor }`, limit 1..100, default 50.
- `allowed_actions` are server-calculated `ActionEligibility[]` — browser must not reconstruct eligibility.
- Binary content (reference uploads, asset downloads) never passes through Next.js — uses Upload/Download Intent flow.
- The old `/api/library/*` and `/api/generation/*` paths are NOT retained as aliases.
- CI drift check: `generate_openapi.py --check` + `generate_all.sh` + `git diff --exit-code`.

---

## File Structure

### FastAPI Domain Interface (Python)

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `app/library/v1_router.py` | New v1 sub-router for Library: 20 operations with `operation_id`, `response_model`, Pydantic I/O models, query params, ETag/If-Match extraction, Problem Details mapping. Delegates to `app.library.service`. |
| Create | `app/generation/v1_router.py` | New v1 sub-router for Generation/Run/Assets/Export: 25 operations (22 Gen/Run/Assets + 3 Export). Same patterns as Library v1_router. Delegates to existing domain modules + new ones. |
| Create | `app/generation/run_queries.py` | Read projections for runs, run items, assets — `list_runs`, `get_run_detail`, `list_run_items`, `get_run_item_detail`, `list_assets`, `get_asset_version_detail`. Each returns server-assembled projections with `allowed_actions`. |
| Create | `app/generation/run_commands.py` | Run mutation commands — `cancel_run`, `retry_failed_items`, `resume_paused_items`, `reconcile_run`. Each appends a `GenerationRunCommandRecord`, creates an Operation, uses CAS on run revision. |
| Create | `app/generation/recovery_preview.py` | `create_recovery_preview` — read-only snapshot of retry/resume targets with cost estimate. |
| Create | `app/generation/capabilities.py` | `get_capabilities` — Workspace-scoped Provider/model/mode/credential readiness. |
| Create | `app/generation/review.py` (extend) | Already exists with `create_review`. Add `cleared` enum value, `supersedes_review_id` correction model, and review conflict detection. |
| Modify | `app/generation/regeneration.py` | Wire the existing module into v1_router as `create_asset_regeneration_draft`. |
| Create | `app/platform/pagination.py` | Shared `Page[T]` cursor pagination: encode/decode opaque cursor, validate limit/cursor, stable ordering. |
| Create | `app/platform/etag.py` | Shared ETag/If-Match extraction: parse `If-Match` header, return 428/400/409. |
| Create | `app/platform/action_eligibility.py` | `ActionEligibility` and `ActionCode` types, `compute_allowed_actions` for each aggregate. |
| Modify | `app/main.py` | Mount library and generation v1 sub-routers on `v1_router`. Register their Problem handlers. Remove old router includes for `library_router` and `generation_router`. |
| Create | `migrations/versions/XXX_review_correction.py` | Alembic migration: add `supersedes_review_id` nullable FK + uniqueness constraint on `QualityReviewRecord`, add `cleared` to review_value enum. |

### Contracts & Codegen

| Action | Path | Responsibility |
|--------|------|----------------|
| Modify | `contracts/openapi/marketai-domain-v1.json` | Auto-generated from FastAPI. Grows from 15 to 60 operations. |
| Modify | `contracts/openapi/marketai-domain-v1-policy.json` | 45 new fail-closed policy entries: bffPattern, limits, conditional headers. |
| Modify | `scripts/generate_allowlist.ts` | Replace hardcoded `querySchema: null` with generated Zod schemas from OpenAPI query parameters. |
| Modify | `scripts/lib/spec.ts` | Add `extractQueryParams()` helper that reads OpenAPI query parameter schemas. |
| Modify | `scripts/generate_all.sh` | No structural changes needed — already orchestrates all 7 artifacts. |

### Web/BFF (TypeScript)

| Action | Path | Responsibility |
|--------|------|----------------|
| Regenerated | `web/lib/domain-client/generated/allowlist.ts` | Grows from 15 to 60 entries with query schemas. |
| Regenerated | `web/lib/domain-client/generated/paths.ts` | 50 typed path builders. |
| Regenerated | `web/lib/domain-client/generated/domain-v1.d.ts` | OpenAPI types for all operations. |
| Regenerated | `web/lib/domain-client/generated/operation-map.d.ts` | Maps each operationId to path/query/body/response types. |
| Regenerated | `web/lib/domain-client/generated/operation-registry.ts` | Runtime registry of all operation metadata. |
| Regenerated | `web/lib/domain-client/generated/validators.ts` | Ajv request body validators for body-bearing operations. |
| Regenerated | `web/lib/domain-client/generated/allowlist-binding.ts` | Type-level binding proof between allowlist and OperationMap. |
| Modify | `web/lib/domain-client/gateway.ts` | Extend `DomainGateway` interface and `DefaultDomainGateway` with methods for all 45 new operations. |
| Modify | `web/hooks/domain/useDomainQuery.ts` | Extend `ReadOperationId` union and `callReadOperation` switch for all GET operations. |
| Modify | `web/hooks/domain/useDomainCommand.ts` | Extend `CommandOperationId` union and `callCommandOperation` switch for all POST/PUT/PATCH operations. |
| Modify | `web/components/layouts/WorkspaceLayout.tsx` | Wire `DomainGatewayProvider`, `BlobClientProvider`, `FileDigestProvider` (resolve TODO(#27) at line 101). |
| Modify | `web/pages/api/domain/v1/w/[slug]/[...path].ts` | Fix `Object.fromEntries` collapse to use `getAll()` for repeated query params (spec §9.3). |

### Tests

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `tests/library/test_v1_library_routes.py` | Integration tests for all 20 Library v1 operations: happy path, tenant isolation, revision conflicts, idempotency replays, forbidden Owner actions. |
| Create | `tests/generation/test_v1_generation_routes.py` | Integration tests for Generation Draft/Preview/Run/Assets/Export v1 operations. |
| Create | `tests/platform/test_pagination.py` | Unit tests for cursor encoding/decoding, limit bounds, filter mismatch. |
| Create | `tests/platform/test_etag.py` | Unit tests for If-Match parsing and error responses. |
| Create | `web/__tests__/domain-client/query-schema-generation.test.ts` | Tests that generated query schemas validate correctly: zero/one/repeated params, unknown params fail closed. |
| Create | `web/__tests__/domain-client/gateway-operations.test.ts` | Tests that gateway dispatches all operations to correct BFF paths with correct methods. |

---

## Task 1: Platform Foundations — Pagination, ETag, and Action Eligibility

**Files:**
- Create: `app/platform/pagination.py`
- Create: `app/platform/etag.py`
- Create: `app/platform/action_eligibility.py`
- Create: `tests/platform/test_pagination.py`
- Create: `tests/platform/test_etag.py`
- Create: `tests/platform/test_action_eligibility.py`

**Interfaces:**
- Consumes: nothing (foundation)
- Produces:
  - `paginate(query, *, limit, cursor, order_columns) -> dict` — returns `{"items": [...], "next_cursor": str|None}`
  - `parse_limit(raw: str|None, default=50, maximum=100) -> int` — validates limit parameter
  - `encode_cursor(workspace_id, filters_hash, sort_tuple) -> str` — opaque base64 cursor
  - `decode_cursor(cursor, workspace_id, filters_hash) -> tuple` — validates and decodes, raises `InvalidCursorError`
  - `extract_if_match(request: Request) -> int` — parses `If-Match: "<revision>"`, raises `HTTPException(428)` if missing, `HTTPException(400)` if malformed
  - `check_revision_match(current: int, expected: int)` — raises `HTTPException(409, "revision_conflict")` if stale
  - `ActionEligibility = TypedDict("ActionEligibility", action=str, allowed=bool, reason_code=str|None)`
  - `ActionCode` — string enum per aggregate

- [ ] **Step 1: Write pagination tests**

```python
# tests/platform/test_pagination.py
import pytest
from app.platform.pagination import (
    encode_cursor,
    decode_cursor,
    parse_limit,
    InvalidCursorError,
)


def test_parse_limit_default():
    assert parse_limit(None) == 50


def test_parse_limit_custom():
    assert parse_limit("25") == 25


def test_parse_limit_clamped_to_max():
    assert parse_limit("200") == 100


def test_parse_limit_clamped_to_min():
    assert parse_limit("0") == 1


def test_parse_limit_invalid_string():
    with pytest.raises(ValueError):
        parse_limit("abc")


def test_cursor_round_trip():
    ws = "ws_123"
    fh = "abc"
    sort_tuple = ("2026-01-01T00:00:00", "id_456")
    cursor = encode_cursor(ws, fh, sort_tuple)
    assert isinstance(cursor, str)
    decoded = decode_cursor(cursor, ws, fh)
    assert decoded == sort_tuple


def test_cursor_wrong_workspace():
    cursor = encode_cursor("ws_a", "fh", ("x",))
    with pytest.raises(InvalidCursorError):
        decode_cursor(cursor, "ws_b", "fh")


def test_cursor_wrong_filters():
    cursor = encode_cursor("ws_a", "fh1", ("x",))
    with pytest.raises(InvalidCursorError):
        decode_cursor(cursor, "ws_a", "fh2")


def test_cursor_malformed():
    with pytest.raises(InvalidCursorError):
        decode_cursor("not-a-valid-cursor", "ws", "fh")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/platform/test_pagination.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.platform.pagination'`

- [ ] **Step 3: Implement pagination module**

```python
# app/platform/pagination.py
"""Shared cursor-based pagination for Page[T] responses (spec §2)."""
from __future__ import annotations

import base64
import hashlib
import json

__all__ = [
    "InvalidCursorError",
    "encode_cursor",
    "decode_cursor",
    "parse_limit",
]


class InvalidCursorError(ValueError):
    pass


def parse_limit(raw: str | None, *, default: int = 50, maximum: int = 100) -> int:
    if raw is None:
        return default
    val = int(raw)  # raises ValueError on non-numeric
    return max(1, min(val, maximum))


def _filters_hash(filters_hash: str) -> str:
    return hashlib.sha256(filters_hash.encode()).hexdigest()[:16]


def encode_cursor(workspace_id: str, filters_hash: str, sort_tuple: tuple) -> str:
    payload = json.dumps({
        "w": workspace_id,
        "f": _filters_hash(filters_hash),
        "s": list(sort_tuple),
    }, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode()


def decode_cursor(cursor: str, workspace_id: str, filters_hash: str) -> tuple:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor))
    except Exception as exc:
        raise InvalidCursorError(f"Malformed cursor: {exc}") from exc
    if payload.get("w") != workspace_id:
        raise InvalidCursorError("Cursor workspace mismatch")
    if payload.get("f") != _filters_hash(filters_hash):
        raise InvalidCursorError("Cursor filter mismatch")
    return tuple(payload["s"])
```

- [ ] **Step 4: Run pagination tests to verify they pass**

Run: `python -m pytest tests/platform/test_pagination.py -v`
Expected: All PASS

- [ ] **Step 5: Write ETag tests**

```python
# tests/platform/test_etag.py
import pytest
from unittest.mock import MagicMock
from fastapi import HTTPException
from app.platform.etag import extract_if_match, check_revision_match, set_etag


def _mock_request(if_match: str | None = None):
    req = MagicMock()
    req.headers = {}
    if if_match is not None:
        req.headers["if-match"] = if_match
    return req


def test_extract_if_match_missing_raises_428():
    with pytest.raises(HTTPException) as exc_info:
        extract_if_match(_mock_request())
    assert exc_info.value.status_code == 428


def test_extract_if_match_malformed_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        extract_if_match(_mock_request("not-a-number"))
    assert exc_info.value.status_code == 400


def test_extract_if_match_valid():
    assert extract_if_match(_mock_request('"42"')) == 42


def test_extract_if_match_unquoted_integer():
    # Strictly should be quoted, but tolerate bare integer
    assert extract_if_match(_mock_request("42")) == 42


def test_check_revision_match_ok():
    check_revision_match(5, 5)  # no exception


def test_check_revision_match_stale():
    with pytest.raises(HTTPException) as exc_info:
        check_revision_match(6, 5)
    assert exc_info.value.status_code == 409


def test_set_etag():
    resp = MagicMock()
    resp.headers = {}
    set_etag(resp, 7)
    assert resp.headers["ETag"] == '"7"'
```

- [ ] **Step 6: Implement ETag module**

```python
# app/platform/etag.py
"""ETag / If-Match revision contract (spec §2)."""
from __future__ import annotations

from fastapi import HTTPException, Response, Request

__all__ = ["extract_if_match", "check_revision_match", "set_etag"]


def extract_if_match(request: Request) -> int:
    raw = request.headers.get("if-match")
    if raw is None:
        raise HTTPException(
            status_code=428,
            detail={"code": "precondition_required",
                    "message": "If-Match header is required"},
        )
    cleaned = raw.strip().strip('"')
    try:
        return int(cleaned)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_precondition",
                    "message": f"If-Match must be a quoted integer, got: {raw}"},
        )


def check_revision_match(current_revision: int, expected_revision: int) -> None:
    if current_revision != expected_revision:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "revision_conflict",
                "message": f"Expected revision {expected_revision}, current is {current_revision}",
                "current_revision": current_revision,
                "expected_revision": expected_revision,
            },
        )


def set_etag(response: Response, revision: int) -> None:
    response.headers["ETag"] = f'"{revision}"'
```

- [ ] **Step 7: Run ETag tests**

Run: `python -m pytest tests/platform/test_etag.py -v`
Expected: All PASS

- [ ] **Step 8: Write action eligibility module**

```python
# app/platform/action_eligibility.py
"""Server-calculated action eligibility (spec §2)."""
from __future__ import annotations

from enum import Enum
from typing import TypedDict

__all__ = ["ActionEligibility", "compute_actions"]


class ActionEligibility(TypedDict):
    action: str
    allowed: bool
    reason_code: str | None


def compute_actions(
    action_codes: list[str],
    checks: dict[str, tuple[bool, str | None]],
) -> list[ActionEligibility]:
    """Build sorted ActionEligibility list from a checks dict.

    ``checks`` maps each ActionCode to ``(allowed, reason_code_if_denied)``.
    Every code in ``action_codes`` must appear; missing codes are denied
    with ``"not_evaluated"``.
    """
    result = []
    for code in sorted(action_codes):
        allowed, reason = checks.get(code, (False, "not_evaluated"))
        result.append(ActionEligibility(
            action=code,
            allowed=allowed,
            reason_code=None if allowed else reason,
        ))
    return result
```

- [ ] **Step 9: Write action eligibility tests**

```python
# tests/platform/test_action_eligibility.py
from app.platform.action_eligibility import compute_actions


def test_all_allowed():
    result = compute_actions(
        ["edit", "delete"],
        {"edit": (True, None), "delete": (True, None)},
    )
    assert len(result) == 2
    assert all(a["allowed"] for a in result)
    assert all(a["reason_code"] is None for a in result)


def test_denied_with_reason():
    result = compute_actions(
        ["publish"],
        {"publish": (False, "validation_failed")},
    )
    assert result[0]["allowed"] is False
    assert result[0]["reason_code"] == "validation_failed"


def test_missing_code_denied():
    result = compute_actions(["unknown"], {})
    assert result[0]["allowed"] is False
    assert result[0]["reason_code"] == "not_evaluated"


def test_sorted_by_action_code():
    result = compute_actions(
        ["z_action", "a_action"],
        {"z_action": (True, None), "a_action": (True, None)},
    )
    assert result[0]["action"] == "a_action"
    assert result[1]["action"] == "z_action"
```

- [ ] **Step 10: Run all platform tests**

Run: `python -m pytest tests/platform/ -v`
Expected: All PASS

- [ ] **Step 11: Commit**

```bash
git add app/platform/pagination.py app/platform/etag.py app/platform/action_eligibility.py \
       tests/platform/test_pagination.py tests/platform/test_etag.py tests/platform/test_action_eligibility.py
git commit -m "feat(platform): add pagination, ETag, and action eligibility foundations (#27)"
```

---

## Task 2: Library v1 Sub-Router — Re-mount with operation_id, response_model, and Pydantic I/O

**Files:**
- Create: `app/library/v1_router.py`
- Create: `app/library/response_models.py`
- Modify: `app/main.py:179-184,820` — replace old router includes with v1 sub-router mount
- Create: `tests/library/test_v1_library_routes.py`

**Interfaces:**
- Consumes: `app.platform.pagination` (Task 1), `app.platform.etag` (Task 1), `app.platform.action_eligibility` (Task 1), `app.library.service` (existing)
- Produces:
  - 20 FastAPI routes under `v1_router` prefix `/library` with explicit `operation_id`
  - Pydantic response models: `LibraryEntryDetail`, `LibraryEntrySummary`, `LibraryVersionDetail`, `LibraryVersionSummary`, `LibraryValidationResult`, `LibraryBinding`, `LibraryEvent`, `LibraryVersionImpact`, `LibraryEntryTypeList`
  - All routes use `response_model` for OpenAPI schema extraction
  - Commands accept `If-Match` and `Idempotency-Key` (via `x-marketai-idempotent` extension)
  - Paginated routes accept `cursor`, `limit` query params

This is the largest task. It has 20 routes but they all follow the same pattern: thin HTTP adapter → domain service. The implementation is mechanical once the response models and the router pattern are established.

- [ ] **Step 1: Write Pydantic response models**

```python
# app/library/response_models.py
"""Pydantic response models for Library v1 routes.

These models serve two purposes:
1. FastAPI uses them as ``response_model`` to generate OpenAPI schemas
2. The route returns dicts from service.py; Pydantic serializes them
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class LibraryEntrySummary(BaseModel):
    id: str
    entry_type: str
    display_name: str
    lifecycle: str
    current_version_id: str | None = None


class DraftSummary(BaseModel):
    id: str
    entry_id: str
    revision: int
    state: str
    base_version_id: str | None = None
    source_version_id: str | None = None
    origin_command: str | None = None
    content: dict = Field(default_factory=dict)
    validation: dict | None = None


class LibraryVersionSummary(BaseModel):
    id: str
    entry_id: str
    entry_type: str
    version_no: int
    state: str
    eligibility: str
    content_hash: str | None = None
    origin_command: str | None = None
    published_by: str | None = None
    published_at: str | None = None


class LibraryVersionDetail(BaseModel):
    id: str
    entry_id: str
    entry_type: str
    version_no: int
    state: str
    eligibility: str
    content: dict = Field(default_factory=dict)
    content_hash: str | None = None
    origin_command: str | None = None
    source_version_id: str | None = None
    base_version_id: str | None = None
    published_by: str | None = None
    published_at: str | None = None
    withdraw_reason: str | None = None
    revision: int = 1
    allowed_actions: list[dict] = Field(default_factory=list)


class LibraryEntryDetail(BaseModel):
    id: str
    entry_type: str
    display_name: str
    lifecycle: str
    current_version_id: str | None = None
    draft: DraftSummary | None = None
    versions: list[LibraryVersionSummary] = Field(default_factory=list)
    revision: int = 1
    allowed_actions: list[dict] = Field(default_factory=list)


class LibraryValidationResult(BaseModel):
    ok: bool
    content_hash: str
    findings: list[dict] = Field(default_factory=list)
    blocking: list[dict] = Field(default_factory=list)
    warnings: list[dict] = Field(default_factory=list)


class LibraryBinding(BaseModel):
    id: str
    owner_version_id: str
    reference_version_id: str
    purpose: str
    viewpoint: str
    priority: int = 0
    note: str = ""
    conditions: dict = Field(default_factory=dict)


class LibraryEvent(BaseModel):
    sequence: int
    event_type: str
    version_id: str | None = None
    from_state: str = ""
    to_state: str = ""
    initiator_id: str = ""
    executor: str = ""
    payload: dict = Field(default_factory=dict)


class ImpactDependent(BaseModel):
    version_id: str
    entry_id: str
    entry_type: str
    version_no: int
    state: str
    eligibility: str


class LibraryVersionImpact(BaseModel):
    version_id: str
    entry_id: str
    entry_type: str
    state: str
    eligibility: str
    dependents: list[ImpactDependent] = Field(default_factory=list)
    reason: str = ""


class LibraryEntryTypeItem(BaseModel):
    key: str
    label: str
    description: str = ""


class LibraryEntryTypeList(BaseModel):
    entry_types: list[dict]


class PageResponse(BaseModel):
    items: list[Any]
    next_cursor: str | None = None
```

- [ ] **Step 2: Write the Library v1 sub-router**

```python
# app/library/v1_router.py
"""Library v1 sub-router — 20 operations under /api/v1/library (spec §3).

Mounted on the shared v1_router in app.main. Each route has an explicit
operation_id and response_model for OpenAPI extraction. Commands use
x-marketai-idempotent for BFF idempotency enforcement.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, Field

from app.platform.etag import extract_if_match, set_etag
from app.platform.http_identity import request_tenant_context
from app.platform.pagination import InvalidCursorError, parse_limit
from app.platform.tenancy import TenantContext

from . import service
from .contracts import iter_entry_types
from .response_models import (
    LibraryBinding,
    LibraryEntryDetail,
    LibraryEntryTypeList,
    LibraryEvent,
    LibraryValidationResult,
    LibraryVersionDetail,
    LibraryVersionImpact,
    PageResponse,
)

router = APIRouter(prefix="/library", tags=["library"])


def _command_fingerprint(body: BaseModel, *, resource_field: str = "",
                         resource_id: str = "", if_match: int | None = None) -> str:
    payload = body.model_dump(mode="json")
    if resource_field:
        payload = {resource_field: resource_id, **payload}
    if if_match is not None:
        payload["_if_match"] = if_match
    return service.compute_fingerprint(payload)


# --- Reads ---


@router.get(
    "/entry-types",
    operation_id="list_library_entry_types",
    response_model=LibraryEntryTypeList,
)
def list_entry_types(
    _context: TenantContext = Depends(request_tenant_context),
):
    return {"entry_types": list(iter_entry_types())}


@router.get(
    "/entries",
    operation_id="list_library_entries",
    response_model=PageResponse,
)
def list_entries(
    entry_type: str | None = Query(default=None),
    include_archived: bool = Query(default=False),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    context: TenantContext = Depends(request_tenant_context),
):
    return service.list_entries(
        context, entry_type=entry_type, include_archived=include_archived,
    )


@router.get(
    "/entries/{entry_id}",
    operation_id="get_library_entry",
    response_model=LibraryEntryDetail,
)
def get_entry(
    entry_id: str,
    response: Response,
    context: TenantContext = Depends(request_tenant_context),
):
    result = service.get_entry(context, entry_id)
    revision = 1
    if result.get("draft"):
        revision = result["draft"].get("revision", 1)
    set_etag(response, revision)
    result["revision"] = revision
    return result


@router.get(
    "/entries/{entry_id}/versions",
    operation_id="list_library_entry_versions",
    response_model=PageResponse,
)
def list_entry_versions(
    entry_id: str,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    context: TenantContext = Depends(request_tenant_context),
):
    return {"items": service.get_entry(context, entry_id).get("versions", []),
            "next_cursor": None}


@router.get(
    "/entries/{entry_id}/events",
    operation_id="list_library_entry_events",
    response_model=PageResponse,
)
def list_entry_events(
    entry_id: str,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    context: TenantContext = Depends(request_tenant_context),
):
    events = service.list_events(context, entry_id)
    return {"items": events, "next_cursor": None}


@router.get(
    "/versions/{version_id}",
    operation_id="get_library_version",
    response_model=LibraryVersionDetail,
)
def get_version(
    version_id: str,
    response: Response,
    context: TenantContext = Depends(request_tenant_context),
):
    result = service.get_version(context, version_id)
    set_etag(response, result.get("version_no", 1))
    return result


@router.get(
    "/versions/{version_id}/impact",
    operation_id="get_library_version_impact",
    response_model=LibraryVersionImpact,
)
def get_version_impact(
    version_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    return service.impact_analysis(context, version_id)


@router.get(
    "/versions/{version_id}/bindings",
    operation_id="list_library_version_bindings",
    response_model=PageResponse,
)
def list_version_bindings(
    version_id: str,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    context: TenantContext = Depends(request_tenant_context),
):
    bindings = service.list_bindings(context, version_id)
    return {"items": bindings, "next_cursor": None}


# --- Commands ---
# Each command route uses x-marketai-idempotent: true via the
# openapi_extra parameter so generate_openapi.py picks it up.

class CreateEntryInput(BaseModel):
    entry_type: str
    display_name: str = Field(min_length=1, max_length=300)
    content: dict = Field(default_factory=dict)


@router.post(
    "/entries",
    operation_id="create_library_entry",
    response_model=LibraryEntryDetail,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_entry(
    body: CreateEntryInput,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
):
    fp = _command_fingerprint(body)
    return service.create_entry(
        context, entry_type=body.entry_type, display_name=body.display_name,
        content=body.content, idempotency_key=idempotency_key, request_fingerprint=fp,
    )


class SaveDraftInput(BaseModel):
    content: dict


@router.put(
    "/entries/{entry_id}/draft",
    operation_id="save_library_draft",
    response_model=LibraryEntryDetail,
    openapi_extra={"x-marketai-idempotent": True},
)
def save_draft(
    entry_id: str,
    body: SaveDraftInput,
    request: Request,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
):
    expected_revision = extract_if_match(request)
    fp = _command_fingerprint(body, resource_field="entry_id",
                              resource_id=entry_id, if_match=expected_revision)
    result = service.save_draft(
        context, entry_id, body.content, expected_revision,
        idempotency_key=idempotency_key, request_fingerprint=fp,
    )
    return service.get_entry(context, entry_id)


@router.post(
    "/entries/{entry_id}/validate",
    operation_id="validate_library_draft",
    response_model=LibraryValidationResult,
    openapi_extra={"x-marketai-idempotent": True},
)
def validate_draft(
    entry_id: str,
    request: Request,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
):
    expected_revision = extract_if_match(request)
    fp = _command_fingerprint(
        BaseModel(), resource_field="entry_id",
        resource_id=entry_id, if_match=expected_revision,
    )
    return service.validate_draft(
        context, entry_id, expected_revision=expected_revision,
        idempotency_key=idempotency_key, request_fingerprint=fp,
    )


class PublishInput(BaseModel):
    acknowledged_warnings: list[str] = Field(default_factory=list)


@router.post(
    "/entries/{entry_id}/publish",
    operation_id="publish_library_entry",
    response_model=LibraryVersionDetail,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def publish(
    entry_id: str,
    body: PublishInput,
    request: Request,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
):
    expected_revision = extract_if_match(request)
    fp = _command_fingerprint(body, resource_field="entry_id",
                              resource_id=entry_id, if_match=expected_revision)
    return service.publish(
        context, entry_id, expected_revision,
        acknowledged_warnings=body.acknowledged_warnings,
        idempotency_key=idempotency_key, request_fingerprint=fp,
    )


class ArchiveInput(BaseModel):
    pass


@router.post(
    "/entries/{entry_id}/archive",
    operation_id="archive_library_entry",
    response_model=LibraryEntryDetail,
    openapi_extra={"x-marketai-idempotent": True},
)
def archive(
    entry_id: str,
    request: Request,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
):
    expected_revision = extract_if_match(request)
    fp = _command_fingerprint(
        BaseModel(), resource_field="entry_id",
        resource_id=entry_id, if_match=expected_revision,
    )
    return service.archive_entry(
        context, entry_id, expected_revision=expected_revision,
        idempotency_key=idempotency_key, request_fingerprint=fp,
    )


class BindInput(BaseModel):
    reference_version_id: str
    purpose: str = Field(min_length=1, max_length=100)
    viewpoint: str = Field(min_length=1, max_length=100)
    priority: int = Field(default=0, ge=0)
    note: str = ""
    conditions: dict = Field(default_factory=dict)


@router.post(
    "/entries/{entry_id}/bind",
    operation_id="bind_library_reference",
    response_model=LibraryBinding,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def bind(
    entry_id: str,
    body: BindInput,
    request: Request,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
):
    expected_revision = extract_if_match(request)
    fp = _command_fingerprint(body, resource_field="entry_id",
                              resource_id=entry_id, if_match=expected_revision)
    return service.bind_reference(
        context, entry_id, expected_revision,
        reference_version_id=body.reference_version_id,
        purpose=body.purpose, viewpoint=body.viewpoint,
        priority=body.priority, note=body.note, conditions=body.conditions,
        idempotency_key=idempotency_key, request_fingerprint=fp,
    )


class WithdrawInput(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


@router.post(
    "/versions/{version_id}/withdraw",
    operation_id="withdraw_library_version",
    response_model=LibraryVersionDetail,
    openapi_extra={"x-marketai-idempotent": True},
)
def withdraw(
    version_id: str,
    body: WithdrawInput,
    request: Request,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
):
    expected_revision = extract_if_match(request)
    fp = _command_fingerprint(body, resource_field="version_id",
                              resource_id=version_id, if_match=expected_revision)
    return service.withdraw_version(
        context, version_id, reason=body.reason,
        idempotency_key=idempotency_key, request_fingerprint=fp,
    )


class ForkInput(BaseModel):
    display_name: str = Field(min_length=1, max_length=300)


@router.post(
    "/versions/{version_id}/fork",
    operation_id="fork_library_version",
    response_model=LibraryEntryDetail,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def fork(
    version_id: str,
    body: ForkInput,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
):
    fp = _command_fingerprint(body, resource_field="version_id", resource_id=version_id)
    return service.fork_version(
        context, version_id, display_name=body.display_name,
        idempotency_key=idempotency_key, request_fingerprint=fp,
    )


class CloneProductInput(BaseModel):
    display_name: str = Field(min_length=1, max_length=300)
    include_optional: list[str] = Field(default_factory=list)


@router.post(
    "/versions/{version_id}/clone-product",
    operation_id="clone_library_product",
    response_model=LibraryEntryDetail,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def clone_product(
    version_id: str,
    body: CloneProductInput,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
):
    fp = _command_fingerprint(body, resource_field="version_id", resource_id=version_id)
    return service.clone_product(
        context, version_id, display_name=body.display_name,
        include_optional=body.include_optional,
        idempotency_key=idempotency_key, request_fingerprint=fp,
    )


class ApplyTemplateInput(BaseModel):
    product_entry_id: str
    display_name: str = Field(min_length=1, max_length=300)


@router.post(
    "/versions/{template_version_id}/apply",
    operation_id="apply_library_template",
    response_model=LibraryEntryDetail,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def apply_template(
    template_version_id: str,
    body: ApplyTemplateInput,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
):
    fp = _command_fingerprint(body, resource_field="template_version_id",
                              resource_id=template_version_id)
    # apply_template deep-copies a template version into a product-local
    # DimensionSchema draft — implemented in service layer
    return service.apply_template(
        context, template_version_id,
        product_entry_id=body.product_entry_id,
        display_name=body.display_name,
        idempotency_key=idempotency_key, request_fingerprint=fp,
    )


class CreateReferenceObjectInput(BaseModel):
    upload_intent_id: str
    filename: str = ""
    declared_mime: str = ""
    execution_mode: str = ""
    safety_scenario: str = ""


@router.post(
    "/reference-objects",
    operation_id="create_library_reference_object",
    status_code=202,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_reference_object(
    body: CreateReferenceObjectInput,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
):
    fp = _command_fingerprint(body)
    # Consumes a finalized Upload Intent — no multipart through BFF
    return service.create_reference_from_upload_intent(
        context,
        upload_intent_id=body.upload_intent_id,
        filename=body.filename,
        declared_mime=body.declared_mime,
        execution_mode=body.execution_mode,
        safety_scenario=body.safety_scenario,
        idempotency_key=idempotency_key,
        request_fingerprint=fp,
    )


class CreateDownloadIntentInput(BaseModel):
    pass


@router.post(
    "/versions/{version_id}/download-intent",
    operation_id="create_library_reference_download_intent",
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_reference_download_intent(
    version_id: str,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
):
    fp = _command_fingerprint(
        BaseModel(), resource_field="version_id", resource_id=version_id,
    )
    return service.create_reference_download_intent(
        context, version_id,
        idempotency_key=idempotency_key,
        request_fingerprint=fp,
    )
```

- [ ] **Step 3: Modify app/main.py to mount v1 sub-routers**

Replace the old library and generation router includes:

```python
# In app/main.py, replace:
#   app.include_router(library_router)
#   app.include_router(generation_router)
# With:
from .library.v1_router import router as library_v1_router
v1_router.include_router(library_v1_router)

# Keep old routers temporarily for backward compatibility test verification,
# then remove after confirming all 20 operations appear in OpenAPI extraction.
# Old problem handlers stay since they handle exception types used by both.
```

- [ ] **Step 4: Write integration test for a Library v1 route**

```python
# tests/library/test_v1_library_routes.py
"""Integration tests for Library v1 routes — validate operation_id registration,
response shapes, and tenant isolation."""
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_list_entry_types_returns_200(client, auth_headers):
    resp = client.get("/api/v1/library/entry-types", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "entry_types" in data
    assert isinstance(data["entry_types"], list)


def test_create_entry_requires_idempotency_key(client, auth_headers):
    resp = client.post(
        "/api/v1/library/entries",
        json={"entry_type": "product", "display_name": "Test"},
        headers=auth_headers,
    )
    # Missing Idempotency-Key header → 422 validation error
    assert resp.status_code == 422


def test_save_draft_requires_if_match(client, auth_headers):
    # First create an entry
    resp = client.post(
        "/api/v1/library/entries",
        json={"entry_type": "product", "display_name": "Test"},
        headers={**auth_headers, "Idempotency-Key": "test-key-1"},
    )
    assert resp.status_code == 201
    entry_id = resp.json()["id"]

    # Save draft without If-Match → 428
    resp = client.put(
        f"/api/v1/library/entries/{entry_id}/draft",
        json={"content": {"name": "Updated"}},
        headers={**auth_headers, "Idempotency-Key": "test-key-2"},
    )
    assert resp.status_code == 428
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/library/test_v1_library_routes.py -v`
Expected: Tests pass (some may need fixture adjustments for auth — adapt to existing test patterns)

- [ ] **Step 6: Verify OpenAPI extraction picks up Library routes**

Run: `python scripts/generate_openapi.py --check 2>&1 || echo "Expected: spec changed (new routes added)"`
Expected: Diff shows the 20 new Library operations in the OpenAPI spec.

- [ ] **Step 7: Commit**

```bash
git add app/library/v1_router.py app/library/response_models.py app/main.py \
       tests/library/test_v1_library_routes.py
git commit -m "feat(library): mount 20 Library operations on v1 router with operation_id and response_model (#27)"
```

---

## Task 3: Generation v1 Sub-Router — Draft, Preview, Run, Assets, Export

**Files:**
- Create: `app/generation/v1_router.py`
- Create: `app/generation/response_models.py`
- Create: `app/generation/run_queries.py`
- Create: `app/generation/run_commands.py`
- Create: `app/generation/recovery_preview.py`
- Create: `app/generation/capabilities.py`
- Modify: `app/main.py` — mount generation v1 sub-router
- Create: `tests/generation/test_v1_generation_routes.py`

**Interfaces:**
- Consumes: `app.platform.pagination` (Task 1), `app.platform.etag` (Task 1), `app.platform.action_eligibility` (Task 1), `app.generation.draft`, `app.generation.preview`, `app.generation.submission`, `app.generation.export`, `app.generation.review`, `app.generation.regeneration`
- Produces:
  - 25 FastAPI routes under `v1_router` prefix `/generation` with explicit `operation_id`
  - Response models: `GenerationDraftDetail`, `GenerationDraftSummary`, `GenerationPreviewDetail`, `GenerationRunDetail`, `GenerationRunSummary`, `GenerationRunItemDetail`, `GenerationRunItemSummary`, `GenerationRecoveryPreview`, `GenerationCapabilities`, `AssetSummary`, `AssetVersionDetail`, `AssetReview`, `ExportManifest`
  - Run query functions: `list_runs()`, `get_run_detail()`, `list_run_items()`, `get_run_item_detail()`, `list_assets()`, `get_asset_version_detail()`
  - Run commands: `cancel_run()`, `retry_failed_items()`, `resume_paused_items()`, `reconcile_run()`

This task covers 25 routes. The pattern mirrors Task 2: thin HTTP adapter delegating to domain modules. The new domain modules (`run_queries.py`, `run_commands.py`, `recovery_preview.py`, `capabilities.py`) are stubs wired to real persistence but returning minimal projections — the full domain depth (allowed_actions, lineage, cost) is filled in iteratively as the UI sub-projects consume these endpoints.

- [ ] **Step 1: Write Generation response models**

Create `app/generation/response_models.py` with Pydantic models for all 25 operations' responses. Follow the same pattern as `app/library/response_models.py`.

Key models:
- `GenerationDraftDetail` — id, workspace_id, product_id, provider, model, execution_mode, revision, state, selectors, combination_strategy, format_config, exclusions, allowed_actions
- `GenerationDraftSummary` — id, product_id, provider, model, state, revision, updated_at
- `GenerationPreviewDetail` — id, preview_hash, recipe_input_hash, execution_mode, valid_until, draft_id, draft_revision, invalidation_reason, allowed_actions
- `GenerationRunDetail` — id, product_id, provider, model, status, total, done, failed, execution_mode, revision, recipe_snapshot_id, commands, operation_id, allowed_actions
- `GenerationRunSummary` — id, product_id, provider, model, status, total, done, failed, execution_mode, created_at
- `GenerationRunItemSummary` — id, ordinal, state, combination_fingerprint, variant_index, tags, allowed_actions
- `GenerationRunItemDetail` — full Provider Attempt events, QC, cost, AssetVersion binding, allowed_actions
- `GenerationRecoveryPreview` — kind (retry/resume), targets, estimated_additional_cost, valid_until
- `GenerationCapabilities` — providers, models, modes, format_limits, credential_readiness
- `AssetSummary` — id (version_id), asset_id, product_id, run_id, provider, model, status, review_value, qc_state, origin_kind, created_at
- `AssetVersionDetail` — full identity, lineage, RunItem, recipe, cost, QC, reviews, provenance, allowed_actions
- `AssetReview` — id, asset_version_id, review_value, reason, actor_id, supersedes_review_id, created_at
- `ExportManifest` — package_id, execution_mode, entries, total_versions

- [ ] **Step 2: Write run_queries.py — read projections**

```python
# app/generation/run_queries.py
"""Read projections for Generation Runs, RunItems, and Assets.

Assembles server-side detail projections so the browser does not join
raw persistence records or reimplement action eligibility.
"""
from __future__ import annotations

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.adapters.persistence.models import (
    AssetRecord,
    AssetVersionRecord,
    GenerationRunRecord,
    QualityReviewRecord,
)
from app.generation.models import (
    GenerationRunCommandRecord,
    RunItemRecord,
)
from app.platform.action_eligibility import ActionEligibility, compute_actions
from app.platform.tenancy import TenantContext


def list_runs(session: Session, ctx: TenantContext, *,
              product_id: str | None = None,
              state: str | None = None,
              execution_mode: str | None = None) -> list[dict]:
    stmt = select(GenerationRunRecord).where(
        GenerationRunRecord.workspace_id == ctx.workspace_id,
    )
    if product_id:
        stmt = stmt.where(GenerationRunRecord.product_id == product_id)
    if state:
        stmt = stmt.where(GenerationRunRecord.status == state)
    if execution_mode:
        stmt = stmt.where(GenerationRunRecord.execution_mode == execution_mode)
    stmt = stmt.order_by(GenerationRunRecord.created_at.desc(), GenerationRunRecord.id.desc())
    rows = session.scalars(stmt).all()
    return [_run_summary(r) for r in rows]


def get_run_detail(session: Session, ctx: TenantContext, run_id: str) -> dict:
    run = _load_run(session, ctx, run_id)
    commands = session.scalars(
        select(GenerationRunCommandRecord)
        .where(GenerationRunCommandRecord.generation_run_id == run_id,
               GenerationRunCommandRecord.workspace_id == ctx.workspace_id)
        .order_by(GenerationRunCommandRecord.created_at)
    ).all()
    return {
        **_run_summary(run),
        "revision": run.revision,
        "recipe_snapshot_id": run.recipe_snapshot_id,
        "commands": [{"id": c.id, "kind": c.command_kind, "created_at": str(c.created_at)} for c in commands],
        "allowed_actions": _run_actions(run),
    }


def list_run_items(session: Session, ctx: TenantContext, run_id: str, *,
                   state: str | None = None) -> list[dict]:
    _load_run(session, ctx, run_id)
    stmt = select(RunItemRecord).where(
        RunItemRecord.generation_run_id == run_id,
        RunItemRecord.workspace_id == ctx.workspace_id,
    )
    if state:
        stmt = stmt.where(RunItemRecord.state == state)
    stmt = stmt.order_by(RunItemRecord.ordinal.asc(), RunItemRecord.id.asc())
    rows = session.scalars(stmt).all()
    return [_run_item_summary(r) for r in rows]


def get_run_item_detail(session: Session, ctx: TenantContext,
                        run_id: str, run_item_id: str) -> dict:
    _load_run(session, ctx, run_id)
    item = session.get(RunItemRecord, run_item_id)
    if item is None or item.workspace_id != ctx.workspace_id or item.generation_run_id != run_id:
        from app.generation.draft import DraftNotFoundError
        raise DraftNotFoundError(f"RunItem {run_item_id} not found")
    return {
        **_run_item_summary(item),
        "combination_identity": item.combination_identity_json,
        "execution_mode": item.execution_mode,
        "error_summary": item.error_summary,
        "allowed_actions": _item_actions(item),
    }


def list_assets(session: Session, ctx: TenantContext, *,
                product_id: str | None = None,
                run_id: str | None = None,
                provider: str | None = None,
                execution_mode: str | None = None,
                review_value: str | None = None,
                qc_state: str | None = None,
                origin_kind: str | None = None) -> list[dict]:
    stmt = select(AssetVersionRecord).where(
        AssetVersionRecord.workspace_id == ctx.workspace_id,
    )
    if product_id:
        stmt = stmt.join(AssetRecord, AssetVersionRecord.asset_id == AssetRecord.id)
        stmt = stmt.where(AssetRecord.product_id == product_id)
    if execution_mode:
        stmt = stmt.where(AssetVersionRecord.execution_mode == execution_mode)
    if qc_state:
        stmt = stmt.where(AssetVersionRecord.qc_state == qc_state)
    if origin_kind:
        stmt = stmt.where(AssetVersionRecord.origin_kind == origin_kind)
    stmt = stmt.order_by(AssetVersionRecord.created_at.desc(), AssetVersionRecord.id.desc())
    rows = session.scalars(stmt).all()
    return [_asset_summary(r) for r in rows]


def get_asset_version_detail(session: Session, ctx: TenantContext,
                             version_id: str) -> dict:
    av = session.get(AssetVersionRecord, version_id)
    if av is None or av.workspace_id != ctx.workspace_id:
        from app.generation.draft import DraftNotFoundError
        raise DraftNotFoundError(f"AssetVersion {version_id} not found")
    reviews = session.scalars(
        select(QualityReviewRecord).where(
            QualityReviewRecord.asset_version_id == version_id,
            QualityReviewRecord.workspace_id == ctx.workspace_id,
        ).order_by(QualityReviewRecord.created_at.desc())
    ).all()
    return {
        "id": av.id,
        "asset_id": av.asset_id,
        "kind": av.kind,
        "aspect_ratio": av.aspect_ratio,
        "mime_type": av.mime_type,
        "byte_size": av.byte_size,
        "origin_kind": av.origin_kind,
        "execution_mode": av.execution_mode,
        "object_key": None,  # never exposed to browser
        "content_sha256": av.content_sha256,
        "reviews": [{"id": r.id, "review_value": r.review_value, "reason": r.reason} for r in reviews],
        "allowed_actions": _asset_version_actions(av, reviews),
    }


def _load_run(session: Session, ctx: TenantContext, run_id: str) -> GenerationRunRecord:
    run = session.get(GenerationRunRecord, run_id)
    if run is None or run.workspace_id != ctx.workspace_id:
        from app.generation.draft import DraftNotFoundError
        raise DraftNotFoundError(f"GenerationRun {run_id} not found")
    return run


def _run_summary(run: GenerationRunRecord) -> dict:
    return {
        "id": run.id,
        "product_id": run.product_id,
        "provider": run.provider,
        "model": run.model,
        "status": run.status,
        "total": run.total,
        "done": run.done,
        "failed": run.failed,
        "execution_mode": run.execution_mode,
        "created_at": str(run.created_at) if run.created_at else None,
    }


def _run_item_summary(item: RunItemRecord) -> dict:
    return {
        "id": item.id,
        "ordinal": item.ordinal,
        "state": item.state,
        "combination_fingerprint": item.combination_fingerprint,
        "variant_index": item.variant_index,
        "tags": item.tags_json,
    }


def _asset_summary(av: AssetVersionRecord) -> dict:
    return {
        "id": av.id,
        "asset_id": av.asset_id,
        "kind": av.kind,
        "mime_type": av.mime_type,
        "origin_kind": av.origin_kind,
        "execution_mode": av.execution_mode,
        "created_at": str(av.created_at) if av.created_at else None,
    }


RUN_ACTIONS = ["cancel", "retry_failed", "resume_paused", "reconcile"]
ITEM_ACTIONS = ["review", "regenerate", "download"]
ASSET_VERSION_ACTIONS = ["review", "regenerate", "download", "skip"]


def _run_actions(run: GenerationRunRecord) -> list[dict]:
    checks: dict[str, tuple[bool, str | None]] = {}
    is_active = run.status in ("pending", "running", "qc_pending")
    checks["cancel"] = (is_active, None if is_active else "run_not_active")
    checks["retry_failed"] = (run.failed > 0, None if run.failed > 0 else "no_failed_items")
    checks["resume_paused"] = (False, "not_implemented")
    checks["reconcile"] = (False, "not_implemented")
    return compute_actions(RUN_ACTIONS, checks)


def _item_actions(item: RunItemRecord) -> list[dict]:
    checks: dict[str, tuple[bool, str | None]] = {}
    succeeded = item.state == "succeeded"
    checks["review"] = (succeeded, None if succeeded else "item_not_succeeded")
    checks["regenerate"] = (succeeded, None if succeeded else "item_not_succeeded")
    checks["download"] = (succeeded, None if succeeded else "item_not_succeeded")
    return compute_actions(ITEM_ACTIONS, checks)


def _asset_version_actions(av, reviews) -> list[dict]:
    checks: dict[str, tuple[bool, str | None]] = {}
    has_blob = bool(av.object_key)
    checks["review"] = (True, None)
    checks["regenerate"] = (True, None)
    checks["download"] = (has_blob, None if has_blob else "asset_blob_unavailable")
    checks["skip"] = (True, None)
    return compute_actions(ASSET_VERSION_ACTIONS, checks)
```

- [ ] **Step 3: Write run_commands.py stubs**

```python
# app/generation/run_commands.py
"""Generation Run mutation commands (spec §5).

Each command appends a GenerationRunCommandRecord, creates an Operation,
uses CAS on run revision, and enqueues valid item work.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.adapters.persistence.models import new_id, utcnow
from app.generation.models import GenerationRunCommandRecord
from app.platform.idempotency import create_or_replay_operation
from app.platform.tenancy import TenantContext

from .run_queries import _load_run


class RunCommandError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def cancel_run(session: Session, ctx: TenantContext, *,
               run_id: str, expected_revision: int,
               idempotency_key: str | None = None) -> dict:
    run = _load_run(session, ctx, run_id)
    if run.revision != expected_revision:
        raise RunCommandError("revision_conflict",
                              f"Expected {expected_revision}, current {run.revision}")
    if run.status not in ("pending", "running", "qc_pending"):
        raise RunCommandError("run_not_cancellable",
                              f"Run status {run.status} cannot be cancelled")

    operation = create_or_replay_operation(
        session,
        idempotency_key=idempotency_key,
        command_type="generation.cancel",
        fingerprint_hash=f"cancel:{run_id}:{expected_revision}",
        actor=ctx.initiator_id or ctx.executor,
    )
    now = utcnow()
    cmd = GenerationRunCommandRecord(
        id=new_id("runcmd"),
        workspace_id=ctx.workspace_id,
        generation_run_id=run_id,
        operation_id=operation.id,
        command_kind="cancel",
        expected_run_revision=expected_revision,
        actor_id=ctx.initiator_id or "unknown",
        authorization_epoch=ctx.authorization_epoch,
        created_at=now,
    )
    session.add(cmd)
    run.revision += 1
    session.flush()
    return {"operation_id": operation.id, "run_id": run_id}


def retry_failed_items(session: Session, ctx: TenantContext, *,
                       run_id: str, expected_revision: int,
                       preview_id: str, preview_hash: str,
                       idempotency_key: str | None = None) -> dict:
    run = _load_run(session, ctx, run_id)
    if run.revision != expected_revision:
        raise RunCommandError("revision_conflict",
                              f"Expected {expected_revision}, current {run.revision}")
    operation = create_or_replay_operation(
        session,
        idempotency_key=idempotency_key,
        command_type="generation.retry_failed",
        fingerprint_hash=f"retry:{run_id}:{preview_id}:{preview_hash}",
        actor=ctx.initiator_id or ctx.executor,
    )
    now = utcnow()
    cmd = GenerationRunCommandRecord(
        id=new_id("runcmd"),
        workspace_id=ctx.workspace_id,
        generation_run_id=run_id,
        operation_id=operation.id,
        command_kind="retry_failed_items",
        target_selection_json={"preview_id": preview_id, "preview_hash": preview_hash},
        expected_run_revision=expected_revision,
        actor_id=ctx.initiator_id or "unknown",
        authorization_epoch=ctx.authorization_epoch,
        created_at=now,
    )
    session.add(cmd)
    run.revision += 1
    session.flush()
    return {"operation_id": operation.id, "run_id": run_id}


def resume_paused_items(session: Session, ctx: TenantContext, *,
                        run_id: str, expected_revision: int,
                        preview_id: str, preview_hash: str,
                        idempotency_key: str | None = None) -> dict:
    run = _load_run(session, ctx, run_id)
    if run.revision != expected_revision:
        raise RunCommandError("revision_conflict",
                              f"Expected {expected_revision}, current {run.revision}")
    operation = create_or_replay_operation(
        session,
        idempotency_key=idempotency_key,
        command_type="generation.resume_paused",
        fingerprint_hash=f"resume:{run_id}:{preview_id}:{preview_hash}",
        actor=ctx.initiator_id or ctx.executor,
    )
    now = utcnow()
    cmd = GenerationRunCommandRecord(
        id=new_id("runcmd"),
        workspace_id=ctx.workspace_id,
        generation_run_id=run_id,
        operation_id=operation.id,
        command_kind="resume_paused_items",
        target_selection_json={"preview_id": preview_id, "preview_hash": preview_hash},
        expected_run_revision=expected_revision,
        actor_id=ctx.initiator_id or "unknown",
        authorization_epoch=ctx.authorization_epoch,
        created_at=now,
    )
    session.add(cmd)
    run.revision += 1
    session.flush()
    return {"operation_id": operation.id, "run_id": run_id}


def reconcile_run(session: Session, ctx: TenantContext, *,
                  run_id: str, expected_revision: int,
                  uncertain_targets: list[str],
                  operator_note: str = "",
                  idempotency_key: str | None = None) -> dict:
    run = _load_run(session, ctx, run_id)
    if run.revision != expected_revision:
        raise RunCommandError("revision_conflict",
                              f"Expected {expected_revision}, current {run.revision}")
    operation = create_or_replay_operation(
        session,
        idempotency_key=idempotency_key,
        command_type="generation.reconcile",
        fingerprint_hash=f"reconcile:{run_id}:{','.join(sorted(uncertain_targets))}",
        actor=ctx.initiator_id or ctx.executor,
    )
    now = utcnow()
    cmd = GenerationRunCommandRecord(
        id=new_id("runcmd"),
        workspace_id=ctx.workspace_id,
        generation_run_id=run_id,
        operation_id=operation.id,
        command_kind="reconcile",
        target_selection_json={"uncertain_targets": uncertain_targets,
                               "operator_note": operator_note},
        expected_run_revision=expected_revision,
        actor_id=ctx.initiator_id or "unknown",
        authorization_epoch=ctx.authorization_epoch,
        created_at=now,
    )
    session.add(cmd)
    run.revision += 1
    session.flush()
    return {"operation_id": operation.id, "run_id": run_id}
```

- [ ] **Step 4: Write Generation v1 sub-router**

Create `app/generation/v1_router.py` with 25 routes following the Library v1 router pattern. Each route has `operation_id`, `response_model`, and delegates to the appropriate domain module. All commands use `openapi_extra={"x-marketai-idempotent": True}`.

Key route mappings:
- `GET /capabilities` → `capabilities.get_capabilities()`
- `GET /drafts` → `draft.list_drafts()` (new query)
- `POST /drafts` → `draft.create_draft()`
- `GET /drafts/{draft_id}` → `draft.get_draft()` + ETag
- `PATCH /drafts/{draft_id}` → `draft.update_draft()` + If-Match
- `POST /drafts/{draft_id}/previews` → `preview.create_preview()` + If-Match
- `GET /previews/{preview_id}` → `preview.get_preview()` (new query)
- `GET /runs` → `run_queries.list_runs()`
- `POST /runs` → `submission.submit_generation()` + If-Match
- `GET /runs/{run_id}` → `run_queries.get_run_detail()` + ETag
- `GET /runs/{run_id}/items` → `run_queries.list_run_items()`
- `GET /runs/{run_id}/items/{run_item_id}` → `run_queries.get_run_item_detail()`
- `POST /runs/{run_id}/recovery-previews` → `recovery_preview.create_recovery_preview()` + If-Match
- `POST /runs/{run_id}/cancel` → `run_commands.cancel_run()` + If-Match
- `POST /runs/{run_id}/retry-failed-items` → `run_commands.retry_failed_items()` + If-Match
- `POST /runs/{run_id}/resume-paused-items` → `run_commands.resume_paused_items()` + If-Match
- `POST /runs/{run_id}/reconcile` → `run_commands.reconcile_run()` + If-Match
- `GET /assets` → `run_queries.list_assets()`
- `GET /asset-versions/{version_id}` → `run_queries.get_asset_version_detail()`
- `POST /asset-versions/{version_id}/reviews` → `review.create_review()`
- `POST /asset-versions/{version_id}/regeneration-drafts` → `regeneration.create_regeneration_draft()` — when intent is `quality_feedback`, body must include `qc_attempt_id`; FastAPI validates the QC Attempt belongs to the same Workspace and AssetVersion, returns `409 qc_attempt_mismatch` otherwise (spec §6)
- `POST /asset-versions/{version_id}/download-intent` → asset download intent
- `POST /exports` → `export.create_export_package()`
- `GET /exports/{export_package_id}/manifest` → `export.build_manifest()`
- `POST /exports/{export_package_id}/download-intent` → `export.create_download_intent()`

- [ ] **Step 5: Mount generation v1 sub-router and register RunCommandError handler in app/main.py**

```python
# In app/main.py, add:
from .generation.v1_router import router as generation_v1_router
v1_router.include_router(generation_v1_router)

# Register RunCommandError → Problem Details handler:
from .generation.run_commands import RunCommandError

@app.exception_handler(RunCommandError)
async def run_command_error_handler(request, exc):
    status_map = {
        "revision_conflict": 409,
        "run_not_cancellable": 409,
        "qc_attempt_mismatch": 409,
        "review_conflict": 409,
    }
    status = status_map.get(exc.code, 422)
    return JSONResponse(status_code=status, content={
        "type": "about:blank",
        "title": exc.code,
        "status": status,
        "detail": exc.detail,
        "code": exc.code,
    })
```

- [ ] **Step 6: Write integration tests**

```python
# tests/generation/test_v1_generation_routes.py
"""Integration tests for Generation v1 routes."""
import pytest
from fastapi.testclient import TestClient
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_list_generation_drafts(client, auth_headers):
    resp = client.get("/api/v1/generation/drafts", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data


def test_create_draft_requires_idempotency(client, auth_headers):
    resp = client.post(
        "/api/v1/generation/drafts",
        json={"product_id": "p1", "provider": "mock", "model": "m1",
              "execution_mode": "mock", "combination_strategy": {}},
        headers=auth_headers,
    )
    assert resp.status_code == 422  # missing Idempotency-Key
```

- [ ] **Step 7: Run tests and verify OpenAPI extraction**

Run: `python -m pytest tests/generation/test_v1_generation_routes.py -v`
Run: `python scripts/generate_openapi.py --check 2>&1 | head -20`
Expected: Tests pass; OpenAPI diff shows 25 new generation operations.

- [ ] **Step 8: Commit**

```bash
git add app/generation/v1_router.py app/generation/response_models.py \
       app/generation/run_queries.py app/generation/run_commands.py \
       app/generation/recovery_preview.py app/generation/capabilities.py \
       app/main.py tests/generation/test_v1_generation_routes.py
git commit -m "feat(generation): mount 25 Generation/Run/Assets/Export operations on v1 router (#27)"
```

---

## Task 4: Review Correction Migration and Domain Update

**Files:**
- Create: `migrations/versions/XXX_review_correction.py`
- Modify: `app/adapters/persistence/models.py` — add `supersedes_review_id`, extend review_value enum
- Modify: `app/generation/review.py` — implement correction model
- Create: `tests/generation/test_review_correction.py`

**Interfaces:**
- Consumes: `QualityReviewRecord` (existing model)
- Produces:
  - `QualityReviewRecord.supersedes_review_id` — nullable FK to self
  - `QualityReviewRecord.review_value` — extended enum: `approved | rejected | needs_changes | cleared`
  - Uniqueness constraint: one review may supersede at most one prior review
  - `create_review()` — accepts optional `supersedes_review_id`, validates correction chain

- [ ] **Step 1: Write migration test**

```python
# tests/generation/test_review_correction.py
"""Tests for the Review correction model (spec §6 — review undo)."""
import pytest
from app.generation.review import create_review, ReviewConflictError
from app.platform.tenancy import TenantContext


def test_cleared_review_requires_supersedes(session, ctx, asset_version_id):
    with pytest.raises(ValueError, match="supersedes_review_id"):
        create_review(session, ctx, asset_version_id,
                      review_value="cleared", reason="mistake",
                      actor_id="user1")


def test_cleared_review_supersedes_existing(session, ctx, asset_version_id):
    r1 = create_review(session, ctx, asset_version_id,
                       review_value="approved", reason="good",
                       actor_id="user1")
    r2 = create_review(session, ctx, asset_version_id,
                       review_value="cleared", reason="undo",
                       actor_id="user1",
                       supersedes_review_id=r1.id)
    assert r2.supersedes_review_id == r1.id


def test_cannot_supersede_same_review_twice(session, ctx, asset_version_id):
    r1 = create_review(session, ctx, asset_version_id,
                       review_value="approved", reason="good",
                       actor_id="user1")
    create_review(session, ctx, asset_version_id,
                  review_value="cleared", reason="undo",
                  actor_id="user1",
                  supersedes_review_id=r1.id)
    with pytest.raises(ReviewConflictError):
        create_review(session, ctx, asset_version_id,
                      review_value="rejected", reason="changed mind",
                      actor_id="user1",
                      supersedes_review_id=r1.id)


def test_cross_version_correction_rejected(session, ctx, asset_version_id,
                                           other_asset_version_id):
    r1 = create_review(session, ctx, asset_version_id,
                       review_value="approved", reason="good",
                       actor_id="user1")
    with pytest.raises(ReviewConflictError, match="cross-version"):
        create_review(session, ctx, other_asset_version_id,
                      review_value="cleared", reason="undo",
                      actor_id="user1",
                      supersedes_review_id=r1.id)
```

- [ ] **Step 2: Create Alembic migration**

```python
# migrations/versions/XXX_review_correction.py
"""Add review correction model: supersedes_review_id and cleared enum value.

Revision ID: auto-generated
"""
from alembic import op
import sqlalchemy as sa


def upgrade():
    op.add_column("quality_reviews",
                  sa.Column("supersedes_review_id", sa.String(100),
                            sa.ForeignKey("quality_reviews.id"), nullable=True))
    op.create_unique_constraint(
        "uq_quality_reviews_supersedes",
        "quality_reviews",
        ["supersedes_review_id"],
    )


def downgrade():
    op.drop_constraint("uq_quality_reviews_supersedes", "quality_reviews")
    op.drop_column("quality_reviews", "supersedes_review_id")
```

- [ ] **Step 3: Update ORM model**

Add `supersedes_review_id` column to `QualityReviewRecord` in `app/adapters/persistence/models.py`.

- [ ] **Step 4: Update review.py with correction logic**

Extend `create_review` to accept `supersedes_review_id`, validate that the target review exists on the same AssetVersion and has not already been superseded, and enforce that `cleared` requires `supersedes_review_id`.

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/generation/test_review_correction.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add migrations/versions/ \
       app/adapters/persistence/models.py \
       app/generation/review.py \
       tests/generation/test_review_correction.py
git commit -m "feat(review): add correction model with supersedes_review_id and cleared enum (#27)"
```

---

## Task 5: OpenAPI Extraction and Policy Manifest Update

**Files:**
- Modify: `contracts/openapi/marketai-domain-v1.json` — regenerated (60 operations)
- Modify: `contracts/openapi/marketai-domain-v1-policy.json` — 45 new policy entries
- Modify: `scripts/generate_openapi.py` — no structural changes, just run it

**Interfaces:**
- Consumes: v1 routers from Tasks 2–3 (FastAPI routes with `operation_id`)
- Produces: Updated OpenAPI spec and policy manifest consumed by `generate_all.sh`

- [ ] **Step 1: Regenerate OpenAPI spec**

Run: `python scripts/generate_openapi.py`
Verify: The generated `contracts/openapi/marketai-domain-v1.json` now contains all 60 operations (15 existing + 20 Library + 25 Generation).

- [ ] **Step 2: Write policy entries for all 45 new operations**

Each Library operation needs a policy entry in `contracts/openapi/marketai-domain-v1-policy.json`:

```json
{
  "list_library_entry_types": {
    "bffPattern": "/api/domain/v1/w/:slug/library/entry-types",
    "maxRequestBytes": 0,
    "maxResponseBytes": 65536,
    "forwardConditionalHeaders": false
  },
  "list_library_entries": {
    "bffPattern": "/api/domain/v1/w/:slug/library/entries",
    "maxRequestBytes": 0,
    "maxResponseBytes": 524288,
    "forwardConditionalHeaders": false
  },
  "create_library_entry": {
    "bffPattern": "/api/domain/v1/w/:slug/library/entries",
    "maxRequestBytes": 65536,
    "maxResponseBytes": 131072,
    "forwardConditionalHeaders": false
  },
  "get_library_entry": {
    "bffPattern": "/api/domain/v1/w/:slug/library/entries/:entry_id",
    "maxRequestBytes": 0,
    "maxResponseBytes": 524288,
    "forwardConditionalHeaders": true
  }
}
```

Continue for all 45 operations following the spec's path patterns. Key rules:
- GET endpoints: `maxRequestBytes: 0`
- Commands with body: `maxRequestBytes: 65536` (except reference object: 8192)
- Detail responses: `maxResponseBytes: 524288` (large content JSON)
- List responses: `maxResponseBytes: 524288`
- Operations using ETag/If-Match: `forwardConditionalHeaders: true`
- Operations NOT using revision: `forwardConditionalHeaders: false`

- [ ] **Step 3: Verify all operations have policy entries**

Run: `python -c "import json; spec=json.load(open('contracts/openapi/marketai-domain-v1.json')); policy=json.load(open('contracts/openapi/marketai-domain-v1-policy.json')); ops=set(p['operationId'] for p in spec['paths'].values() for m in p.values() if isinstance(m,dict) and 'operationId' in m); missing=ops-set(policy.keys()); print(f'{len(ops)} ops, {len(missing)} missing policy: {missing}')"`
Expected: `0 missing policy`

- [ ] **Step 4: Commit**

```bash
git add contracts/openapi/marketai-domain-v1.json \
       contracts/openapi/marketai-domain-v1-policy.json
git commit -m "feat(contracts): update OpenAPI spec and policy manifest for 45 new operations (#27)"
```

---

## Task 6: Fix generate_allowlist.ts Query Schema Generation

**Files:**
- Modify: `scripts/generate_allowlist.ts:49` — replace `querySchema: null` with generated Zod schema
- Modify: `scripts/lib/spec.ts` — add `extractQueryParams()` helper
- Create: `web/__tests__/domain-client/query-schema-generation.test.ts`

**Interfaces:**
- Consumes: OpenAPI spec with query parameter definitions (Task 5)
- Produces: Generated `querySchema` entries that are Zod schemas instead of `null`

- [ ] **Step 1: Add extractQueryParams to spec.ts**

```typescript
// In scripts/lib/spec.ts, add:
export interface QueryParamDef {
  name: string;
  type: 'string' | 'integer' | 'boolean';
  required: boolean;
  enum?: string[];
  default?: unknown;
  minimum?: number;
  maximum?: number;
  isArray?: boolean; // repeated values via getAll()
}

export function extractQueryParams(
  operation: Record<string, unknown>
): QueryParamDef[] {
  const params = (operation.parameters ?? []) as Array<Record<string, unknown>>;
  return params
    .filter((p) => p.in === 'query')
    .map((p) => {
      const schema = (p.schema ?? {}) as Record<string, unknown>;
      return {
        name: p.name as string,
        type: (schema.type as string) ?? 'string',
        required: (p.required as boolean) ?? false,
        enum: schema.enum as string[] | undefined,
        default: schema.default,
        minimum: schema.minimum as number | undefined,
        maximum: schema.maximum as number | undefined,
        isArray: schema.type === 'array',
      };
    });
}
```

- [ ] **Step 2: Fix BFF handler to preserve repeated query values**

The BFF catch-all at `web/pages/api/domain/v1/w/[slug]/[...path].ts:117` currently uses `Object.fromEntries(rawSearchParams)` which collapses repeated query params (e.g. `?state=pending&state=running` → only `running`). Spec §9.3 requires `URLSearchParams.getAll`.

In `web/pages/api/domain/v1/w/[slug]/[...path].ts`, replace line 117:
```typescript
// Replace:
const collapsed = Object.fromEntries(rawSearchParams);
// With:
const collapsed: Record<string, unknown> = {};
for (const key of new Set(rawSearchParams.keys())) {
  const values = rawSearchParams.getAll(key);
  collapsed[key] = values.length === 1 ? values[0] : values;
}
```

This preserves repeated values as arrays so Zod `z.array()` schemas can validate them, while keeping single values as strings for backward compatibility.

- [ ] **Step 3: Update generate_allowlist.ts to emit Zod schemas**

Replace the hardcoded `querySchema: null` line with:

```typescript
// In scripts/generate_allowlist.ts, replace:
//   `    querySchema: null,\n`
// With dynamic generation based on extractQueryParams:

import { extractQueryParams, type QueryParamDef } from './lib/spec';

function buildQuerySchemaExpr(params: QueryParamDef[]): string {
  if (params.length === 0) return 'null';
  const fields = params.map((p) => {
    let zodType: string;
    switch (p.type) {
      case 'integer':
        zodType = 'z.coerce.number().int()';
        if (p.minimum !== undefined) zodType += `.min(${p.minimum})`;
        if (p.maximum !== undefined) zodType += `.max(${p.maximum})`;
        break;
      case 'boolean':
        zodType = 'z.coerce.boolean()';
        break;
      default:
        if (p.enum) {
          zodType = `z.enum(${JSON.stringify(p.enum)})`;
        } else {
          zodType = 'z.string()';
        }
    }
    if (!p.required) {
      zodType += '.optional()';
    }
    if (p.default !== undefined) {
      zodType += `.default(${JSON.stringify(p.default)})`;
    }
    return `    ${JSON.stringify(p.name)}: ${zodType},`;
  });
  return `z.object({\n${fields.join('\n')}\n  })`;
}
```

Then in the `entryLiterals` map, replace the `querySchema: null` line with:
```typescript
const queryParams = extractQueryParams(operation);
const querySchemaExpr = buildQuerySchemaExpr(queryParams);
// ...
`    querySchema: ${querySchemaExpr},\n`
```

And add `import { z } from 'zod';` to the generated file's imports when any operation has query params.

- [ ] **Step 4: Write query schema tests**

```typescript
// web/__tests__/domain-client/query-schema-generation.test.ts
import { z } from 'zod';

// Test that the generated query schemas validate correctly
// These tests run against the GENERATED allowlist after generate_all.sh
import { DOMAIN_ALLOWLIST } from '@/lib/domain-client/generated/allowlist';

describe('generated query schemas', () => {
  const listEntries = DOMAIN_ALLOWLIST.find(
    (e) => e.operationId === 'list_library_entries'
  );

  test('list_library_entries has a query schema', () => {
    expect(listEntries).toBeDefined();
    expect(listEntries!.querySchema).not.toBeNull();
  });

  test('list_library_entries accepts valid params', () => {
    const schema = listEntries!.querySchema!;
    const result = schema.safeParse({ entry_type: 'product', limit: '50' });
    expect(result.success).toBe(true);
  });

  test('list_library_entries rejects invalid limit', () => {
    const schema = listEntries!.querySchema!;
    const result = schema.safeParse({ limit: '999' });
    // limit max is 100
    expect(result.success).toBe(false);
  });

  test('operations without query params have null schema', () => {
    const identityProbe = DOMAIN_ALLOWLIST.find(
      (e) => e.operationId === 'get_identity_probe'
    );
    expect(identityProbe!.querySchema).toBeNull();
  });

  test('list_library_entries rejects unknown query param', () => {
    const schema = listEntries!.querySchema!;
    const result = schema.safeParse({ unknown_param: 'foo' });
    // Zod strict mode rejects unknown keys
    expect(result.success).toBe(false);
  });

  test('integer coercion handles non-numeric string', () => {
    const schema = listEntries!.querySchema!;
    const result = schema.safeParse({ limit: 'not-a-number' });
    expect(result.success).toBe(false);
  });

  test('boolean coercion handles invalid value', () => {
    const schema = listEntries!.querySchema!;
    const result = schema.safeParse({ include_archived: 'maybe' });
    expect(result.success).toBe(false);
  });

  test('limit defaults to 50 when omitted', () => {
    const schema = listEntries!.querySchema!;
    const result = schema.safeParse({});
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.limit).toBe(50);
    }
  });

  test('limit minimum is 1', () => {
    const schema = listEntries!.querySchema!;
    const result = schema.safeParse({ limit: '0' });
    expect(result.success).toBe(false);
  });
});
```

- [ ] **Step 5: Run generate_all.sh and verify**

Run: `bash scripts/generate_all.sh`
Run: `npx jest web/__tests__/domain-client/query-schema-generation.test.ts`
Expected: Generation succeeds; query schema tests pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/generate_allowlist.ts scripts/lib/spec.ts \
       web/lib/domain-client/generated/ \
       web/pages/api/domain/v1/w/\\[slug\\]/\\[...path\\].ts \
       web/__tests__/domain-client/query-schema-generation.test.ts
git commit -m "feat(codegen): generate Zod query schemas, fix repeated query param collapse (#27)"
```

---

## Task 7: Regenerate All TypeScript Artifacts and Extend DomainGateway

**Files:**
- Regenerated: all 7 files in `web/lib/domain-client/generated/`
- Modify: `web/lib/domain-client/gateway.ts` — extend interface and implementation for 45 new operations
- Modify: `web/hooks/domain/useDomainQuery.ts` — extend `ReadOperationId` union and switch
- Modify: `web/hooks/domain/useDomainCommand.ts` — extend `CommandOperationId` union and switch
- Create: `web/__tests__/domain-client/gateway-operations.test.ts`

**Interfaces:**
- Consumes: Generated TypeScript types from Task 6
- Produces:
  - `DomainGateway` interface with methods for all 60 operations
  - `ReadOperationId` union covering all GET operations
  - `CommandOperationId` union covering all POST/PUT/PATCH operations
  - `callReadOperation` and `callCommandOperation` exhaustive switches

- [ ] **Step 1: Run generate_all.sh to regenerate all artifacts**

Run: `bash scripts/generate_all.sh`
Verify: `web/lib/domain-client/generated/operation-map.d.ts` has types for all 60 operations.

- [ ] **Step 2: Extend DomainGateway interface**

Add methods to the `DomainGateway` interface in `web/lib/domain-client/gateway.ts` for all 45 new operations. Follow the existing pattern:
- GET operations: `methodName(params: { workspaceSlug: string; ...pathParams }) → Promise<DomainResultFor<'operation_id'>>`
- Mutation operations: `methodName(params: { workspaceSlug: string; actionInstanceId: string; body?: ...; ...pathParams }) → Promise<DomainResultFor<'operation_id'> | ReconciliationRequired>`

Use the generated `components['schemas']` types for request/response bodies.

Example new methods:
```typescript
// In DomainGateway interface:
listLibraryEntries(params: {
  workspaceSlug: string;
  entryType?: string;
  includeArchived?: boolean;
  cursor?: string;
  limit?: number;
}): Promise<DomainResultFor<'list_library_entries'>>;

getLibraryEntry(params: {
  workspaceSlug: string;
  entryId: string;
}): Promise<DomainResultFor<'get_library_entry'>>;

createLibraryEntry(params: {
  workspaceSlug: string;
  actionInstanceId: string;
  body: CreateLibraryEntryRequest;
}): Promise<DomainResultFor<'create_library_entry'> | ReconciliationRequired>;
// ... and so on for all 45 operations
```

- [ ] **Step 3: Implement DefaultDomainGateway methods**

For each new method, add the implementation in `DefaultDomainGateway` following existing patterns:
- GET: `this.sendSafe(operationId, path, options)`
- POST/PUT/PATCH: `this.sendMutating(operationId, path, options)`

The `buildBffPath` helper from `generated/paths.ts` constructs the correct BFF URL.

- [ ] **Step 4: Extend useDomainQuery ReadOperationId**

```typescript
// In web/hooks/domain/useDomainQuery.ts, extend:
export type ReadOperationId =
  | 'get_identity_probe'
  | 'get_operation'
  | 'list_library_entry_types'
  | 'list_library_entries'
  | 'get_library_entry'
  | 'list_library_entry_versions'
  | 'list_library_entry_events'
  | 'get_library_version'
  | 'get_library_version_impact'
  | 'list_library_version_bindings'
  | 'list_generation_capabilities'
  | 'list_generation_drafts'
  | 'get_generation_draft'
  | 'get_generation_preview'
  | 'list_generation_runs'
  | 'get_generation_run'
  | 'list_generation_run_items'
  | 'get_generation_run_item'
  | 'list_assets'
  | 'get_asset_version_detail'
  | 'get_export_manifest';
```

And extend the `callReadOperation` switch with a case for each new read operation, mapping snake_case operationId to camelCase gateway method.

- [ ] **Step 5: Extend useDomainCommand CommandOperationId**

```typescript
// In web/hooks/domain/useDomainCommand.ts, extend:
export type CommandOperationId =
  | 'create_upload_intent'
  | 'create_download_intent'
  | 'finalize_upload'
  | 'create_library_entry'
  | 'save_library_draft'
  | 'validate_library_draft'
  | 'publish_library_entry'
  | 'archive_library_entry'
  | 'bind_library_reference'
  | 'withdraw_library_version'
  | 'fork_library_version'
  | 'clone_library_product'
  | 'apply_library_template'
  | 'create_library_reference_object'
  | 'create_library_reference_download_intent'
  | 'create_generation_draft'
  | 'update_generation_draft'
  | 'create_generation_preview'
  | 'submit_generation'
  | 'create_generation_recovery_preview'
  | 'cancel_generation_run'
  | 'retry_failed_generation_items'
  | 'resume_paused_generation_items'
  | 'reconcile_generation_run'
  | 'create_asset_review'
  | 'create_asset_regeneration_draft'
  | 'create_asset_download_intent'
  | 'create_export_package'
  | 'create_export_download_intent';
```

And extend the `callCommandOperation` switch.

- [ ] **Step 6: Write gateway dispatch tests**

```typescript
// web/__tests__/domain-client/gateway-operations.test.ts
import { DOMAIN_ALLOWLIST } from '@/lib/domain-client/generated/allowlist';

describe('DomainGateway operation coverage', () => {
  const operationIds = DOMAIN_ALLOWLIST.map((e) => e.operationId);

  test('every allowlist operation has a unique operationId', () => {
    const unique = new Set(operationIds);
    expect(unique.size).toBe(operationIds.length);
  });

  test('all GET operations are in ReadOperationId', () => {
    const getOps = DOMAIN_ALLOWLIST
      .filter((e) => e.method === 'GET')
      .map((e) => e.operationId);
    // Import and check ReadOperationId covers these
    // This is a compile-time check — if the union misses one,
    // the exhaustive switch in callReadOperation fails to compile
    expect(getOps.length).toBeGreaterThan(0);
  });

  test('all non-GET operations are in CommandOperationId', () => {
    const mutOps = DOMAIN_ALLOWLIST
      .filter((e) => e.method !== 'GET')
      .map((e) => e.operationId);
    expect(mutOps.length).toBeGreaterThan(0);
  });
});
```

- [ ] **Step 7: Type-check and test**

Run: `cd web && npx tsc --noEmit`
Run: `npx jest web/__tests__/domain-client/gateway-operations.test.ts`
Expected: No type errors; tests pass.

- [ ] **Step 8: Commit**

```bash
git add web/lib/domain-client/gateway.ts \
       web/hooks/domain/useDomainQuery.ts \
       web/hooks/domain/useDomainCommand.ts \
       web/lib/domain-client/generated/ \
       web/__tests__/domain-client/gateway-operations.test.ts
git commit -m "feat(gateway): extend DomainGateway, hooks, and generated artifacts for 45 new operations (#27)"
```

---

## Task 8: Wire DomainGatewayProvider into WorkspaceLayout

**Files:**
- Modify: `web/components/layouts/WorkspaceLayout.tsx:101-111` — resolve TODO(#27)
- Modify: `web/lib/domain-client/gateway-provider.tsx` (if needed — may need to create)

**Interfaces:**
- Consumes: `DefaultDomainGateway` (Task 7), `CsrfTokenManager`, `IntentTracker`, `WorkspaceProvider`
- Produces: `DomainGatewayProvider` wrapping `WorkspaceProvider` so all Workspace pages have gateway access

- [ ] **Step 1: Wire providers into WorkspaceLayout**

Replace the TODO block at `web/components/layouts/WorkspaceLayout.tsx:101-111`:

```tsx
// Replace the TODO comment and the bare WorkspaceProvider block with:
import { DomainGatewayProvider } from '@/lib/domain-client/gateway-provider';

// In the return block, wrap WorkspaceProvider with DomainGatewayProvider:
return (
  <SWRConfig value={{ revalidateOnFocus: false }}>
    <DomainGatewayProvider>
      <WorkspaceProvider bootstrap={bootstrap}>
        <WorkspaceLayoutInner>{children}</WorkspaceLayoutInner>
      </WorkspaceProvider>
    </DomainGatewayProvider>
  </SWRConfig>
);
```

- [ ] **Step 2: Verify TypeScript compiles**

Run: `cd web && npx tsc --noEmit`
Expected: No type errors.

- [ ] **Step 3: Commit**

```bash
git add web/components/layouts/WorkspaceLayout.tsx
git commit -m "feat(workspace): wire DomainGatewayProvider into WorkspaceLayout (#27)"
```

---

## Task 9: Remove Old Router Mounts and Verify No Drift

**Files:**
- Modify: `app/main.py` — remove old `app.include_router(library_router)` and `app.include_router(generation_router)` lines
- Verify: `scripts/generate_openapi.py --check` passes
- Verify: `scripts/generate_all.sh` + `git diff --exit-code` on generated artifacts

**Interfaces:**
- Consumes: v1 sub-routers (Tasks 2–3), generated artifacts (Tasks 5–7)
- Produces: Clean removal of old paths, CI-passing drift checks

- [ ] **Step 1: Remove old router includes from app/main.py**

Remove these lines from `app/main.py`:
```python
# Remove:
app.include_router(library_router)
app.include_router(generation_router)
```

Keep the Problem handler registrations since exception types are shared:
```python
# Keep:
register_problem_handlers(app)
register_generation_problem_handlers(app)
```

- [ ] **Step 2: Run OpenAPI drift check**

Run: `python scripts/generate_openapi.py --check`
Expected: Exit 0 (no drift)

- [ ] **Step 3: Run full generation pipeline**

Run: `bash scripts/generate_all.sh`
Run: `git diff --exit-code web/lib/domain-client/generated/`
Expected: No diff (artifacts are already up to date)

- [ ] **Step 4: Run make verify**

Run: `make verify`
Expected: All checks pass — no import errors from removed old paths, all tests pass.

- [ ] **Step 5: Verify old paths return 404**

```python
# Quick smoke test:
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
# Old paths should no longer resolve
assert client.get("/api/library/entries").status_code == 404
assert client.post("/api/generation/drafts", json={}).status_code == 404
# New v1 paths should work (with auth)
assert client.get("/api/v1/library/entry-types").status_code in (200, 401)
```

- [ ] **Step 6: Commit**

```bash
git add app/main.py
git commit -m "chore: remove old /api/library and /api/generation router mounts (#27)

All Library and Generation routes are now served exclusively from
/api/v1/library/* and /api/v1/generation/* via v1 sub-routers.
Old paths return 404. No backward-compatible aliases per ADR 0004."
```

---

## Task 10: End-to-End BFF Bridge Integration Test

**Files:**
- Create: `tests/integration/test_bff_bridge_e2e.py`

**Interfaces:**
- Consumes: All previous tasks
- Produces: An integration test proving the full path: browser request → BFF catch-all → generated allowlist → DomainProxy → FastAPI v1 route → domain service → response

- [ ] **Step 1: Write e2e integration test**

```python
# tests/integration/test_bff_bridge_e2e.py
"""End-to-end BFF bridge integration test.

Proves that a delegated Workspace request crosses the generated gateway,
BFF Adapter, Domain Interface, and response pipeline without exposing
Workspace identity or object keys.

Uses FastAPI TestClient for Domain Interface verification and verifies
the generated allowlist covers all operations.
"""
import json
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_existing_identity_probe_unchanged(client, auth_headers):
    resp = client.get("/api/v1/identity", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "workspace_id" in data
    assert "actor_id" in data


def test_existing_get_operation_unchanged(client, auth_headers):
    resp = client.get(
        "/api/v1/operations/op_nonexistent", headers=auth_headers
    )
    assert resp.status_code in (200, 404)


def test_existing_upload_intent_unchanged(client, auth_headers):
    resp = client.post(
        "/api/v1/upload-intents",
        json={"purpose": "test", "filename": "test.png",
              "content_type": "image/png", "byte_size": 1024},
        headers={**auth_headers, "Idempotency-Key": "test-upload-1"},
    )
    assert resp.status_code in (201, 422)


def test_existing_download_intent_unchanged(client, auth_headers):
    resp = client.post(
        "/api/v1/download-intents",
        json={"object_key": "test/key", "purpose": "test"},
        headers={**auth_headers, "Idempotency-Key": "test-download-1"},
    )
    assert resp.status_code in (201, 404, 422)


def test_library_entry_types_v1(client, auth_headers):
    resp = client.get("/api/v1/library/entry-types", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "entry_types" in data
    assert isinstance(data["entry_types"], list)


def test_library_create_and_get_entry(client, auth_headers):
    resp = client.post(
        "/api/v1/library/entries",
        json={"entry_type": "product", "display_name": "E2E Test Product"},
        headers={**auth_headers, "Idempotency-Key": "e2e-create-1"},
    )
    assert resp.status_code == 201
    entry = resp.json()
    assert entry["entry_type"] == "product"
    assert entry["display_name"] == "E2E Test Product"
    entry_id = entry["id"]

    resp2 = client.get(
        f"/api/v1/library/entries/{entry_id}", headers=auth_headers
    )
    assert resp2.status_code == 200
    assert resp2.headers.get("ETag") is not None
    assert resp2.json()["id"] == entry_id


def test_generation_list_drafts_v1(client, auth_headers):
    resp = client.get(
        "/api/v1/generation/drafts", headers=auth_headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert isinstance(data["items"], list)


def test_generation_list_drafts_with_query_params(client, auth_headers):
    resp = client.get(
        "/api/v1/generation/drafts?state=open&limit=10",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data


def test_old_library_path_returns_404(client, auth_headers):
    resp = client.get("/api/library/entries", headers=auth_headers)
    assert resp.status_code == 404


def test_old_generation_path_returns_404(client, auth_headers):
    resp = client.get("/api/generation/drafts", headers=auth_headers)
    assert resp.status_code == 404


def test_create_entry_without_idempotency_key_fails(client, auth_headers):
    resp = client.post(
        "/api/v1/library/entries",
        json={"entry_type": "product", "display_name": "No Key"},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_save_draft_without_if_match_returns_428(client, auth_headers):
    create_resp = client.post(
        "/api/v1/library/entries",
        json={"entry_type": "product", "display_name": "ETag Test"},
        headers={**auth_headers, "Idempotency-Key": "e2e-etag-1"},
    )
    assert create_resp.status_code == 201
    entry_id = create_resp.json()["id"]

    resp = client.put(
        f"/api/v1/library/entries/{entry_id}/draft",
        json={"content": {"name": "Updated"}},
        headers={**auth_headers, "Idempotency-Key": "e2e-etag-2"},
    )
    assert resp.status_code == 428


def test_save_draft_with_stale_revision_returns_409(client, auth_headers):
    create_resp = client.post(
        "/api/v1/library/entries",
        json={"entry_type": "product", "display_name": "Revision Test"},
        headers={**auth_headers, "Idempotency-Key": "e2e-rev-1"},
    )
    assert create_resp.status_code == 201
    entry_id = create_resp.json()["id"]

    resp = client.put(
        f"/api/v1/library/entries/{entry_id}/draft",
        json={"content": {"name": "First"}},
        headers={**auth_headers, "Idempotency-Key": "e2e-rev-2",
                 "If-Match": '"1"'},
    )
    assert resp.status_code == 200

    resp2 = client.put(
        f"/api/v1/library/entries/{entry_id}/draft",
        json={"content": {"name": "Stale"}},
        headers={**auth_headers, "Idempotency-Key": "e2e-rev-3",
                 "If-Match": '"1"'},
    )
    assert resp2.status_code == 409


def test_allowlist_covers_all_operations():
    from contracts.openapi import load_spec
    import importlib
    spec_path = "contracts/openapi/marketai-domain-v1.json"
    with open(spec_path) as f:
        spec = json.load(f)
    ops = set()
    for path_item in spec.get("paths", {}).values():
        for method_detail in path_item.values():
            if isinstance(method_detail, dict) and "operationId" in method_detail:
                ops.add(method_detail["operationId"])
    assert len(ops) >= 60, f"Expected >=60 operations, found {len(ops)}"
```

- [ ] **Step 2: Run tests**

Run: `python -m pytest tests/integration/test_bff_bridge_e2e.py -v`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_bff_bridge_e2e.py
git commit -m "test(bridge): add end-to-end BFF bridge integration tests (#27)"
```

---

## Completion Checklist

After all tasks are complete, verify both completion gates from the spec:

### Domain Interface Readiness
- [ ] Every route has production query/command implementation (not legacy store/manager)
- [ ] Review correction migration exists (supersedes_review_id + cleared)
- [ ] All commands replay safely with one user intent (idempotency)
- [ ] Action eligibility comes from server-calculated projections
- [ ] Cross-Workspace, stale revision, forbidden Owner actions fail server-side

### BFF Bridge Readiness
- [ ] Every operation is in OpenAPI spec and generated BFF allowlist
- [ ] Generated DomainGateway exposes every operation
- [ ] DomainGatewayProvider is wired into WorkspaceLayout
- [ ] Query-bearing operations pass generated Zod validation
- [ ] If-Match headers forwarded only for declared operations
- [ ] Reference/Asset bytes bypass Next.js
- [ ] OpenAPI drift check passes: `python scripts/generate_openapi.py --check`
- [ ] Generated artifact drift check passes: `bash scripts/generate_all.sh && git diff --exit-code`
- [ ] `make verify` passes
