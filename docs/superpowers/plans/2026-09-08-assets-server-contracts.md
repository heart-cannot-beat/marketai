# Assets Server Contracts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete Phase 0 (Contract Completion) from the Issue #49 design spec: deliver production-quality server routes, domain functions, projections, migrations, Worker, and tests so the browser UI plan has real domain truth to consume.

**Architecture:** Ten tasks deliver the server contracts bottom-up. Tasks 1-2 extend BlobStore and asset download paths. Task 3 adds the typed gallery projection with keyset cursor and server facets. Task 4 adds the full detail projection. Task 5 makes review idempotent with effective-review projection. Task 6 adds contextual experiment conclusions. Task 7 makes regeneration replayable and adds the review-summary batch. Task 8 adds Loser exclusion to generation preview. Task 9 delivers real async export (202 + Worker + deterministic ZIP). Task 10 adds PostgreSQL RLS, denial audit logging, and regenerates OpenAPI/types.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 16, pytest, Pydantic v2.

**Spec:** `docs/superpowers/specs/2026-09-03-assets-review-regeneration-export-design.md` (rev 5, §0)

## Global Constraints

- `META_WRITE_ACCESS_TOKEN` stays empty; no Meta writes.
- FastAPI derives Workspace and actor identity from verified BFF claims.
- Domain services receive explicit `TenantContext`; `DEFAULT_WORKSPACE_ID` is not a tenant boundary.
- Production tenancy uses PostgreSQL RLS and separate database roles.
- Immutable versions and append-only evidence for historical decisions.
- External or paid calls use durable jobs, idempotency, cost reservation, and recoverable evidence.
- Controlled Mock is explicit and cannot mix with real evidence.
- Signed URLs never appear in logs — only object keys.
- Cross-workspace denials fail closed (invisible 404 or explicit 403 per surface) and emit structured audit records.
- All domain functions are workspace-scoped via `TenantContext`.
- Idempotent commands use `create_or_replay_intent()` with fingerprinted bodies.

## Dependency

This plan has no upstream dependency — it IS the hard gate for the browser UI plan. Tasks can begin immediately on the `Raybbish/build-the-assets-review-regeneration-and-export` branch.

## File Structure

```
app/
├── adapters/
│   ├── blob_store_s3.py                   # MODIFY: add open_committed, commit_generated_artifact
│   ├── blob_store_local.py                # MODIFY: same interface additions
│   └── persistence/
│       └── models.py                      # MODIFY: add committed_object_key, committed_storage_version_id
├── platform/
│   ├── blob_store.py                      # MODIFY: extend BlobStore Protocol
│   ├── download_intents.py                # MODIFY: support exact committed ref verification
│   ├── rls.py                             # CREATE: RLS setup and session-variable injection
│   └── denial_audit.py                    # CREATE: structured denial audit events
├── generation/
│   ├── models.py                          # MODIFY: add ExperimentConclusionRecord
│   ├── review.py                          # MODIFY: add idempotent replay, effective-review projection
│   ├── export.py                          # MODIFY: 202 response, Worker enqueue, selector resolution
│   ├── export_worker.py                   # CREATE: deterministic ZIP builder
│   ├── regeneration.py                    # MODIFY: add replay, review-summary batch
│   ├── asset_queries.py                   # CREATE: typed gallery page, keyset cursor, facets, detail
│   ├── experiment_conclusions.py          # CREATE: contextual conclusion projection
│   ├── v1_router.py                       # MODIFY: wire new routes and projections
│   └── response_models.py                 # MODIFY: add AssetGalleryPage, AssetVersionDetail, etc.
├── migrations/
│   └── versions/
│       ├── xxxx_committed_blob_refs.py    # Task 1
│       ├── xxxx_experiment_conclusions.py # Task 6
│       └── xxxx_rls_policies.py           # Task 10
tests/
├── generation/
│   ├── test_committed_blob_refs.py        # Task 1
│   ├── test_asset_download_intent.py      # Task 2
│   ├── test_asset_gallery_page.py         # Task 3
│   ├── test_asset_version_detail.py       # Task 4
│   ├── test_review_idempotent.py          # Task 5
│   ├── test_experiment_conclusions.py     # Task 6
│   ├── test_regeneration_replay.py        # Task 7
│   ├── test_loser_exclusion.py            # Task 8
│   ├── test_export_async.py              # Task 9
│   ├── test_export_worker.py             # Task 9
│   └── test_rls_denial_audit.py          # Task 10
```

---

### Task 1: Committed Blob Refs Migration and BlobStore Extensions

**Files:**
- Modify: `app/adapters/persistence/models.py`
- Modify: `app/platform/blob_store.py`
- Modify: `app/adapters/blob_store_s3.py`
- Modify: `app/adapters/blob_store_local.py`
- Create: `app/migrations/versions/xxxx_committed_blob_refs.py`
- Test: `tests/generation/test_committed_blob_refs.py`

**Interfaces:**
- Consumes: `BlobStore` protocol (existing), `AssetVersionRecord` ORM model (existing), `CommittedBlobRef` dataclass (existing)
- Produces: `committed_object_key` and `committed_storage_version_id` columns on `AssetVersionRecord`; `BlobStore.open_committed(ref) → AsyncIterator[bytes]` for Worker streaming; `BlobStore.commit_generated_artifact(source, spec) → CommittedBlobRef` for Worker ZIP commit; strengthened `issue_download` that verifies exact revision. Tasks 2, 9 consume these.

- [ ] **Step 1: Write the failing test for committed blob ref columns**

Create `tests/generation/test_committed_blob_refs.py`:

```python
import pytest
from sqlalchemy import select

from app.adapters.persistence.models import AssetVersionRecord


def test_asset_version_has_committed_ref_columns(db_session, tenant_ctx):
    """AssetVersionRecord has committed_object_key and committed_storage_version_id."""
    version = db_session.execute(
        select(AssetVersionRecord).limit(1)
    ).scalar_one_or_none()
    # The columns exist on the model (even if no rows yet)
    assert hasattr(AssetVersionRecord, "committed_object_key")
    assert hasattr(AssetVersionRecord, "committed_storage_version_id")


def test_committed_ref_nullable_for_legacy(db_session, tenant_ctx, make_asset_version):
    """Legacy AssetVersions may have null committed refs."""
    version = make_asset_version(committed_object_key=None)
    db_session.flush()
    reloaded = db_session.get(AssetVersionRecord, version.id)
    assert reloaded.committed_object_key is None
    assert reloaded.committed_storage_version_id is None


def test_committed_ref_populated_for_new_versions(db_session, tenant_ctx, make_asset_version):
    """New AssetVersions have committed refs after generation commit."""
    version = make_asset_version(
        committed_object_key="committed/ws1/assets/2026/09/08/ulid123.png",
        committed_storage_version_id="sv-abc123",
    )
    db_session.flush()
    reloaded = db_session.get(AssetVersionRecord, version.id)
    assert reloaded.committed_object_key == "committed/ws1/assets/2026/09/08/ulid123.png"
    assert reloaded.committed_storage_version_id == "sv-abc123"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd app && python -m pytest tests/generation/test_committed_blob_refs.py -v`
Expected: FAIL — `AssetVersionRecord` has no `committed_object_key` attribute.

- [ ] **Step 3: Add migration for committed blob ref columns**

Create the Alembic migration:

```bash
cd app && alembic revision --autogenerate -m "add committed blob refs to asset_versions"
```

Then edit the generated migration to add:

```python
def upgrade() -> None:
    op.add_column("asset_versions", sa.Column("committed_object_key", sa.String(512), nullable=True))
    op.add_column("asset_versions", sa.Column("committed_storage_version_id", sa.String(256), nullable=True))

def downgrade() -> None:
    op.drop_column("asset_versions", "committed_storage_version_id")
    op.drop_column("asset_versions", "committed_object_key")
```

- [ ] **Step 4: Add columns to `AssetVersionRecord` ORM model**

In `app/adapters/persistence/models.py`, add to the `AssetVersionRecord` class:

```python
    committed_object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    committed_storage_version_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
```

- [ ] **Step 5: Run committed-ref column test**

Run: `cd app && python -m pytest tests/generation/test_committed_blob_refs.py -v`
Expected: PASS.

- [ ] **Step 6: Write the failing test for `BlobStore.open_committed`**

Add to `tests/generation/test_committed_blob_refs.py`:

```python
import pytest
from app.platform.blob_store import CommittedBlobRef, BlobStoreError


@pytest.mark.asyncio
async def test_open_committed_returns_bytes(local_blob_store, committed_blob):
    """open_committed yields the exact committed blob bytes."""
    chunks = []
    async for chunk in local_blob_store.open_committed(committed_blob):
        chunks.append(chunk)
    assert b"".join(chunks) == b"test-image-bytes"


@pytest.mark.asyncio
async def test_open_committed_raises_for_missing_blob(local_blob_store):
    """open_committed raises BlobStoreError for a non-existent committed ref."""
    fake_ref = CommittedBlobRef(
        committed_key="committed/ws1/assets/2026/01/01/missing.png",
        storage_version_id="sv-missing",
        size_bytes=100,
        checksum_sha256_b64="",
        content_type_verified="image/png",
    )
    with pytest.raises(BlobStoreError):
        async for _ in local_blob_store.open_committed(fake_ref):
            pass


@pytest.mark.asyncio
async def test_commit_generated_artifact(local_blob_store, tmp_path):
    """commit_generated_artifact commits server-owned bytes and returns CommittedBlobRef."""
    source = tmp_path / "export.zip"
    source.write_bytes(b"PK\x03\x04" + b"\x00" * 100)

    from app.platform.blob_store import GeneratedArtifactSpec
    spec = GeneratedArtifactSpec(
        committed_key="committed/ws1/exports/2026/09/08/pkg1.zip",
        content_type="application/zip",
        expected_sha256_b64="test-sha256",
    )

    ref = await local_blob_store.commit_generated_artifact(source, spec)
    assert isinstance(ref, CommittedBlobRef)
    assert ref.committed_key == spec.committed_key
```

- [ ] **Step 7: Extend BlobStore Protocol**

In `app/platform/blob_store.py`, add the new value object and protocol methods:

```python
from collections.abc import AsyncIterator

@dataclass(frozen=True)
class GeneratedArtifactSpec:
    committed_key: str
    content_type: str
    expected_sha256_b64: str


@runtime_checkable
class BlobStore(Protocol):
    async def issue_upload(self, spec: UploadSpec) -> UploadGrant: ...
    async def inspect_upload(self, ref: StagedBlobRef) -> InspectedUpload: ...
    async def commit_upload(self, inspected: InspectedUpload, expected: ObjectConstraints) -> CommittedBlobRef: ...
    async def issue_download(self, ref: CommittedBlobRef, spec: DownloadSpec) -> DownloadGrant: ...
    async def delete(self, ref: BlobRef) -> None: ...

    async def open_committed(self, ref: CommittedBlobRef) -> AsyncIterator[bytes]: ...

    async def commit_generated_artifact(
        self, source: "Path", spec: GeneratedArtifactSpec
    ) -> CommittedBlobRef: ...
```

- [ ] **Step 8: Implement `open_committed` and `commit_generated_artifact` on `LocalFileBlobStore`**

In `app/adapters/blob_store_local.py`, add:

```python
    async def open_committed(self, ref: CommittedBlobRef) -> AsyncIterator[bytes]:
        path = self.base_dir / ref.committed_key
        if not path.exists():
            raise ObjectNotFoundError(f"committed blob not found: {ref.committed_key}")
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                yield chunk

    async def commit_generated_artifact(
        self, source: Path, spec: GeneratedArtifactSpec
    ) -> CommittedBlobRef:
        validate_object_key(spec.committed_key)
        dest = self.base_dir / spec.committed_key
        dest.parent.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy2(source, dest)
        size = dest.stat().st_size
        return CommittedBlobRef(
            committed_key=spec.committed_key,
            storage_version_id=f"local-{spec.committed_key}",
            size_bytes=size,
            checksum_sha256_b64=spec.expected_sha256_b64,
            content_type_verified=spec.content_type,
        )
```

- [ ] **Step 9: Implement `open_committed` and `commit_generated_artifact` on `S3BlobStore`**

In `app/adapters/blob_store_s3.py`, add equivalent implementations using the S3 client's `get_object` (with `VersionId` from the committed ref) and `put_object` respectively. The `open_committed` method streams the `Body` in 64KB chunks; `commit_generated_artifact` uploads the source file and records the `VersionId` from the response.

- [ ] **Step 10: Run all blob tests**

Run: `cd app && python -m pytest tests/generation/test_committed_blob_refs.py -v`
Expected: All tests PASS.

- [ ] **Step 11: Commit**

```bash
git add app/adapters/persistence/models.py app/platform/blob_store.py app/adapters/blob_store_s3.py app/adapters/blob_store_local.py app/migrations/ tests/generation/test_committed_blob_refs.py
git commit -m "feat(assets): add committed blob refs migration and BlobStore extensions"
```

---

### Task 2: Asset Blob Resolution and Download Intent Route

**Files:**
- Create: `app/generation/asset_queries.py` (initial file — expanded in Tasks 3-4)
- Modify: `app/generation/v1_router.py`
- Modify: `app/platform/download_intents.py`
- Test: `tests/generation/test_asset_download_intent.py`

**Interfaces:**
- Consumes: `CommittedBlobRef` (existing), `BlobStore.issue_download` (existing, strengthened in Task 1), `create_or_replay_intent()` (existing from `app.platform.idempotency`), `TenantContext` (existing)
- Produces: `resolve_asset_blob_ref(session, ctx, version_id) → CommittedBlobRef` raises `asset_blob_unavailable` when missing/uncommitted; idempotent `POST /generation/assets/versions/{version_id}/download-intent` returning `DownloadIntentResponse { intent_id, expires_at, download_grant }`. Task 5 (browser `useAssetImageUrl`) and Task 9 (export download) consume these.

- [ ] **Step 1: Write the failing test for `resolve_asset_blob_ref`**

Create `tests/generation/test_asset_download_intent.py`:

```python
import pytest
from app.platform.blob_store import CommittedBlobRef


def test_resolve_asset_blob_ref_returns_committed_ref(
    db_session, tenant_ctx, make_asset_version
):
    """Returns CommittedBlobRef when version has committed refs."""
    from app.generation.asset_queries import resolve_asset_blob_ref

    version = make_asset_version(
        committed_object_key="committed/ws1/assets/2026/09/08/v1.png",
        committed_storage_version_id="sv-1",
        content_sha256="sha256-abc",
        byte_size=1024,
        mime_type="image/png",
    )
    db_session.flush()

    ref = resolve_asset_blob_ref(db_session, tenant_ctx, version.id)
    assert isinstance(ref, CommittedBlobRef)
    assert ref.committed_key == "committed/ws1/assets/2026/09/08/v1.png"
    assert ref.storage_version_id == "sv-1"


def test_resolve_asset_blob_ref_raises_for_missing_committed_ref(
    db_session, tenant_ctx, make_asset_version
):
    """Raises asset_blob_unavailable when committed_object_key is None."""
    from app.generation.asset_queries import resolve_asset_blob_ref, AssetBlobError

    version = make_asset_version(committed_object_key=None)
    db_session.flush()

    with pytest.raises(AssetBlobError, match="asset_blob_unavailable"):
        resolve_asset_blob_ref(db_session, tenant_ctx, version.id)


def test_resolve_asset_blob_ref_raises_for_wrong_workspace(
    db_session, tenant_ctx, make_asset_version, other_tenant_ctx
):
    """Cross-workspace version lookup returns not found."""
    from app.generation.asset_queries import resolve_asset_blob_ref, AssetBlobError

    version = make_asset_version(
        committed_object_key="committed/ws1/assets/2026/09/08/v1.png",
        committed_storage_version_id="sv-1",
    )
    db_session.flush()

    with pytest.raises(AssetBlobError, match="asset_blob_unavailable"):
        resolve_asset_blob_ref(db_session, other_tenant_ctx, version.id)
```

- [ ] **Step 2: Implement `resolve_asset_blob_ref`**

Create `app/generation/asset_queries.py`:

```python
"""Asset query projections and blob resolution (Issue #49 Phase 0).

This module owns read-side projections for the Asset gallery:
``resolve_asset_blob_ref``, ``AssetGalleryPage``, ``AssetVersionDetail``.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.persistence.models import AssetVersionRecord
from app.platform.blob_store import CommittedBlobRef
from app.platform.tenancy import TenantContext


class AssetBlobError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def resolve_asset_blob_ref(
    session: Session, ctx: TenantContext, version_id: str
) -> CommittedBlobRef:
    version = session.execute(
        select(AssetVersionRecord).where(
            AssetVersionRecord.id == version_id,
            AssetVersionRecord.workspace_id == ctx.workspace_id,
        )
    ).scalar_one_or_none()

    if version is None:
        raise AssetBlobError(
            "asset_blob_unavailable",
            f"AssetVersion {version_id} not found in workspace",
        )

    if not version.committed_object_key or not version.committed_storage_version_id:
        raise AssetBlobError(
            "asset_blob_unavailable",
            f"AssetVersion {version_id} has no committed blob reference",
        )

    return CommittedBlobRef(
        committed_key=version.committed_object_key,
        storage_version_id=version.committed_storage_version_id,
        size_bytes=version.byte_size or 0,
        checksum_sha256_b64=version.content_sha256 or "",
        content_type_verified=version.mime_type or "application/octet-stream",
    )
```

- [ ] **Step 3: Run resolver tests**

Run: `cd app && python -m pytest tests/generation/test_asset_download_intent.py -v`
Expected: All 3 tests PASS.

- [ ] **Step 4: Write the failing test for the download intent HTTP route**

Add to `tests/generation/test_asset_download_intent.py`:

```python
import pytest


def test_download_intent_route_returns_201(
    client, auth_headers, make_asset_version, db_session
):
    """POST /generation/assets/versions/{id}/download-intent returns 201 with grant."""
    version = make_asset_version(
        committed_object_key="committed/ws1/assets/2026/09/08/v1.png",
        committed_storage_version_id="sv-1",
        content_sha256="sha256-abc",
        byte_size=1024,
        mime_type="image/png",
    )
    db_session.commit()

    response = client.post(
        f"/api/v1/generation/assets/versions/{version.id}/download-intent",
        headers={**auth_headers, "Idempotency-Key": "dk-1"},
    )
    assert response.status_code == 201
    data = response.json()
    assert "intent_id" in data
    assert "expires_at" in data
    assert "download_grant" in data
    assert "url" in data["download_grant"]


def test_download_intent_replay_returns_same_intent(
    client, auth_headers, make_asset_version, db_session
):
    """Replaying the same idempotency key returns the same intent."""
    version = make_asset_version(
        committed_object_key="committed/ws1/assets/2026/09/08/v1.png",
        committed_storage_version_id="sv-1",
        content_sha256="sha256-abc",
        byte_size=1024,
        mime_type="image/png",
    )
    db_session.commit()

    headers = {**auth_headers, "Idempotency-Key": "dk-replay"}

    r1 = client.post(
        f"/api/v1/generation/assets/versions/{version.id}/download-intent",
        headers=headers,
    )
    r2 = client.post(
        f"/api/v1/generation/assets/versions/{version.id}/download-intent",
        headers=headers,
    )

    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["intent_id"] == r2.json()["intent_id"]


def test_download_intent_401_for_uncommitted_blob(
    client, auth_headers, make_asset_version, db_session
):
    """Returns 422 asset_blob_unavailable for version without committed ref."""
    version = make_asset_version(committed_object_key=None)
    db_session.commit()

    response = client.post(
        f"/api/v1/generation/assets/versions/{version.id}/download-intent",
        headers={**auth_headers, "Idempotency-Key": "dk-uncommitted"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "asset_blob_unavailable"
```

- [ ] **Step 5: Wire the download intent route in `v1_router.py`**

Add the route to `app/generation/v1_router.py`:

```python
@router.post(
    "/assets/versions/{version_id}/download-intent",
    operation_id="create_generation_asset_version_download_intent",
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
async def create_asset_download_intent(
    version_id: str,
    ctx: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    from app.generation.asset_queries import resolve_asset_blob_ref
    from app.platform.blob_store_factory import get_blob_store

    with tenant_session(ctx) as session:
        committed_ref = resolve_asset_blob_ref(session, ctx, version_id)

        intent = create_download_intent_record(
            session,
            intent_id=new_id("di"),
            owner_kind="asset_version",
            owner_id=version_id,
            committed_blob_ref_key=committed_ref.committed_key,
            committed_blob_ref_version=committed_ref.storage_version_id,
            idempotency_key=idempotency_key,
        )
        session.commit()

    blob_store = get_blob_store()
    grant = await blob_store.issue_download(
        committed_ref,
        DownloadSpec(filename=f"asset-{version_id}.png", expires_in_seconds=300),
    )

    return {
        "intent_id": intent.id,
        "expires_at": grant.expires_at.isoformat(),
        "download_grant": {
            "url": grant.url,
            "kind": "signed_get",
            "headers": {},
            "content_disposition": grant.content_disposition,
        },
    }
```

Register the `AssetBlobError` handler alongside existing problem handlers.

- [ ] **Step 6: Run download intent route tests**

Run: `cd app && python -m pytest tests/generation/test_asset_download_intent.py -v`
Expected: All 6 tests PASS.

- [ ] **Step 7: Commit**

```bash
git add app/generation/asset_queries.py app/generation/v1_router.py app/platform/download_intents.py tests/generation/test_asset_download_intent.py
git commit -m "feat(assets): add asset blob resolution and idempotent download intent route"
```

---

### Task 3: AssetGalleryPage with Server Filters, Keyset Cursor, and Facets

**Files:**
- Modify: `app/generation/asset_queries.py`
- Modify: `app/generation/v1_router.py`
- Modify: `app/generation/response_models.py`
- Test: `tests/generation/test_asset_gallery_page.py`

**Interfaces:**
- Consumes: `AssetVersionRecord` (existing), `TenantContext` (existing), `project_effective_review()` (Task 5 — stub for now), keyset cursor utils
- Produces: `GET /generation/assets` returning `AssetGalleryPage { items: AssetVersionSummary[], next_cursor, facets, allowed_actions }`; keyset cursor over `(created_at DESC, id DESC)` bound to workspace + canonical filter hash; all server-side filters from spec §0.2. Browser Task 1 (`useDomainPaginatedQuery`) consumes this.

- [ ] **Step 1: Write the failing test for gallery pagination**

Create `tests/generation/test_asset_gallery_page.py`:

```python
import pytest


def test_gallery_returns_first_page(client, auth_headers, seed_asset_versions):
    """GET /generation/assets returns paginated AssetVersionSummary items."""
    response = client.get(
        "/api/v1/generation/assets",
        headers=auth_headers,
        params={"limit": 2},
    )
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert "next_cursor" in data
    assert "facets" in data
    assert "allowed_actions" in data
    assert len(data["items"]) == 2
    assert data["next_cursor"] is not None


def test_gallery_keyset_cursor_second_page(client, auth_headers, seed_asset_versions):
    """Second page continues from cursor without repeating items."""
    r1 = client.get(
        "/api/v1/generation/assets",
        headers=auth_headers,
        params={"limit": 2},
    )
    cursor = r1.json()["next_cursor"]

    r2 = client.get(
        "/api/v1/generation/assets",
        headers=auth_headers,
        params={"limit": 2, "cursor": cursor},
    )
    page1_ids = {item["id"] for item in r1.json()["items"]}
    page2_ids = {item["id"] for item in r2.json()["items"]}
    assert page1_ids.isdisjoint(page2_ids)


def test_gallery_filter_by_provider(client, auth_headers, seed_asset_versions):
    """Provider filter narrows results."""
    response = client.get(
        "/api/v1/generation/assets",
        headers=auth_headers,
        params={"provider": "openai"},
    )
    assert response.status_code == 200
    for item in response.json()["items"]:
        assert item["provider"] == "openai"


def test_gallery_filter_by_effective_review(client, auth_headers, seed_asset_versions):
    """review filter returns only versions with that effective review."""
    response = client.get(
        "/api/v1/generation/assets",
        headers=auth_headers,
        params={"review_value": "approved"},
    )
    assert response.status_code == 200
    for item in response.json()["items"]:
        assert item["effective_review_value"] == "approved"


def test_gallery_facets_computed_from_full_dataset(
    client, auth_headers, seed_asset_versions
):
    """Facets reflect the full matching dataset, not just the current page."""
    response = client.get(
        "/api/v1/generation/assets",
        headers=auth_headers,
        params={"limit": 1},
    )
    facets = response.json()["facets"]
    assert "providers" in facets
    assert isinstance(facets["providers"], list)
    assert all("value" in f and "count" in f for f in facets["providers"])


def test_gallery_invalid_cursor_returns_400(client, auth_headers):
    """An invalid cursor returns 400 invalid_cursor."""
    response = client.get(
        "/api/v1/generation/assets",
        headers=auth_headers,
        params={"cursor": "bogus-cursor"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_cursor"


def test_gallery_conclusion_requires_experiment_context(
    client, auth_headers, seed_asset_versions
):
    """conclusion filter without experiment_context returns 422."""
    response = client.get(
        "/api/v1/generation/assets",
        headers=auth_headers,
        params={"conclusion": "winner"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "experiment_context_required"
```

- [ ] **Step 2: Implement `list_asset_versions` query function**

In `app/generation/asset_queries.py`, add:

```python
import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import and_, case, desc, func, select, tuple_
from sqlalchemy.orm import Session

from app.adapters.persistence.models import AssetVersionRecord, QualityReviewRecord
from app.platform.tenancy import TenantContext


class GalleryQueryError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _canonical_filter_hash(filters: dict[str, Any]) -> str:
    clean = {k: v for k, v in sorted(filters.items()) if v is not None}
    return hashlib.sha256(
        json.dumps(clean, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]


def _decode_keyset_cursor(cursor: str, workspace_id: str, filter_hash: str) -> tuple[datetime, str]:
    """Decode an opaque keyset cursor.
    
    Format: base64(json({"ws": workspace_id, "fh": filter_hash, "ca": iso, "id": id}))
    """
    import base64
    try:
        decoded = json.loads(base64.urlsafe_b64decode(cursor + "=="))
    except Exception:
        raise GalleryQueryError("invalid_cursor", "Cursor is malformed")

    if decoded.get("ws") != workspace_id or decoded.get("fh") != filter_hash:
        raise GalleryQueryError("invalid_cursor", "Cursor does not match current scope")

    return datetime.fromisoformat(decoded["ca"]), decoded["id"]


def _encode_keyset_cursor(
    workspace_id: str, filter_hash: str, last_created_at: datetime, last_id: str
) -> str:
    import base64
    payload = json.dumps({
        "ws": workspace_id,
        "fh": filter_hash,
        "ca": last_created_at.isoformat(),
        "id": last_id,
    }, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()


def list_asset_versions(
    session: Session,
    ctx: TenantContext,
    *,
    product_id: str | None = None,
    run_id: str | None = None,
    provider: str | None = None,
    origin_kind: str | None = None,
    execution_mode: str | None = None,
    review_value: str | None = None,
    aspect_ratio: str | None = None,
    qc_state: str | None = None,
    tag: str | None = None,
    experiment_context: str | None = None,
    conclusion: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> dict:
    if conclusion and not experiment_context:
        raise GalleryQueryError(
            "experiment_context_required",
            "conclusion filter requires experiment_context",
        )

    filters = {
        "product_id": product_id,
        "run_id": run_id,
        "provider": provider,
        "origin_kind": origin_kind,
        "execution_mode": execution_mode,
        "review_value": review_value,
        "aspect_ratio": aspect_ratio,
        "qc_state": qc_state,
        "tag": tag,
        "experiment_context": experiment_context,
        "conclusion": conclusion,
    }
    filter_hash = _canonical_filter_hash(filters)

    query = select(AssetVersionRecord).where(
        AssetVersionRecord.workspace_id == ctx.workspace_id,
        AssetVersionRecord.status == "succeeded",
    )

    if product_id:
        query = query.where(AssetVersionRecord.product_id == product_id)
    if run_id:
        query = query.where(AssetVersionRecord.producing_run_id == run_id)
    if provider:
        query = query.where(AssetVersionRecord.provider == provider)
    if origin_kind:
        query = query.where(AssetVersionRecord.origin_kind == origin_kind)
    if execution_mode:
        query = query.where(AssetVersionRecord.execution_mode == execution_mode)
    if aspect_ratio:
        query = query.where(AssetVersionRecord.aspect_ratio == aspect_ratio)

    # Keyset cursor: (created_at DESC, id DESC)
    query = query.order_by(desc(AssetVersionRecord.created_at), desc(AssetVersionRecord.id))

    if cursor:
        last_ca, last_id = _decode_keyset_cursor(cursor, ctx.workspace_id, filter_hash)
        query = query.where(
            tuple_(AssetVersionRecord.created_at, AssetVersionRecord.id)
            < tuple_(last_ca, last_id)
        )

    rows = session.execute(query.limit(limit + 1)).scalars().all()

    has_more = len(rows) > limit
    page_rows = rows[:limit]

    next_cursor = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = _encode_keyset_cursor(
            ctx.workspace_id, filter_hash, last.created_at, last.id
        )

    items = [_version_summary(v) for v in page_rows]
    facets = _compute_facets(session, ctx, filters)
    allowed_actions = _compute_collection_actions(ctx)

    return {
        "items": items,
        "next_cursor": next_cursor,
        "facets": facets,
        "allowed_actions": allowed_actions,
    }


def _version_summary(v: AssetVersionRecord) -> dict:
    return {
        "id": v.id,
        "asset_id": v.asset_id,
        "product_id": v.product_id,
        "run_id": v.producing_run_id,
        "provider": v.provider,
        "model": v.model,
        "status": v.status,
        "aspect_ratio": v.aspect_ratio,
        "stable_dimension_signature": getattr(v, "stable_dimension_signature", None),
        "tags": getattr(v, "tags_json", None) or [],
        "effective_review_value": None,  # Computed in Task 5
        "latest_qc_state": None,  # Computed from QC attempts
        "contextual_conclusion": None,  # Computed in Task 6
        "origin_kind": v.origin_kind,
        "execution_mode": v.execution_mode,
        "mock_evidence": getattr(v, "mock_evidence_json", None),
        "byte_size": v.byte_size or 0,
        "created_at": v.created_at.isoformat() if v.created_at else None,
    }


def _compute_facets(
    session: Session, ctx: TenantContext, filters: dict
) -> dict:
    base = select(AssetVersionRecord).where(
        AssetVersionRecord.workspace_id == ctx.workspace_id,
        AssetVersionRecord.status == "succeeded",
    )

    def _facet_values(column, base_query=base):
        rows = session.execute(
            select(column, func.count()).select_from(base_query.subquery())
            .group_by(column)
            .where(column.isnot(None))
        ).all()
        return [{"value": str(val), "count": cnt} for val, cnt in rows]

    return {
        "providers": _facet_values(AssetVersionRecord.provider),
        "origins": _facet_values(AssetVersionRecord.origin_kind),
        "modes": _facet_values(AssetVersionRecord.execution_mode),
        "reviews": [],  # Filled in Task 5 with effective review
        "aspects": _facet_values(AssetVersionRecord.aspect_ratio),
        "qc_states": [],  # Filled with QC state aggregation
        "tags": [],  # Filled from tag extraction
    }


def _compute_collection_actions(ctx: TenantContext) -> list[str]:
    actions = ["download"]
    if ctx.role in ("OWNER", "ADMIN", "MEMBER"):
        actions.extend(["review", "regenerate"])
    return actions
```

- [ ] **Step 3: Wire the route in `v1_router.py`**

Add:

```python
@router.get(
    "/assets",
    operation_id="list_generation_assets",
)
async def list_assets(
    request: Request,
    ctx: TenantContext = Depends(request_tenant_context),
    product_id: str | None = Query(None),
    run_id: str | None = Query(None),
    provider: str | None = Query(None),
    origin_kind: str | None = Query(None),
    execution_mode: str | None = Query(None),
    review_value: str | None = Query(None),
    aspect_ratio: str | None = Query(None),
    qc_state: str | None = Query(None),
    tag: str | None = Query(None),
    experiment_context: str | None = Query(None),
    conclusion: str | None = Query(None),
    cursor: str | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
):
    from app.generation.asset_queries import list_asset_versions

    with tenant_session(ctx) as session:
        return list_asset_versions(
            session, ctx,
            product_id=product_id,
            run_id=run_id,
            provider=provider,
            origin_kind=origin_kind,
            execution_mode=execution_mode,
            review_value=review_value,
            aspect_ratio=aspect_ratio,
            qc_state=qc_state,
            tag=tag,
            experiment_context=experiment_context,
            conclusion=conclusion,
            cursor=cursor,
            limit=limit,
        )
```

Register `GalleryQueryError` in the problem handlers.

- [ ] **Step 4: Run all gallery tests**

Run: `cd app && python -m pytest tests/generation/test_asset_gallery_page.py -v`
Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/generation/asset_queries.py app/generation/v1_router.py app/generation/response_models.py tests/generation/test_asset_gallery_page.py
git commit -m "feat(assets): add typed AssetGalleryPage with keyset cursor, server filters, and facets"
```

---

### Task 4: AssetVersionDetail Projection

**Files:**
- Modify: `app/generation/asset_queries.py`
- Modify: `app/generation/v1_router.py`
- Modify: `app/generation/response_models.py`
- Test: `tests/generation/test_asset_version_detail.py`

**Interfaces:**
- Consumes: `AssetVersionRecord` (existing), `QualityReviewRecord` (existing), `AssetQCAttemptRecord` (existing), `RunItemRecord` (existing), `resolve_asset_blob_ref` (Task 2)
- Produces: `GET /generation/assets/versions/{version_id}` returning `AssetVersionDetail` with full provenance from spec §0.2. Browser Task 8 (`LightboxModal`) consumes this.

- [ ] **Step 1: Write the failing test**

Create `tests/generation/test_asset_version_detail.py`:

```python
import pytest


def test_detail_returns_full_provenance(
    client, auth_headers, make_asset_version_with_provenance, db_session
):
    """GET /generation/assets/versions/{id} returns the full detail projection."""
    version = make_asset_version_with_provenance()
    db_session.commit()

    response = client.get(
        f"/api/v1/generation/assets/versions/{version.id}",
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()

    assert data["id"] == version.id
    assert "provider" in data
    assert "model" in data
    assert "aspect_ratio" in data
    assert "seed" in data
    assert "requested" in data["seed"]
    assert "effective" in data["seed"]
    assert "unavailable_reason" in data["seed"]
    assert "prompt" in data
    assert "reviews" in data
    assert isinstance(data["reviews"], list)
    assert "qc_attempts" in data
    assert isinstance(data["qc_attempts"], list)
    assert "contextual_conclusions" in data
    assert "parent" in data
    assert "children" in data
    assert "allowed_actions" in data
    assert "review_head_id" in data
    assert "effective_review_value" in data


def test_detail_returns_404_for_wrong_workspace(
    client, auth_headers, make_asset_version, db_session, other_workspace_slug
):
    """Cross-workspace version ID returns 404."""
    version = make_asset_version()
    db_session.commit()

    response = client.get(
        f"/api/v1/generation/assets/versions/{version.id}",
        headers={**auth_headers, "X-Workspace-Slug": other_workspace_slug},
    )
    assert response.status_code == 404


def test_detail_includes_lineage(
    client, auth_headers, make_regenerated_version, db_session
):
    """Parent/children lineage is populated for regenerated versions."""
    parent, child = make_regenerated_version()
    db_session.commit()

    parent_response = client.get(
        f"/api/v1/generation/assets/versions/{parent.id}",
        headers=auth_headers,
    )
    assert parent_response.status_code == 200
    assert len(parent_response.json()["children"]) >= 1

    child_response = client.get(
        f"/api/v1/generation/assets/versions/{child.id}",
        headers=auth_headers,
    )
    assert child_response.status_code == 200
    assert child_response.json()["parent"] is not None
    assert child_response.json()["parent"]["version_id"] == parent.id
```

- [ ] **Step 2: Implement `get_asset_version_detail` in `asset_queries.py`**

Add the detail projection function that loads the version with all related data (reviews, QC attempts, lineage, conclusions) and returns the full spec §0.2 shape. Wire the `GET /generation/assets/versions/{version_id}` route in `v1_router.py`.

- [ ] **Step 3: Run detail tests**

Run: `cd app && python -m pytest tests/generation/test_asset_version_detail.py -v`
Expected: All 3 tests PASS.

- [ ] **Step 4: Commit**

```bash
git add app/generation/asset_queries.py app/generation/v1_router.py app/generation/response_models.py tests/generation/test_asset_version_detail.py
git commit -m "feat(assets): add AssetVersionDetail projection with full provenance"
```

---

### Task 5: Idempotent Review and Effective-Review Projection

**Files:**
- Modify: `app/generation/review.py`
- Modify: `app/generation/v1_router.py` (review creation route)
- Modify: `app/generation/asset_queries.py` (wire effective review into gallery/detail)
- Test: `tests/generation/test_review_idempotent.py`

**Interfaces:**
- Consumes: `create_or_replay_intent()` (existing), `QualityReviewRecord` (existing), `create_review()` (existing)
- Produces: Idempotent review creation with fingerprinted replay; `project_effective_review(session, ctx, version_id) → str | None` returning the head review value or None; effective review wired into gallery facets and detail. Browser Tasks 9, 10 consume the idempotent review route; gallery/detail wire the projection.

- [ ] **Step 1: Write the failing test for idempotent review**

Create `tests/generation/test_review_idempotent.py`:

```python
import pytest


def test_review_replay_returns_same_record(
    client, auth_headers, make_asset_version, db_session
):
    """Replaying a review with the same idempotency key returns the same review."""
    version = make_asset_version()
    db_session.commit()

    headers = {**auth_headers, "Idempotency-Key": "review-replay-1"}
    body = {"version_id": version.id, "review_value": "approved", "reason": "good"}

    r1 = client.post("/api/v1/generation/reviews", json=body, headers=headers)
    r2 = client.post("/api/v1/generation/reviews", json=body, headers=headers)

    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"]


def test_review_replay_different_body_conflicts(
    client, auth_headers, make_asset_version, db_session
):
    """Reusing an idempotency key with different body returns 409."""
    version = make_asset_version()
    db_session.commit()

    headers = {**auth_headers, "Idempotency-Key": "review-conflict-1"}

    r1 = client.post(
        "/api/v1/generation/reviews",
        json={"version_id": version.id, "review_value": "approved", "reason": "good"},
        headers=headers,
    )
    r2 = client.post(
        "/api/v1/generation/reviews",
        json={"version_id": version.id, "review_value": "rejected", "reason": "bad"},
        headers=headers,
    )

    assert r1.status_code == 201
    assert r2.status_code == 409


def test_effective_review_is_newest_non_superseded_head(
    db_session, tenant_ctx, make_asset_version
):
    """project_effective_review returns the newest non-superseded review value."""
    from app.generation.review import create_review, project_effective_review

    version = make_asset_version()
    db_session.flush()

    r1 = create_review(
        db_session, tenant_ctx, version.id, "approved", "initial", "actor-1"
    )
    db_session.flush()

    assert project_effective_review(db_session, tenant_ctx, version.id) == "approved"

    r2 = create_review(
        db_session, tenant_ctx, version.id, "needs_changes", "correction",
        "actor-1", supersedes_review_id=r1.id,
    )
    db_session.flush()

    assert project_effective_review(db_session, tenant_ctx, version.id) == "needs_changes"


def test_cleared_review_removes_effective_value(
    db_session, tenant_ctx, make_asset_version
):
    """cleared review makes effective_review None."""
    from app.generation.review import create_review, project_effective_review

    version = make_asset_version()
    db_session.flush()

    r1 = create_review(
        db_session, tenant_ctx, version.id, "approved", "initial", "actor-1"
    )
    db_session.flush()

    create_review(
        db_session, tenant_ctx, version.id, "cleared", "withdraw",
        "actor-1", supersedes_review_id=r1.id,
    )
    db_session.flush()

    assert project_effective_review(db_session, tenant_ctx, version.id) is None


def test_effective_review_exists_guard(
    db_session, tenant_ctx, make_asset_version
):
    """Submitting review without supersedes_review_id when head exists returns 409."""
    from app.generation.review import create_review
    from app.generation.review import ReviewConflictError

    version = make_asset_version()
    db_session.flush()

    create_review(
        db_session, tenant_ctx, version.id, "approved", "initial", "actor-1"
    )
    db_session.flush()

    with pytest.raises(ReviewConflictError, match="effective_review_exists"):
        create_review(
            db_session, tenant_ctx, version.id, "rejected", "second",
            "actor-2", supersedes_review_id=None,
        )


def test_is_ready_for_experiment_uses_effective_review(
    db_session, tenant_ctx, make_asset_version
):
    """is_ready_for_experiment uses effective review, not any historical Approved."""
    from app.generation.review import create_review, is_ready_for_experiment

    version = make_asset_version()
    db_session.flush()

    r1 = create_review(
        db_session, tenant_ctx, version.id, "approved", "initial", "actor-1"
    )
    db_session.flush()
    assert is_ready_for_experiment(db_session, tenant_ctx, version.id) is True

    create_review(
        db_session, tenant_ctx, version.id, "cleared", "undo",
        "actor-1", supersedes_review_id=r1.id,
    )
    db_session.flush()
    assert is_ready_for_experiment(db_session, tenant_ctx, version.id) is False
```

- [ ] **Step 2: Implement `project_effective_review` in `review.py`**

Add to `app/generation/review.py`:

```python
def project_effective_review(
    session: Session, ctx: TenantContext, version_id: str
) -> str | None:
    """Return the effective review value for a version.
    
    The head is the newest review (by created_at DESC, id DESC) that is
    not superseded by another review. A 'cleared' head yields None.
    """
    superseded_ids = select(QualityReviewRecord.supersedes_review_id).where(
        QualityReviewRecord.asset_version_id == version_id,
        QualityReviewRecord.workspace_id == ctx.workspace_id,
        QualityReviewRecord.supersedes_review_id.isnot(None),
    ).scalar_subquery()

    head = session.execute(
        select(QualityReviewRecord)
        .where(
            QualityReviewRecord.asset_version_id == version_id,
            QualityReviewRecord.workspace_id == ctx.workspace_id,
            ~QualityReviewRecord.id.in_(superseded_ids),
        )
        .order_by(
            desc(QualityReviewRecord.created_at),
            desc(QualityReviewRecord.id),
        )
        .limit(1)
    ).scalar_one_or_none()

    if head is None:
        return None
    if head.review_value == "cleared":
        return None
    return head.review_value
```

- [ ] **Step 3: Add effective-review head guard to `create_review`**

In `create_review()`, before appending the new review, check if an effective head exists and `supersedes_review_id` is not provided:

```python
    current_head = _find_review_head(session, ctx, asset_version_id)
    if current_head and not supersedes_review_id:
        raise ReviewConflictError(
            "effective_review_exists",
            f"AssetVersion {asset_version_id} already has an effective review "
            f"(head={current_head.id}); set supersedes_review_id to correct it",
        )
```

- [ ] **Step 4: Make the review route idempotent**

Wire `create_or_replay_intent()` into the review creation route, fingerprinting `(version_id, review_value, reason, supersedes_review_id)`.

- [ ] **Step 5: Update `is_ready_for_experiment` to use `project_effective_review`**

Replace the existing `is_ready_for_experiment` implementation to use the effective-review projection instead of scanning for any `approved` row.

- [ ] **Step 6: Wire effective review into gallery summaries and facets**

In `asset_queries.py`, populate `effective_review_value` on each `AssetVersionSummary` by batch-calling `project_effective_review`. Add `reviews` facet from the computed effective values.

- [ ] **Step 7: Run all review tests**

Run: `cd app && python -m pytest tests/generation/test_review_idempotent.py -v`
Expected: All 6 tests PASS.

- [ ] **Step 8: Commit**

```bash
git add app/generation/review.py app/generation/v1_router.py app/generation/asset_queries.py tests/generation/test_review_idempotent.py
git commit -m "feat(assets): idempotent review with effective-review projection and head guard"
```

---

### Task 6: ExperimentContextRef and Contextual Conclusions

**Files:**
- Create: `app/generation/experiment_conclusions.py`
- Modify: `app/generation/models.py`
- Create: migration for `experiment_conclusions` table
- Modify: `app/generation/asset_queries.py` (wire conclusions into gallery/detail)
- Test: `tests/generation/test_experiment_conclusions.py`

**Interfaces:**
- Consumes: `AssetVersionRecord` (existing), verdict/decision records (existing), `TenantContext` (existing)
- Produces: `ExperimentContextRef` and `ContextualExperimentConclusion` value objects; `project_contextual_conclusions(session, ctx, version_id) → list[ContextualExperimentConclusion]`; `project_contextual_conclusion_for_context(session, ctx, version_id, context_id) → str | None` for gallery use. Task 8 (Loser exclusion), Task 9 (export winner_only), and browser Tasks 8-11 consume these.

- [ ] **Step 1: Write the failing test**

Create `tests/generation/test_experiment_conclusions.py`:

```python
import pytest


def test_conclusion_returns_winner_for_exact_context(
    db_session, tenant_ctx, make_asset_version, make_experiment_conclusion
):
    """Conclusion projection returns winner when anchored to exact context."""
    from app.generation.experiment_conclusions import (
        project_contextual_conclusions,
    )

    version = make_asset_version()
    db_session.flush()

    make_experiment_conclusion(
        asset_version_id=version.id,
        verdict="winner",
        experiment_id="exp-1",
        market="US",
        placement="feed",
    )
    db_session.flush()

    conclusions = project_contextual_conclusions(db_session, tenant_ctx, version.id)
    assert len(conclusions) == 1
    assert conclusions[0]["verdict"] == "winner"
    assert conclusions[0]["context"]["experiment_id"] == "exp-1"


def test_conclusion_excludes_revoked(
    db_session, tenant_ctx, make_asset_version, make_experiment_conclusion
):
    """Revoked conclusions are excluded from the projection."""
    from app.generation.experiment_conclusions import (
        project_contextual_conclusions,
    )

    version = make_asset_version()
    db_session.flush()

    make_experiment_conclusion(
        asset_version_id=version.id,
        verdict="winner",
        experiment_id="exp-1",
        market="US",
        placement="feed",
        revoked=True,
    )
    db_session.flush()

    conclusions = project_contextual_conclusions(db_session, tenant_ctx, version.id)
    assert len(conclusions) == 0


def test_conclusion_requires_full_context(
    db_session, tenant_ctx, make_asset_version, make_experiment_conclusion
):
    """Legacy rows without full context are excluded."""
    from app.generation.experiment_conclusions import (
        project_contextual_conclusions,
    )

    version = make_asset_version()
    db_session.flush()

    make_experiment_conclusion(
        asset_version_id=version.id,
        verdict="winner",
        experiment_id="exp-1",
        market=None,  # Missing context
        placement=None,
    )
    db_session.flush()

    conclusions = project_contextual_conclusions(db_session, tenant_ctx, version.id)
    assert len(conclusions) == 0


def test_gallery_filter_by_conclusion(
    client, auth_headers, seed_asset_versions_with_conclusions
):
    """Gallery conclusion filter works with experiment context."""
    response = client.get(
        "/api/v1/generation/assets",
        headers=auth_headers,
        params={"experiment_context": "ctx-1", "conclusion": "winner"},
    )
    assert response.status_code == 200
    for item in response.json()["items"]:
        assert item["contextual_conclusion"] == "winner"
```

- [ ] **Step 2: Create the experiment conclusions module**

Create `app/generation/experiment_conclusions.py` with `project_contextual_conclusions()` and `project_contextual_conclusion_for_context()`. These read from the verdict/decision records and filter on full context anchor and non-revoked status.

- [ ] **Step 3: Wire conclusions into gallery and detail projections**

Update `asset_queries.py` to populate `contextual_conclusion` when `experiment_context` is provided, and include all conclusions in the detail view.

- [ ] **Step 4: Run tests**

Run: `cd app && python -m pytest tests/generation/test_experiment_conclusions.py -v`
Expected: All 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/generation/experiment_conclusions.py app/generation/models.py app/generation/asset_queries.py app/migrations/ tests/generation/test_experiment_conclusions.py
git commit -m "feat(assets): add contextual experiment conclusion projection with ExperimentContextRef"
```

---

### Task 7: Regeneration Draft Replay and Review-Summary Batch

**Files:**
- Modify: `app/generation/regeneration.py`
- Modify: `app/generation/v1_router.py`
- Test: `tests/generation/test_regeneration_replay.py`

**Interfaces:**
- Consumes: `create_or_replay_intent()` (existing), `create_regeneration_draft()` (existing), `project_effective_review()` (Task 5), `AssetVersionRecord` (existing)
- Produces: Idempotent regeneration draft creation with replay; `POST /generation/regeneration-draft-batches` (`create_generation_review_summary_regeneration`) — atomic batch that groups candidates by stable dimension signature and creates Needs Changes ×2 or Approved ×3 drafts. Browser Task 10 (`ReviewSummary`) consumes the batch.

- [ ] **Step 1: Write the failing test for regeneration replay**

Create `tests/generation/test_regeneration_replay.py`:

```python
import pytest


def test_regeneration_draft_replay_returns_same_draft(
    client, auth_headers, make_asset_version, db_session
):
    """Replaying a regeneration draft with the same key returns the same draft."""
    version = make_asset_version()
    db_session.commit()

    headers = {**auth_headers, "Idempotency-Key": "regen-replay-1"}
    body = {"source_version_id": version.id, "intent": "same_recipe"}

    r1 = client.post("/api/v1/generation/regeneration-drafts", json=body, headers=headers)
    r2 = client.post("/api/v1/generation/regeneration-drafts", json=body, headers=headers)

    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"]


def test_regeneration_replay_different_body_conflicts(
    client, auth_headers, make_asset_version, db_session
):
    """Reusing idempotency key with different intent returns 409."""
    version = make_asset_version()
    db_session.commit()

    headers = {**auth_headers, "Idempotency-Key": "regen-conflict-1"}

    r1 = client.post(
        "/api/v1/generation/regeneration-drafts",
        json={"source_version_id": version.id, "intent": "same_recipe"},
        headers=headers,
    )
    r2 = client.post(
        "/api/v1/generation/regeneration-drafts",
        json={"source_version_id": version.id, "intent": "new_seed"},
        headers=headers,
    )

    assert r1.status_code == 201
    assert r2.status_code == 409


def test_review_summary_batch_needs_changes_creates_2_per_sig(
    client, auth_headers, make_needs_changes_versions, db_session
):
    """Review summary batch creates 2 drafts per stable dimension signature."""
    version_ids = make_needs_changes_versions(signature="sig-A", count=3)
    db_session.commit()

    headers = {**auth_headers, "Idempotency-Key": "batch-nc-1"}
    body = {
        "action": "regenerate_needs_changes",
        "candidate_version_ids": version_ids,
    }

    response = client.post(
        "/api/v1/generation/regeneration-draft-batches",
        json=body, headers=headers,
    )
    assert response.status_code == 201
    drafts = response.json()["drafts"]
    assert len(drafts) == 1
    assert drafts[0]["requested_variant_count"] == 2
    assert drafts[0]["stable_dimension_signature"] == "sig-A"


def test_review_summary_batch_approved_creates_3_per_sig(
    client, auth_headers, make_approved_versions, db_session
):
    """Approved supplementation creates 3 drafts per signature."""
    version_ids = make_approved_versions(signature="sig-B", count=5)
    db_session.commit()

    headers = {**auth_headers, "Idempotency-Key": "batch-app-1"}
    body = {
        "action": "supplement_approved",
        "candidate_version_ids": version_ids,
    }

    response = client.post(
        "/api/v1/generation/regeneration-draft-batches",
        json=body, headers=headers,
    )
    assert response.status_code == 201
    drafts = response.json()["drafts"]
    assert len(drafts) == 1
    assert drafts[0]["requested_variant_count"] == 3


def test_review_summary_batch_replay_returns_same(
    client, auth_headers, make_needs_changes_versions, db_session
):
    """Replaying the batch returns the same ordered Draft result."""
    version_ids = make_needs_changes_versions(signature="sig-C", count=2)
    db_session.commit()

    headers = {**auth_headers, "Idempotency-Key": "batch-replay-1"}
    body = {
        "action": "regenerate_needs_changes",
        "candidate_version_ids": version_ids,
    }

    r1 = client.post("/api/v1/generation/regeneration-draft-batches", json=body, headers=headers)
    r2 = client.post("/api/v1/generation/regeneration-draft-batches", json=body, headers=headers)

    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["drafts"][0]["generation_draft_id"] == r2.json()["drafts"][0]["generation_draft_id"]


def test_review_summary_batch_cross_workspace_fails(
    client, auth_headers, make_needs_changes_versions, db_session, other_workspace_version_ids
):
    """Cross-workspace candidate IDs fail the whole batch."""
    headers = {**auth_headers, "Idempotency-Key": "batch-xws-1"}
    body = {
        "action": "regenerate_needs_changes",
        "candidate_version_ids": other_workspace_version_ids,
    }

    response = client.post("/api/v1/generation/regeneration-draft-batches", json=body, headers=headers)
    assert response.status_code == 422
```

- [ ] **Step 2: Make regeneration draft creation idempotent**

Wire `create_or_replay_intent()` into `create_regeneration_draft()` in `app/generation/regeneration.py`, fingerprinting `(source_version_id, intent)`.

- [ ] **Step 3: Implement the review-summary batch endpoint**

Add the `create_generation_review_summary_regeneration` function and route:

```python
def create_review_summary_regeneration(
    session: Session,
    ctx: TenantContext,
    *,
    action: str,  # "regenerate_needs_changes" | "supplement_approved"
    candidate_version_ids: list[str],
    idempotency_key: str | None = None,
) -> dict:
    # 1. Load and validate all candidates are in workspace
    # 2. Recompute effective reviews
    # 3. Filter: NC only keeps needs_changes; approved only keeps approved
    # 4. Group by stable_dimension_signature
    # 5. Per group: choose newest version as source (created_at DESC, id DESC)
    # 6. Create draft: NC = 2 variants, approved = 3 variants
    # 7. Return ordered result
    ...
```

- [ ] **Step 4: Run regeneration tests**

Run: `cd app && python -m pytest tests/generation/test_regeneration_replay.py -v`
Expected: All 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/generation/regeneration.py app/generation/v1_router.py tests/generation/test_regeneration_replay.py
git commit -m "feat(assets): idempotent regeneration drafts and atomic review-summary batch"
```

---

### Task 8: Loser Exclusion in Generation Preview

**Files:**
- Modify: `app/generation/preview.py`
- Test: `tests/generation/test_loser_exclusion.py`

**Interfaces:**
- Consumes: `project_contextual_conclusion_for_context()` (Task 6), generation preview (existing)
- Produces: When an experiment context is active, the generation/regeneration preview excludes stable dimension signatures with a confirmed Loser conclusion and returns `skipped_signature_count`, `skipped_call_count`, and authoritative cost saving estimate. Browser Task 10 (ReviewSummary) and generation UI consume this.

- [ ] **Step 1: Write the failing test**

Create `tests/generation/test_loser_exclusion.py`:

```python
import pytest


def test_preview_excludes_loser_signatures(
    db_session, tenant_ctx, make_draft_with_losers
):
    """Preview with experiment context skips Loser dimension signatures."""
    from app.generation.preview import create_preview

    draft = make_draft_with_losers(
        loser_signatures=["sig-loser-1", "sig-loser-2"],
        winner_signatures=["sig-winner-1"],
        experiment_context_id="ctx-1",
    )
    db_session.flush()

    preview = create_preview(
        db_session, tenant_ctx,
        draft_id=draft.id,
        expected_revision=draft.revision,
        experiment_context_id="ctx-1",
    )

    assert preview.skipped_signature_count == 2
    assert preview.skipped_call_count > 0
    assert preview.estimated_cost_saving > 0
    for item in preview.items:
        assert item.stable_dimension_signature not in ["sig-loser-1", "sig-loser-2"]


def test_preview_includes_all_without_context(
    db_session, tenant_ctx, make_draft_with_losers
):
    """Preview without experiment context includes all signatures."""
    from app.generation.preview import create_preview

    draft = make_draft_with_losers(
        loser_signatures=["sig-loser-1"],
        winner_signatures=["sig-winner-1"],
        experiment_context_id=None,
    )
    db_session.flush()

    preview = create_preview(
        db_session, tenant_ctx,
        draft_id=draft.id,
        expected_revision=draft.revision,
    )

    signatures = {item.stable_dimension_signature for item in preview.items}
    assert "sig-loser-1" in signatures


def test_preview_loser_exclusion_opt_out(
    db_session, tenant_ctx, make_draft_with_losers
):
    """User can explicitly disable Loser exclusion."""
    from app.generation.preview import create_preview

    draft = make_draft_with_losers(
        loser_signatures=["sig-loser-1"],
        winner_signatures=["sig-winner-1"],
        experiment_context_id="ctx-1",
    )
    db_session.flush()

    preview = create_preview(
        db_session, tenant_ctx,
        draft_id=draft.id,
        expected_revision=draft.revision,
        experiment_context_id="ctx-1",
        skip_losers=False,
    )

    signatures = {item.stable_dimension_signature for item in preview.items}
    assert "sig-loser-1" in signatures
    assert preview.skipped_signature_count == 0
```

- [ ] **Step 2: Implement Loser exclusion in preview**

Modify `app/generation/preview.py` to accept `experiment_context_id` and `skip_losers` (default `True` when context present). Query Loser conclusions for the context, collect their signatures, and filter them from the preview items. Return skipped counts and cost estimates.

- [ ] **Step 3: Run tests**

Run: `cd app && python -m pytest tests/generation/test_loser_exclusion.py -v`
Expected: All 3 tests PASS.

- [ ] **Step 4: Commit**

```bash
git add app/generation/preview.py tests/generation/test_loser_exclusion.py
git commit -m "feat(assets): add Loser exclusion to generation preview with opt-out"
```

---

### Task 9: Real Async Export (202 + Operation + Worker + Deterministic ZIP)

**Files:**
- Modify: `app/generation/export.py`
- Create: `app/generation/export_worker.py`
- Modify: `app/generation/v1_router.py`
- Test: `tests/generation/test_export_async.py`
- Test: `tests/generation/test_export_worker.py`

**Interfaces:**
- Consumes: `create_export_package()` (existing, modified), `build_manifest()` (existing, expanded), `BlobStore.open_committed()` (Task 1), `BlobStore.commit_generated_artifact()` (Task 1), Operation/Job platform (existing), `resolve_asset_blob_ref()` (Task 2), `project_contextual_conclusions()` (Task 6)
- Produces: `POST /generation/exports` returns `202 { package_id, operation_id, state: 'queued' }` + `Location` header; Worker builds deterministic ZIP with content-hash dedup, full manifest, committed blob, and Operation transitions; `GET /generation/exports/{id}` returns `ExportPackageDetail`. Browser Task 11 (`useExportFlow`) consumes the 202 + polling + download.

- [ ] **Step 1: Write the failing test for 202 export creation**

Create `tests/generation/test_export_async.py`:

```python
import pytest


def test_export_returns_202_with_operation(
    client, auth_headers, seed_export_versions, db_session
):
    """POST /generation/exports returns 202 with package_id and operation_id."""
    response = client.post(
        "/api/v1/generation/exports",
        json={"selector": {"kind": "explicit", "version_ids": seed_export_versions}},
        headers={**auth_headers, "Idempotency-Key": "export-202-1"},
    )
    assert response.status_code == 202
    data = response.json()
    assert "package_id" in data
    assert "operation_id" in data
    assert data["state"] == "queued"
    assert "Location" in response.headers


def test_export_replay_returns_same_package(
    client, auth_headers, seed_export_versions, db_session
):
    """Replaying with the same key returns the same package."""
    headers = {**auth_headers, "Idempotency-Key": "export-replay-1"}
    body = {"selector": {"kind": "explicit", "version_ids": seed_export_versions}}

    r1 = client.post("/api/v1/generation/exports", json=body, headers=headers)
    r2 = client.post("/api/v1/generation/exports", json=body, headers=headers)

    assert r1.status_code == 202
    assert r2.status_code == 202
    assert r1.json()["package_id"] == r2.json()["package_id"]


def test_export_mixed_modes_rejected(
    client, auth_headers, seed_mixed_mode_versions, db_session
):
    """Mixed execution modes are rejected."""
    response = client.post(
        "/api/v1/generation/exports",
        json={"selector": {"kind": "explicit", "version_ids": seed_mixed_mode_versions}},
        headers={**auth_headers, "Idempotency-Key": "export-mixed-1"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "mixed_execution_modes"


def test_export_package_detail_recovers_state(
    client, auth_headers, seed_export_versions, db_session
):
    """GET /generation/exports/{id} returns current state for refresh recovery."""
    r1 = client.post(
        "/api/v1/generation/exports",
        json={"selector": {"kind": "explicit", "version_ids": seed_export_versions}},
        headers={**auth_headers, "Idempotency-Key": "export-detail-1"},
    )
    package_id = r1.json()["package_id"]

    r2 = client.get(
        f"/api/v1/generation/exports/{package_id}",
        headers=auth_headers,
    )
    assert r2.status_code == 200
    assert r2.json()["package_id"] == package_id
    assert "operation_id" in r2.json()
    assert "state" in r2.json()


def test_export_approved_only_preset(
    client, auth_headers, seed_reviewed_versions, db_session
):
    """approved_only preset resolves to only approved versions."""
    response = client.post(
        "/api/v1/generation/exports",
        json={"selector": {"kind": "approved_only", "scope": {}}},
        headers={**auth_headers, "Idempotency-Key": "export-approved-1"},
    )
    assert response.status_code == 202


def test_export_winner_only_requires_context(
    client, auth_headers, seed_export_versions, db_session
):
    """winner_only without experiment_context_id is rejected."""
    response = client.post(
        "/api/v1/generation/exports",
        json={"selector": {"kind": "winner_only", "scope": {}}},
        headers={**auth_headers, "Idempotency-Key": "export-winner-no-ctx"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "experiment_context_required"
```

- [ ] **Step 2: Modify `create_export_package` for 202 flow**

Update `app/generation/export.py`:

- Accept the full `ExportSelector` discriminated union (not just `version_ids`)
- Resolve presets server-side: `approved_only` queries effective-reviewed versions; `winner_only` queries contextual conclusions; `all_successful` queries all succeeded versions
- Create Operation + Job in the same transaction
- Return `{ package_id, operation_id, state }` instead of the ORM record directly

- [ ] **Step 3: Wire the 202 route**

Update the export creation route in `v1_router.py`:

```python
@router.post(
    "/exports",
    operation_id="create_generation_export_package",
    status_code=202,
    openapi_extra={"x-marketai-idempotent": True},
)
async def create_export(
    request: Request,
    response: Response,
    body: CreateExportInput,
    ctx: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    with tenant_session(ctx) as session:
        result = export_module.create_export_package(
            session, ctx,
            selector=body.selector.model_dump(),
            idempotency_key=idempotency_key,
        )
        session.commit()

    response.headers["Location"] = f"/api/v1/operations/{result['operation_id']}"
    return result
```

- [ ] **Step 4: Run async export tests**

Run: `cd app && python -m pytest tests/generation/test_export_async.py -v`
Expected: All 6 tests PASS.

- [ ] **Step 5: Write the failing test for the export Worker**

Create `tests/generation/test_export_worker.py`:

```python
import pytest


@pytest.mark.asyncio
async def test_worker_builds_deterministic_zip(
    db_session, tenant_ctx, make_export_package_with_blobs, local_blob_store
):
    """Worker builds ZIP with deterministic content and commits it."""
    from app.generation.export_worker import handle_export_package

    package = make_export_package_with_blobs(version_count=3)
    db_session.flush()

    await handle_export_package(db_session, local_blob_store, package.id)
    db_session.flush()

    reloaded = db_session.get(type(package), package.id)
    assert reloaded.state == "ready"
    assert reloaded.committed_object_key is not None
    assert reloaded.zip_hash is not None
    assert reloaded.zip_size > 0


@pytest.mark.asyncio
async def test_worker_deduplicates_by_content_hash(
    db_session, tenant_ctx, make_export_package_with_duplicate_blobs, local_blob_store
):
    """Duplicate content hashes produce one physical payload per hash."""
    from app.generation.export_worker import handle_export_package

    package = make_export_package_with_duplicate_blobs()
    db_session.flush()

    await handle_export_package(db_session, local_blob_store, package.id)
    db_session.flush()

    reloaded = db_session.get(type(package), package.id)
    assert reloaded.dedup_count > 0
    assert reloaded.unique_blob_count < reloaded.logical_count


@pytest.mark.asyncio
async def test_worker_fails_on_unavailable_blob(
    db_session, tenant_ctx, make_export_package_with_missing_blob, local_blob_store
):
    """Worker fails the whole package when one blob is unavailable."""
    from app.generation.export_worker import handle_export_package

    package = make_export_package_with_missing_blob()
    db_session.flush()

    await handle_export_package(db_session, local_blob_store, package.id)
    db_session.flush()

    reloaded = db_session.get(type(package), package.id)
    assert reloaded.state == "failed"
    assert reloaded.error_code == "asset_blob_unavailable"


@pytest.mark.asyncio
async def test_worker_includes_manifest_json_in_zip(
    db_session, tenant_ctx, make_export_package_with_blobs, local_blob_store, tmp_path
):
    """The committed ZIP contains manifest.json with full provenance."""
    import zipfile
    from app.generation.export_worker import handle_export_package

    package = make_export_package_with_blobs(version_count=2)
    db_session.flush()

    await handle_export_package(db_session, local_blob_store, package.id)
    db_session.flush()

    reloaded = db_session.get(type(package), package.id)
    zip_path = local_blob_store.base_dir / reloaded.committed_object_key

    with zipfile.ZipFile(zip_path, "r") as zf:
        assert "manifest.json" in zf.namelist()
        import json
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["package_id"] == package.id
        assert len(manifest["entries"]) == 2


@pytest.mark.asyncio
async def test_worker_deterministic_zip_timestamps(
    db_session, tenant_ctx, make_export_package_with_blobs, local_blob_store
):
    """ZIP timestamps and permissions are fixed for determinism."""
    import zipfile
    from app.generation.export_worker import handle_export_package

    package = make_export_package_with_blobs(version_count=1)
    db_session.flush()

    await handle_export_package(db_session, local_blob_store, package.id)
    db_session.flush()

    reloaded = db_session.get(type(package), package.id)
    zip_path = local_blob_store.base_dir / reloaded.committed_object_key

    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            assert info.date_time == (2020, 1, 1, 0, 0, 0)
            assert info.external_attr == 0o644 << 16
```

- [ ] **Step 6: Implement the export Worker**

Create `app/generation/export_worker.py`:

```python
"""Export package Worker handler (spec §0.4).

Builds a deterministic ZIP from frozen AssetVersion blobs, commits it
through BlobStore.commit_generated_artifact, and transitions the package
to ready.
"""
from __future__ import annotations

import hashlib
import io
import json
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.generation.export import _get_package, _load_versions_in_selector_order, build_manifest
from app.generation.asset_queries import resolve_asset_blob_ref
from app.platform.blob_store import (
    BlobStore,
    CommittedBlobRef,
    GeneratedArtifactSpec,
)
from app.platform.tenancy import TenantContext

FIXED_DATETIME = (2020, 1, 1, 0, 0, 0)
FIXED_EXTERNAL_ATTR = 0o644 << 16


async def handle_export_package(
    session: Session,
    blob_store: BlobStore,
    package_id: str,
) -> None:
    # Load package and context
    # Advance state to building
    # For each frozen version:
    #   - resolve_asset_blob_ref
    #   - open_committed and read bytes
    #   - Track content_sha256 → first seen path for dedup
    # Build deterministic ZIP in temp dir
    # Include manifest.json
    # commit_generated_artifact
    # Record zip ref, hash, size, dedup stats
    # Transition to ready
    # On failure: transition to failed with error_code
    ...
```

The full implementation:
1. Loads the frozen selection from the package
2. Resolves and streams each committed blob
3. Deduplicates by `content_sha256` — one physical payload per hash
4. Builds ZIP with fixed timestamps (2020-01-01T00:00:00) and permissions (0o644)
5. Includes `manifest.json` with full provenance
6. Commits through `BlobStore.commit_generated_artifact`
7. Updates the package record atomically

- [ ] **Step 7: Run Worker tests**

Run: `cd app && python -m pytest tests/generation/test_export_worker.py -v`
Expected: All 5 tests PASS.

- [ ] **Step 8: Add export download intent route**

Wire `POST /generation/exports/{id}/download-intent` using the same pattern as the asset download intent from Task 2, but resolving the committed ZIP ref from the package record.

- [ ] **Step 9: Commit**

```bash
git add app/generation/export.py app/generation/export_worker.py app/generation/v1_router.py tests/generation/test_export_async.py tests/generation/test_export_worker.py
git commit -m "feat(assets): real async export with 202, deterministic ZIP Worker, and manifest"
```

---

### Task 10: RLS, Denial Audit, and OpenAPI Regeneration

**Files:**
- Create: `app/platform/rls.py`
- Create: `app/platform/denial_audit.py`
- Create: migration for RLS policies
- Modify: OpenAPI spec, operation map, BFF allowlist
- Test: `tests/generation/test_rls_denial_audit.py`

**Interfaces:**
- Consumes: All generation tables with `workspace_id` column
- Produces: PostgreSQL RLS policies on all generation tables; structured denial audit events for cross-workspace access attempts; regenerated OpenAPI spec, operation map, BFF routes, and TypeScript types for all new operations. This is the final Phase 0 gate — browser plan cannot begin until this passes.

- [ ] **Step 1: Write the failing test for RLS**

Create `tests/generation/test_rls_denial_audit.py`:

```python
import pytest


def test_rls_blocks_cross_workspace_asset_read(
    db_session, tenant_ctx, other_tenant_ctx, make_asset_version
):
    """RLS prevents reading another workspace's assets."""
    from app.generation.asset_queries import list_asset_versions

    version = make_asset_version()
    db_session.flush()

    result = list_asset_versions(db_session, other_tenant_ctx)
    assert len(result["items"]) == 0


def test_rls_blocks_cross_workspace_review(
    db_session, tenant_ctx, other_tenant_ctx, make_asset_version
):
    """RLS prevents reviewing another workspace's asset."""
    from app.generation.review import create_review, ReviewError

    version = make_asset_version()
    db_session.flush()

    with pytest.raises(ReviewError, match="asset_version_not_found"):
        create_review(
            db_session, other_tenant_ctx, version.id,
            "approved", "xws", "actor-evil",
        )


def test_rls_blocks_cross_workspace_export(
    db_session, tenant_ctx, other_tenant_ctx, make_asset_version
):
    """RLS prevents exporting another workspace's assets."""
    from app.generation.export import create_export_package, ExportError

    version = make_asset_version()
    db_session.flush()

    with pytest.raises(ExportError, match="asset_version_not_found"):
        create_export_package(
            db_session, other_tenant_ctx,
            selector={"version_ids": [version.id]},
        )


def test_denial_audit_emitted_on_cross_workspace(
    db_session, tenant_ctx, other_tenant_ctx, make_asset_version, audit_log
):
    """Cross-workspace access attempts emit structured audit records."""
    from app.generation.asset_queries import resolve_asset_blob_ref, AssetBlobError

    version = make_asset_version(
        committed_object_key="committed/ws1/assets/2026/09/08/v1.png",
        committed_storage_version_id="sv-1",
    )
    db_session.flush()

    with pytest.raises(AssetBlobError):
        resolve_asset_blob_ref(db_session, other_tenant_ctx, version.id)

    events = audit_log.events_for("generation.cross_workspace_denial")
    assert len(events) >= 1
    assert events[0]["target_workspace_id"] != other_tenant_ctx.workspace_id
```

- [ ] **Step 2: Create the RLS setup module**

Create `app/platform/rls.py`:

```python
"""PostgreSQL Row-Level Security setup for multi-tenant isolation.

Each session sets `app.current_workspace_id` via `SET LOCAL`, and RLS
policies on generation tables enforce `workspace_id = current_setting('app.current_workspace_id')`.
"""
from sqlalchemy import text
from sqlalchemy.orm import Session


def set_rls_context(session: Session, workspace_id: str) -> None:
    session.execute(text("SET LOCAL app.current_workspace_id = :ws"), {"ws": workspace_id})


RLS_TABLES = [
    "asset_versions",
    "assets",
    "quality_reviews",
    "generation_drafts",
    "generation_runs",
    "run_items",
    "export_packages",
    "asset_qc_attempts",
]
```

- [ ] **Step 3: Create the denial audit module**

Create `app/platform/denial_audit.py`:

```python
"""Structured denial audit events for cross-workspace access attempts."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger("denial_audit")


@dataclass
class DenialAuditEvent:
    event_type: str
    resource_kind: str
    resource_id: str
    requesting_workspace_id: str
    target_workspace_id: str | None
    actor_id: str
    timestamp: str
    detail: str


def emit_denial(
    event_type: str,
    resource_kind: str,
    resource_id: str,
    requesting_workspace_id: str,
    target_workspace_id: str | None,
    actor_id: str,
    detail: str = "",
) -> DenialAuditEvent:
    event = DenialAuditEvent(
        event_type=event_type,
        resource_kind=resource_kind,
        resource_id=resource_id,
        requesting_workspace_id=requesting_workspace_id,
        target_workspace_id=target_workspace_id,
        actor_id=actor_id,
        timestamp=datetime.utcnow().isoformat(),
        detail=detail,
    )
    logger.warning(
        "denial_audit",
        extra={
            "event_type": event.event_type,
            "resource_kind": event.resource_kind,
            "resource_id": event.resource_id,
            "requesting_workspace_id": event.requesting_workspace_id,
            "actor_id": event.actor_id,
        },
    )
    return event
```

- [ ] **Step 4: Create the RLS migration**

Create an Alembic migration that enables RLS on all generation tables:

```python
def upgrade() -> None:
    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY workspace_isolation ON {table}
            USING (workspace_id = current_setting('app.current_workspace_id', true))
        """)

def downgrade() -> None:
    for table in RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS workspace_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
```

- [ ] **Step 5: Regenerate OpenAPI, operation map, and types**

Run the generation scripts to produce updated:
- `docs/openapi/generation-v1.yaml`
- `web/lib/domain-client/generated/operation-map.ts`
- BFF allowlist with all new operations
- TypeScript types from OpenAPI

Verify all new operations are present:
- `list_generation_assets`
- `get_generation_asset_version`
- `create_generation_asset_version_download_intent`
- `create_generation_asset_review` (updated with idempotency)
- `create_generation_regeneration_draft` (updated with replay)
- `create_generation_review_summary_regeneration`
- `create_generation_export_package` (updated to 202)
- `get_generation_export_manifest`
- `create_generation_export_download_intent`

- [ ] **Step 6: Run all RLS/audit tests**

Run: `cd app && python -m pytest tests/generation/test_rls_denial_audit.py -v`
Expected: All 4 tests PASS.

- [ ] **Step 7: Run the full test suite**

Run: `cd app && python -m pytest tests/generation/ -v`
Expected: All generation tests PASS. No placeholder routes remain.

- [ ] **Step 8: Commit**

```bash
git add app/platform/rls.py app/platform/denial_audit.py app/migrations/ docs/openapi/ web/lib/domain-client/generated/ tests/generation/test_rls_denial_audit.py
git commit -m "feat(assets): PostgreSQL RLS, denial audit logging, and OpenAPI regeneration"
```

---

## Self-Review

### Spec §0 Coverage

| §0 Item | Task | Notes |
|---------|------|-------|
| §0.1 Committed Asset Media | Task 1 (migration + BlobStore), Task 2 (resolution + download intent) | `open_committed`, `commit_generated_artifact`, exact revision verification |
| §0.2 Typed Gallery/Detail | Task 3 (gallery), Task 4 (detail) | Keyset cursor, facets, full provenance |
| §0.3 Idempotent Review | Task 5 | Fingerprinted replay, effective-review projection, head guard, cleared |
| §0.4 Real Async Export | Task 9 | 202, Worker, deterministic ZIP, content-hash dedup, manifest |
| §0.5 Full Manifest | Task 9 (manifest included in ZIP) | All provenance fields from spec |
| §0.6 Regeneration Lineage | Task 7 | Idempotent replay, review-summary batch (NC=2, approved=3) |
| §0.7 Contextual Conclusions | Task 6 | `ExperimentContextRef`, non-revoked filter, full context anchor |
| §0.8 Batch Rail (#48 dep) | N/A — #48 dependency, not reimplemented | Plan declares the gate |
| §0.9 Recovery | Tasks 2, 5, 7, 9 | Every idempotent command uses `create_or_replay_intent()` |
| §0.10 Checklist rows 1-18 | Tasks 1-10 cover all rows | Final verification in Task 10 Step 7 |

### §0.10 Checklist Mapping

| # | Checklist Item | Task |
|---|---|---|
| 1 | Committed asset refs | Task 1 |
| 2 | Both download-intent routes | Task 2, Task 9 Step 8 |
| 3 | BlobStore deepening | Task 1 |
| 4 | Typed gallery | Task 3 |
| 5 | Asset detail | Task 4 |
| 6 | Review idempotent | Task 5 |
| 7 | Effective-review projection | Task 5 |
| 8 | Regeneration lineage | Task 7 |
| 9 | Regeneration replay + batch | Task 7 |
| 10 | Contextual conclusions | Task 6 |
| 11 | Server export selectors | Task 9 |
| 12 | 202 export + Job | Task 9 |
| 13 | Deterministic ZIP | Task 9 |
| 14 | Manifest | Task 9 |
| 15 | #48 contracts | Declared gate, not reimplemented |
| 16 | Pending-action recovery | Tasks 2, 5, 7, 9 |
| 17 | Export HTTP recovery | Task 9 |
| 18 | OpenAPI regeneration | Task 10 |

### Deviations

1. **Pagination**: Task 3 replaces the existing offset-based `_paginate` helper with a proper keyset cursor for the asset gallery. The offset pagination remains for other endpoints (drafts, runs) that still use it.

2. **ExportPackageRecord columns**: The export Worker needs `committed_object_key`, `zip_hash`, `zip_size`, `dedup_count`, `unique_blob_count`, `logical_count`, `logical_bytes`, and `error_code` columns. These may need a separate migration if not already present from the existing export implementation.

3. **Facets**: Review facets and QC state facets are wired as stubs in Task 3 and completed when Task 5 (effective review) and the QC infrastructure are in place. Tag facets require the tag extraction mechanism.

### Type Consistency

- `CommittedBlobRef` from `app.platform.blob_store` flows consistently through `resolve_asset_blob_ref` → `issue_download` → `open_committed` → `commit_generated_artifact`.
- `project_effective_review` returns `str | None`, used in gallery `_version_summary`, detail projection, `is_ready_for_experiment`, and export `approved_only` selector.
- `project_contextual_conclusions` returns `list[dict]` with the `ContextualExperimentConclusion` shape from spec §0.7.
- `create_or_replay_intent()` fingerprint pattern is consistent across review, regeneration, export, and download-intent routes.
- `ExportSelector` discriminated union shape matches spec §0.4.
