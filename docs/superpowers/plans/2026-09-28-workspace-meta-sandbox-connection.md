# Workspace Meta Sandbox Connection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an Owner-only Workspace Settings section for creating and viewing a `customer_sandbox` Meta connection, with same-origin BFF routing, so the golden-path Step 0.9 passes.

**Architecture:** The FastAPI domain service (`app/meta/connections.py`) gains Owner-role enforcement, a duplicate-connection guard, and server-side sandbox `ad_account_id` generation. A new list endpoint lets the UI discover existing connections after page refresh. The control-plane BFF proxy (`web/pages/api/domain/v1/w/[slug]/[...path].ts`) already routes to these endpoints via the generated allowlist; after regenerating contracts with explicit `operation_id` values, the frontend wires typed hooks and a new `MetaConnectionSection` component into the existing Settings page.

**Tech Stack:** Python/FastAPI/SQLAlchemy (backend), Next.js/React/TypeScript/SWR (frontend), Playwright (e2e), pytest (backend tests), Jest (frontend tests)

**Spec:** Issue [#119](https://github.com/heart-cannot-beat/marketai-archive/issues/119)

## Global Constraints

- `META_WRITE_ACCESS_TOKEN` stays empty; no Meta write operations
- `customer_sandbox` is the only mode exposed to SaaS users (ADR 0009)
- `ad_account_id` MUST start with `act_` — downstream code auto-prepends the prefix when missing (`app/launch/service.py:573`, `app/experiments/service.py:233`)
- Generated files must stay in sync: CI runs `git diff --exit-code` after `scripts/generate_all.sh`
- Mode select MUST be a native `<select>` element (Playwright `selectOption` requires it)
- Negative tests go in pytest (`tests/`) or jest (`web/__tests__/`), not in `web/e2e/workspace/` (CI only runs `--project golden-path`)

---

### Task 1: Service layer — Owner enforcement, sandbox ID, duplicate guard

**Files:**
- Modify: `app/meta/connections.py`
- Modify: `tests/test_meta_connection_lifecycle.py`

**Interfaces:**
- Consumes: `TenantContext.role` (`app/platform/tenancy.py:111`), `MetaConnectionRecord` (`app/meta/models.py`), partial unique index `uq_meta_connection_active` (`app/meta/models.py:87`)
- Produces: `MetaForbidden` exception class, `ConnectionAlreadyExists` exception class, `_sandbox_ad_account_id(workspace_id: str) -> str` (used by Task 2 tests), updated `create_connection(session, *, context, ad_account_id=None, scenario_config=None)` signature

- [ ] **Step 1: Write failing test — Member rejected from create_connection**

Add to `tests/test_meta_connection_lifecycle.py`:

```python
def test_create_connection_member_forbidden(db_session):
    ws = _workspace(db_session)
    ctx = TenantContext(
        workspace_id=ws.id, role="member",
        initiator_id="usr_a", meta_mode="customer_sandbox",
    )
    with pytest.raises(connections.MetaForbidden):
        connections.create_connection(db_session, context=ctx, ad_account_id="act_test_123")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_meta_connection_lifecycle.py::test_create_connection_member_forbidden -v`
Expected: FAIL — `AttributeError: module 'app.meta.connections' has no attribute 'MetaForbidden'`

- [ ] **Step 3: Add error classes and `_require_owner` to `app/meta/connections.py`**

After the existing `ConnectionLifecycleError` class (line 54–55), add:

```python
class MetaForbidden(ConnectionLifecycleError):
    """Raised when the caller's role is insufficient for Meta connection management."""


class ConnectionAlreadyExists(ConnectionLifecycleError):
    """Raised when an active connection already exists for (workspace_id, meta_mode)."""


def _require_owner(context: TenantContext) -> None:
    if context.role not in ("owner", "system"):
        raise MetaForbidden(
            f"只有 Owner 可以管理 Meta 连接 (当前角色: {context.role!r})"
        )
```

- [ ] **Step 4: Add `_require_owner()` call to `create_connection`**

In `create_connection` (line 149), add immediately after `meta_mode = _require_meta_mode(context)` (line 172):

```python
    _require_owner(context)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_meta_connection_lifecycle.py::test_create_connection_member_forbidden -v`
Expected: PASS

- [ ] **Step 6: Update all 14 existing tests from `role="member"` to `role="owner"`**

In `tests/test_meta_connection_lifecycle.py`, replace every occurrence of `role="member"` with `role="owner"`. All 14 tests use this pattern in their `TenantContext` construction. Use a global find-and-replace within the file — every `role="member"` in this file is a connection lifecycle test context.

- [ ] **Step 7: Run existing tests to verify they still pass**

Run: `python -m pytest tests/test_meta_connection_lifecycle.py -v`
Expected: all 14 existing tests PASS, plus the new `test_create_connection_member_forbidden` PASS

- [ ] **Step 8: Write failing test — Member rejected from retire_connection**

Add to `tests/test_meta_connection_lifecycle.py`:

```python
def test_retire_connection_member_forbidden(db_session):
    ws = _workspace(db_session)
    owner_ctx = TenantContext(
        workspace_id=ws.id, role="owner",
        initiator_id="usr_a", meta_mode="customer_sandbox",
    )
    conn = connections.create_connection(
        db_session, context=owner_ctx, ad_account_id="act_test_retire",
    )
    member_ctx = TenantContext(
        workspace_id=ws.id, role="member",
        initiator_id="usr_b", meta_mode="customer_sandbox",
    )
    with pytest.raises(connections.MetaForbidden):
        connections.retire_connection(db_session, context=member_ctx, connection_id=conn.id)
```

- [ ] **Step 9: Add `_require_owner()` to `retire_connection`**

In `retire_connection` (line 343), add immediately after `connection = _get_connection(...)` (line 360):

```python
    _require_owner(context)
```

- [ ] **Step 10: Run test to verify it passes**

Run: `python -m pytest tests/test_meta_connection_lifecycle.py::test_retire_connection_member_forbidden -v`
Expected: PASS

- [ ] **Step 11: Write failing test — duplicate connection rejected**

```python
def test_create_connection_duplicate_rejected(db_session):
    ws = _workspace(db_session)
    ctx = TenantContext(
        workspace_id=ws.id, role="owner",
        initiator_id="usr_a", meta_mode="customer_sandbox",
    )
    connections.create_connection(db_session, context=ctx, ad_account_id="act_first")
    with pytest.raises(connections.ConnectionAlreadyExists):
        connections.create_connection(db_session, context=ctx, ad_account_id="act_second")
```

- [ ] **Step 12: Implement `_check_no_active_connection` and add to `create_connection`**

Add after `_require_owner` function:

```python
def _check_no_active_connection(
    session: Session, *, workspace_id: str, meta_mode: str,
) -> None:
    existing = session.execute(
        select(MetaConnectionRecord).where(
            MetaConnectionRecord.workspace_id == workspace_id,
            MetaConnectionRecord.meta_mode == meta_mode,
            MetaConnectionRecord.state == "active",
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ConnectionAlreadyExists(
            f"active connection already exists for workspace "
            f"{workspace_id!r} mode {meta_mode!r}: {existing.id!r}"
        )
```

In `create_connection`, add after `_require_owner(context)`:

```python
    _check_no_active_connection(
        session, workspace_id=context.workspace_id, meta_mode=meta_mode,
    )
```

- [ ] **Step 13: Run test to verify it passes**

Run: `python -m pytest tests/test_meta_connection_lifecycle.py::test_create_connection_duplicate_rejected -v`
Expected: PASS

- [ ] **Step 14: Write failing test — sandbox ad_account_id auto-generated**

```python
def test_create_connection_sandbox_generates_ad_account_id(db_session):
    ws = _workspace(db_session)
    ctx = TenantContext(
        workspace_id=ws.id, role="owner",
        initiator_id="usr_a", meta_mode="customer_sandbox",
    )
    conn = connections.create_connection(db_session, context=ctx)
    assert conn.ad_account_id.startswith("act_sandbox_")
    assert len(conn.ad_account_id) == len("act_sandbox_") + 16
```

- [ ] **Step 15: Implement `_sandbox_ad_account_id` and update `create_connection` signature**

Add after the `import hashlib` (already present at line 28):

```python
def _sandbox_ad_account_id(workspace_id: str) -> str:
    h = hashlib.sha256(workspace_id.encode()).hexdigest()[:16]
    return f"act_sandbox_{h}"
```

Change `create_connection` signature from `ad_account_id: str` to `ad_account_id: str | None = None`. After `_check_no_active_connection(...)`, add:

```python
    if ad_account_id is None:
        if meta_mode in SANDBOX_MODES:
            ad_account_id = _sandbox_ad_account_id(context.workspace_id)
        else:
            raise ConnectionLifecycleError(
                "ad_account_id is required for non-sandbox connections"
            )
```

- [ ] **Step 16: Run test to verify it passes**

Run: `python -m pytest tests/test_meta_connection_lifecycle.py::test_create_connection_sandbox_generates_ad_account_id -v`
Expected: PASS

- [ ] **Step 17: Add deferred-role-check comment on `create_binding` and `supersede_binding`**

At the top of `create_binding` (line 216), add:

```python
    # TODO(#119): Owner role check deferred — create_binding/supersede_binding
    # currently allow any role. Add _require_owner() when binding management
    # surfaces in the UI.
```

Add the same comment at the top of `supersede_binding` (line 256).

- [ ] **Step 18: Run full test suite and commit**

Run: `python -m pytest tests/test_meta_connection_lifecycle.py -v`
Expected: all tests PASS (14 updated + 4 new)

```bash
git add app/meta/connections.py tests/test_meta_connection_lifecycle.py
git commit -m "$(cat <<'EOF'
feat(meta): Owner enforcement, duplicate guard, sandbox ad_account_id

- create_connection and retire_connection now require role=owner|system
- Pre-check for existing active connection → ConnectionAlreadyExists
- Sandbox modes auto-generate act_sandbox_{sha256[:16]} when no ad_account_id
- Updated 14 existing lifecycle tests from role=member to role=owner
- Added 4 new tests: member forbidden (create/retire), duplicate, sandbox ID

Issue #119

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: API layer — operation_ids, list endpoint, error mapping, API tests

**Files:**
- Modify: `app/meta/api.py`
- Modify: `tests/test_meta_api_routes.py`

**Interfaces:**
- Consumes: `MetaForbidden`, `ConnectionAlreadyExists` from Task 1; existing `_paginate`, `_connection_dict`, `_effective_context` helpers in `app/meta/api.py`
- Produces: `list_meta_connections` endpoint (`GET /api/v1/meta/connections?meta_mode=...`); explicit `operation_id` on all 9 routes; `CreateConnectionRequest.ad_account_id` becomes `Optional[str]`

- [ ] **Step 1: Write failing test — Member gets 403 on create**

Add to `tests/test_meta_api_routes.py` inside the existing `MetaApiRoutesTestCase` class:

```python
def test_create_connection_member_forbidden(self):
    """Member role → 403 from service-layer Owner check."""
    self.app.dependency_overrides[request_tenant_context] = lambda: TenantContext(
        workspace_id=self.ws.id, role="member",
        initiator_id="usr_member", meta_mode=None,
    )
    resp = self.client.post("/api/v1/meta/connections", json={
        "meta_mode": "customer_sandbox", "ad_account_id": "act_test_member",
    })
    self.assertEqual(resp.status_code, 403)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_meta_api_routes.py::MetaApiRoutesTestCase::test_create_connection_member_forbidden -v`
Expected: FAIL — returns 201 instead of 403

- [ ] **Step 3: Update `_connection_error_status` to handle new exception types**

In `app/meta/api.py`, replace the existing `_connection_error_status` function (line 175–176):

```python
def _connection_error_status(exc: connections_service.ConnectionLifecycleError) -> int:
    if isinstance(exc, connections_service.MetaForbidden):
        return 403
    if isinstance(exc, connections_service.ConnectionAlreadyExists):
        return 409
    return 404 if "not found" in str(exc) else 400
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_meta_api_routes.py::MetaApiRoutesTestCase::test_create_connection_member_forbidden -v`
Expected: PASS

- [ ] **Step 5: Write failing test — duplicate creation returns 409**

```python
def test_create_connection_duplicate_returns_409(self):
    """Second create for same (workspace, mode) → 409, original unchanged."""
    resp1 = self.client.post("/api/v1/meta/connections", json={
        "meta_mode": "customer_sandbox", "ad_account_id": "act_dup_test",
    })
    self.assertEqual(resp1.status_code, 201)
    original_id = resp1.json()["id"]

    resp2 = self.client.post("/api/v1/meta/connections", json={
        "meta_mode": "customer_sandbox", "ad_account_id": "act_dup_test_2",
    })
    self.assertEqual(resp2.status_code, 409)

    # Original connection unchanged
    resp3 = self.client.get(
        f"/api/v1/meta/connections/{original_id}",
        params={"meta_mode": "customer_sandbox"},
    )
    self.assertEqual(resp3.status_code, 200)
    self.assertEqual(resp3.json()["state"], "active")
    self.assertEqual(resp3.json()["ad_account_id"], "act_dup_test")
```

- [ ] **Step 6: Run to verify it fails, then verify it passes**

The pre-check from Task 1 already raises `ConnectionAlreadyExists` before reaching the unique index. With the updated `_connection_error_status` from Step 3, this should already pass. Run:

`python -m pytest tests/test_meta_api_routes.py::MetaApiRoutesTestCase::test_create_connection_duplicate_returns_409 -v`

If it passes, good. If the IntegrityError path is hit instead (concurrent race), add the concurrency guard in the next step.

- [ ] **Step 7: Add IntegrityError concurrency guard to create endpoint**

In `app/meta/api.py`, update the `create_connection_v1` function. Wrap the `with tenant_session(effective)` block in a try/except for `IntegrityError`:

```python
@router_v1.post("/connections", status_code=201, operation_id="create_meta_connection")
def create_meta_connection(
    body: CreateConnectionRequest, context: TenantContext = Depends(request_tenant_context),
):
    effective = _effective_context(context, body.meta_mode)
    try:
        with tenant_session(effective) as session:
            try:
                connection = connections_service.create_connection(
                    session, context=effective, ad_account_id=body.ad_account_id,
                )
            except connections_service.ConnectionLifecycleError as exc:
                raise HTTPException(_connection_error_status(exc), str(exc)) from exc
    except IntegrityError as exc:
        if "uq_meta_connection_active" in str(exc).lower():
            raise HTTPException(
                409, {"code": "connection_already_exists",
                      "message": "active connection already exists"},
            ) from exc
        raise
    return _connection_dict(connection)
```

- [ ] **Step 8: Make `ad_account_id` optional in `CreateConnectionRequest`**

Change `CreateConnectionRequest` (line 87–89):

```python
class CreateConnectionRequest(BaseModel):
    meta_mode: str
    ad_account_id: str | None = None
```

- [ ] **Step 9: Add explicit `operation_id` to all existing routes**

Add `operation_id=` to each decorator. The function names stay unchanged:

```python
@router_v1.post("/connections", status_code=201, operation_id="create_meta_connection")
# (already done in Step 7)

@router_v1.get("/connections/{connection_id}", operation_id="get_meta_connection")

@router_v1.post("/connections/{connection_id}/retire", operation_id="retire_meta_connection")

@router_v1.get("/connections/{connection_id}/bindings", operation_id="list_meta_bindings")

@router_v1.post("/connections/{connection_id}/bindings", status_code=201, operation_id="create_meta_binding")

@router_v1.post("/connections/{connection_id}/bindings/{binding_id}/supersede", status_code=201, operation_id="supersede_meta_binding")

@router_v1.get("/contracts", operation_id="list_meta_contracts")

@router_v1.post("/contracts", status_code=201, operation_id="create_meta_contract")
```

- [ ] **Step 10: Write failing test — list endpoint**

```python
def test_list_meta_connections(self):
    """GET /api/v1/meta/connections returns connections for workspace+mode."""
    resp = self.client.get(
        "/api/v1/meta/connections", params={"meta_mode": "customer_sandbox"},
    )
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.json()["items"], [])

    # Create one
    create_resp = self.client.post("/api/v1/meta/connections", json={
        "meta_mode": "customer_sandbox",
    })
    self.assertEqual(create_resp.status_code, 201)

    resp2 = self.client.get(
        "/api/v1/meta/connections", params={"meta_mode": "customer_sandbox"},
    )
    self.assertEqual(resp2.status_code, 200)
    items = resp2.json()["items"]
    self.assertEqual(len(items), 1)
    self.assertEqual(items[0]["state"], "active")
    self.assertTrue(items[0]["ad_account_id"].startswith("act_sandbox_"))
```

- [ ] **Step 11: Implement list endpoint**

Add after the `create_meta_connection` function in `app/meta/api.py`:

```python
@router_v1.get("/connections", operation_id="list_meta_connections")
def list_meta_connections(
    meta_mode: str = Query(...),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    context: TenantContext = Depends(request_tenant_context),
):
    effective = _effective_context(context, meta_mode)
    with tenant_session(effective) as session:
        rows = session.scalars(
            select(MetaConnectionRecord)
            .where(
                MetaConnectionRecord.workspace_id == effective.workspace_id,
                MetaConnectionRecord.meta_mode == effective.meta_mode,
            )
            .order_by(MetaConnectionRecord.created_at)
        ).all()
        items = [_connection_dict(row) for row in rows]
    return _paginate(
        items, context=effective,
        filters_key=f"meta_connections:{effective.meta_mode}",
        cursor=cursor, limit=limit,
    )
```

**Important:** Place this route BEFORE the `get_connection_v1` route (`/connections/{connection_id}`). FastAPI matches routes in order, and `/connections/{connection_id}` would greedily match `?meta_mode=...` as a connection_id path param.

- [ ] **Step 12: Run list test to verify it passes**

Run: `python -m pytest tests/test_meta_api_routes.py::MetaApiRoutesTestCase::test_list_meta_connections -v`
Expected: PASS

- [ ] **Step 13: Write and run cross-workspace test**

```python
def test_create_connection_cross_workspace_denied(self):
    """Connection created in workspace A is not visible from workspace B."""
    resp = self.client.post("/api/v1/meta/connections", json={
        "meta_mode": "customer_sandbox",
    })
    self.assertEqual(resp.status_code, 201)
    conn_id = resp.json()["id"]

    # Switch to workspace B
    ws_b = self._create_workspace("ws_meta_api_cross")
    self.app.dependency_overrides[request_tenant_context] = lambda: TenantContext(
        workspace_id=ws_b.id, role="owner",
        initiator_id="usr_meta_api", meta_mode=None,
    )
    resp2 = self.client.get(
        f"/api/v1/meta/connections/{conn_id}",
        params={"meta_mode": "customer_sandbox"},
    )
    self.assertEqual(resp2.status_code, 404)
```

Note: Check if `_create_workspace` helper exists in the test class. If not, create one following the existing `setUp` pattern — inspect the class to find how `self.ws` is created. The key point is to create a second workspace and override the dependency to use it.

Run: `python -m pytest tests/test_meta_api_routes.py::MetaApiRoutesTestCase::test_create_connection_cross_workspace_denied -v`
Expected: PASS (existing tenant scoping already handles this)

- [ ] **Step 14: Run full API test suite and commit**

Run: `python -m pytest tests/test_meta_api_routes.py -v`
Expected: all existing + 4 new tests PASS

```bash
git add app/meta/api.py tests/test_meta_api_routes.py
git commit -m "$(cat <<'EOF'
feat(meta): list endpoint, explicit operation_ids, 403/409 mapping

- GET /api/v1/meta/connections?meta_mode= lists connections by workspace
- Explicit operation_id on all 9 meta routes (replaces auto-generated names)
- CreateConnectionRequest.ad_account_id now optional (sandbox generates it)
- MetaForbidden → 403, ConnectionAlreadyExists → 409
- IntegrityError on uq_meta_connection_active → 409 (concurrency guard)
- 4 new API route tests: member 403, duplicate 409, list, cross-workspace

Issue #119

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: OpenAPI contracts + regeneration

**Files:**
- Regenerate: `contracts/openapi/marketai-domain-v1.json` (via `scripts/generate_openapi.py`)
- Modify: `contracts/openapi/marketai-domain-v1-policy.json`
- Regenerate: `web/lib/domain-client/generated/*` (via `scripts/generate_all.sh`)

**Interfaces:**
- Consumes: explicit `operation_id` values from Task 2 routes
- Produces: updated `DomainOperationMap` with new operation IDs (`create_meta_connection`, `list_meta_connections`, etc.), updated `DOMAIN_ALLOWLIST`, updated `BFF_ROUTES` in `generated/paths.ts`, updated request/response validators

- [ ] **Step 1: Regenerate OpenAPI spec from FastAPI**

```bash
python scripts/generate_openapi.py
```

Verify: `contracts/openapi/marketai-domain-v1.json` now contains `"operationId": "create_meta_connection"` (not the old auto-generated name) and `"operationId": "list_meta_connections"` (the new endpoint).

```bash
python -c "
import json
with open('contracts/openapi/marketai-domain-v1.json') as f:
    spec = json.load(f)
ops = []
for path, methods in spec.get('paths', {}).items():
    for method, detail in methods.items():
        if 'meta' in path.lower() and isinstance(detail, dict):
            ops.append(detail.get('operationId', 'MISSING'))
print('\n'.join(sorted(ops)))
"
```

Expected output should include: `create_meta_connection`, `get_meta_connection`, `list_meta_connections`, `retire_meta_connection`, `create_meta_binding`, `list_meta_bindings`, `supersede_meta_binding`, `list_meta_contracts`, `create_meta_contract` — no auto-generated long names.

- [ ] **Step 2: Update policy JSON — rename 8 keys and add `list_meta_connections`**

In `contracts/openapi/marketai-domain-v1-policy.json`, rename these keys (preserve their values exactly):

| Old key | New key |
|---------|---------|
| `create_connection_v1_api_v1_meta_connections_post` | `create_meta_connection` |
| `get_connection_v1_api_v1_meta_connections__connection_id__get` | `get_meta_connection` |
| `retire_connection_v1_api_v1_meta_connections__connection_id__retire_post` | `retire_meta_connection` |
| `list_bindings_v1_api_v1_meta_connections__connection_id__bindings_get` | `list_meta_bindings` |
| `create_binding_v1_api_v1_meta_connections__connection_id__bindings_post` | `create_meta_binding` |
| `supersede_binding_v1_api_v1_meta_connections__connection_id__bindings__binding_id__supersede_post` | `supersede_meta_binding` |
| `list_contracts_v1_api_v1_meta_contracts_get` | `list_meta_contracts` |
| `create_contract_v1_api_v1_meta_contracts_post` | `create_meta_contract` |

Add a new entry for the list endpoint:

```json
"list_meta_connections": {
  "bffPattern": "/api/domain/v1/w/:slug/meta/connections",
  "maxRequestBytes": 0,
  "maxResponseBytes": 131072,
  "forwardConditionalHeaders": false
}
```

- [ ] **Step 3: Regenerate all TypeScript domain-client files**

```bash
cd web && bash ../scripts/generate_all.sh
```

The script runs: `openapi-typescript` → `generate_operation_map.ts` → `generate_operation_registry.ts` → `generate_response_validators.ts` → `generate_paths.ts` → `generate_allowlist.ts` → `generate_allowlist_binding.ts` → `prettier --write`.

- [ ] **Step 4: Verify generated files contain new operation IDs**

```bash
grep -c "create_meta_connection" web/lib/domain-client/generated/paths.ts
grep -c "list_meta_connections" web/lib/domain-client/generated/paths.ts
grep -c "create_connection_v1_api_v1" web/lib/domain-client/generated/paths.ts
```

Expected: first two > 0, last one = 0 (old names gone).

- [ ] **Step 5: Verify no drift in generated files**

```bash
cd web && git diff --stat lib/domain-client/generated/
```

All changes should be operation ID renames and the new list endpoint. No unexpected deletions.

- [ ] **Step 6: Commit**

```bash
git add contracts/openapi/ web/lib/domain-client/generated/
git commit -m "$(cat <<'EOF'
chore(contracts): explicit meta operation_ids + list_meta_connections

- Renamed 8 auto-generated operation IDs to explicit names
- Added list_meta_connections to OpenAPI spec and policy
- Regenerated all domain-client TypeScript bindings

Issue #119

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Navigation test ID

**Files:**
- Modify: `web/components/shared/shell/NavigationItems.tsx`
- Modify: `web/components/workspace/WorkspaceNavigation.tsx`
- Modify: `web/__tests__/components/workspace/navigation.spec.tsx`

**Interfaces:**
- Consumes: `MenuItem` interface (`NavigationItems.tsx:4`), `GuardedLink` component
- Produces: `data-testid="workspace-settings-link"` on the Settings nav link (consumed by golden-path fixture `web/e2e/golden-path/fixtures/workspace-setup.ts:23`)

- [ ] **Step 1: Write failing test for data-testid on Settings link**

Add to `web/__tests__/components/workspace/navigation.spec.tsx`:

```typescript
test('Settings link has workspace-settings-link testid', () => {
  // Re-render with Owner access
  jest.mocked(useCanAccess).mockReturnValue({
    canAccess: () => true,
  } as any);
  const { container } = render(
    <WorkspaceNavigation slug="test-ws" activePathname="/w/test-ws/settings" />
  );
  const settingsLink = container.querySelector(
    '[data-testid="workspace-settings-link"]'
  );
  expect(settingsLink).toBeTruthy();
  expect(settingsLink?.textContent).toContain('Settings');
});
```

Note: Check the existing mock setup in the file. The test mocks `useCanAccess` and `NavigationItems`. Since the existing mock replaces `NavigationItems` with a simplified renderer, the `data-testid` must flow through the `MenuItem` → mock renderer. If the mock doesn't render `testId`, update the mock:

```typescript
jest.mock('@/components/shared/shell/NavigationItems', () => ({
  __esModule: true,
  default: ({ menus }: { menus: Array<{ name: string; active: boolean; testId?: string }> }) => (
    <nav>
      {menus.map((m) => (
        <a key={m.name} data-active={m.active} data-testid={m.testId}>
          {m.name}
        </a>
      ))}
    </nav>
  ),
}));
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx jest __tests__/components/workspace/navigation.spec.tsx --verbose`
Expected: FAIL — `data-testid` not found

- [ ] **Step 3: Add `testId` to `MenuItem` interface**

In `web/components/shared/shell/NavigationItems.tsx`, add `testId` to the `MenuItem` interface (line 4–10):

```typescript
export interface MenuItem {
  name: string;
  href: string;
  icon?: any;
  active?: boolean;
  items?: Omit<MenuItem, 'icon' | 'items'>[];
  className?: string;
  testId?: string;
}
```

- [ ] **Step 4: Forward `testId` as `data-testid` in `NavigationItem`**

In `NavigationItem` (line 47–67), add `data-testid={menu.testId}` to the `GuardedLink`:

```typescript
const NavigationItem = ({ menu, className }: NavigationItemProps) => {
  return (
    <GuardedLink
      href={menu.href}
      data-testid={menu.testId}
      className={`group flex items-center rounded text-sm text-gray-900 hover:bg-gray-100 hover:text-gray-900 dark:text-gray-100 dark:hover:text-gray-100 dark:hover:bg-gray-800 px-2 p-2 gap-2 ${
        menu.active ? 'text-white bg-gray-800 font-semibold' : ''
      }${className}`}
    >
```

- [ ] **Step 5: Add `testId` to Settings menu item in `WorkspaceNavigation`**

In `web/components/workspace/WorkspaceNavigation.tsx`, update the Settings item (line 77–82):

```typescript
{
  name: 'Settings',
  href: `/w/${slug}/settings`,
  icon: Cog6ToothIcon,
  active: isActive('settings'),
  testId: 'workspace-settings-link',
},
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd web && npx jest __tests__/components/workspace/navigation.spec.tsx --verbose`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add web/components/shared/shell/NavigationItems.tsx web/components/workspace/WorkspaceNavigation.tsx web/__tests__/components/workspace/navigation.spec.tsx
git commit -m "$(cat <<'EOF'
feat(web): testId on MenuItem, workspace-settings-link on Settings nav

- MenuItem interface now accepts optional testId, forwarded as data-testid
- Settings navigation link carries data-testid="workspace-settings-link"
- Golden-path fixture workspace-setup.ts:23 depends on this selector

Issue #119

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Gateway + hooks wiring

**Files:**
- Modify: `web/lib/domain-client/gateway.ts` (interface ~line 390 + implementation ~line 3490)
- Modify: `web/hooks/domain/useDomainCommand.ts` (type ~line 25 + switch ~line 142)
- Modify: `web/hooks/domain/useDomainQuery.ts` (type ~line 23 + switch ~line 83)

**Interfaces:**
- Consumes: `DomainOperationMap['create_meta_connection']`, `DomainOperationMap['list_meta_connections']`, `DomainOperationMap['get_meta_connection']` from Task 3 regenerated types; `buildBffPath` from `generated/paths.ts`; `DomainResultFor`, `ReconciliationRequired` from `result-types.ts`
- Produces: `useDomainCommand('create_meta_connection')` and `useDomainQuery('list_meta_connections', { meta_mode })` hooks usable by Task 6 component

- [ ] **Step 1: Add gateway interface methods**

In `web/lib/domain-client/gateway.ts`, find the `DomainGateway` interface (abstract class). Add three methods near other Meta-related methods (e.g., after `createMetaLaunchPreview`):

```typescript
createMetaConnection(params: {
  workspaceSlug: string;
  actionInstanceId: string;
  body: DomainOperationMap['create_meta_connection']['requestBody'];
}): Promise<
  DomainResultFor<'create_meta_connection'> | ReconciliationRequired
>;

listMetaConnections(params: {
  workspaceSlug: string;
  metaMode: string;
}): Promise<DomainResultFor<'list_meta_connections'>>;

getMetaConnection(params: {
  workspaceSlug: string;
  connectionId: string;
  metaMode: string;
}): Promise<DomainResultFor<'get_meta_connection'>>;
```

- [ ] **Step 2: Add `DefaultDomainGateway` implementations**

In the `DefaultDomainGateway` class (starts at line 1190), add:

```typescript
async createMetaConnection(params: {
  workspaceSlug: string;
  actionInstanceId: string;
  body: DomainOperationMap['create_meta_connection']['requestBody'];
}): Promise<
  DomainResultFor<'create_meta_connection'> | ReconciliationRequired
> {
  const path = buildBffPath('create_meta_connection', {
    slug: params.workspaceSlug,
  });
  return this.runCommand(
    'create_meta_connection',
    params.actionInstanceId,
    path,
    'POST',
    params.body,
    201,
    'created'
  );
}

async listMetaConnections(params: {
  workspaceSlug: string;
  metaMode: string;
}): Promise<DomainResultFor<'list_meta_connections'>> {
  const path =
    buildBffPath('list_meta_connections', {
      slug: params.workspaceSlug,
    }) + this.buildQuery({ meta_mode: params.metaMode });
  return this.sendSafe<'list_meta_connections'>(path);
}

async getMetaConnection(params: {
  workspaceSlug: string;
  connectionId: string;
  metaMode: string;
}): Promise<DomainResultFor<'get_meta_connection'>> {
  const path =
    buildBffPath('get_meta_connection', {
      slug: params.workspaceSlug,
      connection_id: params.connectionId,
    }) + this.buildQuery({ meta_mode: params.metaMode });
  return this.sendSafe<'get_meta_connection'>(path);
}
```

- [ ] **Step 3: Add `create_meta_connection` to `CommandOperationId`**

In `web/hooks/domain/useDomainCommand.ts`, add to the `CommandOperationId` union type (line 25–100):

```typescript
  | 'create_meta_connection'
```

Add a case to `callCommandOperation` switch (line 142+):

```typescript
    case 'create_meta_connection':
      return gateway.createMetaConnection({
        workspaceSlug,
        actionInstanceId,
        body: body as DomainOperationMap['create_meta_connection']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
```

- [ ] **Step 4: Add `list_meta_connections` and `get_meta_connection` to `ReadOperationId`**

In `web/hooks/domain/useDomainQuery.ts`, add to the `ReadOperationId` union type (line 23–71):

```typescript
  | 'list_meta_connections'
  | 'get_meta_connection'
```

Add cases to `callReadOperation` switch (line 83+):

```typescript
    case 'list_meta_connections': {
      const p =
        params as DomainOperationMap['list_meta_connections']['queryParams'];
      return gateway.listMetaConnections({
        workspaceSlug,
        metaMode: p.meta_mode,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_meta_connection': {
      const p = params as DomainOperationMap['get_meta_connection']['pathParams'] &
        DomainOperationMap['get_meta_connection']['queryParams'];
      return gateway.getMetaConnection({
        workspaceSlug,
        connectionId: p.connection_id,
        metaMode: p.meta_mode,
      }) as Promise<DomainResultFor<K>>;
    }
```

- [ ] **Step 5: Verify TypeScript compiles**

```bash
cd web && npx tsc --noEmit 2>&1 | head -30
```

Expected: no errors related to meta connection types. If errors appear about missing keys in `DomainOperationMap`, verify Task 3 regeneration completed correctly.

- [ ] **Step 6: Commit**

```bash
git add web/lib/domain-client/gateway.ts web/hooks/domain/useDomainCommand.ts web/hooks/domain/useDomainQuery.ts
git commit -m "$(cat <<'EOF'
feat(web): gateway + hook wiring for meta connection operations

- DomainGateway: createMetaConnection, listMetaConnections, getMetaConnection
- CommandOperationId: create_meta_connection
- ReadOperationId: list_meta_connections, get_meta_connection

Issue #119

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: MetaConnectionSection component + settings page

**Files:**
- Create: `web/components/workspace/MetaConnectionSection.tsx`
- Modify: `web/pages/w/[slug]/settings.tsx`
- Modify: `web/components/workspace/index.ts`
- Create: `web/__tests__/components/workspace/MetaConnectionSection.spec.tsx`

**Interfaces:**
- Consumes: `useDomainQuery('list_meta_connections', ...)` from Task 5, `useDomainCommand('create_meta_connection')` from Task 5, `domainQueryKey` from `query-keys.ts`, `ModeProvenance` from `workspace/ModeProvenance.tsx`, `Card` from `shared/Card.tsx`, `useWorkspace()` from `WorkspaceProvider`
- Produces: Six test IDs: `meta-connection-section`, `create-meta-connection`, `meta-connection-mode-select`, `confirm-meta-connection`, `meta-connection-status-active`; consumed by golden-path fixture `web/e2e/golden-path/fixtures/workspace-setup.ts`

- [ ] **Step 1: Write the component test**

Create `web/__tests__/components/workspace/MetaConnectionSection.spec.tsx`:

```tsx
/** @jest-environment jsdom */
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const mockExecute = jest.fn();
const mockRefresh = jest.fn();

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'test-ws' } }),
}));

jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: jest.fn(),
}));

jest.mock('@/hooks/domain/useDomainCommand', () => ({
  useDomainCommand: jest.fn(() => ({
    execute: mockExecute,
    isPending: false,
    result: undefined,
    problem: undefined,
    reconciliation: undefined,
  })),
}));

jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => ({}),
}));

import { MetaConnectionSection } from '@/components/workspace/MetaConnectionSection';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';

const mockUseDomainQuery = jest.mocked(useDomainQuery);

describe('MetaConnectionSection', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  test('shows create button when no connection exists', () => {
    mockUseDomainQuery.mockReturnValue({
      data: { items: [], next_cursor: null },
      problem: undefined,
      isLoading: false,
      isStale: false,
      lastSuccessfulAt: null,
      refresh: mockRefresh,
    } as any);

    render(<MetaConnectionSection />);
    expect(
      screen.getByTestId('meta-connection-section')
    ).toBeInTheDocument();
    expect(
      screen.getByTestId('create-meta-connection')
    ).toBeInTheDocument();
  });

  test('shows active status when connection exists', () => {
    mockUseDomainQuery.mockReturnValue({
      data: {
        items: [
          {
            id: 'mcon_123',
            workspace_id: 'ws_1',
            meta_mode: 'customer_sandbox',
            ad_account_id: 'act_sandbox_abc123',
            state: 'active',
            created_at: '2026-01-01T00:00:00',
            updated_at: '2026-01-01T00:00:00',
            retired_at: null,
          },
        ],
        next_cursor: null,
      },
      problem: undefined,
      isLoading: false,
      isStale: false,
      lastSuccessfulAt: Date.now(),
      refresh: mockRefresh,
    } as any);

    render(<MetaConnectionSection />);
    expect(
      screen.getByTestId('meta-connection-status-active')
    ).toBeInTheDocument();
    expect(
      screen.queryByTestId('create-meta-connection')
    ).not.toBeInTheDocument();
  });

  test('create flow shows mode select and confirm', () => {
    mockUseDomainQuery.mockReturnValue({
      data: { items: [], next_cursor: null },
      problem: undefined,
      isLoading: false,
      isStale: false,
      lastSuccessfulAt: null,
      refresh: mockRefresh,
    } as any);

    render(<MetaConnectionSection />);
    fireEvent.click(screen.getByTestId('create-meta-connection'));
    expect(
      screen.getByTestId('meta-connection-mode-select')
    ).toBeInTheDocument();
    expect(
      screen.getByTestId('confirm-meta-connection')
    ).toBeInTheDocument();
  });

  test('confirm calls create command and refreshes', async () => {
    mockUseDomainQuery.mockReturnValue({
      data: { items: [], next_cursor: null },
      problem: undefined,
      isLoading: false,
      isStale: false,
      lastSuccessfulAt: null,
      refresh: mockRefresh,
    } as any);

    mockExecute.mockResolvedValue({
      kind: 'success',
      data: {
        id: 'mcon_new',
        state: 'active',
        meta_mode: 'customer_sandbox',
        ad_account_id: 'act_sandbox_abc',
      },
      meta: { etag: null, retryAt: null, location: null },
    });

    render(<MetaConnectionSection />);
    fireEvent.click(screen.getByTestId('create-meta-connection'));
    fireEvent.click(screen.getByTestId('confirm-meta-connection'));

    await waitFor(() => {
      expect(mockExecute).toHaveBeenCalledWith(
        expect.objectContaining({
          body: { meta_mode: 'customer_sandbox' },
        })
      );
    });
    await waitFor(() => {
      expect(mockRefresh).toHaveBeenCalled();
    });
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx jest __tests__/components/workspace/MetaConnectionSection.spec.tsx --verbose`
Expected: FAIL — module not found

- [ ] **Step 3: Create `MetaConnectionSection` component**

Create `web/components/workspace/MetaConnectionSection.tsx`:

```tsx
import { useState, useCallback } from 'react';
import { Card } from '@/components/shared';
import {
  ModeProvenance,
  type ProvenanceViewModel,
} from '@/components/workspace/ModeProvenance';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';

const META_MODE = 'customer_sandbox';

function modeToProvenance(mode: string): ProvenanceViewModel {
  if (mode === 'customer_sandbox') return { mode: 'sandbox' };
  if (mode === 'controlled_mock') return { mode: 'controlled-mock' };
  return { mode: 'sandbox' };
}

export function MetaConnectionSection() {
  const [showCreate, setShowCreate] = useState(false);
  const [selectedMode, setSelectedMode] = useState<string>(META_MODE);

  const {
    data: listData,
    isLoading,
    refresh,
  } = useDomainQuery('list_meta_connections', { meta_mode: META_MODE });

  const { execute, isPending } = useDomainCommand('create_meta_connection');

  const activeConnection =
    listData && 'items' in (listData as any)
      ? (listData as any).items?.find(
          (c: { state: string }) => c.state === 'active'
        )
      : null;

  const handleCreate = useCallback(async () => {
    await execute({
      actionInstanceId: crypto.randomUUID(),
      body: { meta_mode: selectedMode },
    });
    setShowCreate(false);
    await refresh();
  }, [execute, selectedMode, refresh]);

  return (
    <div data-testid="meta-connection-section">
      <Card>
        <Card.Body>
          <Card.Header>
            <Card.Title>Meta Connection</Card.Title>
            <Card.Description>
              Connect your Workspace to a Meta ad account for experiments and
              scale monitoring.
            </Card.Description>
          </Card.Header>

          {isLoading && <p className="text-sm text-gray-500">Loading...</p>}

          {activeConnection && (
            <div className="flex flex-col gap-3">
              <div
                className="flex items-center gap-2"
                data-testid="meta-connection-status-active"
              >
                <span className="badge badge-sm badge-success">Active</span>
                <ModeProvenance
                  provenance={modeToProvenance(activeConnection.meta_mode)}
                />
              </div>
              <dl className="grid grid-cols-2 gap-2 text-sm">
                <dt className="text-gray-500">Connection ID</dt>
                <dd className="font-mono text-xs">{activeConnection.id}</dd>
                <dt className="text-gray-500">Ad Account</dt>
                <dd className="font-mono text-xs">
                  {activeConnection.ad_account_id}
                </dd>
              </dl>
            </div>
          )}

          {!isLoading && !activeConnection && !showCreate && (
            <button
              type="button"
              data-testid="create-meta-connection"
              className="btn btn-primary btn-sm"
              onClick={() => setShowCreate(true)}
            >
              Create Connection
            </button>
          )}

          {showCreate && (
            <div className="flex flex-col gap-3">
              <label className="text-sm font-medium" htmlFor="meta-mode-select">
                Evidence Mode
              </label>
              <select
                id="meta-mode-select"
                data-testid="meta-connection-mode-select"
                className="select select-bordered select-sm w-full max-w-xs"
                value={selectedMode}
                onChange={(e) => setSelectedMode(e.target.value)}
              >
                <option value="customer_sandbox">Customer Sandbox</option>
              </select>
              <div className="flex gap-2">
                <button
                  type="button"
                  data-testid="confirm-meta-connection"
                  className="btn btn-primary btn-sm"
                  disabled={isPending}
                  onClick={handleCreate}
                >
                  {isPending ? 'Connecting...' : 'Confirm'}
                </button>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => setShowCreate(false)}
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </Card.Body>
      </Card>
    </div>
  );
}
```

- [ ] **Step 4: Export from barrel**

In `web/components/workspace/index.ts`, add:

```typescript
export { MetaConnectionSection } from './MetaConnectionSection';
```

- [ ] **Step 5: Run component test to verify it passes**

Run: `cd web && npx jest __tests__/components/workspace/MetaConnectionSection.spec.tsx --verbose`
Expected: all 4 tests PASS

- [ ] **Step 6: Add `MetaConnectionSection` to the settings page**

In `web/pages/w/[slug]/settings.tsx`, add the import and render the section below `TeamSettings`:

```tsx
import { MetaConnectionSection } from '@/components/workspace/MetaConnectionSection';
```

Update the render in `SettingsPage`:

```tsx
const SettingsPage: NextPageWithLayout<Props> = () => {
  const { t } = useTranslation('common');
  const { isLoading, isError, team } = useTeam();
  if (isLoading) return <Loading />;
  if (isError || !team) return <Error message="Could not load settings" />;

  return (
    <>
      <h1 className="text-2xl font-bold outline-none">{t('team-settings')}</h1>
      <TeamSettings team={team} />
      <div className="mt-6">
        <MetaConnectionSection />
      </div>
    </>
  );
};
```

- [ ] **Step 7: Run TypeScript check**

```bash
cd web && npx tsc --noEmit 2>&1 | head -20
```

Expected: no errors

- [ ] **Step 8: Commit**

```bash
git add web/components/workspace/MetaConnectionSection.tsx web/components/workspace/index.ts web/pages/w/\[slug\]/settings.tsx web/__tests__/components/workspace/MetaConnectionSection.spec.tsx
git commit -m "$(cat <<'EOF'
feat(web): MetaConnectionSection in Workspace Settings

- Owner-only section shows active Meta connection (ID, ad_account_id, mode)
- Create flow: mode select → confirm → BFF command → refresh list
- All 6 golden-path test IDs present
- Native <select> for Playwright selectOption compatibility
- 4 jest tests: no-connection, active-connection, create-flow, confirm

Issue #119

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: BFF 423 lifecycle test

**Files:**
- Modify: `web/__tests__/api/domain/v1/w/domain-proxy-handler.spec.ts`

**Interfaces:**
- Consumes: `prisma.team.findUnique` mock (returns `{ id, lifecycle, authorizationEpoch }`), `getDomainProxy` mock, BFF handler at `web/pages/api/domain/v1/w/[slug]/[...path].ts`
- Produces: test coverage for non-ACTIVE workspace → 423 with no FastAPI call

- [ ] **Step 1: Understand the existing test structure**

Read the file to find how the existing tests set up mocks. Key patterns:
- `prisma.team.findUnique` is mocked to return `{ id: 'ws_1', lifecycle: 'ACTIVE', authorizationEpoch: 0 }`
- `getDomainProxy` returns a mock with a `forward` method
- Tests use `node-mocks-http` `createMocks` to build `req` and `res`

- [ ] **Step 2: Write the 423 test**

Add a test that overrides the prisma mock to return a non-ACTIVE lifecycle:

```typescript
describe('workspace lifecycle gate', () => {
  it('returns 423 workspace_provisioning for non-ACTIVE workspace and does not call FastAPI', async () => {
    // Override the prisma mock for this test
    (prisma.team.findUnique as jest.Mock).mockResolvedValueOnce({
      id: 'ws_provisioning',
      lifecycle: 'PROVISIONING',
      authorizationEpoch: 0,
    });

    const { req, res } = createMocks({
      method: 'POST',
      url: '/api/domain/v1/w/test-slug/meta/connections',
      query: { slug: 'test-slug', path: ['meta', 'connections'] },
      headers: {
        host: new URL(env.appUrl).host,
        origin: env.appUrl,
        'content-type': 'application/json',
        'x-csrf-token': 'valid-csrf',
      },
    });
    // Write the request body
    req.body = JSON.stringify({ meta_mode: 'customer_sandbox' });

    await handler(req as any, res as any);

    expect(res._getStatusCode()).toBe(423);
    const body = JSON.parse(res._getData());
    expect(body.code).toBe('workspace_provisioning');

    // Verify FastAPI was NOT called
    const proxyForward = getDomainProxy().forward as jest.Mock;
    expect(proxyForward).not.toHaveBeenCalled();
  });
});
```

Note: Adapt this to match the file's existing mock setup patterns. The exact mock variable names and setup may differ — read the file to confirm `prisma`, `getDomainProxy`, `createMocks`, and `env` are available and how they're configured.

- [ ] **Step 3: Run test to verify it passes**

Run: `cd web && npx jest __tests__/api/domain/v1/w/domain-proxy-handler.spec.ts --verbose -t "423"`
Expected: PASS

The 423 path already exists in the BFF handler (lines 275–284 of `[...path].ts`): `if (commandWorkspace.lifecycle !== 'ACTIVE')` → `writeHandlerProblemResponse(res, 423, 'workspace_provisioning', ...)`. This test just covers the path that was previously untested.

- [ ] **Step 4: Commit**

```bash
git add web/__tests__/api/domain/v1/w/domain-proxy-handler.spec.ts
git commit -m "$(cat <<'EOF'
test(bff): 423 workspace_provisioning for non-ACTIVE lifecycle

Covers the BFF lifecycle gate for command requests — non-ACTIVE workspace
returns 423 without calling FastAPI. This path was previously untested.

Issue #119

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Verification

After all 7 tasks:

```bash
# Backend tests
python -m pytest tests/test_meta_connection_lifecycle.py tests/test_meta_api_routes.py -v

# Frontend type check
cd web && npx tsc --noEmit

# Frontend unit tests
cd web && npx jest __tests__/components/workspace/ __tests__/api/domain/v1/w/domain-proxy-handler.spec.ts --verbose

# Generated file drift check
cd web && bash ../scripts/generate_all.sh

# Golden path (if CI environment available)
cd web && npx playwright test --project golden-path
```

## Make verify

Run `make verify` as required by the project workflow, plus each ticket-specific command above.
