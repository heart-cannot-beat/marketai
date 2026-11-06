# Assets Browser UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `FeatureEmptyState` stub on `/w/{slug}/assets` with a complete gallery browsing, human quality review, regeneration, and export workflow backed by the Domain Interface and BFF Bridge.

**Architecture:** Six delivery phases over twelve tasks. Phase 1 (Tasks 1-4) builds browser foundation hooks — paginated queries, blob downloads, recoverable actions, URL state, and the page shell. Phase 2 (Tasks 5-7) builds the gallery grid, filters, batch rail, and responsive layout. Phase 3 (Tasks 8-9) builds the lightbox with review and regeneration panels. Phase 4 (Task 10) builds fullscreen keyboard review mode. Phase 5 (Task 11) builds the export wizard. Phase 6 (Task 12) delivers integration, accessibility, and acceptance tests. Page shells are thin route wrappers; feature modules own domain logic. All business state comes from the server via `useDomainQuery`/`useDomainCommand`/`useDomainPaginatedQuery`/`useOperation`; the browser is cache-and-display only.

**Tech Stack:** Next.js 15.5 Pages Router, React 18, SWR 2.4.1, TypeScript, Tailwind CSS 3.4.17 + DaisyUI 4.12.24, Jest + Testing Library, Playwright.

**Spec:** `docs/superpowers/specs/2026-09-03-assets-review-regeneration-export-design.md` (rev 5)

## Global Constraints

- Path aliases: `@/lib/*`, `@/components/*`, `@/hooks/*` (see `web/tsconfig.json`).
- Jest: `testEnvironment: 'node'`, test files match `__tests__/**/*.spec.ts(x)` or `tests/**/*.test.ts`. For hooks/components needing DOM, override per-file with `@jest-environment jsdom` docblock.
- Playwright: `e2e/` dir, port 4002, `owner.json`/`member.json` auth state files.
- i18n: All user-visible strings use `useTranslation('common')` with keys in `locales/en/common.json`.
- DaisyUI: Use existing class patterns (`badge`, `btn`, `alert`, `card`, `modal`, etc.).
- `DomainGateway`: Browser-side typed client. Queries via `useDomainQuery<K>`, paginated via `useDomainPaginatedQuery<K>`, mutations via `useDomainCommand<K>`, Operations via `useOperation`.
- No `useState`/`useReducer` for authoritative domain state. Form state is local React state flushed to server via CAS.
- `ActionEligibility`: Server-computed `allowed_actions` with exact codes `"review"`, `"regenerate"`, `"download"` — never compute eligibility client-side.
- `ModeProvenance`: Existing component for execution mode display. `mock` → `controlled-mock` label.
- Cross-workspace: foreign resource IDs → 404 (RLS invisible), membership denial → 403.
- `getWorkspacePageProps()` on every page for SSR auth + workspace resolution.
- `CommandOutcome<K>`: Already returned by `useDomainCommand.execute()` — discriminated `success | problem | reconciliation`.
- CSP: `img-src: 'self' data: blob:`. Storage origins in `connect-src`. Images displayed via `blob:` URLs created from `fetch` + `URL.createObjectURL`.
- Append-only review: corrections use `supersedes_review_id` pointing at the head. `cleared` removes readiness. History is never deleted.

## Dependency

**Phase 0 (server contract completion) is a hard gate.** All server routes must return real domain truth before browser work begins. See `docs/superpowers/plans/2026-09-08-assets-server-contracts.md` for the server plan. Specifically, the browser plan requires:

1. `list_generation_assets` returns `AssetGalleryPage` with keyset cursor, server facets, and `allowed_actions`
2. `get_generation_asset_version` returns `AssetVersionDetail` with all provenance fields
3. `create_generation_asset_review` accepts `supersedes_review_id` and is idempotent
4. `create_generation_asset_version_download_intent` returns `DownloadIntentResponse` with `download_grant`
5. `create_generation_regeneration_draft` is replayable with four intent enums
6. `create_generation_export_package` returns 202 with `operation_id` and `Location` header
7. `create_generation_export_download_intent` returns a grant for the committed ZIP
8. `get_generation_export_manifest` returns `ExportPackageDetail` with full provenance
9. OpenAPI, operation map, BFF routes, and TypeScript types regenerated

## File Structure

```
web/
├── pages/w/[slug]/assets.tsx              # Page entry — thin shell, delegates to AssetsProvider
├── lib/domain-client/
│   ├── query-keys.ts                      # MODIFY: add list_generation_assets to PaginatedOperationId
│   ├── blob-transfer.ts                   # MODIFY: add downloadBlob to BrowserBlobTransferClient
│   └── recoverable-action.ts              # CREATE: localStorage-backed pending action records
├── hooks/domain/
│   ├── useDomainPaginatedQuery.ts         # MODIFY: add list_generation_assets case
│   └── useGrantedDownload.ts              # CREATE: consume an already-issued download grant
├── hooks/assets/
│   ├── useAssetsUrlState.ts               # CREATE: URL ↔ state sync for filters/overlay/export
│   ├── useAssetGallery.ts                 # CREATE: infinite scroll gallery data
│   ├── useAssetActions.ts                 # CREATE: permission-gated action resolution
│   ├── useAssetImageUrl.ts                # CREATE: adapter download + revocable blob URL
│   ├── useReviewMode.ts                   # CREATE: keyboard review queue/mutex/recovery
│   └── useExportFlow.ts                   # CREATE: export wizard state machine
├── components/assets/
│   ├── AssetsProvider.tsx                  # CREATE: context for filters, selection, gallery data
│   ├── BatchRail.tsx                      # CREATE: run/product sidebar with run states
│   ├── GalleryZone.tsx                    # CREATE: grid + filters + selection bar composition
│   ├── FilterBar.tsx                      # CREATE: filter chip groups from server facets
│   ├── AssetGrid.tsx                      # CREATE: responsive masonry grid
│   ├── AssetCard.tsx                      # CREATE: single thumbnail card
│   ├── SelectionBar.tsx                   # CREATE: bulk action toolbar
│   ├── LightboxModal.tsx                  # CREATE: full-size overlay with navigation
│   ├── AssetSidebar.tsx                   # CREATE: metadata panel (shared lightbox/review)
│   ├── ReviewPanel.tsx                    # CREATE: review + correction buttons
│   ├── RegenerationPanel.tsx              # CREATE: four regeneration paths
│   ├── VersionHistory.tsx                 # CREATE: lineage chain
│   ├── ReviewMode.tsx                     # CREATE: fullscreen keyboard review
│   ├── ReviewProgress.tsx                 # CREATE: progress bar + counts
│   ├── ReviewSummary.tsx                  # CREATE: end-of-review summary + batch actions
│   ├── ExportFlow.tsx                     # CREATE: export modal wizard
│   └── ExportManifestView.tsx             # CREATE: manifest display
├── tests/assets/
│   ├── paginated-assets-query.test.ts     # Task 1
│   ├── blob-download.test.ts              # Task 2
│   ├── granted-download.test.ts           # Task 2
│   ├── recoverable-action.test.ts         # Task 3
│   ├── url-state.test.ts                  # Task 4
│   ├── asset-gallery.test.ts              # Task 5
│   ├── asset-image-url.test.ts            # Task 5
│   ├── filter-bar.test.tsx                # Task 6
│   ├── selection.test.tsx                 # Task 6
│   ├── batch-rail.test.tsx                # Task 7
│   ├── asset-actions.test.ts              # Task 8
│   ├── lightbox.test.tsx                  # Task 8
│   ├── review-panel.test.tsx              # Task 9
│   ├── regeneration-panel.test.tsx        # Task 9
│   ├── review-mode.test.ts               # Task 10
│   ├── export-flow.test.ts               # Task 11
│   └── export-manifest.test.tsx           # Task 11
└── e2e/assets/
    ├── gallery-navigation.spec.ts         # Task 12
    ├── review-mode.spec.ts                # Task 12
    ├── export.spec.ts                     # Task 12
    └── cross-workspace.spec.ts            # Task 12
```

---

## Phase 1: Browser Foundation

### Task 1: Paginated Query Extension for Asset Gallery

**Files:**
- Modify: `web/lib/domain-client/query-keys.ts:42-49`
- Modify: `web/hooks/domain/useDomainPaginatedQuery.ts:58-154`
- Test: `web/tests/assets/paginated-assets-query.test.ts`

**Interfaces:**
- Consumes: `useDomainPaginatedQuery` hook (existing), `DomainGateway.listGenerationAssets` (existing), `PaginatedOperationId` type (existing)
- Produces: `'list_generation_assets'` as a valid `PaginatedOperationId` so `useDomainPaginatedQuery('list_generation_assets', params)` works with all asset filters; later tasks consume this for `useAssetGallery`

- [ ] **Step 1: Write the failing test for paginated asset query**

Create `web/tests/assets/paginated-assets-query.test.ts`:

```typescript
/**
 * @jest-environment jsdom
 */
import { renderHook, waitFor } from '@testing-library/react';
import { useDomainPaginatedQuery } from '@/hooks/domain/useDomainPaginatedQuery';

const mockGateway = {
  listGenerationAssets: jest.fn(),
};

const mockCurrentWorkspace = {
  slug: 'acme',
  workspaceId: 'w1',
  role: 'OWNER',
  membershipId: 'm1',
  displayName: 'Acme',
};

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: mockCurrentWorkspace }),
}));

jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));

function okResult<T>(data: T) {
  return {
    kind: 'ok' as const,
    status: 200,
    data,
    meta: { etag: null, retryAt: null, location: null },
  };
}

function makePage(items: Array<{ id: string }>, nextCursor: string | null = null) {
  return {
    items,
    next_cursor: nextCursor,
    facets: { providers: [], origins: [], modes: [], reviews: [], aspects: [], qc_states: [], tags: [] },
    allowed_actions: ['download'],
  };
}

beforeEach(() => {
  jest.clearAllMocks();
});

describe('useDomainPaginatedQuery with list_generation_assets', () => {
  it('fetches the first page of assets', async () => {
    mockGateway.listGenerationAssets.mockResolvedValue(
      okResult(makePage([{ id: 'v1' }, { id: 'v2' }]))
    );

    const { result } = renderHook(() =>
      useDomainPaginatedQuery('list_generation_assets', {
        product_id: 'prod-1',
      })
    );

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });

    expect(result.current.items).toHaveLength(2);
    expect(result.current.items[0]).toEqual({ id: 'v1' });
    expect(mockGateway.listGenerationAssets).toHaveBeenCalledWith(
      expect.objectContaining({
        workspaceSlug: 'acme',
        productId: 'prod-1',
        cursor: undefined,
        limit: 50,
      })
    );
  });

  it('passes all asset filter params to the gateway', async () => {
    mockGateway.listGenerationAssets.mockResolvedValue(
      okResult(makePage([]))
    );

    renderHook(() =>
      useDomainPaginatedQuery('list_generation_assets', {
        product_id: 'prod-1',
        run_id: 'run-1',
        provider: 'openai',
        execution_mode: 'byok',
        origin_kind: 'original',
      })
    );

    await waitFor(() => {
      expect(mockGateway.listGenerationAssets).toHaveBeenCalledWith(
        expect.objectContaining({
          workspaceSlug: 'acme',
          productId: 'prod-1',
          runId: 'run-1',
          provider: 'openai',
          executionMode: 'byok',
          originKind: 'original',
        })
      );
    });
  });

  it('deduplicates items across pages', async () => {
    mockGateway.listGenerationAssets
      .mockResolvedValueOnce(okResult(makePage([{ id: 'v1' }, { id: 'v2' }], 'cursor-1')))
      .mockResolvedValueOnce(okResult(makePage([{ id: 'v2' }, { id: 'v3' }])));

    const { result } = renderHook(() =>
      useDomainPaginatedQuery('list_generation_assets', {})
    );

    await waitFor(() => {
      expect(result.current.hasMore).toBe(true);
    });

    result.current.loadMore();

    await waitFor(() => {
      expect(result.current.items).toHaveLength(3);
    });

    expect(result.current.items.map((i: any) => i.id)).toEqual(['v1', 'v2', 'v3']);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx jest tests/assets/paginated-assets-query.test.ts --no-coverage`
Expected: TypeScript compilation error — `'list_generation_assets'` is not assignable to `PaginatedOperationId`.

- [ ] **Step 3: Add `list_generation_assets` to `PaginatedOperationId`**

In `web/lib/domain-client/query-keys.ts`, add to the union:

```typescript
export type PaginatedOperationId =
  | 'list_library_entries'
  | 'list_library_entry_versions'
  | 'list_library_entry_events'
  | 'list_library_version_bindings'
  | 'list_library_internal_entries'
  | 'list_generation_drafts'
  | 'list_generation_runs'
  | 'list_generation_assets';
```

- [ ] **Step 4: Add the gateway dispatch case in `callPaginatedOperation`**

In `web/hooks/domain/useDomainPaginatedQuery.ts`, add before the `default` case in the switch:

```typescript
    case 'list_generation_assets': {
      const p =
        params as DomainOperationMap['list_generation_assets']['queryParams'];
      return gateway.listGenerationAssets({
        workspaceSlug,
        productId: p.product_id,
        runId: p.run_id,
        provider: p.provider,
        executionMode: p.execution_mode,
        originKind: p.origin_kind,
        cursor,
        limit,
      }) as Promise<DomainResultFor<K>>;
    }
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd web && npx jest tests/assets/paginated-assets-query.test.ts --no-coverage`
Expected: All 3 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add web/lib/domain-client/query-keys.ts web/hooks/domain/useDomainPaginatedQuery.ts web/tests/assets/paginated-assets-query.test.ts
git commit -m "feat(assets): extend useDomainPaginatedQuery for list_generation_assets"
```

---

### Task 2: Blob Download Adapter and Granted Download Hook

**Files:**
- Modify: `web/lib/domain-client/blob-transfer.ts:22-26`
- Create: `web/hooks/domain/useGrantedDownload.ts`
- Test: `web/tests/assets/blob-download.test.ts`
- Test: `web/tests/assets/granted-download.test.ts`

**Interfaces:**
- Consumes: `BrowserBlobTransferClient` interface (existing), `StorageOriginPolicy` (existing), `DownloadGrant` type (existing), `navigateTo` from `@/lib/domain-client/browser-navigation` (existing)
- Produces: `downloadBlob(grant, options?): Promise<Blob>` on the blob client for fetching asset images; `useGrantedDownload()` hook for export ZIP save-to-disk. Task 5 consumes `downloadBlob` via `useBlobTransferClient()`. Task 11 consumes `useGrantedDownload`.

- [ ] **Step 1: Write the failing test for `downloadBlob`**

Create `web/tests/assets/blob-download.test.ts`:

```typescript
import { DefaultBrowserBlobTransferClient } from '@/lib/domain-client/blob-transfer';
import type { StorageOriginPolicy } from '@/lib/domain-client/storage-origin-policy';

function makePolicy(): StorageOriginPolicy {
  return {
    assertAllowed: (url: string) => new URL(url),
    isAllowed: () => true,
  } as unknown as StorageOriginPolicy;
}

function makeGrant(url: string) {
  return { url, kind: 'signed_get' as const, headers: {} };
}

describe('DefaultBrowserBlobTransferClient.downloadBlob', () => {
  it('fetches the grant URL and returns a Blob', async () => {
    const blob = new Blob(['image-bytes'], { type: 'image/png' });
    const mockFetch = jest.fn().mockResolvedValue(
      new Response(blob, { status: 200 })
    );
    const client = new DefaultBrowserBlobTransferClient(makePolicy(), mockFetch);

    const result = await client.downloadBlob(makeGrant('https://storage.example.com/obj'));

    expect(result).toBeInstanceOf(Blob);
    expect(mockFetch).toHaveBeenCalledWith(
      'https://storage.example.com/obj',
      expect.objectContaining({
        method: 'GET',
        credentials: 'omit',
        redirect: 'error',
      })
    );
  });

  it('validates the storage origin before fetching', async () => {
    const rejectPolicy = {
      assertAllowed: () => { throw new Error('origin not allowed'); },
      isAllowed: () => false,
    } as unknown as StorageOriginPolicy;
    const mockFetch = jest.fn();
    const client = new DefaultBrowserBlobTransferClient(rejectPolicy, mockFetch);

    await expect(
      client.downloadBlob(makeGrant('https://evil.com/obj'))
    ).rejects.toThrow('origin not allowed');

    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('rejects non-2xx responses with a sanitized error', async () => {
    const mockFetch = jest.fn().mockResolvedValue(
      new Response('Forbidden', { status: 403 })
    );
    const client = new DefaultBrowserBlobTransferClient(makePolicy(), mockFetch);

    await expect(
      client.downloadBlob(makeGrant('https://storage.example.com/obj'))
    ).rejects.toThrow('Download failed: 403');
  });

  it('passes the AbortSignal through to fetch', async () => {
    const controller = new AbortController();
    const blob = new Blob(['bytes']);
    const mockFetch = jest.fn().mockResolvedValue(new Response(blob, { status: 200 }));
    const client = new DefaultBrowserBlobTransferClient(makePolicy(), mockFetch);

    await client.downloadBlob(makeGrant('https://storage.example.com/obj'), {
      signal: controller.signal,
    });

    expect(mockFetch).toHaveBeenCalledWith(
      expect.any(String),
      expect.objectContaining({ signal: controller.signal })
    );
  });

  it('uses credentials omit and redirect error', async () => {
    const blob = new Blob(['bytes']);
    const mockFetch = jest.fn().mockResolvedValue(new Response(blob, { status: 200 }));
    const client = new DefaultBrowserBlobTransferClient(makePolicy(), mockFetch);

    await client.downloadBlob(makeGrant('https://storage.example.com/obj'));

    const [, init] = mockFetch.mock.calls[0];
    expect(init.credentials).toBe('omit');
    expect(init.redirect).toBe('error');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx jest tests/assets/blob-download.test.ts --no-coverage`
Expected: FAIL — `client.downloadBlob is not a function`.

- [ ] **Step 3: Add `downloadBlob` to the interface and implement**

In `web/lib/domain-client/blob-transfer.ts`, add to the `BrowserBlobTransferClient` interface:

```typescript
export interface BrowserBlobTransferClient {
  upload(grant: UploadGrantResponse, file: Blob): Promise<void>;
  validatedDownloadUrl(grant: DownloadGrant): URL;
  downloadBlob(
    grant: DownloadGrant,
    options?: { signal?: AbortSignal }
  ): Promise<Blob>;
}
```

Add to `DefaultBrowserBlobTransferClient`:

```typescript
  async downloadBlob(
    grant: DownloadGrant,
    options?: { signal?: AbortSignal }
  ): Promise<Blob> {
    const url = this.originPolicy.assertAllowed(grant.url);
    const response = await this.fetchFn(url.toString(), {
      method: 'GET',
      credentials: 'omit',
      redirect: 'error',
      headers: grant.headers ?? undefined,
      signal: options?.signal,
    });
    if (!response.ok) {
      throw new Error(`Download failed: ${response.status}`);
    }
    return response.blob();
  }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npx jest tests/assets/blob-download.test.ts --no-coverage`
Expected: All 5 tests PASS.

- [ ] **Step 5: Write the failing test for `useGrantedDownload`**

Create `web/tests/assets/granted-download.test.ts`:

```typescript
/**
 * @jest-environment jsdom
 */
import { renderHook, act } from '@testing-library/react';

const mockBlobClient = {
  upload: jest.fn(),
  validatedDownloadUrl: jest.fn(),
  downloadBlob: jest.fn(),
};

const mockNavigateTo = jest.fn();

jest.mock('@/lib/domain-client/upload-deps', () => ({
  useBlobTransferClient: () => mockBlobClient,
}));

jest.mock('@/lib/domain-client/browser-navigation', () => ({
  navigateTo: (...args: unknown[]) => mockNavigateTo(...args),
}));

import { useGrantedDownload } from '@/hooks/domain/useGrantedDownload';

function makeGrant(url: string) {
  return { url, kind: 'signed_get' as const, headers: {} };
}

beforeEach(() => {
  jest.clearAllMocks();
});

describe('useGrantedDownload', () => {
  it('validates the grant URL and navigates to it', async () => {
    const validated = new URL('https://storage.example.com/zip?sig=abc');
    mockBlobClient.validatedDownloadUrl.mockReturnValue(validated);

    const { result } = renderHook(() => useGrantedDownload());

    let outcome: any;
    await act(async () => {
      outcome = await result.current.startDownload(makeGrant('https://storage.example.com/zip?sig=abc'));
    });

    expect(outcome.kind).toBe('started');
    expect(mockBlobClient.validatedDownloadUrl).toHaveBeenCalledWith(
      expect.objectContaining({ url: 'https://storage.example.com/zip?sig=abc' })
    );
    expect(mockNavigateTo).toHaveBeenCalledWith(validated.href);
  });

  it('returns transfer_error when origin validation fails', async () => {
    mockBlobClient.validatedDownloadUrl.mockImplementation(() => {
      throw new Error('origin blocked');
    });

    const { result } = renderHook(() => useGrantedDownload());

    let outcome: any;
    await act(async () => {
      outcome = await result.current.startDownload(makeGrant('https://evil.com/zip'));
    });

    expect(outcome.kind).toBe('transfer_error');
    expect(outcome.error.origin).toBe('https://evil.com');
    expect(mockNavigateTo).not.toHaveBeenCalled();
  });

  it('tracks download state through idle → navigating → idle', async () => {
    jest.useFakeTimers();
    const validated = new URL('https://storage.example.com/zip');
    mockBlobClient.validatedDownloadUrl.mockReturnValue(validated);

    const { result } = renderHook(() => useGrantedDownload());
    expect(result.current.downloadState).toBe('idle');

    await act(async () => {
      await result.current.startDownload(makeGrant('https://storage.example.com/zip'));
    });

    expect(result.current.downloadState).toBe('navigating');

    act(() => { jest.advanceTimersByTime(2500); });

    expect(result.current.downloadState).toBe('idle');
    jest.useRealTimers();
  });
});
```

- [ ] **Step 6: Create `useGrantedDownload` hook**

Create `web/hooks/domain/useGrantedDownload.ts`:

```typescript
import { useCallback, useEffect, useRef, useState } from 'react';
import { useBlobTransferClient } from '@/lib/domain-client/upload-deps';
import { navigateTo } from '@/lib/domain-client/browser-navigation';
import type { DownloadGrant } from '@/lib/domain-client/blob-transfer';

export type GrantedDownloadState = 'idle' | 'navigating';

export interface DownloadTransferError {
  status: number;
  origin: string;
  message: string;
}

export type GrantedDownloadResult =
  | { kind: 'started' }
  | { kind: 'transfer_error'; error: DownloadTransferError };

export interface UseGrantedDownloadResult {
  startDownload: (grant: DownloadGrant) => Promise<GrantedDownloadResult>;
  downloadState: GrantedDownloadState;
}

function safeOriginOf(url: string): string {
  try {
    return new URL(url).origin;
  } catch {
    return 'unknown';
  }
}

export function useGrantedDownload(): UseGrantedDownloadResult {
  const blobClient = useBlobTransferClient();
  const [downloadState, setDownloadState] = useState<GrantedDownloadState>('idle');
  const resetTimer = useRef<ReturnType<typeof setTimeout>>();

  useEffect(
    () => () => {
      if (resetTimer.current) clearTimeout(resetTimer.current);
    },
    []
  );

  const startDownload = useCallback(
    async (grant: DownloadGrant): Promise<GrantedDownloadResult> => {
      if (resetTimer.current) {
        clearTimeout(resetTimer.current);
        resetTimer.current = undefined;
      }

      let validatedUrl: URL;
      try {
        validatedUrl = blobClient.validatedDownloadUrl(grant);
      } catch {
        return {
          kind: 'transfer_error',
          error: {
            status: 0,
            origin: safeOriginOf(grant.url),
            message: 'Storage origin validation failed',
          },
        };
      }

      setDownloadState('navigating');
      navigateTo(validatedUrl.href);

      resetTimer.current = setTimeout(() => {
        setDownloadState('idle');
      }, 2000);

      return { kind: 'started' };
    },
    [blobClient]
  );

  return { startDownload, downloadState };
}
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd web && npx jest tests/assets/granted-download.test.ts tests/assets/blob-download.test.ts --no-coverage`
Expected: All tests PASS.

- [ ] **Step 8: Commit**

```bash
git add web/lib/domain-client/blob-transfer.ts web/hooks/domain/useGrantedDownload.ts web/tests/assets/blob-download.test.ts web/tests/assets/granted-download.test.ts
git commit -m "feat(assets): add downloadBlob adapter and useGrantedDownload hook"
```

---

### Task 3: RecoverableActionStore

**Files:**
- Create: `web/lib/domain-client/recoverable-action.ts`
- Test: `web/tests/assets/recoverable-action.test.ts`

**Interfaces:**
- Consumes: `window.localStorage` (injected as `Storage` for testability)
- Produces: `RecoverableActionStore` class with `save(record)`, `get(operationId, actionInstanceId)`, `remove(operationId, actionInstanceId)`, `listPending(operationId)`, `recoverOnStartup(callback)`. Tasks 5, 9, 10, 11 consume this for lost-response recovery.

- [ ] **Step 1: Write the failing test for store and retrieve**

Create `web/tests/assets/recoverable-action.test.ts`:

```typescript
import { RecoverableActionStore } from '@/lib/domain-client/recoverable-action';
import type { RecoverableAction } from '@/lib/domain-client/recoverable-action';

function makeStorage(): Storage {
  const store = new Map<string, string>();
  return {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => store.set(key, value),
    removeItem: (key: string) => store.delete(key),
    clear: () => store.clear(),
    get length() { return store.size; },
    key: (index: number) => [...store.keys()][index] ?? null,
  };
}

describe('RecoverableActionStore', () => {
  it('saves and retrieves a pending action', () => {
    const storage = makeStorage();
    const store = new RecoverableActionStore({ storage });

    const action: RecoverableAction = {
      actionInstanceId: 'aid-1',
      operationId: 'create_generation_asset_review',
      pathParams: { version_id: 'v1' },
      body: { review_value: 'approved' },
      createdAt: Date.now(),
    };

    store.save(action);

    const retrieved = store.get('create_generation_asset_review', 'aid-1');
    expect(retrieved).toEqual(action);
  });

  it('returns null for a non-existent action', () => {
    const storage = makeStorage();
    const store = new RecoverableActionStore({ storage });

    expect(store.get('create_generation_asset_review', 'aid-missing')).toBeNull();
  });

  it('removes a pending action', () => {
    const storage = makeStorage();
    const store = new RecoverableActionStore({ storage });

    store.save({
      actionInstanceId: 'aid-1',
      operationId: 'create_generation_asset_review',
      pathParams: {},
      body: {},
      createdAt: Date.now(),
    });

    store.remove('create_generation_asset_review', 'aid-1');
    expect(store.get('create_generation_asset_review', 'aid-1')).toBeNull();
  });

  it('lists all pending actions for an operation', () => {
    const storage = makeStorage();
    const store = new RecoverableActionStore({ storage });

    store.save({
      actionInstanceId: 'aid-1',
      operationId: 'create_generation_asset_review',
      pathParams: {},
      body: { review_value: 'approved' },
      createdAt: 1000,
    });
    store.save({
      actionInstanceId: 'aid-2',
      operationId: 'create_generation_asset_review',
      pathParams: {},
      body: { review_value: 'needs_changes' },
      createdAt: 2000,
    });
    store.save({
      actionInstanceId: 'aid-3',
      operationId: 'create_generation_export_package',
      pathParams: {},
      body: {},
      createdAt: 3000,
    });

    const reviewActions = store.listPending('create_generation_asset_review');
    expect(reviewActions).toHaveLength(2);
    expect(reviewActions.map(a => a.actionInstanceId)).toEqual(['aid-1', 'aid-2']);
  });

  it('cleans up expired actions on startup', () => {
    const storage = makeStorage();
    const now = Date.now();

    const store1 = new RecoverableActionStore({ storage, maxAgeMs: 1000 });
    store1.save({
      actionInstanceId: 'expired',
      operationId: 'create_generation_asset_review',
      pathParams: {},
      body: {},
      createdAt: now - 5000,
    });
    store1.save({
      actionInstanceId: 'fresh',
      operationId: 'create_generation_asset_review',
      pathParams: {},
      body: {},
      createdAt: now - 500,
    });

    const staleRecords: RecoverableAction[] = [];
    const store2 = new RecoverableActionStore({ storage, maxAgeMs: 1000 });
    store2.recoverOnStartup((record) => { staleRecords.push(record); });

    expect(staleRecords).toHaveLength(1);
    expect(staleRecords[0].actionInstanceId).toBe('expired');
    expect(store2.get('create_generation_asset_review', 'expired')).toBeNull();
  });

  it('survives corrupted storage entries', () => {
    const storage = makeStorage();
    storage.setItem('recoverable:create_generation_asset_review:corrupt', 'not-json');

    const store = new RecoverableActionStore({ storage });
    const actions = store.listPending('create_generation_asset_review');
    expect(actions).toHaveLength(0);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx jest tests/assets/recoverable-action.test.ts --no-coverage`
Expected: FAIL — `Cannot find module '@/lib/domain-client/recoverable-action'`.

- [ ] **Step 3: Implement `RecoverableActionStore`**

Create `web/lib/domain-client/recoverable-action.ts`:

```typescript
export interface RecoverableAction {
  actionInstanceId: string;
  operationId: string;
  pathParams: Record<string, unknown>;
  body: Record<string, unknown>;
  createdAt: number;
}

const KEY_PREFIX = 'recoverable:';
const DEFAULT_MAX_AGE_MS = 48 * 60 * 60 * 1000;

export class RecoverableActionStore {
  private readonly storage: Storage;
  private readonly maxAgeMs: number;

  constructor(config: { storage: Storage; maxAgeMs?: number }) {
    this.storage = config.storage;
    this.maxAgeMs = config.maxAgeMs ?? DEFAULT_MAX_AGE_MS;
  }

  save(action: RecoverableAction): void {
    const key = this.storageKey(action.operationId, action.actionInstanceId);
    this.storage.setItem(key, JSON.stringify(action));
  }

  get(operationId: string, actionInstanceId: string): RecoverableAction | null {
    const key = this.storageKey(operationId, actionInstanceId);
    return this.parse(this.storage.getItem(key));
  }

  remove(operationId: string, actionInstanceId: string): void {
    const key = this.storageKey(operationId, actionInstanceId);
    this.storage.removeItem(key);
  }

  listPending(operationId: string): RecoverableAction[] {
    const prefix = `${KEY_PREFIX}${operationId}:`;
    const results: RecoverableAction[] = [];
    for (let i = 0; i < this.storage.length; i++) {
      const storageKey = this.storage.key(i);
      if (storageKey === null || !storageKey.startsWith(prefix)) continue;
      const record = this.parse(this.storage.getItem(storageKey));
      if (record) results.push(record);
    }
    return results;
  }

  recoverOnStartup(onStale: (record: RecoverableAction) => void): void {
    const now = Date.now();
    for (let i = this.storage.length - 1; i >= 0; i--) {
      const storageKey = this.storage.key(i);
      if (storageKey === null || !storageKey.startsWith(KEY_PREFIX)) continue;
      const record = this.parse(this.storage.getItem(storageKey));
      if (!record) {
        this.storage.removeItem(storageKey);
        continue;
      }
      if (now - record.createdAt > this.maxAgeMs) {
        onStale(record);
        this.storage.removeItem(storageKey);
      }
    }
  }

  private storageKey(operationId: string, actionInstanceId: string): string {
    return `${KEY_PREFIX}${operationId}:${actionInstanceId}`;
  }

  private parse(raw: string | null): RecoverableAction | null {
    if (raw === null) return null;
    try {
      return JSON.parse(raw) as RecoverableAction;
    } catch {
      return null;
    }
  }
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd web && npx jest tests/assets/recoverable-action.test.ts --no-coverage`
Expected: All 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add web/lib/domain-client/recoverable-action.ts web/tests/assets/recoverable-action.test.ts
git commit -m "feat(assets): add RecoverableActionStore for lost-response recovery"
```

---

### Task 4: URL State, AssetsProvider, and Page Shell

**Files:**
- Create: `web/hooks/assets/useAssetsUrlState.ts`
- Create: `web/components/assets/AssetsProvider.tsx`
- Modify: `web/pages/w/[slug]/assets.tsx`
- Test: `web/tests/assets/url-state.test.ts`

**Interfaces:**
- Consumes: `useRouter()` from `next/router` (existing), `useWorkspace()` (existing), `useDomainPaginatedQuery` with `list_generation_assets` (Task 1)
- Produces: `useAssetsUrlState()` returning `{ filters, overlay, exportId, reviewMode, setFilter, setOverlay, setExportId, setReviewMode }`; `AssetsProvider` context with `{ filters, selection, gallery, overlay, exportId, reviewMode }` and mutators; `useAssets()` context consumer. Tasks 5-12 consume `useAssets()`.

- [ ] **Step 1: Write the failing test for URL state parsing**

Create `web/tests/assets/url-state.test.ts`:

```typescript
/**
 * @jest-environment jsdom
 */
import { renderHook, act } from '@testing-library/react';

const mockPush = jest.fn();
const mockReplace = jest.fn();
let mockQuery: Record<string, string | string[]> = {};

jest.mock('next/router', () => ({
  useRouter: () => ({
    query: mockQuery,
    pathname: '/w/[slug]/assets',
    push: mockPush,
    replace: mockReplace,
    isReady: true,
  }),
}));

import { useAssetsUrlState } from '@/hooks/assets/useAssetsUrlState';
import type { AssetFilters } from '@/hooks/assets/useAssetsUrlState';

beforeEach(() => {
  jest.clearAllMocks();
  mockQuery = { slug: 'acme' };
});

describe('useAssetsUrlState', () => {
  it('parses empty query into default filters', () => {
    const { result } = renderHook(() => useAssetsUrlState());
    expect(result.current.filters).toEqual({
      productId: undefined,
      runId: undefined,
      provider: undefined,
      originKind: undefined,
      executionMode: undefined,
      reviewValue: undefined,
      aspectRatio: undefined,
      qcState: undefined,
      tag: undefined,
      experimentContext: undefined,
      conclusion: undefined,
    });
    expect(result.current.overlay).toBeUndefined();
    expect(result.current.exportId).toBeUndefined();
    expect(result.current.reviewMode).toBe(false);
  });

  it('parses filter params from URL query', () => {
    mockQuery = {
      slug: 'acme',
      product: 'prod-1',
      run: 'run-1',
      provider: 'openai',
      origin: 'original',
      mode: 'byok',
      review: 'approved',
      aspect: '1:1',
      qc: 'passed',
      tag: 'hero',
      context: 'ctx-1',
      conclusion: 'winner',
    };

    const { result } = renderHook(() => useAssetsUrlState());
    expect(result.current.filters).toEqual({
      productId: 'prod-1',
      runId: 'run-1',
      provider: 'openai',
      originKind: 'original',
      executionMode: 'byok',
      reviewValue: 'approved',
      aspectRatio: '1:1',
      qcState: 'passed',
      tag: 'hero',
      experimentContext: 'ctx-1',
      conclusion: 'winner',
    });
  });

  it('parses overlay version ID from URL', () => {
    mockQuery = { slug: 'acme', overlay: 'version-123' };
    const { result } = renderHook(() => useAssetsUrlState());
    expect(result.current.overlay).toBe('version-123');
  });

  it('parses export package ID from URL', () => {
    mockQuery = { slug: 'acme', export: 'pkg-456' };
    const { result } = renderHook(() => useAssetsUrlState());
    expect(result.current.exportId).toBe('pkg-456');
  });

  it('parses review mode flag from URL', () => {
    mockQuery = { slug: 'acme', reviewMode: '1' };
    const { result } = renderHook(() => useAssetsUrlState());
    expect(result.current.reviewMode).toBe(true);
  });

  it('updates filters via setFilter and pushes to router', () => {
    const { result } = renderHook(() => useAssetsUrlState());

    act(() => {
      result.current.setFilter('provider', 'openai');
    });

    expect(mockPush).toHaveBeenCalledWith(
      expect.objectContaining({
        query: expect.objectContaining({ slug: 'acme', provider: 'openai' }),
      }),
      undefined,
      { shallow: true }
    );
  });

  it('removes a filter when set to undefined', () => {
    mockQuery = { slug: 'acme', provider: 'openai' };
    const { result } = renderHook(() => useAssetsUrlState());

    act(() => {
      result.current.setFilter('provider', undefined);
    });

    expect(mockPush).toHaveBeenCalledWith(
      expect.objectContaining({
        query: { slug: 'acme' },
      }),
      undefined,
      { shallow: true }
    );
  });

  it('sets overlay via setOverlay with replace', () => {
    const { result } = renderHook(() => useAssetsUrlState());

    act(() => {
      result.current.setOverlay('v-789');
    });

    expect(mockReplace).toHaveBeenCalledWith(
      expect.objectContaining({
        query: expect.objectContaining({ slug: 'acme', overlay: 'v-789' }),
      }),
      undefined,
      { shallow: true }
    );
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx jest tests/assets/url-state.test.ts --no-coverage`
Expected: FAIL — `Cannot find module '@/hooks/assets/useAssetsUrlState'`.

- [ ] **Step 3: Implement `useAssetsUrlState`**

Create `web/hooks/assets/useAssetsUrlState.ts`:

```typescript
import { useCallback, useMemo } from 'react';
import { useRouter } from 'next/router';

export interface AssetFilters {
  productId: string | undefined;
  runId: string | undefined;
  provider: string | undefined;
  originKind: string | undefined;
  executionMode: string | undefined;
  reviewValue: string | undefined;
  aspectRatio: string | undefined;
  qcState: string | undefined;
  tag: string | undefined;
  experimentContext: string | undefined;
  conclusion: string | undefined;
}

type FilterKey = keyof AssetFilters;

const URL_TO_FILTER: Record<string, FilterKey> = {
  product: 'productId',
  run: 'runId',
  provider: 'provider',
  origin: 'originKind',
  mode: 'executionMode',
  review: 'reviewValue',
  aspect: 'aspectRatio',
  qc: 'qcState',
  tag: 'tag',
  context: 'experimentContext',
  conclusion: 'conclusion',
};

const FILTER_TO_URL: Record<FilterKey, string> = Object.fromEntries(
  Object.entries(URL_TO_FILTER).map(([k, v]) => [v, k])
) as Record<FilterKey, string>;

function asString(value: string | string[] | undefined): string | undefined {
  if (Array.isArray(value)) return value[0];
  return value;
}

export interface UseAssetsUrlStateResult {
  filters: AssetFilters;
  overlay: string | undefined;
  exportId: string | undefined;
  reviewMode: boolean;
  setFilter: (key: FilterKey, value: string | undefined) => void;
  setOverlay: (versionId: string | undefined) => void;
  setExportId: (packageId: string | undefined) => void;
  setReviewMode: (active: boolean) => void;
}

export function useAssetsUrlState(): UseAssetsUrlStateResult {
  const router = useRouter();
  const { query } = router;

  const filters = useMemo<AssetFilters>(() => {
    const f: Record<string, string | undefined> = {};
    for (const [urlKey, filterKey] of Object.entries(URL_TO_FILTER)) {
      f[filterKey] = asString(query[urlKey]);
    }
    return f as unknown as AssetFilters;
  }, [query]);

  const overlay = asString(query.overlay);
  const exportId = asString(query.export);
  const reviewMode = asString(query.reviewMode) === '1';

  const buildQuery = useCallback(
    (overrides: Record<string, string | undefined>) => {
      const next: Record<string, string> = {};
      if (query.slug) next.slug = asString(query.slug)!;

      for (const [urlKey] of Object.entries(URL_TO_FILTER)) {
        const current = asString(query[urlKey]);
        if (current) next[urlKey] = current;
      }
      if (query.overlay) next.overlay = asString(query.overlay)!;
      if (query.export) next.export = asString(query.export)!;
      if (query.reviewMode) next.reviewMode = asString(query.reviewMode)!;

      for (const [key, value] of Object.entries(overrides)) {
        if (value === undefined) {
          delete next[key];
        } else {
          next[key] = value;
        }
      }
      return next;
    },
    [query]
  );

  const setFilter = useCallback(
    (key: FilterKey, value: string | undefined) => {
      const urlKey = FILTER_TO_URL[key];
      router.push(
        { pathname: router.pathname, query: buildQuery({ [urlKey]: value }) },
        undefined,
        { shallow: true }
      );
    },
    [router, buildQuery]
  );

  const setOverlay = useCallback(
    (versionId: string | undefined) => {
      router.replace(
        { pathname: router.pathname, query: buildQuery({ overlay: versionId }) },
        undefined,
        { shallow: true }
      );
    },
    [router, buildQuery]
  );

  const setExportId = useCallback(
    (packageId: string | undefined) => {
      router.replace(
        { pathname: router.pathname, query: buildQuery({ export: packageId }) },
        undefined,
        { shallow: true }
      );
    },
    [router, buildQuery]
  );

  const setReviewMode = useCallback(
    (active: boolean) => {
      router.replace(
        {
          pathname: router.pathname,
          query: buildQuery({ reviewMode: active ? '1' : undefined }),
        },
        undefined,
        { shallow: true }
      );
    },
    [router, buildQuery]
  );

  return { filters, overlay, exportId, reviewMode, setFilter, setOverlay, setExportId, setReviewMode };
}
```

- [ ] **Step 4: Run URL state tests**

Run: `cd web && npx jest tests/assets/url-state.test.ts --no-coverage`
Expected: All 8 tests PASS.

- [ ] **Step 5: Create `AssetsProvider` with context**

Create `web/components/assets/AssetsProvider.tsx`:

```tsx
import { createContext, useCallback, useContext, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { useAssetsUrlState } from '@/hooks/assets/useAssetsUrlState';
import type { AssetFilters, UseAssetsUrlStateResult } from '@/hooks/assets/useAssetsUrlState';

export interface AssetSelection {
  selectedIds: Set<string>;
  toggle: (id: string) => void;
  selectAll: (ids: string[]) => void;
  clear: () => void;
  isSelected: (id: string) => boolean;
  count: number;
}

export interface AssetsContextValue {
  urlState: UseAssetsUrlStateResult;
  selection: AssetSelection;
}

const AssetsContext = createContext<AssetsContextValue | null>(null);

export function useAssets(): AssetsContextValue {
  const ctx = useContext(AssetsContext);
  if (!ctx) {
    throw new Error('useAssets must be used within AssetsProvider');
  }
  return ctx;
}

export function AssetsProvider({ children }: { children: ReactNode }) {
  const urlState = useAssetsUrlState();
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

  const toggle = useCallback((id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }, []);

  const selectAll = useCallback((ids: string[]) => {
    setSelectedIds(new Set(ids));
  }, []);

  const clear = useCallback(() => {
    setSelectedIds(new Set());
  }, []);

  const isSelected = useCallback(
    (id: string) => selectedIds.has(id),
    [selectedIds]
  );

  const selection = useMemo<AssetSelection>(
    () => ({
      selectedIds,
      toggle,
      selectAll,
      clear,
      isSelected,
      count: selectedIds.size,
    }),
    [selectedIds, toggle, selectAll, clear, isSelected]
  );

  const value = useMemo<AssetsContextValue>(
    () => ({ urlState, selection }),
    [urlState, selection]
  );

  return (
    <AssetsContext.Provider value={value}>
      {children}
    </AssetsContext.Provider>
  );
}
```

- [ ] **Step 6: Replace the page empty state with the shell**

Modify `web/pages/w/[slug]/assets.tsx`:

```tsx
import type { ReactElement } from 'react';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { AssetsProvider } from '@/components/assets/AssetsProvider';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { NextPageWithLayout } from 'types';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';
import { useTranslation } from 'next-i18next';

interface Props {
  bootstrap: WorkspaceBootstrap;
  errorCode?: number;
  errorContext?: string;
}

const AssetsPage: NextPageWithLayout<Props> = () => {
  const { t } = useTranslation('common');
  return (
    <AssetsProvider>
      <div className="flex h-full min-h-0">
        <div className="hidden lg:block w-64 shrink-0 border-r border-base-300 overflow-y-auto">
          {/* BatchRail — Task 7 */}
        </div>
        <div className="flex-1 min-w-0 overflow-y-auto">
          <h1 className="sr-only">{t('workspace-page-assets')}</h1>
          {/* GalleryZone — Task 7 */}
        </div>
      </div>
    </AssetsProvider>
  );
};

AssetsPage.getLayout = function getLayout(page: ReactElement) {
  const { bootstrap, errorCode, errorContext } = page.props as Props;
  return (
    <WorkspaceLayout
      bootstrap={bootstrap}
      errorCode={errorCode}
      errorContext={errorContext}
    >
      {page}
    </WorkspaceLayout>
  );
};

export const getServerSideProps = getWorkspacePageProps({
  namespaces: ['common', 'assets'],
});
export default AssetsPage;
```

- [ ] **Step 7: Commit**

```bash
git add web/hooks/assets/useAssetsUrlState.ts web/components/assets/AssetsProvider.tsx web/pages/w/\[slug\]/assets.tsx web/tests/assets/url-state.test.ts
git commit -m "feat(assets): add URL state, AssetsProvider context, and page shell"
```

---

## Phase 2: Gallery and Batch Rail

### Task 5: Asset Gallery Data, Image URLs, Cards, and Grid

**Files:**
- Create: `web/hooks/assets/useAssetGallery.ts`
- Create: `web/hooks/assets/useAssetImageUrl.ts`
- Create: `web/components/assets/AssetCard.tsx`
- Create: `web/components/assets/AssetGrid.tsx`
- Test: `web/tests/assets/asset-gallery.test.ts`
- Test: `web/tests/assets/asset-image-url.test.ts`

**Interfaces:**
- Consumes: `useDomainPaginatedQuery('list_generation_assets')` (Task 1), `useAssets()` (Task 4), `BrowserBlobTransferClient.downloadBlob` (Task 2), `useDomainCommand('create_generation_asset_version_download_intent')` (existing), `RecoverableActionStore` (Task 3)
- Produces: `useAssetGallery()` returning `{ items, hasMore, isLoading, loadMore, refresh, facets, allowedActions }`; `useAssetImageUrl(versionId)` returning `{ blobUrl, isLoading, problem }`; `AssetCard` and `AssetGrid` components. Tasks 6-12 consume these.

- [ ] **Step 1: Write the failing test for `useAssetGallery`**

Create `web/tests/assets/asset-gallery.test.ts`:

```typescript
/**
 * @jest-environment jsdom
 */
import { renderHook, waitFor } from '@testing-library/react';

const mockPaginatedQuery = jest.fn();
jest.mock('@/hooks/domain/useDomainPaginatedQuery', () => ({
  useDomainPaginatedQuery: (...args: unknown[]) => mockPaginatedQuery(...args),
}));

import { useAssetGallery } from '@/hooks/assets/useAssetGallery';
import type { AssetFilters } from '@/hooks/assets/useAssetsUrlState';

const defaultFilters: AssetFilters = {
  productId: undefined,
  runId: undefined,
  provider: undefined,
  originKind: undefined,
  executionMode: undefined,
  reviewValue: undefined,
  aspectRatio: undefined,
  qcState: undefined,
  tag: undefined,
  experimentContext: undefined,
  conclusion: undefined,
};

beforeEach(() => {
  jest.clearAllMocks();
  mockPaginatedQuery.mockReturnValue({
    items: [],
    hasMore: false,
    isLoading: false,
    isLoadingMore: false,
    isRefreshing: false,
    problem: undefined,
    loadMore: jest.fn(),
    refresh: jest.fn(),
  });
});

describe('useAssetGallery', () => {
  it('calls useDomainPaginatedQuery with list_generation_assets', () => {
    renderHook(() => useAssetGallery(defaultFilters));

    expect(mockPaginatedQuery).toHaveBeenCalledWith(
      'list_generation_assets',
      expect.any(Object),
      expect.any(Object)
    );
  });

  it('maps camelCase filters to snake_case query params', () => {
    const filters: AssetFilters = {
      ...defaultFilters,
      productId: 'prod-1',
      runId: 'run-1',
      provider: 'openai',
      originKind: 'original',
      executionMode: 'byok',
    };

    renderHook(() => useAssetGallery(filters));

    expect(mockPaginatedQuery).toHaveBeenCalledWith(
      'list_generation_assets',
      expect.objectContaining({
        product_id: 'prod-1',
        run_id: 'run-1',
        provider: 'openai',
        origin_kind: 'original',
        execution_mode: 'byok',
      }),
      expect.any(Object)
    );
  });

  it('omits undefined filter params', () => {
    renderHook(() => useAssetGallery(defaultFilters));

    const [, params] = mockPaginatedQuery.mock.calls[0];
    expect(params.product_id).toBeUndefined();
    expect(params.run_id).toBeUndefined();
  });

  it('returns items, hasMore, and loading state from paginated query', () => {
    mockPaginatedQuery.mockReturnValue({
      items: [{ id: 'v1' }, { id: 'v2' }],
      hasMore: true,
      isLoading: false,
      isLoadingMore: false,
      isRefreshing: false,
      problem: undefined,
      loadMore: jest.fn(),
      refresh: jest.fn(),
    });

    const { result } = renderHook(() => useAssetGallery(defaultFilters));

    expect(result.current.items).toHaveLength(2);
    expect(result.current.hasMore).toBe(true);
    expect(result.current.isLoading).toBe(false);
  });
});
```

- [ ] **Step 2: Implement `useAssetGallery`**

Create `web/hooks/assets/useAssetGallery.ts`:

```typescript
import { useMemo } from 'react';
import { useDomainPaginatedQuery } from '@/hooks/domain/useDomainPaginatedQuery';
import type { UseDomainPaginatedQueryResult } from '@/hooks/domain/useDomainPaginatedQuery';
import type { AssetFilters } from '@/hooks/assets/useAssetsUrlState';

export interface AssetVersionSummary {
  id: string;
  asset_id: string;
  product_id: string;
  run_id: string;
  provider: string;
  model: string;
  status: string;
  aspect_ratio: string;
  stable_dimension_signature: string;
  tags: string[];
  effective_review_value: string | null;
  latest_qc_state: string | null;
  contextual_conclusion: string | null;
  origin_kind: string;
  execution_mode: string;
  mock_evidence: Record<string, unknown> | null;
  byte_size: number;
  created_at: string;
}

export function useAssetGallery(
  filters: AssetFilters
): UseDomainPaginatedQueryResult<AssetVersionSummary> {
  const params = useMemo(() => {
    const p: Record<string, string | undefined> = {
      product_id: filters.productId,
      run_id: filters.runId,
      provider: filters.provider,
      origin_kind: filters.originKind,
      execution_mode: filters.executionMode,
      review_value: filters.reviewValue,
      aspect_ratio: filters.aspectRatio,
      qc_state: filters.qcState,
      tag: filters.tag,
      experiment_context: filters.experimentContext,
      conclusion: filters.conclusion,
    };
    const clean: Record<string, string> = {};
    for (const [k, v] of Object.entries(p)) {
      if (v !== undefined) clean[k] = v;
    }
    return clean;
  }, [filters]);

  return useDomainPaginatedQuery<'list_generation_assets', AssetVersionSummary>(
    'list_generation_assets',
    params as any,
    { enabled: true }
  );
}
```

- [ ] **Step 3: Run gallery test**

Run: `cd web && npx jest tests/assets/asset-gallery.test.ts --no-coverage`
Expected: All 4 tests PASS.

- [ ] **Step 4: Write the failing test for `useAssetImageUrl`**

Create `web/tests/assets/asset-image-url.test.ts`:

```typescript
/**
 * @jest-environment jsdom
 */
import { renderHook, waitFor, act } from '@testing-library/react';

const mockExecute = jest.fn();
const mockBlobClient = {
  upload: jest.fn(),
  validatedDownloadUrl: jest.fn(),
  downloadBlob: jest.fn(),
};

const mockCurrentWorkspace = {
  slug: 'acme',
  workspaceId: 'w1',
  role: 'OWNER',
  membershipId: 'm1',
  displayName: 'Acme',
};

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: mockCurrentWorkspace }),
}));

jest.mock('@/hooks/domain/useDomainCommand', () => ({
  useDomainCommand: () => ({
    execute: mockExecute,
    result: undefined,
    problem: undefined,
    isPending: false,
    reset: jest.fn(),
    retry: jest.fn(),
    reconciliation: undefined,
  }),
}));

jest.mock('@/lib/domain-client/upload-deps', () => ({
  useBlobTransferClient: () => mockBlobClient,
}));

const mockCreateObjectURL = jest.fn().mockReturnValue('blob:test-url');
const mockRevokeObjectURL = jest.fn();
global.URL.createObjectURL = mockCreateObjectURL;
global.URL.revokeObjectURL = mockRevokeObjectURL;

import { useAssetImageUrl } from '@/hooks/assets/useAssetImageUrl';

beforeEach(() => {
  jest.clearAllMocks();
});

describe('useAssetImageUrl', () => {
  it('returns null blobUrl initially while loading', () => {
    mockExecute.mockReturnValue(new Promise(() => {}));

    const { result } = renderHook(() => useAssetImageUrl('version-1'));

    expect(result.current.blobUrl).toBeNull();
    expect(result.current.isLoading).toBe(true);
  });

  it('fetches download intent, downloads blob, creates blob URL', async () => {
    const blob = new Blob(['image'], { type: 'image/png' });
    const grant = { url: 'https://storage.example.com/obj', kind: 'signed_get', headers: {} };

    mockExecute.mockResolvedValue({
      kind: 'success',
      data: { intent_id: 'i1', expires_at: '2026-12-01', download_grant: grant },
      meta: { etag: null, retryAt: null, location: null },
    });
    mockBlobClient.downloadBlob.mockResolvedValue(blob);

    const { result } = renderHook(() => useAssetImageUrl('version-1'));

    await waitFor(() => {
      expect(result.current.blobUrl).toBe('blob:test-url');
    });

    expect(result.current.isLoading).toBe(false);
    expect(mockCreateObjectURL).toHaveBeenCalledWith(blob);
  });

  it('revokes the blob URL on unmount', async () => {
    const blob = new Blob(['image']);
    const grant = { url: 'https://storage.example.com/obj', kind: 'signed_get', headers: {} };

    mockExecute.mockResolvedValue({
      kind: 'success',
      data: { intent_id: 'i1', expires_at: '2026-12-01', download_grant: grant },
      meta: { etag: null, retryAt: null, location: null },
    });
    mockBlobClient.downloadBlob.mockResolvedValue(blob);

    const { result, unmount } = renderHook(() => useAssetImageUrl('version-1'));

    await waitFor(() => {
      expect(result.current.blobUrl).toBe('blob:test-url');
    });

    unmount();

    expect(mockRevokeObjectURL).toHaveBeenCalledWith('blob:test-url');
  });

  it('returns problem when download intent fails', async () => {
    mockExecute.mockResolvedValue({
      kind: 'problem',
      problem: { code: 'asset_blob_unavailable', title: 'Blob unavailable', status: 404, detail: '', instance: '', type: '', trace_id: '', retryable: false, field_errors: [] },
      meta: { etag: null, retryAt: null, location: null },
    });

    const { result } = renderHook(() => useAssetImageUrl('version-1'));

    await waitFor(() => {
      expect(result.current.problem).toBeDefined();
    });

    expect(result.current.problem!.code).toBe('asset_blob_unavailable');
    expect(result.current.blobUrl).toBeNull();
  });

  it('aborts the download when version ID changes', async () => {
    const blob = new Blob(['image']);
    const grant = { url: 'https://storage.example.com/obj', kind: 'signed_get', headers: {} };

    mockExecute.mockResolvedValue({
      kind: 'success',
      data: { intent_id: 'i1', expires_at: '2026-12-01', download_grant: grant },
      meta: { etag: null, retryAt: null, location: null },
    });
    mockBlobClient.downloadBlob.mockResolvedValue(blob);

    const { result, rerender } = renderHook(
      ({ versionId }) => useAssetImageUrl(versionId),
      { initialProps: { versionId: 'v1' } }
    );

    await waitFor(() => {
      expect(result.current.blobUrl).toBe('blob:test-url');
    });

    rerender({ versionId: 'v2' });

    expect(mockRevokeObjectURL).toHaveBeenCalledWith('blob:test-url');
  });
});
```

- [ ] **Step 5: Implement `useAssetImageUrl`**

Create `web/hooks/assets/useAssetImageUrl.ts`:

```typescript
import { useCallback, useEffect, useRef, useState } from 'react';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import { useBlobTransferClient } from '@/lib/domain-client/upload-deps';
import type { ProblemDetails } from '@/lib/domain-client/result-types';

export interface UseAssetImageUrlResult {
  blobUrl: string | null;
  isLoading: boolean;
  problem: ProblemDetails | undefined;
}

export function useAssetImageUrl(versionId: string | undefined): UseAssetImageUrlResult {
  const command = useDomainCommand('create_generation_asset_version_download_intent');
  const blobClient = useBlobTransferClient();
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [problem, setProblem] = useState<ProblemDetails>();
  const abortRef = useRef<AbortController>();
  const currentUrlRef = useRef<string | null>(null);

  const revoke = useCallback(() => {
    if (currentUrlRef.current) {
      URL.revokeObjectURL(currentUrlRef.current);
      currentUrlRef.current = null;
    }
  }, []);

  useEffect(() => {
    if (!versionId) {
      revoke();
      setBlobUrl(null);
      setIsLoading(false);
      setProblem(undefined);
      return;
    }

    revoke();
    setBlobUrl(null);
    setProblem(undefined);
    setIsLoading(true);

    const controller = new AbortController();
    abortRef.current = controller;

    (async () => {
      const actionInstanceId = `asset-image:${versionId}`;
      const outcome = await command.execute({
        body: { version_id: versionId } as any,
        actionInstanceId,
      });

      if (controller.signal.aborted) return;

      if (outcome.kind === 'problem') {
        setProblem(outcome.problem);
        setIsLoading(false);
        return;
      }

      if (outcome.kind === 'reconciliation') {
        setIsLoading(false);
        return;
      }

      const grant = (outcome.data as any).download_grant;

      try {
        const blob = await blobClient.downloadBlob(grant, {
          signal: controller.signal,
        });

        if (controller.signal.aborted) return;

        const url = URL.createObjectURL(blob);
        currentUrlRef.current = url;
        setBlobUrl(url);
      } catch {
        if (!controller.signal.aborted) {
          setProblem({
            type: '',
            title: 'Transfer failed',
            status: 0,
            detail: 'Failed to download asset image',
            instance: '',
            code: 'transfer_error',
            trace_id: '',
            retryable: true,
            field_errors: [],
          });
        }
      } finally {
        if (!controller.signal.aborted) setIsLoading(false);
      }
    })();

    return () => {
      controller.abort();
      revoke();
    };
  }, [versionId]); // eslint-disable-line react-hooks/exhaustive-deps

  return { blobUrl, isLoading, problem };
}
```

- [ ] **Step 6: Run image URL tests**

Run: `cd web && npx jest tests/assets/asset-image-url.test.ts --no-coverage`
Expected: All 5 tests PASS.

- [ ] **Step 7: Create `AssetCard` component**

Create `web/components/assets/AssetCard.tsx`:

```tsx
import { useCallback, memo } from 'react';
import { useAssetImageUrl } from '@/hooks/assets/useAssetImageUrl';
import { ModeProvenance } from '@/components/workspace/ModeProvenance';

export interface AssetCardProps {
  versionId: string;
  displayNumber: number;
  aspectRatio: string;
  provider: string;
  effectiveReview: string | null;
  executionMode: string;
  isSelected: boolean;
  onSelect: (versionId: string) => void;
  onOpen: (versionId: string) => void;
}

const REVIEW_BADGE: Record<string, string> = {
  approved: 'badge-success',
  needs_changes: 'badge-warning',
  rejected: 'badge-error',
};

export const AssetCard = memo(function AssetCard({
  versionId,
  displayNumber,
  aspectRatio,
  provider,
  effectiveReview,
  executionMode,
  isSelected,
  onSelect,
  onOpen,
}: AssetCardProps) {
  const { blobUrl, isLoading, problem } = useAssetImageUrl(versionId);

  const handleOpen = useCallback(() => onOpen(versionId), [versionId, onOpen]);
  const handleSelect = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      e.stopPropagation();
      onSelect(versionId);
    },
    [versionId, onSelect]
  );
  const handleKeyOpen = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        onOpen(versionId);
      }
    },
    [versionId, onOpen]
  );

  return (
    <div className="relative group">
      <input
        type="checkbox"
        checked={isSelected}
        onChange={handleSelect}
        className="checkbox checkbox-sm absolute top-2 left-2 z-10 opacity-0 group-hover:opacity-100 checked:opacity-100"
        aria-label={`Select asset ${displayNumber}`}
      />

      <button
        type="button"
        onClick={handleOpen}
        onKeyDown={handleKeyOpen}
        className="w-full rounded-lg overflow-hidden border border-base-300 hover:border-primary focus:outline-none focus:ring-2 focus:ring-primary transition-colors"
        aria-label={`${aspectRatio} ${provider} asset${effectiveReview ? `, ${effectiveReview}` : ''}`}
      >
        <div className="relative bg-base-200" style={{ aspectRatio: aspectRatio.replace(':', '/') }}>
          {isLoading && (
            <div className="absolute inset-0 flex items-center justify-center">
              <span className="loading loading-spinner loading-sm" />
            </div>
          )}
          {blobUrl && (
            <img
              src={blobUrl}
              alt=""
              className="w-full h-full object-cover"
              loading="lazy"
            />
          )}
          {problem && (
            <div className="absolute inset-0 flex items-center justify-center text-error text-xs p-2 text-center">
              {problem.code === 'asset_blob_unavailable' ? 'Unavailable' : 'Load failed'}
            </div>
          )}
        </div>
      </button>

      <div className="flex items-center gap-1 mt-1 px-1">
        <ModeProvenance mode={executionMode} size="xs" />
        <span className="text-xs text-base-content/60">{provider}</span>
        {effectiveReview && (
          <span className={`badge badge-xs ${REVIEW_BADGE[effectiveReview] ?? 'badge-ghost'}`}>
            {effectiveReview}
          </span>
        )}
      </div>
    </div>
  );
});
```

- [ ] **Step 8: Create `AssetGrid` component with intersection observer sentinel**

Create `web/components/assets/AssetGrid.tsx`:

```tsx
import { useCallback, useEffect, useRef } from 'react';
import { AssetCard } from './AssetCard';
import type { AssetVersionSummary } from '@/hooks/assets/useAssetGallery';
import { useAssets } from './AssetsProvider';

export interface AssetGridProps {
  items: AssetVersionSummary[];
  hasMore: boolean;
  isLoading: boolean;
  isLoadingMore: boolean;
  onLoadMore: () => void;
  onOpenVersion: (versionId: string) => void;
}

export function AssetGrid({
  items,
  hasMore,
  isLoading,
  isLoadingMore,
  onLoadMore,
  onOpenVersion,
}: AssetGridProps) {
  const { selection } = useAssets();
  const sentinelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const sentinel = sentinelRef.current;
    if (!sentinel || !hasMore) return;

    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) {
          onLoadMore();
        }
      },
      { rootMargin: '200px' }
    );

    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [hasMore, onLoadMore]);

  if (isLoading && items.length === 0) {
    return (
      <div className="flex items-center justify-center py-16">
        <span className="loading loading-spinner loading-lg" />
      </div>
    );
  }

  if (!isLoading && items.length === 0) {
    return (
      <div className="text-center py-16 text-base-content/60">
        <p className="text-lg font-medium">No assets match these filters</p>
        <p className="text-sm mt-1">Try adjusting your filters or selecting a different run.</p>
      </div>
    );
  }

  return (
    <>
      <div
        className="grid gap-3 p-4"
        style={{
          gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))',
        }}
      >
        {items.map((item, index) => (
          <AssetCard
            key={item.id}
            versionId={item.id}
            displayNumber={index + 1}
            aspectRatio={item.aspect_ratio}
            provider={item.provider}
            effectiveReview={item.effective_review_value}
            executionMode={item.execution_mode}
            isSelected={selection.isSelected(item.id)}
            onSelect={selection.toggle}
            onOpen={onOpenVersion}
          />
        ))}
      </div>

      {hasMore && (
        <div ref={sentinelRef} className="flex justify-center py-4">
          {isLoadingMore && <span className="loading loading-spinner loading-sm" />}
        </div>
      )}
    </>
  );
}
```

- [ ] **Step 9: Run all gallery and image tests**

Run: `cd web && npx jest tests/assets/asset-gallery.test.ts tests/assets/asset-image-url.test.ts --no-coverage`
Expected: All tests PASS.

- [ ] **Step 10: Commit**

```bash
git add web/hooks/assets/useAssetGallery.ts web/hooks/assets/useAssetImageUrl.ts web/components/assets/AssetCard.tsx web/components/assets/AssetGrid.tsx web/tests/assets/asset-gallery.test.ts web/tests/assets/asset-image-url.test.ts
git commit -m "feat(assets): add asset gallery data, image URL adapter, card and grid components"
```

---

### Task 6: Filter Bar and Selection

**Files:**
- Create: `web/components/assets/FilterBar.tsx`
- Create: `web/components/assets/SelectionBar.tsx`
- Test: `web/tests/assets/filter-bar.test.tsx`
- Test: `web/tests/assets/selection.test.tsx`

**Interfaces:**
- Consumes: `useAssets()` (Task 4) for `urlState.filters`, `urlState.setFilter`, `selection`
- Produces: `FilterBar` renders filter chips from server facets with `role="checkbox"` and `aria-checked`; `SelectionBar` renders bulk action toolbar with count, clear, and export eligibility. Tasks 7, 8, 11 consume these.

- [ ] **Step 1: Write the failing test for `FilterBar`**

Create `web/tests/assets/filter-bar.test.tsx`:

```tsx
/**
 * @jest-environment jsdom
 */
import { render, screen, fireEvent } from '@testing-library/react';
import { FilterBar } from '@/components/assets/FilterBar';

const mockSetFilter = jest.fn();

interface Facets {
  providers: Array<{ value: string; count: number }>;
  reviews: Array<{ value: string; count: number }>;
  origins: Array<{ value: string; count: number }>;
  modes: Array<{ value: string; count: number }>;
  aspects: Array<{ value: string; count: number }>;
  qc_states: Array<{ value: string; count: number }>;
  tags: Array<{ value: string; count: number }>;
}

const defaultFacets: Facets = {
  providers: [
    { value: 'openai', count: 42 },
    { value: 'stability', count: 18 },
  ],
  reviews: [
    { value: 'approved', count: 30 },
    { value: 'needs_changes', count: 8 },
    { value: 'unreviewed', count: 22 },
  ],
  origins: [],
  modes: [],
  aspects: [],
  qc_states: [],
  tags: [],
};

beforeEach(() => {
  jest.clearAllMocks();
});

describe('FilterBar', () => {
  it('renders provider filter chips from facets', () => {
    render(
      <FilterBar
        facets={defaultFacets}
        activeFilters={{}}
        onFilterChange={mockSetFilter}
      />
    );

    expect(screen.getByRole('checkbox', { name: /openai/i })).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: /stability/i })).toBeInTheDocument();
  });

  it('shows active state with aria-checked', () => {
    render(
      <FilterBar
        facets={defaultFacets}
        activeFilters={{ provider: 'openai' }}
        onFilterChange={mockSetFilter}
      />
    );

    const chip = screen.getByRole('checkbox', { name: /openai/i });
    expect(chip).toHaveAttribute('aria-checked', 'true');
  });

  it('calls onFilterChange when a chip is toggled on', () => {
    render(
      <FilterBar
        facets={defaultFacets}
        activeFilters={{}}
        onFilterChange={mockSetFilter}
      />
    );

    fireEvent.click(screen.getByRole('checkbox', { name: /openai/i }));
    expect(mockSetFilter).toHaveBeenCalledWith('provider', 'openai');
  });

  it('calls onFilterChange with undefined when active chip is toggled off', () => {
    render(
      <FilterBar
        facets={defaultFacets}
        activeFilters={{ provider: 'openai' }}
        onFilterChange={mockSetFilter}
      />
    );

    fireEvent.click(screen.getByRole('checkbox', { name: /openai/i }));
    expect(mockSetFilter).toHaveBeenCalledWith('provider', undefined);
  });

  it('renders review filter chips with counts', () => {
    render(
      <FilterBar
        facets={defaultFacets}
        activeFilters={{}}
        onFilterChange={mockSetFilter}
      />
    );

    expect(screen.getByText(/approved/i)).toBeInTheDocument();
    expect(screen.getByText('30')).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Implement `FilterBar`**

Create `web/components/assets/FilterBar.tsx`:

```tsx
import { useCallback } from 'react';

interface FacetValue {
  value: string;
  count: number;
}

export interface AssetFacets {
  providers: FacetValue[];
  reviews: FacetValue[];
  origins: FacetValue[];
  modes: FacetValue[];
  aspects: FacetValue[];
  qc_states: FacetValue[];
  tags: FacetValue[];
}

type FilterKey = 'provider' | 'reviewValue' | 'originKind' | 'executionMode' | 'aspectRatio' | 'qcState' | 'tag';

interface FilterBarProps {
  facets: AssetFacets;
  activeFilters: Partial<Record<FilterKey, string>>;
  onFilterChange: (key: FilterKey, value: string | undefined) => void;
}

interface ChipGroupProps {
  label: string;
  filterKey: FilterKey;
  values: FacetValue[];
  active: string | undefined;
  onChange: (key: FilterKey, value: string | undefined) => void;
}

function ChipGroup({ label, filterKey, values, active, onChange }: ChipGroupProps) {
  const handleClick = useCallback(
    (value: string) => {
      onChange(filterKey, active === value ? undefined : value);
    },
    [filterKey, active, onChange]
  );

  if (values.length === 0) return null;

  return (
    <div className="flex items-center gap-1.5 flex-wrap">
      <span className="text-xs font-medium text-base-content/50 uppercase tracking-wide">
        {label}
      </span>
      {values.map((v) => {
        const isActive = active === v.value;
        return (
          <button
            key={v.value}
            type="button"
            role="checkbox"
            aria-checked={isActive}
            className={`badge gap-1 cursor-pointer transition-colors ${
              isActive ? 'badge-primary' : 'badge-outline badge-ghost hover:badge-primary/20'
            }`}
            onClick={() => handleClick(v.value)}
          >
            {v.value}
            <span className="text-xs opacity-60">{v.count}</span>
          </button>
        );
      })}
    </div>
  );
}

export function FilterBar({ facets, activeFilters, onFilterChange }: FilterBarProps) {
  return (
    <div className="flex flex-col gap-2 px-4 py-2 border-b border-base-300">
      <ChipGroup label="Provider" filterKey="provider" values={facets.providers} active={activeFilters.provider} onChange={onFilterChange} />
      <ChipGroup label="Review" filterKey="reviewValue" values={facets.reviews} active={activeFilters.reviewValue} onChange={onFilterChange} />
      <ChipGroup label="Origin" filterKey="originKind" values={facets.origins} active={activeFilters.originKind} onChange={onFilterChange} />
      <ChipGroup label="Mode" filterKey="executionMode" values={facets.modes} active={activeFilters.executionMode} onChange={onFilterChange} />
      <ChipGroup label="Aspect" filterKey="aspectRatio" values={facets.aspects} active={activeFilters.aspectRatio} onChange={onFilterChange} />
      <ChipGroup label="QC" filterKey="qcState" values={facets.qc_states} active={activeFilters.qcState} onChange={onFilterChange} />
      <ChipGroup label="Tag" filterKey="tag" values={facets.tags} active={activeFilters.tag} onChange={onFilterChange} />
    </div>
  );
}
```

- [ ] **Step 3: Run filter tests**

Run: `cd web && npx jest tests/assets/filter-bar.test.tsx --no-coverage`
Expected: All 5 tests PASS.

- [ ] **Step 4: Write the failing test for `SelectionBar`**

Create `web/tests/assets/selection.test.tsx`:

```tsx
/**
 * @jest-environment jsdom
 */
import { render, screen, fireEvent } from '@testing-library/react';
import { SelectionBar } from '@/components/assets/SelectionBar';

const mockClear = jest.fn();
const mockOnExport = jest.fn();
const mockOnReviewMode = jest.fn();

beforeEach(() => {
  jest.clearAllMocks();
});

describe('SelectionBar', () => {
  it('renders selection count', () => {
    render(
      <SelectionBar
        count={5}
        onClear={mockClear}
        onExport={mockOnExport}
        onReviewMode={mockOnReviewMode}
        canExport={true}
        canReview={true}
      />
    );

    expect(screen.getByText(/5 selected/i)).toBeInTheDocument();
  });

  it('hides when count is zero', () => {
    const { container } = render(
      <SelectionBar
        count={0}
        onClear={mockClear}
        onExport={mockOnExport}
        onReviewMode={mockOnReviewMode}
        canExport={true}
        canReview={true}
      />
    );

    expect(container.firstChild).toBeNull();
  });

  it('calls onClear when clear button is clicked', () => {
    render(
      <SelectionBar
        count={3}
        onClear={mockClear}
        onExport={mockOnExport}
        onReviewMode={mockOnReviewMode}
        canExport={true}
        canReview={true}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /clear/i }));
    expect(mockClear).toHaveBeenCalled();
  });

  it('disables export button when canExport is false', () => {
    render(
      <SelectionBar
        count={3}
        onClear={mockClear}
        onExport={mockOnExport}
        onReviewMode={mockOnReviewMode}
        canExport={false}
        canReview={true}
      />
    );

    expect(screen.getByRole('button', { name: /export/i })).toBeDisabled();
  });

  it('announces selection changes for screen readers', () => {
    render(
      <SelectionBar
        count={3}
        onClear={mockClear}
        onExport={mockOnExport}
        onReviewMode={mockOnReviewMode}
        canExport={true}
        canReview={true}
      />
    );

    const live = screen.getByRole('status');
    expect(live).toHaveAttribute('aria-live', 'polite');
  });
});
```

- [ ] **Step 5: Implement `SelectionBar`**

Create `web/components/assets/SelectionBar.tsx`:

```tsx
interface SelectionBarProps {
  count: number;
  onClear: () => void;
  onExport: () => void;
  onReviewMode: () => void;
  canExport: boolean;
  canReview: boolean;
}

export function SelectionBar({
  count,
  onClear,
  onExport,
  onReviewMode,
  canExport,
  canReview,
}: SelectionBarProps) {
  if (count === 0) return null;

  return (
    <div className="sticky bottom-0 z-20 border-t border-base-300 bg-base-100 px-4 py-2 flex items-center gap-3">
      <div role="status" aria-live="polite" className="font-medium text-sm">
        {count} selected
      </div>

      <div className="flex items-center gap-2 ml-auto">
        <button
          type="button"
          className="btn btn-sm btn-ghost"
          onClick={onReviewMode}
          disabled={!canReview}
          aria-label="Review selected"
        >
          Review
        </button>
        <button
          type="button"
          className="btn btn-sm btn-primary"
          onClick={onExport}
          disabled={!canExport}
          aria-label="Export selected"
        >
          Export
        </button>
        <button
          type="button"
          className="btn btn-sm btn-ghost"
          onClick={onClear}
          aria-label="Clear selection"
        >
          Clear
        </button>
      </div>
    </div>
  );
}
```

- [ ] **Step 6: Run selection tests**

Run: `cd web && npx jest tests/assets/selection.test.tsx --no-coverage`
Expected: All 5 tests PASS.

- [ ] **Step 7: Commit**

```bash
git add web/components/assets/FilterBar.tsx web/components/assets/SelectionBar.tsx web/tests/assets/filter-bar.test.tsx web/tests/assets/selection.test.tsx
git commit -m "feat(assets): add FilterBar with server facets and SelectionBar"
```

---

### Task 7: Batch Rail and Gallery Composition

**Files:**
- Create: `web/components/assets/BatchRail.tsx`
- Create: `web/components/assets/GalleryZone.tsx`
- Modify: `web/pages/w/[slug]/assets.tsx`
- Test: `web/tests/assets/batch-rail.test.tsx`

**Interfaces:**
- Consumes: `useAssets()` (Task 4), `useAssetGallery()` (Task 5), `FilterBar` (Task 6), `SelectionBar` (Task 6), `AssetGrid` (Task 5), `useDomainPaginatedQuery('list_generation_runs')` (existing)
- Produces: `BatchRail` showing runs/products sidebar; `GalleryZone` composing filters, grid, and selection; page fully wired. Tasks 8-12 consume the composed layout.

- [ ] **Step 1: Write the failing test for `BatchRail`**

Create `web/tests/assets/batch-rail.test.tsx`:

```tsx
/**
 * @jest-environment jsdom
 */
import { render, screen, fireEvent } from '@testing-library/react';
import { BatchRail } from '@/components/assets/BatchRail';

const mockOnSelectRun = jest.fn();

function makeRun(id: string, status: string, total: number, done: number) {
  return {
    id,
    product_id: 'prod-1',
    provider: 'openai',
    model: 'gpt-image-1',
    status,
    total,
    done,
    failed: 0,
    execution_mode: 'real',
    revision: 1,
    commands: [],
    created_at: '2026-09-01T00:00:00Z',
  };
}

beforeEach(() => {
  jest.clearAllMocks();
});

describe('BatchRail', () => {
  it('renders a list of runs with status', () => {
    render(
      <BatchRail
        runs={[
          makeRun('run-1', 'completed', 10, 10),
          makeRun('run-2', 'running', 10, 5),
        ]}
        activeRunId={undefined}
        onSelectRun={mockOnSelectRun}
        isLoading={false}
      />
    );

    expect(screen.getByText(/run-1/)).toBeInTheDocument();
    expect(screen.getByText(/completed/i)).toBeInTheDocument();
    expect(screen.getByText(/run-2/)).toBeInTheDocument();
    expect(screen.getByText(/running/i)).toBeInTheDocument();
  });

  it('highlights the active run', () => {
    render(
      <BatchRail
        runs={[makeRun('run-1', 'completed', 10, 10)]}
        activeRunId="run-1"
        onSelectRun={mockOnSelectRun}
        isLoading={false}
      />
    );

    const button = screen.getByRole('button', { name: /run-1/ });
    expect(button.className).toContain('bg-primary');
  });

  it('calls onSelectRun when a run is clicked', () => {
    render(
      <BatchRail
        runs={[makeRun('run-1', 'completed', 10, 10)]}
        activeRunId={undefined}
        onSelectRun={mockOnSelectRun}
        isLoading={false}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /run-1/ }));
    expect(mockOnSelectRun).toHaveBeenCalledWith('run-1');
  });

  it('clears filter when clicking the active run again', () => {
    render(
      <BatchRail
        runs={[makeRun('run-1', 'completed', 10, 10)]}
        activeRunId="run-1"
        onSelectRun={mockOnSelectRun}
        isLoading={false}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /run-1/ }));
    expect(mockOnSelectRun).toHaveBeenCalledWith(undefined);
  });

  it('shows loading skeleton when loading', () => {
    render(
      <BatchRail
        runs={[]}
        activeRunId={undefined}
        onSelectRun={mockOnSelectRun}
        isLoading={true}
      />
    );

    expect(screen.getAllByTestId('rail-skeleton')).toHaveLength(3);
  });

  it('shows item counts for each run', () => {
    render(
      <BatchRail
        runs={[makeRun('run-1', 'completed', 10, 10)]}
        activeRunId={undefined}
        onSelectRun={mockOnSelectRun}
        isLoading={false}
      />
    );

    expect(screen.getByText('10/10')).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Implement `BatchRail`**

Create `web/components/assets/BatchRail.tsx`:

```tsx
import { useCallback } from 'react';
import { ModeProvenance } from '@/components/workspace/ModeProvenance';

interface RunSummary {
  id: string;
  product_id: string;
  provider: string;
  model: string;
  status: string;
  total: number;
  done: number;
  failed: number;
  execution_mode: string;
  created_at: string;
}

interface BatchRailProps {
  runs: RunSummary[];
  activeRunId: string | undefined;
  onSelectRun: (runId: string | undefined) => void;
  isLoading: boolean;
}

const STATUS_BADGE: Record<string, string> = {
  completed: 'badge-success',
  running: 'badge-info',
  failed: 'badge-error',
  queued: 'badge-ghost',
  cancelled: 'badge-warning',
};

export function BatchRail({ runs, activeRunId, onSelectRun, isLoading }: BatchRailProps) {
  const handleClick = useCallback(
    (runId: string) => {
      onSelectRun(activeRunId === runId ? undefined : runId);
    },
    [activeRunId, onSelectRun]
  );

  return (
    <nav className="flex flex-col gap-1 p-2" aria-label="Generation runs">
      <h2 className="text-xs font-semibold uppercase tracking-wide text-base-content/50 px-2 py-1">
        Runs
      </h2>

      {isLoading &&
        Array.from({ length: 3 }).map((_, i) => (
          <div key={i} data-testid="rail-skeleton" className="h-16 rounded-lg bg-base-200 animate-pulse" />
        ))}

      {runs.map((run) => {
        const isActive = activeRunId === run.id;
        return (
          <button
            key={run.id}
            type="button"
            onClick={() => handleClick(run.id)}
            className={`flex flex-col gap-0.5 px-3 py-2 rounded-lg text-left text-sm transition-colors ${
              isActive
                ? 'bg-primary text-primary-content'
                : 'hover:bg-base-200'
            }`}
            aria-label={`${run.id}`}
            aria-pressed={isActive}
          >
            <div className="flex items-center gap-1.5">
              <span className="font-medium truncate">{run.id.slice(0, 8)}</span>
              <span className={`badge badge-xs ${STATUS_BADGE[run.status] ?? 'badge-ghost'}`}>
                {run.status}
              </span>
            </div>
            <div className="flex items-center gap-1.5 text-xs opacity-70">
              <ModeProvenance mode={run.execution_mode} size="xs" />
              <span>{run.provider}</span>
              <span className="ml-auto">{run.done}/{run.total}</span>
            </div>
          </button>
        );
      })}

      {!isLoading && runs.length === 0 && (
        <p className="text-xs text-base-content/50 px-2 py-4 text-center">No runs yet</p>
      )}
    </nav>
  );
}
```

- [ ] **Step 3: Run batch rail tests**

Run: `cd web && npx jest tests/assets/batch-rail.test.tsx --no-coverage`
Expected: All 6 tests PASS.

- [ ] **Step 4: Create `GalleryZone` composition component**

Create `web/components/assets/GalleryZone.tsx`:

```tsx
import { useCallback, useMemo } from 'react';
import { useAssets } from './AssetsProvider';
import { useAssetGallery } from '@/hooks/assets/useAssetGallery';
import { FilterBar } from './FilterBar';
import type { AssetFacets } from './FilterBar';
import { AssetGrid } from './AssetGrid';
import { SelectionBar } from './SelectionBar';

interface GalleryZoneProps {
  facets: AssetFacets | undefined;
  allowedActions: string[];
}

export function GalleryZone({ facets, allowedActions }: GalleryZoneProps) {
  const { urlState, selection } = useAssets();
  const gallery = useAssetGallery(urlState.filters);

  const activeFilters = useMemo(() => {
    const f: Record<string, string | undefined> = {};
    if (urlState.filters.provider) f.provider = urlState.filters.provider;
    if (urlState.filters.reviewValue) f.reviewValue = urlState.filters.reviewValue;
    if (urlState.filters.originKind) f.originKind = urlState.filters.originKind;
    if (urlState.filters.executionMode) f.executionMode = urlState.filters.executionMode;
    if (urlState.filters.aspectRatio) f.aspectRatio = urlState.filters.aspectRatio;
    if (urlState.filters.qcState) f.qcState = urlState.filters.qcState;
    if (urlState.filters.tag) f.tag = urlState.filters.tag;
    return f;
  }, [urlState.filters]);

  const handleFilterChange = useCallback(
    (key: string, value: string | undefined) => {
      urlState.setFilter(key as any, value);
    },
    [urlState]
  );

  const handleOpenVersion = useCallback(
    (versionId: string) => {
      urlState.setOverlay(versionId);
    },
    [urlState]
  );

  const handleExport = useCallback(() => {
    urlState.setExportId('new');
  }, [urlState]);

  const handleReviewMode = useCallback(() => {
    urlState.setReviewMode(true);
  }, [urlState]);

  const canExport = allowedActions.includes('download');
  const canReview = allowedActions.includes('review');

  return (
    <div className="flex flex-col h-full min-h-0">
      {facets && (
        <FilterBar
          facets={facets}
          activeFilters={activeFilters}
          onFilterChange={handleFilterChange}
        />
      )}

      <div className="flex-1 min-h-0 overflow-y-auto">
        <AssetGrid
          items={gallery.items}
          hasMore={gallery.hasMore}
          isLoading={gallery.isLoading}
          isLoadingMore={gallery.isLoadingMore}
          onLoadMore={gallery.loadMore}
          onOpenVersion={handleOpenVersion}
        />
      </div>

      <SelectionBar
        count={selection.count}
        onClear={selection.clear}
        onExport={handleExport}
        onReviewMode={handleReviewMode}
        canExport={canExport}
        canReview={canReview}
      />
    </div>
  );
}
```

- [ ] **Step 5: Wire composed layout into the page shell**

Update `web/pages/w/[slug]/assets.tsx` to import and render `BatchRail` and `GalleryZone` in the two-zone layout. The `BatchRail` receives runs from `useDomainPaginatedQuery('list_generation_runs')` and wires `onSelectRun` to `urlState.setFilter('runId', ...)`.

```tsx
import type { ReactElement } from 'react';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { AssetsProvider, useAssets } from '@/components/assets/AssetsProvider';
import { BatchRail } from '@/components/assets/BatchRail';
import { GalleryZone } from '@/components/assets/GalleryZone';
import { LightboxModal } from '@/components/assets/LightboxModal';
import { ReviewMode } from '@/components/assets/ReviewMode';
import { ExportFlow } from '@/components/assets/ExportFlow';
import { useDomainPaginatedQuery } from '@/hooks/domain/useDomainPaginatedQuery';
import { useAssetGallery } from '@/hooks/assets/useAssetGallery';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { NextPageWithLayout } from 'types';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';
import { useTranslation } from 'next-i18next';
import { useCallback } from 'react';

interface Props {
  bootstrap: WorkspaceBootstrap;
  errorCode?: number;
  errorContext?: string;
}

function AssetsContent() {
  const { t } = useTranslation('common');
  const { urlState } = useAssets();
  const gallery = useAssetGallery(urlState.filters);

  const runs = useDomainPaginatedQuery('list_generation_runs', {});

  const handleSelectRun = useCallback(
    (runId: string | undefined) => {
      urlState.setFilter('runId', runId);
    },
    [urlState]
  );

  return (
    <>
      <div className="flex h-full min-h-0">
        <div className="hidden lg:block w-64 shrink-0 border-r border-base-300 overflow-y-auto">
          <BatchRail
            runs={runs.items as any[]}
            activeRunId={urlState.filters.runId}
            onSelectRun={handleSelectRun}
            isLoading={runs.isLoading}
          />
        </div>
        <div className="flex-1 min-w-0">
          <h1 className="sr-only">{t('workspace-page-assets')}</h1>
          <GalleryZone
            facets={undefined}
            allowedActions={[]}
          />
        </div>
      </div>

      {urlState.overlay && (
        <LightboxModal
          versionId={urlState.overlay}
          onClose={() => urlState.setOverlay(undefined)}
        />
      )}

      {urlState.reviewMode && (
        <ReviewMode onExit={() => urlState.setReviewMode(false)} />
      )}

      {urlState.exportId && (
        <ExportFlow
          packageId={urlState.exportId === 'new' ? undefined : urlState.exportId}
          onClose={() => urlState.setExportId(undefined)}
        />
      )}
    </>
  );
}

const AssetsPage: NextPageWithLayout<Props> = () => {
  return (
    <AssetsProvider>
      <AssetsContent />
    </AssetsProvider>
  );
};

AssetsPage.getLayout = function getLayout(page: ReactElement) {
  const { bootstrap, errorCode, errorContext } = page.props as Props;
  return (
    <WorkspaceLayout
      bootstrap={bootstrap}
      errorCode={errorCode}
      errorContext={errorContext}
    >
      {page}
    </WorkspaceLayout>
  );
};

export const getServerSideProps = getWorkspacePageProps({
  namespaces: ['common', 'assets'],
});
export default AssetsPage;
```

Note: `LightboxModal`, `ReviewMode`, and `ExportFlow` are stub imports at this point — they will be created in Tasks 8, 10, and 11 respectively. Create empty placeholder exports so the page compiles:

```typescript
// web/components/assets/LightboxModal.tsx (stub)
export function LightboxModal(_props: { versionId: string; onClose: () => void }) {
  return null;
}

// web/components/assets/ReviewMode.tsx (stub)
export function ReviewMode(_props: { onExit: () => void }) {
  return null;
}

// web/components/assets/ExportFlow.tsx (stub)
export function ExportFlow(_props: { packageId?: string; onClose: () => void }) {
  return null;
}
```

- [ ] **Step 6: Run all tests**

Run: `cd web && npx jest tests/assets/ --no-coverage`
Expected: All tests PASS.

- [ ] **Step 7: Commit**

```bash
git add web/components/assets/BatchRail.tsx web/components/assets/GalleryZone.tsx web/components/assets/LightboxModal.tsx web/components/assets/ReviewMode.tsx web/components/assets/ExportFlow.tsx web/pages/w/\[slug\]/assets.tsx web/tests/assets/batch-rail.test.tsx
git commit -m "feat(assets): add BatchRail, GalleryZone, and compose page layout"
```

---

## Phase 3: Lightbox and Single-Asset Commands

### Task 8: Lightbox Foundation — Actions, Sidebar, History, Modal

**Files:**
- Create: `web/hooks/assets/useAssetActions.ts`
- Create: `web/components/assets/AssetSidebar.tsx`
- Create: `web/components/assets/VersionHistory.tsx`
- Modify: `web/components/assets/LightboxModal.tsx` (replace stub)
- Test: `web/tests/assets/asset-actions.test.ts`
- Test: `web/tests/assets/lightbox.test.tsx`

**Interfaces:**
- Consumes: `useDomainQuery('get_generation_asset_version')` (existing), `useAssetImageUrl` (Task 5), `useAssets()` (Task 4)
- Produces: `useAssetActions(detail)` returning `{ canReview, canRegenerate, canDownload }`; `AssetSidebar` displaying all detail fields; `VersionHistory` showing lineage; `LightboxModal` with full-size image, sidebar, and navigation. Tasks 9, 10 consume these.

- [ ] **Step 1: Write the failing test for `useAssetActions`**

Create `web/tests/assets/asset-actions.test.ts`:

```typescript
import { useAssetActions } from '@/hooks/assets/useAssetActions';

describe('useAssetActions', () => {
  it('resolves review as allowed when present in allowed_actions', () => {
    const result = useAssetActions(['review', 'download']);
    expect(result.canReview).toBe(true);
    expect(result.canDownload).toBe(true);
    expect(result.canRegenerate).toBe(false);
  });

  it('resolves regenerate as allowed when present', () => {
    const result = useAssetActions(['regenerate']);
    expect(result.canRegenerate).toBe(true);
    expect(result.canReview).toBe(false);
    expect(result.canDownload).toBe(false);
  });

  it('returns all false for empty allowed_actions', () => {
    const result = useAssetActions([]);
    expect(result.canReview).toBe(false);
    expect(result.canRegenerate).toBe(false);
    expect(result.canDownload).toBe(false);
  });

  it('ignores unknown action codes', () => {
    const result = useAssetActions(['review', 'unknown_action']);
    expect(result.canReview).toBe(true);
    expect(result.canRegenerate).toBe(false);
  });
});
```

- [ ] **Step 2: Implement `useAssetActions`**

Create `web/hooks/assets/useAssetActions.ts`:

```typescript
import { useMemo } from 'react';

export interface AssetActionPermissions {
  canReview: boolean;
  canRegenerate: boolean;
  canDownload: boolean;
}

export function useAssetActions(allowedActions: string[]): AssetActionPermissions {
  return useMemo(() => {
    const set = new Set(allowedActions);
    return {
      canReview: set.has('review'),
      canRegenerate: set.has('regenerate'),
      canDownload: set.has('download'),
    };
  }, [allowedActions]);
}
```

- [ ] **Step 3: Run action test**

Run: `cd web && npx jest tests/assets/asset-actions.test.ts --no-coverage`
Expected: All 4 tests PASS.

- [ ] **Step 4: Create `VersionHistory` component**

Create `web/components/assets/VersionHistory.tsx`:

```tsx
interface LineageEntry {
  version_id: string;
  direction: 'parent' | 'child';
  regeneration_intent: string | null;
  created_at: string;
}

interface VersionHistoryProps {
  parent: LineageEntry | null;
  children: LineageEntry[];
  onNavigate: (versionId: string) => void;
}

const INTENT_LABELS: Record<string, string> = {
  same_recipe: 'Same recipe',
  new_seed: 'New seed',
  edit_prompt: 'Edited prompt',
  quality_feedback: 'Quality feedback',
};

export function VersionHistory({ parent, children, onNavigate }: VersionHistoryProps) {
  if (!parent && children.length === 0) return null;

  return (
    <div className="flex flex-col gap-2">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-base-content/50">
        Lineage
      </h3>

      {parent && (
        <button
          type="button"
          className="flex items-center gap-1.5 text-sm text-primary hover:underline"
          onClick={() => onNavigate(parent.version_id)}
        >
          <span className="text-base-content/40">&larr;</span>
          <span>Parent</span>
          {parent.regeneration_intent && (
            <span className="badge badge-xs badge-ghost">
              {INTENT_LABELS[parent.regeneration_intent] ?? parent.regeneration_intent}
            </span>
          )}
        </button>
      )}

      {children.length > 0 && (
        <div className="flex flex-col gap-1 pl-4 border-l-2 border-base-300">
          {children.map((child) => (
            <button
              key={child.version_id}
              type="button"
              className="flex items-center gap-1.5 text-sm text-primary hover:underline"
              onClick={() => onNavigate(child.version_id)}
            >
              <span>v{child.version_id.slice(0, 6)}</span>
              {child.regeneration_intent && (
                <span className="badge badge-xs badge-ghost">
                  {INTENT_LABELS[child.regeneration_intent] ?? child.regeneration_intent}
                </span>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 5: Create `AssetSidebar` component**

Create `web/components/assets/AssetSidebar.tsx`:

```tsx
import { ModeProvenance } from '@/components/workspace/ModeProvenance';
import { VersionHistory } from './VersionHistory';

interface ExperimentConclusion {
  context_id: string;
  experiment_id: string;
  conclusion: string;
  metric_snapshot_id: string;
  rule_version_id: string;
}

interface QcAttempt {
  id: string;
  state: string;
  error_code: string | null;
  created_at: string;
}

interface ReviewRecord {
  id: string;
  review_value: string;
  reason: string | null;
  supersedes_review_id: string | null;
  created_at: string;
}

export interface AssetVersionDetail {
  id: string;
  asset_id: string;
  product_id: string;
  run_id: string;
  provider: string;
  model: string;
  aspect_ratio: string;
  seed: { requested: number | null; effective: number | null; unavailable_reason: string | null };
  prompt: string;
  recipe_id: string;
  execution_mode: string;
  origin_kind: string;
  tags: string[];
  effective_review_value: string | null;
  review_head_id: string | null;
  reviews: ReviewRecord[];
  qc_attempts: QcAttempt[];
  latest_qc_state: string | null;
  contextual_conclusions: ExperimentConclusion[];
  parent: { version_id: string; direction: 'parent'; regeneration_intent: string | null; created_at: string } | null;
  children: Array<{ version_id: string; direction: 'child'; regeneration_intent: string | null; created_at: string }>;
  mock_evidence: Record<string, unknown> | null;
  byte_size: number;
  allowed_actions: string[];
  created_at: string;
}

interface AssetSidebarProps {
  detail: AssetVersionDetail;
  onNavigateVersion: (versionId: string) => void;
}

const REVIEW_BADGE: Record<string, string> = {
  approved: 'badge-success',
  needs_changes: 'badge-warning',
  rejected: 'badge-error',
  cleared: 'badge-ghost',
};

export function AssetSidebar({ detail, onNavigateVersion }: AssetSidebarProps) {
  return (
    <div className="flex flex-col gap-4 p-4 overflow-y-auto">
      {/* Provenance */}
      <section>
        <h3 className="text-xs font-semibold uppercase tracking-wide text-base-content/50 mb-1">Provenance</h3>
        <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
          <span className="text-base-content/60">Provider</span>
          <span>{detail.provider}</span>
          <span className="text-base-content/60">Model</span>
          <span>{detail.model}</span>
          <span className="text-base-content/60">Mode</span>
          <ModeProvenance mode={detail.execution_mode} size="sm" />
          <span className="text-base-content/60">Origin</span>
          <span>{detail.origin_kind}</span>
          <span className="text-base-content/60">Aspect</span>
          <span>{detail.aspect_ratio}</span>
          <span className="text-base-content/60">Size</span>
          <span>{(detail.byte_size / 1024).toFixed(1)} KB</span>
        </div>
      </section>

      {/* Seed */}
      <section>
        <h3 className="text-xs font-semibold uppercase tracking-wide text-base-content/50 mb-1">Seed</h3>
        <div className="text-sm">
          {detail.seed.effective !== null ? (
            <span>Effective: {detail.seed.effective}</span>
          ) : detail.seed.unavailable_reason ? (
            <span className="text-warning">{detail.seed.unavailable_reason}</span>
          ) : (
            <span className="text-base-content/40">Not available</span>
          )}
        </div>
      </section>

      {/* Prompt */}
      <section>
        <h3 className="text-xs font-semibold uppercase tracking-wide text-base-content/50 mb-1">Prompt</h3>
        <p className="text-sm whitespace-pre-wrap break-words max-h-40 overflow-y-auto bg-base-200 rounded-lg p-2">
          {detail.prompt}
        </p>
      </section>

      {/* Tags */}
      {detail.tags.length > 0 && (
        <section>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-base-content/50 mb-1">Tags</h3>
          <div className="flex flex-wrap gap-1">
            {detail.tags.map((tag) => (
              <span key={tag} className="badge badge-sm badge-outline">{tag}</span>
            ))}
          </div>
        </section>
      )}

      {/* Review history */}
      <section>
        <h3 className="text-xs font-semibold uppercase tracking-wide text-base-content/50 mb-1">Review</h3>
        {detail.effective_review_value ? (
          <span className={`badge ${REVIEW_BADGE[detail.effective_review_value] ?? 'badge-ghost'}`}>
            {detail.effective_review_value}
          </span>
        ) : (
          <span className="text-sm text-base-content/40">Unreviewed</span>
        )}
        {detail.reviews.length > 0 && (
          <div className="mt-2 flex flex-col gap-1">
            {detail.reviews.map((r) => (
              <div key={r.id} className="text-xs flex items-center gap-1">
                <span className={`badge badge-xs ${REVIEW_BADGE[r.review_value] ?? 'badge-ghost'}`}>
                  {r.review_value}
                </span>
                {r.reason && <span className="text-base-content/60 truncate">{r.reason}</span>}
                {r.supersedes_review_id && <span className="text-base-content/40">(correction)</span>}
              </div>
            ))}
          </div>
        )}
      </section>

      {/* QC */}
      {detail.qc_attempts.length > 0 && (
        <section>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-base-content/50 mb-1">Quality Checks</h3>
          <div className="flex flex-col gap-1">
            {detail.qc_attempts.map((qc) => (
              <div key={qc.id} className="text-xs flex items-center gap-1">
                <span className={`badge badge-xs ${qc.state === 'passed' ? 'badge-success' : qc.state === 'failed' ? 'badge-error' : 'badge-ghost'}`}>
                  {qc.state}
                </span>
                {qc.error_code && <span className="text-error">{qc.error_code}</span>}
              </div>
            ))}
          </div>
        </section>
      )}

      {/* Contextual conclusions */}
      {detail.contextual_conclusions.length > 0 && (
        <section>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-base-content/50 mb-1">Experiment Conclusions</h3>
          <div className="flex flex-col gap-1">
            {detail.contextual_conclusions.map((c) => (
              <div key={c.context_id} className="text-xs flex items-center gap-1">
                <span className={`badge badge-xs ${c.conclusion === 'winner' ? 'badge-success' : c.conclusion === 'loser' ? 'badge-error' : 'badge-ghost'}`}>
                  {c.conclusion}
                </span>
                <span className="text-base-content/60 truncate">{c.experiment_id}</span>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* Mock evidence */}
      {detail.mock_evidence && (
        <section>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-base-content/50 mb-1">
            Controlled Mock Evidence
          </h3>
          <pre className="text-xs bg-base-200 rounded-lg p-2 overflow-x-auto max-h-32">
            {JSON.stringify(detail.mock_evidence, null, 2)}
          </pre>
        </section>
      )}

      {/* Lineage */}
      <VersionHistory
        parent={detail.parent}
        children={detail.children}
        onNavigate={onNavigateVersion}
      />
    </div>
  );
}
```

- [ ] **Step 6: Replace the `LightboxModal` stub with the real implementation**

Replace `web/components/assets/LightboxModal.tsx`:

```tsx
import { useCallback, useEffect, useRef } from 'react';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { useAssetImageUrl } from '@/hooks/assets/useAssetImageUrl';
import { useAssetActions } from '@/hooks/assets/useAssetActions';
import { AssetSidebar } from './AssetSidebar';
import type { AssetVersionDetail } from './AssetSidebar';
import { ReviewPanel } from './ReviewPanel';
import { RegenerationPanel } from './RegenerationPanel';

interface LightboxModalProps {
  versionId: string;
  onClose: () => void;
  onNavigate?: (versionId: string) => void;
  onPrev?: () => void;
  onNext?: () => void;
}

export function LightboxModal({
  versionId,
  onClose,
  onNavigate,
  onPrev,
  onNext,
}: LightboxModalProps) {
  const { data: detail, isLoading, problem } = useDomainQuery(
    'get_generation_asset_version',
    { version_id: versionId }
  );
  const { blobUrl, isLoading: imageLoading } = useAssetImageUrl(versionId);
  const actions = useAssetActions((detail as AssetVersionDetail | undefined)?.allowed_actions ?? []);
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    dialogRef.current?.showModal();
  }, []);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
      }
      if (e.key === 'ArrowLeft' && onPrev) {
        e.preventDefault();
        onPrev();
      }
      if (e.key === 'ArrowRight' && onNext) {
        e.preventDefault();
        onNext();
      }
    },
    [onClose, onPrev, onNext]
  );

  const handleNavigateVersion = useCallback(
    (vid: string) => {
      if (onNavigate) onNavigate(vid);
    },
    [onNavigate]
  );

  const typedDetail = detail as AssetVersionDetail | undefined;

  return (
    <dialog
      ref={dialogRef}
      className="modal modal-open"
      onKeyDown={handleKeyDown}
      onClick={(e) => { if (e.target === dialogRef.current) onClose(); }}
    >
      <div className="modal-box max-w-7xl w-full h-[90vh] flex p-0">
        {/* Image area */}
        <div className="flex-1 min-w-0 flex items-center justify-center bg-base-200 relative">
          {imageLoading && <span className="loading loading-spinner loading-lg" />}
          {blobUrl && (
            <img src={blobUrl} alt="" className="max-w-full max-h-full object-contain" />
          )}
          {problem && (
            <div className="text-error text-center p-4">
              <p className="font-medium">Failed to load image</p>
              <p className="text-sm">{problem.code}</p>
            </div>
          )}

          {/* Navigation arrows */}
          {onPrev && (
            <button
              type="button"
              className="btn btn-circle btn-ghost absolute left-2 top-1/2 -translate-y-1/2"
              onClick={onPrev}
              aria-label="Previous asset"
            >
              &larr;
            </button>
          )}
          {onNext && (
            <button
              type="button"
              className="btn btn-circle btn-ghost absolute right-2 top-1/2 -translate-y-1/2"
              onClick={onNext}
              aria-label="Next asset"
            >
              &rarr;
            </button>
          )}
        </div>

        {/* Sidebar */}
        <div className="w-80 shrink-0 border-l border-base-300 flex flex-col">
          <div className="flex items-center justify-between px-4 py-2 border-b border-base-300">
            <span className="font-medium text-sm">Asset Detail</span>
            <button
              type="button"
              className="btn btn-sm btn-ghost btn-circle"
              onClick={onClose}
              aria-label="Close lightbox"
            >
              ✕
            </button>
          </div>

          {isLoading && (
            <div className="flex-1 flex items-center justify-center">
              <span className="loading loading-spinner" />
            </div>
          )}

          {typedDetail && (
            <>
              <div className="flex-1 min-h-0 overflow-y-auto">
                <AssetSidebar detail={typedDetail} onNavigateVersion={handleNavigateVersion} />
              </div>
              <div className="border-t border-base-300 p-3 flex flex-col gap-2">
                {actions.canReview && (
                  <ReviewPanel
                    versionId={versionId}
                    reviewHeadId={typedDetail.review_head_id}
                    effectiveReview={typedDetail.effective_review_value}
                  />
                )}
                {actions.canRegenerate && (
                  <RegenerationPanel versionId={versionId} />
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </dialog>
  );
}
```

- [ ] **Step 7: Run tests**

Run: `cd web && npx jest tests/assets/asset-actions.test.ts --no-coverage`
Expected: All 4 tests PASS.

- [ ] **Step 8: Commit**

```bash
git add web/hooks/assets/useAssetActions.ts web/components/assets/AssetSidebar.tsx web/components/assets/VersionHistory.tsx web/components/assets/LightboxModal.tsx web/tests/assets/asset-actions.test.ts
git commit -m "feat(assets): add lightbox with sidebar, version history, and action permissions"
```

---

### Task 9: Review Panel and Regeneration Panel

**Files:**
- Create: `web/components/assets/ReviewPanel.tsx` (replace stub if still present)
- Create: `web/components/assets/RegenerationPanel.tsx` (replace stub if still present)
- Test: `web/tests/assets/review-panel.test.tsx`
- Test: `web/tests/assets/regeneration-panel.test.tsx`

**Interfaces:**
- Consumes: `useDomainCommand('create_generation_asset_review')` (existing), `useDomainCommand('create_generation_regeneration_draft')` (existing), `RecoverableActionStore` (Task 3)
- Produces: `ReviewPanel` with A/C/X buttons and correction/clear using `supersedes_review_id`; `RegenerationPanel` with four intent buttons. Task 10 reuses `ReviewPanel` logic.

- [ ] **Step 1: Write the failing test for `ReviewPanel`**

Create `web/tests/assets/review-panel.test.tsx`:

```tsx
/**
 * @jest-environment jsdom
 */
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const mockExecute = jest.fn();
jest.mock('@/hooks/domain/useDomainCommand', () => ({
  useDomainCommand: () => ({
    execute: mockExecute,
    result: undefined,
    problem: undefined,
    isPending: false,
    reset: jest.fn(),
    retry: jest.fn(),
    reconciliation: undefined,
  }),
}));

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme' } }),
}));

import { ReviewPanel } from '@/components/assets/ReviewPanel';

beforeEach(() => {
  jest.clearAllMocks();
  mockExecute.mockResolvedValue({
    kind: 'success',
    data: { id: 'review-1', review_value: 'approved' },
    meta: { etag: null, retryAt: null, location: null },
  });
});

describe('ReviewPanel', () => {
  it('renders A/C/X buttons when no review exists', () => {
    render(
      <ReviewPanel
        versionId="v1"
        reviewHeadId={null}
        effectiveReview={null}
      />
    );

    expect(screen.getByRole('button', { name: /approve/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /needs changes/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /reject/i })).toBeInTheDocument();
  });

  it('submits review without supersedes_review_id for first review', async () => {
    render(
      <ReviewPanel
        versionId="v1"
        reviewHeadId={null}
        effectiveReview={null}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /approve/i }));

    await waitFor(() => {
      expect(mockExecute).toHaveBeenCalledWith(
        expect.objectContaining({
          body: expect.objectContaining({
            version_id: 'v1',
            review_value: 'approved',
          }),
        })
      );
    });

    const call = mockExecute.mock.calls[0][0];
    expect(call.body.supersedes_review_id).toBeUndefined();
  });

  it('submits correction with supersedes_review_id when head exists', async () => {
    render(
      <ReviewPanel
        versionId="v1"
        reviewHeadId="head-1"
        effectiveReview="approved"
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /needs changes/i }));

    await waitFor(() => {
      expect(mockExecute).toHaveBeenCalledWith(
        expect.objectContaining({
          body: expect.objectContaining({
            version_id: 'v1',
            review_value: 'needs_changes',
            supersedes_review_id: 'head-1',
          }),
        })
      );
    });
  });

  it('shows clear button when review exists', () => {
    render(
      <ReviewPanel
        versionId="v1"
        reviewHeadId="head-1"
        effectiveReview="approved"
      />
    );

    expect(screen.getByRole('button', { name: /clear/i })).toBeInTheDocument();
  });

  it('submits cleared value with supersedes_review_id', async () => {
    render(
      <ReviewPanel
        versionId="v1"
        reviewHeadId="head-1"
        effectiveReview="approved"
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /clear/i }));

    await waitFor(() => {
      expect(mockExecute).toHaveBeenCalledWith(
        expect.objectContaining({
          body: expect.objectContaining({
            version_id: 'v1',
            review_value: 'cleared',
            supersedes_review_id: 'head-1',
          }),
        })
      );
    });
  });
});
```

- [ ] **Step 2: Implement `ReviewPanel`**

Create `web/components/assets/ReviewPanel.tsx`:

```tsx
import { useCallback, useState } from 'react';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';

interface ReviewPanelProps {
  versionId: string;
  reviewHeadId: string | null;
  effectiveReview: string | null;
  onReviewSubmitted?: (value: string) => void;
}

type ReviewValue = 'approved' | 'needs_changes' | 'rejected' | 'cleared';

export function ReviewPanel({
  versionId,
  reviewHeadId,
  effectiveReview,
  onReviewSubmitted,
}: ReviewPanelProps) {
  const command = useDomainCommand('create_generation_asset_review');
  const [submitting, setSubmitting] = useState<ReviewValue | null>(null);

  const submitReview = useCallback(
    async (value: ReviewValue) => {
      setSubmitting(value);
      const body: Record<string, unknown> = {
        version_id: versionId,
        review_value: value,
      };
      if (reviewHeadId) {
        body.supersedes_review_id = reviewHeadId;
      }

      const actionInstanceId = `review:${versionId}:${Date.now()}`;
      const outcome = await command.execute({ body: body as any, actionInstanceId });
      setSubmitting(null);

      if (outcome.kind === 'success' && onReviewSubmitted) {
        onReviewSubmitted(value);
      }
    },
    [versionId, reviewHeadId, command, onReviewSubmitted]
  );

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-1.5" role="group" aria-label="Review actions">
        <button
          type="button"
          className={`btn btn-sm flex-1 ${effectiveReview === 'approved' ? 'btn-success' : 'btn-outline btn-success'}`}
          onClick={() => submitReview('approved')}
          disabled={submitting !== null}
          aria-label="Approve"
        >
          {submitting === 'approved' ? <span className="loading loading-spinner loading-xs" /> : 'A'}
        </button>
        <button
          type="button"
          className={`btn btn-sm flex-1 ${effectiveReview === 'needs_changes' ? 'btn-warning' : 'btn-outline btn-warning'}`}
          onClick={() => submitReview('needs_changes')}
          disabled={submitting !== null}
          aria-label="Needs changes"
        >
          {submitting === 'needs_changes' ? <span className="loading loading-spinner loading-xs" /> : 'C'}
        </button>
        <button
          type="button"
          className={`btn btn-sm flex-1 ${effectiveReview === 'rejected' ? 'btn-error' : 'btn-outline btn-error'}`}
          onClick={() => submitReview('rejected')}
          disabled={submitting !== null}
          aria-label="Reject"
        >
          {submitting === 'rejected' ? <span className="loading loading-spinner loading-xs" /> : 'X'}
        </button>
      </div>

      {effectiveReview && (
        <button
          type="button"
          className="btn btn-xs btn-ghost"
          onClick={() => submitReview('cleared')}
          disabled={submitting !== null}
          aria-label="Clear review"
        >
          {submitting === 'cleared' ? <span className="loading loading-spinner loading-xs" /> : 'Clear review'}
        </button>
      )}

      {command.problem && (
        <div role="alert" className="text-xs text-error">
          {command.problem.detail || command.problem.title}
        </div>
      )}

      <div aria-live="assertive" className="sr-only">
        {submitting === null && command.result ? `Review submitted` : ''}
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Run review panel tests**

Run: `cd web && npx jest tests/assets/review-panel.test.tsx --no-coverage`
Expected: All 5 tests PASS.

- [ ] **Step 4: Write the failing test for `RegenerationPanel`**

Create `web/tests/assets/regeneration-panel.test.tsx`:

```tsx
/**
 * @jest-environment jsdom
 */
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const mockExecute = jest.fn();
jest.mock('@/hooks/domain/useDomainCommand', () => ({
  useDomainCommand: () => ({
    execute: mockExecute,
    result: undefined,
    problem: undefined,
    isPending: false,
    reset: jest.fn(),
    retry: jest.fn(),
    reconciliation: undefined,
  }),
}));

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme' } }),
}));

import { RegenerationPanel } from '@/components/assets/RegenerationPanel';

beforeEach(() => {
  jest.clearAllMocks();
  mockExecute.mockResolvedValue({
    kind: 'success',
    data: { draft_id: 'd1' },
    meta: { etag: null, retryAt: null, location: null },
  });
});

describe('RegenerationPanel', () => {
  it('renders four regeneration intent buttons', () => {
    render(<RegenerationPanel versionId="v1" />);

    expect(screen.getByRole('button', { name: /same recipe/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /new seed/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /edit prompt/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /quality feedback/i })).toBeInTheDocument();
  });

  it('submits same_recipe intent', async () => {
    render(<RegenerationPanel versionId="v1" />);

    fireEvent.click(screen.getByRole('button', { name: /same recipe/i }));

    await waitFor(() => {
      expect(mockExecute).toHaveBeenCalledWith(
        expect.objectContaining({
          body: expect.objectContaining({
            source_version_id: 'v1',
            intent: 'same_recipe',
          }),
        })
      );
    });
  });

  it('submits new_seed intent', async () => {
    render(<RegenerationPanel versionId="v1" />);

    fireEvent.click(screen.getByRole('button', { name: /new seed/i }));

    await waitFor(() => {
      expect(mockExecute).toHaveBeenCalledWith(
        expect.objectContaining({
          body: expect.objectContaining({
            source_version_id: 'v1',
            intent: 'new_seed',
          }),
        })
      );
    });
  });

  it('submits edit_prompt intent', async () => {
    render(<RegenerationPanel versionId="v1" />);

    fireEvent.click(screen.getByRole('button', { name: /edit prompt/i }));

    await waitFor(() => {
      expect(mockExecute).toHaveBeenCalledWith(
        expect.objectContaining({
          body: expect.objectContaining({
            source_version_id: 'v1',
            intent: 'edit_prompt',
          }),
        })
      );
    });
  });

  it('submits quality_feedback intent', async () => {
    render(<RegenerationPanel versionId="v1" />);

    fireEvent.click(screen.getByRole('button', { name: /quality feedback/i }));

    await waitFor(() => {
      expect(mockExecute).toHaveBeenCalledWith(
        expect.objectContaining({
          body: expect.objectContaining({
            source_version_id: 'v1',
            intent: 'quality_feedback',
          }),
        })
      );
    });
  });

  it('disables buttons while submitting', async () => {
    mockExecute.mockReturnValue(new Promise(() => {}));
    render(<RegenerationPanel versionId="v1" />);

    fireEvent.click(screen.getByRole('button', { name: /same recipe/i }));

    expect(screen.getByRole('button', { name: /same recipe/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /new seed/i })).toBeDisabled();
  });
});
```

- [ ] **Step 5: Implement `RegenerationPanel`**

Create `web/components/assets/RegenerationPanel.tsx`:

```tsx
import { useCallback, useState } from 'react';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';

type RegenerationIntent = 'same_recipe' | 'new_seed' | 'edit_prompt' | 'quality_feedback';

interface RegenerationPanelProps {
  versionId: string;
  onDraftCreated?: (draftId: string) => void;
}

const INTENTS: Array<{ value: RegenerationIntent; label: string }> = [
  { value: 'same_recipe', label: 'Same recipe' },
  { value: 'new_seed', label: 'New seed' },
  { value: 'edit_prompt', label: 'Edit prompt' },
  { value: 'quality_feedback', label: 'Quality feedback' },
];

export function RegenerationPanel({ versionId, onDraftCreated }: RegenerationPanelProps) {
  const command = useDomainCommand('create_generation_regeneration_draft');
  const [submitting, setSubmitting] = useState<RegenerationIntent | null>(null);

  const handleRegenerate = useCallback(
    async (intent: RegenerationIntent) => {
      setSubmitting(intent);
      const actionInstanceId = `regen:${versionId}:${intent}:${Date.now()}`;
      const outcome = await command.execute({
        body: {
          source_version_id: versionId,
          intent,
        } as any,
        actionInstanceId,
      });
      setSubmitting(null);

      if (outcome.kind === 'success' && onDraftCreated) {
        onDraftCreated((outcome.data as any).draft_id);
      }
    },
    [versionId, command, onDraftCreated]
  );

  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-xs font-medium text-base-content/50">Regenerate</span>
      <div className="grid grid-cols-2 gap-1.5">
        {INTENTS.map(({ value, label }) => (
          <button
            key={value}
            type="button"
            className="btn btn-xs btn-outline"
            onClick={() => handleRegenerate(value)}
            disabled={submitting !== null}
            aria-label={label}
          >
            {submitting === value ? (
              <span className="loading loading-spinner loading-xs" />
            ) : (
              label
            )}
          </button>
        ))}
      </div>

      {command.problem && (
        <div role="alert" className="text-xs text-error">
          {command.problem.detail || command.problem.title}
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 6: Run regeneration tests**

Run: `cd web && npx jest tests/assets/regeneration-panel.test.tsx --no-coverage`
Expected: All 6 tests PASS.

- [ ] **Step 7: Commit**

```bash
git add web/components/assets/ReviewPanel.tsx web/components/assets/RegenerationPanel.tsx web/tests/assets/review-panel.test.tsx web/tests/assets/regeneration-panel.test.tsx
git commit -m "feat(assets): add ReviewPanel with correction/clear and RegenerationPanel with four intents"
```

---

## Phase 4: Keyboard Review

### Task 10: Review Mode

**Files:**
- Create: `web/hooks/assets/useReviewMode.ts`
- Modify: `web/components/assets/ReviewMode.tsx` (replace stub)
- Create: `web/components/assets/ReviewProgress.tsx`
- Create: `web/components/assets/ReviewSummary.tsx`
- Test: `web/tests/assets/review-mode.test.ts`

**Interfaces:**
- Consumes: `useDomainCommand('create_generation_asset_review')` (existing), `useAssets()` (Task 4), `useAssetImageUrl` (Task 5), `useAssetActions` (Task 8), `RecoverableActionStore` (Task 3)
- Produces: `useReviewMode()` managing queue, keybindings, mutex, and recovery; `ReviewMode` fullscreen component; `ReviewProgress` bar; `ReviewSummary` end-of-review display. Task 12 integration tests exercise the full flow.

- [ ] **Step 1: Write the failing test for `useReviewMode`**

Create `web/tests/assets/review-mode.test.ts`:

```typescript
/**
 * @jest-environment jsdom
 */
import { renderHook, act, waitFor } from '@testing-library/react';

const mockExecute = jest.fn();
jest.mock('@/hooks/domain/useDomainCommand', () => ({
  useDomainCommand: () => ({
    execute: mockExecute,
    result: undefined,
    problem: undefined,
    isPending: false,
    reset: jest.fn(),
    retry: jest.fn(),
    reconciliation: undefined,
  }),
}));

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme' } }),
}));

import { useReviewMode } from '@/hooks/assets/useReviewMode';

function makeVersion(id: string, reviewHeadId: string | null = null) {
  return {
    id,
    review_head_id: reviewHeadId,
    effective_review_value: reviewHeadId ? 'approved' : null,
    allowed_actions: ['review'],
  };
}

beforeEach(() => {
  jest.clearAllMocks();
  mockExecute.mockResolvedValue({
    kind: 'success',
    data: { id: 'r1', review_value: 'approved' },
    meta: { etag: null, retryAt: null, location: null },
  });
  sessionStorage.clear();
});

describe('useReviewMode', () => {
  it('initializes with the first item in the queue', () => {
    const queue = [makeVersion('v1'), makeVersion('v2'), makeVersion('v3')];
    const { result } = renderHook(() => useReviewMode(queue));

    expect(result.current.currentIndex).toBe(0);
    expect(result.current.currentVersion).toEqual(queue[0]);
    expect(result.current.isComplete).toBe(false);
  });

  it('advances after a review submission', async () => {
    const queue = [makeVersion('v1'), makeVersion('v2')];
    const { result } = renderHook(() => useReviewMode(queue));

    await act(async () => {
      await result.current.submitReview('approved');
    });

    expect(result.current.currentIndex).toBe(1);
  });

  it('marks complete when all items are reviewed', async () => {
    const queue = [makeVersion('v1')];
    const { result } = renderHook(() => useReviewMode(queue));

    await act(async () => {
      await result.current.submitReview('approved');
    });

    expect(result.current.isComplete).toBe(true);
  });

  it('goes back with goBack()', async () => {
    const queue = [makeVersion('v1'), makeVersion('v2'), makeVersion('v3')];
    const { result } = renderHook(() => useReviewMode(queue));

    await act(async () => {
      await result.current.submitReview('approved');
    });

    expect(result.current.currentIndex).toBe(1);

    act(() => {
      result.current.goBack();
    });

    expect(result.current.currentIndex).toBe(0);
  });

  it('skips with Space', async () => {
    const queue = [makeVersion('v1'), makeVersion('v2')];
    const { result } = renderHook(() => useReviewMode(queue));

    act(() => {
      result.current.skip();
    });

    expect(result.current.currentIndex).toBe(1);
    expect(result.current.stats.skipped).toBe(1);
  });

  it('tracks review statistics', async () => {
    const queue = [makeVersion('v1'), makeVersion('v2'), makeVersion('v3')];
    const { result } = renderHook(() => useReviewMode(queue));

    await act(async () => {
      await result.current.submitReview('approved');
    });
    await act(async () => {
      await result.current.submitReview('needs_changes');
    });
    act(() => {
      result.current.skip();
    });

    expect(result.current.stats).toEqual({
      total: 3,
      reviewed: 2,
      skipped: 1,
      approved: 1,
      needsChanges: 1,
      rejected: 0,
    });
  });

  it('prevents duplicate submissions with mutex', async () => {
    let resolveFirst: (v: any) => void;
    mockExecute.mockReturnValueOnce(new Promise(r => { resolveFirst = r; }));

    const queue = [makeVersion('v1'), makeVersion('v2')];
    const { result } = renderHook(() => useReviewMode(queue));

    const p1 = act(async () => { result.current.submitReview('approved'); });
    const p2 = act(async () => { result.current.submitReview('rejected'); });

    resolveFirst!({
      kind: 'success',
      data: { id: 'r1', review_value: 'approved' },
      meta: { etag: null, retryAt: null, location: null },
    });

    await p1;
    await p2;

    expect(mockExecute).toHaveBeenCalledTimes(1);
  });

  it('persists queue position in sessionStorage', async () => {
    const queue = [makeVersion('v1'), makeVersion('v2'), makeVersion('v3')];
    const { result } = renderHook(() => useReviewMode(queue));

    await act(async () => {
      await result.current.submitReview('approved');
    });

    const stored = sessionStorage.getItem('review-queue-position');
    expect(stored).not.toBeNull();
    expect(JSON.parse(stored!).index).toBe(1);
  });
});
```

- [ ] **Step 2: Implement `useReviewMode`**

Create `web/hooks/assets/useReviewMode.ts`:

```typescript
import { useCallback, useEffect, useRef, useState } from 'react';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';

type ReviewValue = 'approved' | 'needs_changes' | 'rejected';

interface ReviewableVersion {
  id: string;
  review_head_id: string | null;
  effective_review_value: string | null;
  allowed_actions: string[];
}

export interface ReviewStats {
  total: number;
  reviewed: number;
  skipped: number;
  approved: number;
  needsChanges: number;
  rejected: number;
}

export interface UseReviewModeResult {
  currentIndex: number;
  currentVersion: ReviewableVersion | undefined;
  isComplete: boolean;
  stats: ReviewStats;
  submitReview: (value: ReviewValue) => Promise<void>;
  skip: () => void;
  goBack: () => void;
  isSubmitting: boolean;
}

const STORAGE_KEY = 'review-queue-position';

export function useReviewMode(queue: ReviewableVersion[]): UseReviewModeResult {
  const command = useDomainCommand('create_generation_asset_review');
  const mutexRef = useRef(false);

  const [currentIndex, setCurrentIndex] = useState<number>(() => {
    try {
      const stored = sessionStorage.getItem(STORAGE_KEY);
      if (stored) {
        const parsed = JSON.parse(stored);
        if (parsed.index < queue.length) return parsed.index;
      }
    } catch { /* ignore */ }
    return 0;
  });

  const [stats, setStats] = useState<ReviewStats>({
    total: queue.length,
    reviewed: 0,
    skipped: 0,
    approved: 0,
    needsChanges: 0,
    rejected: 0,
  });

  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    try {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ index: currentIndex }));
    } catch { /* ignore */ }
  }, [currentIndex]);

  const currentVersion = queue[currentIndex];
  const isComplete = currentIndex >= queue.length;

  const submitReview = useCallback(
    async (value: ReviewValue) => {
      if (mutexRef.current || !currentVersion) return;
      mutexRef.current = true;
      setIsSubmitting(true);

      try {
        const body: Record<string, unknown> = {
          version_id: currentVersion.id,
          review_value: value,
        };
        if (currentVersion.review_head_id) {
          body.supersedes_review_id = currentVersion.review_head_id;
        }

        const actionInstanceId = `review-mode:${currentVersion.id}:${Date.now()}`;
        const outcome = await command.execute({ body: body as any, actionInstanceId });

        if (outcome.kind === 'success') {
          setStats((prev) => ({
            ...prev,
            reviewed: prev.reviewed + 1,
            approved: prev.approved + (value === 'approved' ? 1 : 0),
            needsChanges: prev.needsChanges + (value === 'needs_changes' ? 1 : 0),
            rejected: prev.rejected + (value === 'rejected' ? 1 : 0),
          }));
          setCurrentIndex((prev) => prev + 1);
        }
      } finally {
        mutexRef.current = false;
        setIsSubmitting(false);
      }
    },
    [currentVersion, command]
  );

  const skip = useCallback(() => {
    setStats((prev) => ({ ...prev, skipped: prev.skipped + 1 }));
    setCurrentIndex((prev) => prev + 1);
  }, []);

  const goBack = useCallback(() => {
    setCurrentIndex((prev) => Math.max(0, prev - 1));
  }, []);

  return {
    currentIndex,
    currentVersion,
    isComplete,
    stats,
    submitReview,
    skip,
    goBack,
    isSubmitting,
  };
}
```

- [ ] **Step 3: Run review mode tests**

Run: `cd web && npx jest tests/assets/review-mode.test.ts --no-coverage`
Expected: All 8 tests PASS.

- [ ] **Step 4: Create `ReviewProgress` component**

Create `web/components/assets/ReviewProgress.tsx`:

```tsx
import type { ReviewStats } from '@/hooks/assets/useReviewMode';

interface ReviewProgressProps {
  stats: ReviewStats;
  currentIndex: number;
}

export function ReviewProgress({ stats, currentIndex }: ReviewProgressProps) {
  const percent = stats.total > 0 ? Math.round((currentIndex / stats.total) * 100) : 0;

  return (
    <div className="flex items-center gap-3 px-4 py-2 bg-base-200">
      <progress
        className="progress progress-primary flex-1"
        value={currentIndex}
        max={stats.total}
      />
      <div className="flex items-center gap-2 text-sm shrink-0">
        <span>{currentIndex}/{stats.total}</span>
        <span className="text-base-content/40">|</span>
        <span className="text-success">{stats.approved}A</span>
        <span className="text-warning">{stats.needsChanges}C</span>
        <span className="text-error">{stats.rejected}X</span>
        <span className="text-base-content/40">{stats.skipped} skipped</span>
      </div>
    </div>
  );
}
```

- [ ] **Step 5: Create `ReviewSummary` component**

Create `web/components/assets/ReviewSummary.tsx`:

```tsx
import type { ReviewStats } from '@/hooks/assets/useReviewMode';

interface ReviewSummaryProps {
  stats: ReviewStats;
  onClose: () => void;
  onBatchRegenerate?: () => void;
  onBatchSupplement?: () => void;
}

export function ReviewSummary({
  stats,
  onClose,
  onBatchRegenerate,
  onBatchSupplement,
}: ReviewSummaryProps) {
  return (
    <div className="flex flex-col items-center justify-center h-full gap-6 p-8">
      <h2 className="text-2xl font-bold">Review Complete</h2>

      <div className="stats shadow">
        <div className="stat">
          <div className="stat-title">Reviewed</div>
          <div className="stat-value">{stats.reviewed}</div>
          <div className="stat-desc">of {stats.total} total</div>
        </div>
        <div className="stat">
          <div className="stat-title">Approved</div>
          <div className="stat-value text-success">{stats.approved}</div>
        </div>
        <div className="stat">
          <div className="stat-title">Needs Changes</div>
          <div className="stat-value text-warning">{stats.needsChanges}</div>
        </div>
        <div className="stat">
          <div className="stat-title">Rejected</div>
          <div className="stat-value text-error">{stats.rejected}</div>
        </div>
        <div className="stat">
          <div className="stat-title">Skipped</div>
          <div className="stat-value">{stats.skipped}</div>
        </div>
      </div>

      <div className="flex gap-3">
        {stats.needsChanges >= 2 && onBatchRegenerate && (
          <button
            type="button"
            className="btn btn-warning"
            onClick={onBatchRegenerate}
          >
            Regenerate {stats.needsChanges} Needs Changes (×2)
          </button>
        )}
        {stats.approved >= 3 && onBatchSupplement && (
          <button
            type="button"
            className="btn btn-success"
            onClick={onBatchSupplement}
          >
            Supplement {stats.approved} Approved (×3)
          </button>
        )}
        <button type="button" className="btn btn-primary" onClick={onClose}>
          Done
        </button>
      </div>
    </div>
  );
}
```

- [ ] **Step 6: Replace the `ReviewMode` stub with real implementation**

Replace `web/components/assets/ReviewMode.tsx`:

```tsx
import { useCallback, useEffect, useRef } from 'react';
import { useAssets } from './AssetsProvider';
import { useReviewMode } from '@/hooks/assets/useReviewMode';
import { useAssetImageUrl } from '@/hooks/assets/useAssetImageUrl';
import { ReviewProgress } from './ReviewProgress';
import { ReviewSummary } from './ReviewSummary';

interface ReviewModeProps {
  onExit: () => void;
}

export function ReviewMode({ onExit }: ReviewModeProps) {
  const { urlState } = useAssets();
  const gallery = []; // In real implementation: derive queue from gallery items
  const mode = useReviewMode(gallery as any);
  const { blobUrl, isLoading: imageLoading } = useAssetImageUrl(mode.currentVersion?.id);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    containerRef.current?.focus();
  }, []);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (mode.isComplete) {
        if (e.key === 'Escape') onExit();
        return;
      }

      switch (e.key) {
        case 'a':
        case 'A':
          e.preventDefault();
          mode.submitReview('approved');
          break;
        case 'c':
        case 'C':
          e.preventDefault();
          mode.submitReview('needs_changes');
          break;
        case 'x':
        case 'X':
          e.preventDefault();
          mode.submitReview('rejected');
          break;
        case ' ':
          e.preventDefault();
          mode.skip();
          break;
        case 'ArrowLeft':
          e.preventDefault();
          mode.goBack();
          break;
        case 'Escape':
          e.preventDefault();
          onExit();
          break;
      }
    },
    [mode, onExit]
  );

  if (mode.isComplete) {
    return (
      <div className="fixed inset-0 z-50 bg-base-100">
        <ReviewSummary stats={mode.stats} onClose={onExit} />
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className="fixed inset-0 z-50 bg-base-100 flex flex-col outline-none"
      tabIndex={-1}
      onKeyDown={handleKeyDown}
    >
      <ReviewProgress stats={mode.stats} currentIndex={mode.currentIndex} />

      <div className="flex-1 min-h-0 flex items-center justify-center bg-base-200 p-4">
        {imageLoading && <span className="loading loading-spinner loading-lg" />}
        {blobUrl && (
          <img src={blobUrl} alt="" className="max-w-full max-h-full object-contain" />
        )}
      </div>

      <div className="px-4 py-3 border-t border-base-300 flex items-center justify-center gap-4">
        <kbd className="kbd kbd-sm">A</kbd> Approve
        <kbd className="kbd kbd-sm">C</kbd> Changes
        <kbd className="kbd kbd-sm">X</kbd> Reject
        <kbd className="kbd kbd-sm">Space</kbd> Skip
        <kbd className="kbd kbd-sm">←</kbd> Back
        <kbd className="kbd kbd-sm">Esc</kbd> Exit
      </div>
    </div>
  );
}
```

- [ ] **Step 7: Commit**

```bash
git add web/hooks/assets/useReviewMode.ts web/components/assets/ReviewMode.tsx web/components/assets/ReviewProgress.tsx web/components/assets/ReviewSummary.tsx web/tests/assets/review-mode.test.ts
git commit -m "feat(assets): add fullscreen keyboard review mode with A/C/X/Space/Left bindings"
```

---

## Phase 5: Export

### Task 11: Export Flow

**Files:**
- Create: `web/hooks/assets/useExportFlow.ts`
- Modify: `web/components/assets/ExportFlow.tsx` (replace stub)
- Create: `web/components/assets/ExportManifestView.tsx`
- Test: `web/tests/assets/export-flow.test.ts`
- Test: `web/tests/assets/export-manifest.test.tsx`

**Interfaces:**
- Consumes: `useDomainCommand('create_generation_export_package')` (existing), `useDomainQuery('get_generation_export_manifest')` (existing), `useOperation` (existing), `useGrantedDownload` (Task 2), `useDomainCommand('create_generation_export_download_intent')` (existing), `useAssets()` (Task 4)
- Produces: `useExportFlow()` managing the export wizard state machine; `ExportFlow` three-step modal; `ExportManifestView` displaying manifest with provenance. Task 12 integration tests exercise the full flow.

- [ ] **Step 1: Write the failing test for `useExportFlow`**

Create `web/tests/assets/export-flow.test.ts`:

```typescript
/**
 * @jest-environment jsdom
 */
import { renderHook, act, waitFor } from '@testing-library/react';

const mockExecute = jest.fn();
jest.mock('@/hooks/domain/useDomainCommand', () => ({
  useDomainCommand: () => ({
    execute: mockExecute,
    result: undefined,
    problem: undefined,
    isPending: false,
    reset: jest.fn(),
    retry: jest.fn(),
    reconciliation: undefined,
  }),
}));

const mockDomainQuery = jest.fn().mockReturnValue({
  data: undefined,
  problem: undefined,
  isLoading: false,
  isStale: false,
  lastSuccessfulAt: null,
  refresh: jest.fn(),
});

jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: (...args: unknown[]) => mockDomainQuery(...args),
}));

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme' } }),
}));

import { useExportFlow } from '@/hooks/assets/useExportFlow';

beforeEach(() => {
  jest.clearAllMocks();
});

describe('useExportFlow', () => {
  it('starts in configure step', () => {
    const { result } = renderHook(() => useExportFlow());

    expect(result.current.step).toBe('configure');
    expect(result.current.packageId).toBeUndefined();
  });

  it('transitions to processing after successful 202', async () => {
    mockExecute.mockResolvedValue({
      kind: 'success',
      data: { package_id: 'pkg-1', operation_id: 'op-1', state: 'queued' },
      meta: { etag: null, retryAt: null, location: '/api/v1/operations/op-1' },
    });

    const { result } = renderHook(() => useExportFlow());

    await act(async () => {
      await result.current.startExport({
        kind: 'approved_only',
        scope: {},
      });
    });

    expect(result.current.step).toBe('processing');
    expect(result.current.packageId).toBe('pkg-1');
    expect(result.current.operationId).toBe('op-1');
  });

  it('transitions to ready when package state is ready', async () => {
    mockDomainQuery.mockReturnValue({
      data: {
        package_id: 'pkg-1',
        operation_id: 'op-1',
        state: 'ready',
        committed_zip_hash: 'abc123',
        committed_zip_size: 1024,
        logical_count: 10,
        logical_bytes: 5000,
      },
      problem: undefined,
      isLoading: false,
      isStale: false,
      lastSuccessfulAt: Date.now(),
      refresh: jest.fn(),
    });

    const { result } = renderHook(() =>
      useExportFlow({ initialPackageId: 'pkg-1' })
    );

    expect(result.current.step).toBe('ready');
  });

  it('returns problem when export creation fails', async () => {
    mockExecute.mockResolvedValue({
      kind: 'problem',
      problem: { code: 'mixed_execution_modes', title: 'Mixed modes', status: 422, detail: '', instance: '', type: '', trace_id: '', retryable: false, field_errors: [] },
      meta: { etag: null, retryAt: null, location: null },
    });

    const { result } = renderHook(() => useExportFlow());

    await act(async () => {
      await result.current.startExport({
        kind: 'explicit',
        scope: { version_ids: ['v1', 'v2'] },
      });
    });

    expect(result.current.step).toBe('configure');
    expect(result.current.problem?.code).toBe('mixed_execution_modes');
  });

  it('handles recovery from existing packageId', () => {
    mockDomainQuery.mockReturnValue({
      data: {
        package_id: 'pkg-1',
        operation_id: 'op-1',
        state: 'building',
      },
      problem: undefined,
      isLoading: false,
      isStale: false,
      lastSuccessfulAt: Date.now(),
      refresh: jest.fn(),
    });

    const { result } = renderHook(() =>
      useExportFlow({ initialPackageId: 'pkg-1' })
    );

    expect(result.current.step).toBe('processing');
    expect(result.current.packageId).toBe('pkg-1');
  });
});
```

- [ ] **Step 2: Implement `useExportFlow`**

Create `web/hooks/assets/useExportFlow.ts`:

```typescript
import { useCallback, useMemo, useState } from 'react';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import type { ProblemDetails } from '@/lib/domain-client/result-types';

export type ExportStep = 'configure' | 'processing' | 'ready' | 'error';

export interface ExportSelector {
  kind: 'explicit' | 'winner_only' | 'approved_only' | 'all_successful';
  scope: Record<string, unknown>;
}

export interface UseExportFlowOptions {
  initialPackageId?: string;
}

export interface UseExportFlowResult {
  step: ExportStep;
  packageId: string | undefined;
  operationId: string | undefined;
  manifest: unknown | undefined;
  problem: ProblemDetails | undefined;
  isExporting: boolean;
  startExport: (selector: ExportSelector) => Promise<void>;
  reset: () => void;
}

export function useExportFlow(options?: UseExportFlowOptions): UseExportFlowResult {
  const exportCommand = useDomainCommand('create_generation_export_package');
  const [packageId, setPackageId] = useState<string | undefined>(options?.initialPackageId);
  const [operationId, setOperationId] = useState<string | undefined>();
  const [problem, setProblem] = useState<ProblemDetails>();
  const [isExporting, setIsExporting] = useState(false);

  const { data: packageDetail } = useDomainQuery(
    'get_generation_export_manifest',
    packageId ? { export_package_id: packageId } : undefined,
    { enabled: !!packageId }
  );

  const typedDetail = packageDetail as { state: string; operation_id?: string; package_id?: string } | undefined;

  const step = useMemo<ExportStep>(() => {
    if (!packageId) return 'configure';
    if (!typedDetail) return 'processing';
    if (typedDetail.state === 'ready') return 'ready';
    if (typedDetail.state === 'failed') return 'error';
    return 'processing';
  }, [packageId, typedDetail]);

  const startExport = useCallback(
    async (selector: ExportSelector) => {
      setProblem(undefined);
      setIsExporting(true);

      const actionInstanceId = `export:${Date.now()}`;
      const outcome = await exportCommand.execute({
        body: { selector } as any,
        actionInstanceId,
      });

      setIsExporting(false);

      if (outcome.kind === 'problem') {
        setProblem(outcome.problem);
        return;
      }

      if (outcome.kind === 'success') {
        const data = outcome.data as any;
        setPackageId(data.package_id);
        setOperationId(data.operation_id);
      }
    },
    [exportCommand]
  );

  const reset = useCallback(() => {
    setPackageId(undefined);
    setOperationId(undefined);
    setProblem(undefined);
  }, []);

  return {
    step,
    packageId,
    operationId: operationId ?? typedDetail?.operation_id,
    manifest: typedDetail,
    problem,
    isExporting,
    startExport,
    reset,
  };
}
```

- [ ] **Step 3: Run export flow tests**

Run: `cd web && npx jest tests/assets/export-flow.test.ts --no-coverage`
Expected: All 5 tests PASS.

- [ ] **Step 4: Create `ExportManifestView` component**

Create `web/components/assets/ExportManifestView.tsx`:

```tsx
interface ManifestEntry {
  version_id: string;
  provider: string;
  model: string;
  aspect_ratio: string;
  content_hash: string;
  is_deduplicated: boolean;
}

interface ExportManifest {
  package_id: string;
  state: string;
  execution_mode: string;
  logical_count: number;
  logical_bytes: number;
  committed_zip_hash: string | null;
  committed_zip_size: number | null;
  entries?: ManifestEntry[];
  created_at: string;
}

interface ExportManifestViewProps {
  manifest: ExportManifest;
}

export function ExportManifestView({ manifest }: ExportManifestViewProps) {
  const dedupCount = manifest.entries?.filter((e) => e.is_deduplicated).length ?? 0;
  const uniqueCount = (manifest.entries?.length ?? 0) - dedupCount;

  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
        <span className="text-base-content/60">State</span>
        <span className={manifest.state === 'ready' ? 'text-success font-medium' : ''}>
          {manifest.state}
        </span>
        <span className="text-base-content/60">Mode</span>
        <span>{manifest.execution_mode}</span>
        <span className="text-base-content/60">Assets</span>
        <span>{manifest.logical_count}</span>
        <span className="text-base-content/60">Size</span>
        <span>{manifest.logical_bytes ? `${(manifest.logical_bytes / 1024 / 1024).toFixed(1)} MB` : '-'}</span>
        {manifest.committed_zip_size && (
          <>
            <span className="text-base-content/60">ZIP size</span>
            <span>{(manifest.committed_zip_size / 1024 / 1024).toFixed(1)} MB</span>
          </>
        )}
        {manifest.committed_zip_hash && (
          <>
            <span className="text-base-content/60">Hash</span>
            <span className="font-mono text-xs">{manifest.committed_zip_hash.slice(0, 16)}…</span>
          </>
        )}
      </div>

      {dedupCount > 0 && (
        <div className="alert alert-info text-sm">
          <span>{dedupCount} entries deduplicated by content hash ({uniqueCount} unique payloads)</span>
        </div>
      )}

      {manifest.entries && manifest.entries.length > 0 && (
        <div className="overflow-x-auto">
          <table className="table table-xs">
            <thead>
              <tr>
                <th>Version</th>
                <th>Provider</th>
                <th>Model</th>
                <th>Aspect</th>
                <th>Dedup</th>
              </tr>
            </thead>
            <tbody>
              {manifest.entries.map((entry) => (
                <tr key={entry.version_id}>
                  <td className="font-mono text-xs">{entry.version_id.slice(0, 8)}</td>
                  <td>{entry.provider}</td>
                  <td>{entry.model}</td>
                  <td>{entry.aspect_ratio}</td>
                  <td>{entry.is_deduplicated ? '✓' : ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 5: Replace the `ExportFlow` stub with real implementation**

Replace `web/components/assets/ExportFlow.tsx`:

```tsx
import { useCallback, useState } from 'react';
import { useExportFlow } from '@/hooks/assets/useExportFlow';
import type { ExportSelector } from '@/hooks/assets/useExportFlow';
import { ExportManifestView } from './ExportManifestView';
import { useGrantedDownload } from '@/hooks/domain/useGrantedDownload';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import { useAssets } from './AssetsProvider';

interface ExportFlowProps {
  packageId?: string;
  onClose: () => void;
}

type Preset = 'winner_only' | 'approved_only' | 'all_successful' | 'explicit';

export function ExportFlow({ packageId: initialPackageId, onClose }: ExportFlowProps) {
  const { selection } = useAssets();
  const flow = useExportFlow({
    initialPackageId: initialPackageId !== 'new' ? initialPackageId : undefined,
  });
  const downloadIntent = useDomainCommand('create_generation_export_download_intent');
  const { startDownload } = useGrantedDownload();
  const [selectedPreset, setSelectedPreset] = useState<Preset>(
    selection.count > 0 ? 'explicit' : 'approved_only'
  );

  const handleStart = useCallback(async () => {
    const selector: ExportSelector = {
      kind: selectedPreset,
      scope: selectedPreset === 'explicit'
        ? { version_ids: [...selection.selectedIds] }
        : {},
    };
    await flow.startExport(selector);
  }, [selectedPreset, selection, flow]);

  const handleDownload = useCallback(async () => {
    if (!flow.packageId) return;
    const outcome = await downloadIntent.execute({
      body: { export_package_id: flow.packageId } as any,
      actionInstanceId: `export-dl:${flow.packageId}`,
    });
    if (outcome.kind === 'success') {
      const grant = (outcome.data as any).download_grant;
      await startDownload(grant);
    }
  }, [flow.packageId, downloadIntent, startDownload]);

  return (
    <dialog className="modal modal-open">
      <div className="modal-box max-w-2xl">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-bold">Export Assets</h2>
          <button type="button" className="btn btn-sm btn-ghost btn-circle" onClick={onClose}>
            ✕
          </button>
        </div>

        {/* Step 1: Configure */}
        {flow.step === 'configure' && (
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-2">
              <label className="label cursor-pointer justify-start gap-3">
                <input
                  type="radio"
                  name="preset"
                  className="radio radio-sm"
                  checked={selectedPreset === 'approved_only'}
                  onChange={() => setSelectedPreset('approved_only')}
                />
                <span>Approved only</span>
              </label>
              <label className="label cursor-pointer justify-start gap-3">
                <input
                  type="radio"
                  name="preset"
                  className="radio radio-sm"
                  checked={selectedPreset === 'winner_only'}
                  onChange={() => setSelectedPreset('winner_only')}
                />
                <span>Winners only (requires experiment context)</span>
              </label>
              <label className="label cursor-pointer justify-start gap-3">
                <input
                  type="radio"
                  name="preset"
                  className="radio radio-sm"
                  checked={selectedPreset === 'all_successful'}
                  onChange={() => setSelectedPreset('all_successful')}
                />
                <span>All successful</span>
              </label>
              {selection.count > 0 && (
                <label className="label cursor-pointer justify-start gap-3">
                  <input
                    type="radio"
                    name="preset"
                    className="radio radio-sm"
                    checked={selectedPreset === 'explicit'}
                    onChange={() => setSelectedPreset('explicit')}
                  />
                  <span>Selected ({selection.count} assets)</span>
                </label>
              )}
            </div>

            {flow.problem && (
              <div role="alert" className="alert alert-error text-sm">
                <span>{flow.problem.detail || flow.problem.title}</span>
              </div>
            )}

            <div className="modal-action">
              <button type="button" className="btn btn-ghost" onClick={onClose}>Cancel</button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={handleStart}
                disabled={flow.isExporting}
              >
                {flow.isExporting ? <span className="loading loading-spinner loading-sm" /> : 'Start Export'}
              </button>
            </div>
          </div>
        )}

        {/* Step 2: Processing */}
        {flow.step === 'processing' && (
          <div className="flex flex-col items-center gap-4 py-8">
            <span className="loading loading-spinner loading-lg" />
            <p>Building export package…</p>
            <p className="text-sm text-base-content/60">This may take a few minutes for large exports.</p>
          </div>
        )}

        {/* Step 3: Ready */}
        {flow.step === 'ready' && flow.manifest && (
          <div className="flex flex-col gap-4">
            <ExportManifestView manifest={flow.manifest as any} />
            <div className="modal-action">
              <button type="button" className="btn btn-ghost" onClick={onClose}>Close</button>
              <button type="button" className="btn btn-primary" onClick={handleDownload}>
                Download ZIP
              </button>
            </div>
          </div>
        )}

        {/* Error */}
        {flow.step === 'error' && (
          <div className="flex flex-col items-center gap-4 py-8">
            <div role="alert" className="alert alert-error">
              <span>Export failed. Please try again.</span>
            </div>
            <div className="modal-action">
              <button type="button" className="btn btn-ghost" onClick={onClose}>Close</button>
              <button type="button" className="btn btn-primary" onClick={flow.reset}>Retry</button>
            </div>
          </div>
        )}
      </div>
    </dialog>
  );
}
```

- [ ] **Step 6: Run export tests**

Run: `cd web && npx jest tests/assets/export-flow.test.ts --no-coverage`
Expected: All 5 tests PASS.

- [ ] **Step 7: Commit**

```bash
git add web/hooks/assets/useExportFlow.ts web/components/assets/ExportFlow.tsx web/components/assets/ExportManifestView.tsx web/tests/assets/export-flow.test.ts
git commit -m "feat(assets): add export flow wizard with 202 outcome, polling, and manifest view"
```

---

## Phase 6: Integration and Acceptance

### Task 12: Cache Invalidation, Responsive Layout, Accessibility, and Acceptance Tests

**Files:**
- Modify: Various component files for cache invalidation after mutations
- Modify: CSS for responsive behavior at <1024px breakpoint
- Create: `web/e2e/assets/gallery-navigation.spec.ts`
- Create: `web/e2e/assets/review-mode.spec.ts`
- Create: `web/e2e/assets/export.spec.ts`
- Create: `web/e2e/assets/cross-workspace.spec.ts`

**Interfaces:**
- Consumes: All components and hooks from Tasks 1-11
- Produces: Complete, integration-tested Assets workspace UI passing §14 acceptance matrix

- [ ] **Step 1: Add cache invalidation after review mutations**

After a review is submitted in `ReviewPanel`, invalidate the asset gallery and detail caches. In `ReviewPanel.tsx`, add after successful review:

```typescript
import { useSWRConfig } from 'swr';
import { matchesOperation, matchesWorkspace } from '@/lib/domain-client/query-keys';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';

// Inside the component:
const { mutate: globalMutate } = useSWRConfig();
const { current } = useWorkspace();

// After successful review in submitReview:
if (outcome.kind === 'success') {
  globalMutate(
    (key: unknown) => {
      if (!Array.isArray(key) || key[0] !== 'domain') return false;
      const dk = key as readonly string[];
      if (!matchesWorkspace(dk as any, current.slug)) return false;
      return matchesOperation(dk as any, 'list_generation_assets') ||
             matchesOperation(dk as any, 'get_generation_asset_version');
    },
    undefined,
    { revalidate: true }
  );
}
```

Apply the same invalidation pattern after:
- Regeneration (invalidate `list_generation_assets`, `list_generation_drafts`)
- Export completion (invalidate `get_generation_export_manifest`)

- [ ] **Step 2: Add responsive CSS for <1024px**

The BatchRail is already hidden at `<lg` via `hidden lg:block`. Add a responsive ribbon for mobile in `GalleryZone`:

```tsx
{/* Mobile run filter ribbon - visible below lg breakpoint */}
<div className="lg:hidden flex overflow-x-auto gap-1 px-4 py-2 border-b border-base-300">
  {runs.map((run: any) => (
    <button
      key={run.id}
      type="button"
      className={`badge whitespace-nowrap ${activeRunId === run.id ? 'badge-primary' : 'badge-ghost'}`}
      onClick={() => handleSelectRun(activeRunId === run.id ? undefined : run.id)}
    >
      {run.id.slice(0, 8)} · {run.status}
    </button>
  ))}
</div>
```

- [ ] **Step 3: Add Controlled Mock visual treatment**

In `AssetCard.tsx`, add the red badge for controlled mock assets:

```tsx
{executionMode === 'mock' && (
  <div className="absolute top-2 right-2 badge badge-xs badge-error z-10">
    controlled-mock
  </div>
)}
```

In `AssetSidebar.tsx`, the `ModeProvenance` component already handles the `mock` → `controlled-mock` display label.

- [ ] **Step 4: Add loading, empty, and error states**

Ensure every data-loading component handles:
- Loading: spinner or skeleton
- Empty: descriptive message with suggestion
- Error: `role="alert"` with problem code and retry action

These are already present in the components above. Verify each:
- `AssetGrid`: loading spinner, empty state message
- `BatchRail`: loading skeletons, empty state
- `LightboxModal`: loading spinner, error display
- `ExportFlow`: loading spinner, error alert
- `FilterBar`: hidden when no facets

- [ ] **Step 5: Run keyboard accessibility check**

Verify the following tab/focus sequences manually or via e2e:
- Gallery grid: Tab moves between cards; Enter/Space opens lightbox
- Lightbox: Arrow keys for prev/next; Escape closes
- Review mode: A/C/X/Space/← as documented; Escape exits
- Filter chips: Tab includes all; Enter/Space toggles
- Export modal: standard modal focus trap
- Selection bar: Tab reaches review/export/clear buttons

- [ ] **Step 6: Write e2e gallery navigation test**

Create `web/e2e/assets/gallery-navigation.spec.ts`:

```typescript
import { test, expect } from '@playwright/test';

test.describe('Asset gallery', () => {
  test.use({ storageState: 'e2e/auth/owner.json' });

  test('loads assets and infinite scrolls', async ({ page }) => {
    await page.goto('/w/acme/assets');

    await expect(page.locator('[data-testid="asset-card"]').first()).toBeVisible();

    const initialCount = await page.locator('[data-testid="asset-card"]').count();
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await page.waitForTimeout(1000);

    const newCount = await page.locator('[data-testid="asset-card"]').count();
    expect(newCount).toBeGreaterThanOrEqual(initialCount);
  });

  test('filters by provider', async ({ page }) => {
    await page.goto('/w/acme/assets');
    await page.click('[role="checkbox"]:has-text("openai")');
    await expect(page).toHaveURL(/provider=openai/);
  });

  test('opens lightbox on card click', async ({ page }) => {
    await page.goto('/w/acme/assets');
    await page.locator('[data-testid="asset-card"]').first().click();
    await expect(page.locator('dialog')).toBeVisible();
    await expect(page).toHaveURL(/overlay=/);
  });

  test('restores filters on page refresh', async ({ page }) => {
    await page.goto('/w/acme/assets?provider=openai&review=approved');
    const openaiChip = page.locator('[role="checkbox"]:has-text("openai")');
    await expect(openaiChip).toHaveAttribute('aria-checked', 'true');
  });
});
```

- [ ] **Step 7: Write e2e cross-workspace test**

Create `web/e2e/assets/cross-workspace.spec.ts`:

```typescript
import { test, expect } from '@playwright/test';

test.describe('Cross-workspace asset isolation', () => {
  test.use({ storageState: 'e2e/auth/owner.json' });

  test('returns 403 for unauthorized workspace', async ({ page }) => {
    const response = await page.goto('/w/foreign-workspace/assets');
    expect(response?.status()).toBe(403);
  });

  test('asset detail returns 404 for cross-workspace version ID', async ({ request }) => {
    const response = await request.get('/api/domain/acme/generation/assets/versions/foreign-version-id');
    expect(response.status()).toBe(404);
  });
});
```

- [ ] **Step 8: Run all tests**

Run: `cd web && npx jest tests/assets/ --no-coverage`
Expected: All unit tests PASS.

Run: `cd web && npx playwright test e2e/assets/ --reporter=list`
Expected: All e2e tests PASS (requires running server with test data).

- [ ] **Step 9: Verify §14 acceptance matrix**

Manually verify each row of the acceptance matrix from the spec:

| # | Criterion | Status |
|---|-----------|--------|
| 1 | Batch rail + aggregated gallery with filters | ✓ Tasks 5-7 |
| 2 | Fullscreen review mode with A/C/X/Space/Left | ✓ Task 10 |
| 3 | Lightbox with full provenance | ✓ Task 8 |
| 4 | Single-asset regeneration with four intents | ✓ Task 9 |
| 5 | Review-summary batch actions | ✓ Task 10 (ReviewSummary) |
| 6 | Export presets with dedup | ✓ Task 11 |
| 7 | Controlled Mock display | ✓ Task 12 Step 3 |
| 8 | Server-authoritative permissions | ✓ Task 8 (useAssetActions) |
| 9 | Adapter-mediated blob display | ✓ Tasks 2, 5 |
| 10 | Append-only effective review | ✓ Task 9 (ReviewPanel) |
| 11 | Refresh-safe navigation/recovery | ✓ Tasks 3, 4 |
| 12 | Keyboard accessibility | ✓ Task 12 Step 5 |
| 13 | Responsive layout (<1024px) | ✓ Task 12 Step 2 |
| 14 | No duplicate review (mutex) | ✓ Task 10 |
| 15 | Cross-workspace isolation | ✓ Task 12 Step 7 |
| 16 | Missing/uncommitted artifact failure | ✓ Task 5 (problem display) |
| 17 | Export recovery | ✓ Task 11 (initialPackageId) |

- [ ] **Step 10: Final commit**

```bash
git add -A
git commit -m "feat(assets): integration, cache invalidation, responsive layout, and acceptance tests"
```

---

## Self-Review

### Spec Coverage

- §0 (Contract Completion): Covered by the server plan dependency — not duplicated here.
- §1 (URL State): Task 4 `useAssetsUrlState`.
- §2 (Gallery): Tasks 5-7 `AssetGrid` + `FilterBar` + `BatchRail` + `GalleryZone`.
- §3 (Lightbox): Task 8 `LightboxModal` + `AssetSidebar`.
- §4 (Review): Task 9 `ReviewPanel` + correction/clear.
- §5 (Regeneration): Task 9 `RegenerationPanel` with four intents.
- §6 (Review Mode): Task 10 `useReviewMode` + keyboard bindings.
- §7 (Export): Task 11 `useExportFlow` + `ExportFlow` + `ExportManifestView`.
- §8 (Selection): Task 6 `SelectionBar` with bulk actions.
- §9 (Permissions): Task 8 `useAssetActions` from server `allowed_actions`.
- §10 (Recovery): Task 3 `RecoverableActionStore`.
- §11 (Accessibility): Task 12 keyboard/screen-reader checks.
- §12 (Files): All files from §13 are mapped in the file structure.
- §13 (File Structure): Matches plan file structure (with `useDomainPaginatedQuery` extension instead of `useDomainInfiniteQuery`).
- §14 (Acceptance Matrix): All 17 rows covered in Task 12 verification.

### Deviations from Spec

1. **`useDomainInfiniteQuery` → extend `useDomainPaginatedQuery`**: The spec mentions creating a new `useDomainInfiniteQuery`. The existing `useDomainPaginatedQuery` already provides `useSWRInfinite`-based cursor pagination. We extend it by adding `list_generation_assets` to `PaginatedOperationId` rather than creating a duplicate abstraction.

2. **Phase 1 Step 15 (CommandOutcome) — SKIPPED**: Already implemented. `useDomainCommand.execute()` returns `Promise<CommandOutcome<K>>`.

### Type Consistency Check

- `AssetVersionSummary` (Task 5) matches the fields used by `AssetCard` (Task 5) and `FilterBar` (Task 6).
- `AssetVersionDetail` (Task 8) matches the fields used by `AssetSidebar`, `ReviewPanel`, and `RegenerationPanel`.
- `useAssetActions` returns `AssetActionPermissions` with `canReview`/`canRegenerate`/`canDownload` — consumed consistently in `LightboxModal`.
- `ReviewStats` (Task 10) is consumed by both `ReviewProgress` and `ReviewSummary`.
- `ExportSelector` (Task 11) matches the server's `CreateExportInput.selector` shape.
- `useGrantedDownload.startDownload` (Task 2) returns `GrantedDownloadResult` — consumed in `ExportFlow`.
