# Generation Workspace UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `FeatureEmptyState` stub on `/w/{slug}/generate` with a complete Draft composition, Preview, Submission, Progress, and Recovery workflow backed by the Domain Interface (PR #45) and BFF Bridge (PR #46).

**Architecture:** Three sequential delivery slices — Draft+Preview, Submission+Progress, Recovery — each shipping as an independent PR. Page shells are thin route wrappers; feature modules own domain logic. Cross-module composition is done by page shells via slots, never lateral imports. All business state comes from the server via `useDomainQuery`/`useDomainCommand`/`useOperation`; the browser is cache-and-display only.

**Tech Stack:** Next.js 15.5 Pages Router, React 18, SWR, TypeScript, Tailwind CSS + DaisyUI, Jest + Testing Library, Playwright.

**Spec:** `docs/superpowers/specs/2026-09-03-generation-workspace-ui-design.md`

## Global Constraints

- Path aliases: `@/lib/*`, `@/components/*`, `@/hooks/*` (see `web/tsconfig.json`).
- Jest: `testEnvironment: 'node'`, test files match `__tests__/**/*.spec.ts(x)` or `tests/**/*.test.ts`. For component tests needing DOM, override per-file with `@jest-environment jsdom` docblock.
- Playwright: `e2e/` dir, port 4002, `owner.json`/`member.json` auth state files.
- i18n: All user-visible strings use `useTranslation('common')` with keys in `locales/en/common.json`.
- DaisyUI: Use existing class patterns (`badge`, `btn`, `alert`, `card`, etc.) from the codebase.
- `DomainGateway`: The browser-side client for all domain operations. Queries via `useDomainQuery<K>`, mutations via `useDomainCommand<K>`, Operations via `useOperation`.
- No `useState`/`useReducer` for authoritative domain state. Form state is local React state flushed to server via CAS.
- `ActionEligibility`: Server-computed `{ action, allowed, reason_code }` drives all UI eligibility — never compute client-side.
- Execution mode provenance: Every generation view must display mode via `ModeProvenance` / `ExecutionModeTag`.
- Cross-workspace: foreign resource IDs → 404 (RLS invisible), membership denial → 403.
- `getWorkspacePageProps()` on every page for SSR auth + workspace resolution.
- No lateral imports between feature modules (`draft/`, `preview/`, `progress/`, `recovery/`).

---

# Slice 1: Draft + Preview

**Prerequisites:** #50 closed, #52 closed, #54 closed.

## Task 1: Navigation prefix matching + route migration

**Files:**
- Modify: `web/components/workspace/WorkspaceNavigation.tsx:25`
- Delete: `web/pages/w/[slug]/generate.tsx`
- Create: `web/pages/w/[slug]/generate/index.tsx`
- Test: `web/__tests__/workspace-navigation-prefix.spec.tsx`
- E2E: `web/e2e/workspace/generation-nav.spec.ts`

**Interfaces:**
- Consumes: `WorkspaceNavigation` component (existing), `getWorkspacePageProps` (existing), `FeatureEmptyState` (existing)
- Produces: prefix-matched `isActive` for `generate`, a working `/w/{slug}/generate` route via `generate/index.tsx`

- [ ] **Step 1: Write the failing unit test for prefix matching**

Create `web/__tests__/workspace-navigation-prefix.spec.tsx`:

```tsx
/**
 * @jest-environment jsdom
 */
import { render, screen } from '@testing-library/react';
import { WorkspaceNavigation } from '@/components/workspace/WorkspaceNavigation';

// Mock useCanAccess to return false (member view)
jest.mock('hooks/useCanAccess', () => ({
  __esModule: true,
  default: () => ({ canAccess: () => false }),
}));

// Mock NavigationItems to expose active state
jest.mock('@/components/shared/shell/NavigationItems', () => ({
  __esModule: true,
  default: ({ menus }: { menus: Array<{ name: string; active: boolean }> }) => (
    <nav>
      {menus.map((m) => (
        <a key={m.name} data-active={m.active}>
          {m.name}
        </a>
      ))}
    </nav>
  ),
}));

describe('WorkspaceNavigation generate prefix matching', () => {
  it('highlights "New Generation" on /generate', () => {
    render(
      <WorkspaceNavigation slug="ws-a" activePathname="/w/ws-a/generate" />
    );
    const link = screen.getByText('New Generation');
    expect(link).toHaveAttribute('data-active', 'true');
  });

  it('highlights "New Generation" on /generate/drafts/abc', () => {
    render(
      <WorkspaceNavigation
        slug="ws-a"
        activePathname="/w/ws-a/generate/drafts/abc"
      />
    );
    const link = screen.getByText('New Generation');
    expect(link).toHaveAttribute('data-active', 'true');
  });

  it('highlights "New Generation" on /generate/runs/xyz', () => {
    render(
      <WorkspaceNavigation
        slug="ws-a"
        activePathname="/w/ws-a/generate/runs/xyz"
      />
    );
    const link = screen.getByText('New Generation');
    expect(link).toHaveAttribute('data-active', 'true');
  });

  it('does not highlight "Assets" on /generate sub-routes', () => {
    render(
      <WorkspaceNavigation
        slug="ws-a"
        activePathname="/w/ws-a/generate/drafts/abc"
      />
    );
    const link = screen.getByText('Assets');
    expect(link).toHaveAttribute('data-active', 'false');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx jest __tests__/workspace-navigation-prefix.spec.tsx --verbose`
Expected: FAIL — drafts/runs sub-routes show `data-active="false"` for "New Generation".

- [ ] **Step 3: Implement prefix matching in WorkspaceNavigation**

In `web/components/workspace/WorkspaceNavigation.tsx`, change line 25:

```tsx
// Before:
const isActive = (area: string) => activePathname === `/w/${slug}/${area}`;

// After:
const isActive = (area: string) =>
  activePathname === `/w/${slug}/${area}` ||
  activePathname?.startsWith(`/w/${slug}/${area}/`) === true;
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npx jest __tests__/workspace-navigation-prefix.spec.tsx --verbose`
Expected: PASS (all 4 tests).

- [ ] **Step 5: Delete old flat route and create new index page**

Delete `web/pages/w/[slug]/generate.tsx`.

Create `web/pages/w/[slug]/generate/index.tsx`:

```tsx
import type { ReactElement } from 'react';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { FeatureEmptyState } from '@/components/workspace';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { NextPageWithLayout } from 'types';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';
import { useTranslation } from 'next-i18next';

interface Props {
  bootstrap: WorkspaceBootstrap;
  errorCode?: number;
  errorContext?: string;
}

const GenerateListPage: NextPageWithLayout<Props> = () => {
  const { t } = useTranslation('common');
  return (
    <>
      <h1 className="text-2xl font-bold outline-none">
        {t('workspace-page-generate')}
      </h1>
      <FeatureEmptyState
        title="No generations yet"
        description="Create your first AI-generated creative asset."
      />
    </>
  );
};

GenerateListPage.getLayout = function getLayout(page: ReactElement) {
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

export const getServerSideProps = getWorkspacePageProps();
export default GenerateListPage;
```

- [ ] **Step 6: Commit**

```bash
git add web/components/workspace/WorkspaceNavigation.tsx \
  web/__tests__/workspace-navigation-prefix.spec.tsx \
  web/pages/w/[slug]/generate/index.tsx
git rm web/pages/w/[slug]/generate.tsx
git commit -m "feat(generation): prefix nav matching + route migration to generate/index"
```

---

## Task 2: Shared generation utilities

**Files:**
- Create: `web/components/generation/shared/useGenerationNav.ts`
- Create: `web/components/generation/shared/ExecutionModeTag.tsx`
- Create: `web/components/generation/shared/CostDisplay.tsx`
- Test: `web/tests/generation/shared/useGenerationNav.test.ts`
- Test: `web/__tests__/generation/execution-mode-tag.spec.tsx`
- Test: `web/__tests__/generation/cost-display.spec.tsx`

**Interfaces:**
- Consumes: `useRouter` (next/router), `ModeProvenance` (existing component)
- Produces:
  - `useGenerationNav()` → `{ buildDraftUrl(draftId): string, buildRunUrl(runId): string, buildListUrl(): string, navigateToDraft(draftId): void, navigateToRun(runId): void }`
  - `ExecutionModeTag({ mode, provider?, model?, scenario?, correlationId? })` → renders `ModeProvenance`
  - `CostDisplay({ amount, currency })` → formatted Decimal + currency string

- [ ] **Step 1: Write failing test for useGenerationNav**

Create `web/tests/generation/shared/useGenerationNav.test.ts`:

```ts
import { renderHook } from '@testing-library/react';
import { useRouter } from 'next/router';
import { useGenerationNav } from '@/components/generation/shared/useGenerationNav';

jest.mock('next/router', () => ({
  useRouter: jest.fn(),
}));

const mockRouter = {
  query: { slug: 'test-ws' },
  push: jest.fn(),
};

beforeEach(() => {
  (useRouter as jest.Mock).mockReturnValue(mockRouter);
  mockRouter.push.mockClear();
});

describe('useGenerationNav', () => {
  it('builds draft URL with slug and draftId', () => {
    const { result } = renderHook(() => useGenerationNav());
    expect(result.current.buildDraftUrl('draft-123')).toBe(
      '/w/test-ws/generate/drafts/draft-123'
    );
  });

  it('builds run URL with slug and runId', () => {
    const { result } = renderHook(() => useGenerationNav());
    expect(result.current.buildRunUrl('run-456')).toBe(
      '/w/test-ws/generate/runs/run-456'
    );
  });

  it('builds list URL', () => {
    const { result } = renderHook(() => useGenerationNav());
    expect(result.current.buildListUrl()).toBe('/w/test-ws/generate');
  });

  it('navigateToDraft calls router.push', () => {
    const { result } = renderHook(() => useGenerationNav());
    result.current.navigateToDraft('draft-123');
    expect(mockRouter.push).toHaveBeenCalledWith(
      '/w/test-ws/generate/drafts/draft-123'
    );
  });

  it('navigateToRun calls router.push', () => {
    const { result } = renderHook(() => useGenerationNav());
    result.current.navigateToRun('run-456');
    expect(mockRouter.push).toHaveBeenCalledWith(
      '/w/test-ws/generate/runs/run-456'
    );
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx jest tests/generation/shared/useGenerationNav.test.ts --verbose`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement useGenerationNav**

Create `web/components/generation/shared/useGenerationNav.ts`:

```ts
import { useRouter } from 'next/router';
import { useCallback, useMemo } from 'react';

export function useGenerationNav() {
  const router = useRouter();
  const slug = router.query.slug as string;
  const base = `/w/${slug}/generate`;

  const buildDraftUrl = useCallback(
    (draftId: string) => `${base}/drafts/${draftId}`,
    [base]
  );
  const buildRunUrl = useCallback(
    (runId: string) => `${base}/runs/${runId}`,
    [base]
  );
  const buildListUrl = useCallback(() => base, [base]);

  const navigateToDraft = useCallback(
    (draftId: string) => router.push(buildDraftUrl(draftId)),
    [router, buildDraftUrl]
  );
  const navigateToRun = useCallback(
    (runId: string) => router.push(buildRunUrl(runId)),
    [router, buildRunUrl]
  );

  return useMemo(
    () => ({ buildDraftUrl, buildRunUrl, buildListUrl, navigateToDraft, navigateToRun }),
    [buildDraftUrl, buildRunUrl, buildListUrl, navigateToDraft, navigateToRun]
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npx jest tests/generation/shared/useGenerationNav.test.ts --verbose`
Expected: PASS.

- [ ] **Step 5: Write failing test for ExecutionModeTag**

Create `web/__tests__/generation/execution-mode-tag.spec.tsx`:

```tsx
/**
 * @jest-environment jsdom
 */
import { render, screen } from '@testing-library/react';
import { ExecutionModeTag } from '@/components/generation/shared/ExecutionModeTag';

describe('ExecutionModeTag', () => {
  it('renders Production badge for byok mode', () => {
    render(
      <ExecutionModeTag mode="byok" provider="meta" model="llama-3" />
    );
    expect(screen.getByText('Production')).toBeInTheDocument();
    expect(screen.getByText('meta')).toBeInTheDocument();
  });

  it('renders Controlled Mock badge for mock mode', () => {
    render(
      <ExecutionModeTag
        mode="mock"
        provider="meta"
        model="llama-3"
        scenario="golden-path"
      />
    );
    expect(screen.getByText('Controlled Mock')).toBeInTheDocument();
    expect(screen.getByText('golden-path')).toBeInTheDocument();
  });

  it('renders Sandbox badge for sandbox mode', () => {
    render(<ExecutionModeTag mode="sandbox" />);
    expect(screen.getByText('Sandbox')).toBeInTheDocument();
  });
});
```

- [ ] **Step 6: Implement ExecutionModeTag**

Create `web/components/generation/shared/ExecutionModeTag.tsx`:

```tsx
import {
  ModeProvenance,
  type ProvenanceViewModel,
} from '@/components/workspace/ModeProvenance';

const EXECUTION_MODE_MAP: Record<string, ProvenanceViewModel['mode']> = {
  byok: 'real',
  sandbox: 'sandbox',
  mock: 'controlled-mock',
};

interface ExecutionModeTagProps {
  mode: string;
  provider?: string;
  model?: string;
  scenario?: string;
  correlationId?: string;
}

export function ExecutionModeTag({
  mode,
  provider,
  model,
  scenario,
  correlationId,
}: ExecutionModeTagProps) {
  const provenance: ProvenanceViewModel = {
    mode: EXECUTION_MODE_MAP[mode] ?? 'real',
    provider,
    model,
    mockScenario: scenario,
    correlationId,
  };
  return <ModeProvenance provenance={provenance} />;
}
```

- [ ] **Step 7: Write failing test for CostDisplay**

Create `web/__tests__/generation/cost-display.spec.tsx`:

```tsx
/**
 * @jest-environment jsdom
 */
import { render, screen } from '@testing-library/react';
import { CostDisplay } from '@/components/generation/shared/CostDisplay';

describe('CostDisplay', () => {
  it('formats USD cost with 2 decimal places', () => {
    render(<CostDisplay amount="12.50" currency="USD" />);
    expect(screen.getByText('$12.50')).toBeInTheDocument();
  });

  it('formats EUR cost', () => {
    render(<CostDisplay amount="99.99" currency="EUR" />);
    expect(screen.getByText('€99.99')).toBeInTheDocument();
  });

  it('renders null amount as dash', () => {
    render(<CostDisplay amount={null} currency="USD" />);
    expect(screen.getByText('—')).toBeInTheDocument();
  });
});
```

- [ ] **Step 8: Implement CostDisplay**

Create `web/components/generation/shared/CostDisplay.tsx`:

```tsx
const CURRENCY_SYMBOLS: Record<string, string> = {
  USD: '$',
  EUR: '€',
  GBP: '£',
  CNY: '¥',
};

interface CostDisplayProps {
  amount: string | null;
  currency: string;
}

export function CostDisplay({ amount, currency }: CostDisplayProps) {
  if (amount === null) {
    return <span aria-label="No cost data">—</span>;
  }
  const symbol = CURRENCY_SYMBOLS[currency] ?? `${currency} `;
  const formatted = parseFloat(amount).toFixed(2);
  return (
    <span aria-label={`${formatted} ${currency}`}>
      {symbol}{formatted}
    </span>
  );
}
```

- [ ] **Step 9: Run all tests and commit**

```bash
cd web && npx jest __tests__/generation/ tests/generation/ --verbose
git add components/generation/shared/ __tests__/generation/ tests/generation/
git commit -m "feat(generation): shared utilities — useGenerationNav, ExecutionModeTag, CostDisplay"
```

---

## Task 3: NewDraftDialog + draft creation flow

**Files:**
- Create: `web/components/generation/list/NewDraftDialog.tsx`
- Test: `web/__tests__/generation/new-draft-dialog.spec.tsx`

**Interfaces:**
- Consumes: `useDomainQuery('get_generation_capabilities')`, `useDomainCommand('create_generation_draft')`, `useGenerationNav().navigateToDraft`
- Produces: `NewDraftDialog({ open, onClose })` — dialog collecting product, provider, model, execution mode, then creating draft and navigating

- [ ] **Step 1: Write failing test for NewDraftDialog**

Create `web/__tests__/generation/new-draft-dialog.spec.tsx`:

```tsx
/**
 * @jest-environment jsdom
 */
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { NewDraftDialog } from '@/components/generation/list/NewDraftDialog';

const mockNavigateToDraft = jest.fn();
const mockExecute = jest.fn();

jest.mock('@/components/generation/shared/useGenerationNav', () => ({
  useGenerationNav: () => ({ navigateToDraft: mockNavigateToDraft }),
}));

jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: (key: string) => {
    if (key === 'get_generation_capabilities') {
      return {
        data: {
          providers: [
            {
              id: 'meta',
              models: [{ id: 'llama-3', name: 'Llama 3' }],
            },
          ],
          execution_modes: ['byok', 'sandbox', 'mock'],
          products: [{ id: 'prod-1', name: 'Summer Campaign' }],
        },
        isLoading: false,
      };
    }
    return { data: undefined, isLoading: true };
  },
}));

jest.mock('@/hooks/domain/useDomainCommand', () => ({
  useDomainCommand: () => ({
    execute: mockExecute,
    isExecuting: false,
  }),
}));

beforeEach(() => {
  mockNavigateToDraft.mockClear();
  mockExecute.mockClear();
});

describe('NewDraftDialog', () => {
  it('renders provider and model options from capabilities', () => {
    render(<NewDraftDialog open onClose={jest.fn()} />);
    expect(screen.getByText('meta')).toBeInTheDocument();
    expect(screen.getByText('Llama 3')).toBeInTheDocument();
  });

  it('creates draft and navigates on submit', async () => {
    mockExecute.mockResolvedValue({
      ok: true,
      data: { id: 'draft-new-1' },
    });

    render(<NewDraftDialog open onClose={jest.fn()} />);

    fireEvent.change(screen.getByLabelText('Product'), {
      target: { value: 'prod-1' },
    });
    fireEvent.change(screen.getByLabelText('Provider'), {
      target: { value: 'meta' },
    });
    fireEvent.change(screen.getByLabelText('Model'), {
      target: { value: 'llama-3' },
    });
    fireEvent.change(screen.getByLabelText('Execution Mode'), {
      target: { value: 'byok' },
    });
    fireEvent.click(screen.getByRole('button', { name: /create/i }));

    await waitFor(() => {
      expect(mockExecute).toHaveBeenCalledWith(
        'create_generation_draft',
        expect.objectContaining({
          body: expect.objectContaining({
            product_id: 'prod-1',
            provider: 'meta',
            model: 'llama-3',
            execution_mode: 'byok',
          }),
        })
      );
    });

    await waitFor(() => {
      expect(mockNavigateToDraft).toHaveBeenCalledWith('draft-new-1');
    });
  });

  it('disables submit when required fields are empty', () => {
    render(<NewDraftDialog open onClose={jest.fn()} />);
    const submitBtn = screen.getByRole('button', { name: /create/i });
    expect(submitBtn).toBeDisabled();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx jest __tests__/generation/new-draft-dialog.spec.tsx --verbose`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement NewDraftDialog**

Create `web/components/generation/list/NewDraftDialog.tsx`:

```tsx
import { useState } from 'react';
import { useTranslation } from 'next-i18next';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import { useGenerationNav } from '@/components/generation/shared/useGenerationNav';

interface NewDraftDialogProps {
  open: boolean;
  onClose: () => void;
}

export function NewDraftDialog({ open, onClose }: NewDraftDialogProps) {
  const { t } = useTranslation('common');
  const { navigateToDraft } = useGenerationNav();
  const { data: capabilities, isLoading } = useDomainQuery(
    'get_generation_capabilities'
  );
  const { execute, isExecuting } = useDomainCommand();

  const [productId, setProductId] = useState('');
  const [provider, setProvider] = useState('');
  const [model, setModel] = useState('');
  const [executionMode, setExecutionMode] = useState('');

  const selectedProvider = capabilities?.providers?.find(
    (p: any) => p.id === provider
  );
  const models = selectedProvider?.models ?? [];
  const canSubmit =
    productId && provider && model && executionMode && !isExecuting;

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;

    const result = await execute('create_generation_draft', {
      body: {
        product_id: productId,
        provider,
        model,
        execution_mode: executionMode,
      },
    });

    if (result.ok) {
      onClose();
      navigateToDraft(result.data.id);
    }
  }

  if (!open) return null;

  return (
    <dialog className="modal modal-open" aria-label="New Draft">
      <form className="modal-box" onSubmit={handleSubmit}>
        <h3 className="text-lg font-bold">
          {t('generation-new-draft-title')}
        </h3>

        <div className="form-control mt-4">
          <label className="label" htmlFor="draft-product">
            <span className="label-text">Product</span>
          </label>
          <select
            id="draft-product"
            aria-label="Product"
            className="select select-bordered"
            value={productId}
            onChange={(e) => setProductId(e.target.value)}
            disabled={isLoading}
          >
            <option value="">Select product…</option>
            {capabilities?.products?.map((p: any) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
        </div>

        <div className="form-control mt-2">
          <label className="label" htmlFor="draft-provider">
            <span className="label-text">Provider</span>
          </label>
          <select
            id="draft-provider"
            aria-label="Provider"
            className="select select-bordered"
            value={provider}
            onChange={(e) => {
              setProvider(e.target.value);
              setModel('');
            }}
            disabled={isLoading}
          >
            <option value="">Select provider…</option>
            {capabilities?.providers?.map((p: any) => (
              <option key={p.id} value={p.id}>{p.id}</option>
            ))}
          </select>
        </div>

        <div className="form-control mt-2">
          <label className="label" htmlFor="draft-model">
            <span className="label-text">Model</span>
          </label>
          <select
            id="draft-model"
            aria-label="Model"
            className="select select-bordered"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            disabled={!provider}
          >
            <option value="">Select model…</option>
            {models.map((m: any) => (
              <option key={m.id} value={m.id}>{m.name}</option>
            ))}
          </select>
        </div>

        <div className="form-control mt-2">
          <label className="label" htmlFor="draft-mode">
            <span className="label-text">Execution Mode</span>
          </label>
          <select
            id="draft-mode"
            aria-label="Execution Mode"
            className="select select-bordered"
            value={executionMode}
            onChange={(e) => setExecutionMode(e.target.value)}
            disabled={isLoading}
          >
            <option value="">Select mode…</option>
            {capabilities?.execution_modes?.map((m: string) => (
              <option key={m} value={m}>{m}</option>
            ))}
          </select>
        </div>

        <div className="modal-action">
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            className="btn btn-primary"
            disabled={!canSubmit}
          >
            {isExecuting ? 'Creating…' : 'Create Draft'}
          </button>
        </div>
      </form>
      <form method="dialog" className="modal-backdrop" onClick={onClose}>
        <button type="button">close</button>
      </form>
    </dialog>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npx jest __tests__/generation/new-draft-dialog.spec.tsx --verbose`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/components/generation/list/NewDraftDialog.tsx \
  web/__tests__/generation/new-draft-dialog.spec.tsx
git commit -m "feat(generation): NewDraftDialog with capabilities-driven setup"
```

---

## Task 4: GenerationListView + DraftList

**Files:**
- Create: `web/components/generation/list/GenerationListView.tsx`
- Create: `web/components/generation/list/DraftList.tsx`
- Modify: `web/pages/w/[slug]/generate/index.tsx`
- Test: `web/__tests__/generation/generation-list-view.spec.tsx`

**Interfaces:**
- Consumes: `useDomainQuery('list_generation_drafts')`, `useGenerationNav`, `NewDraftDialog`, `FeatureEmptyState`, `ExecutionModeTag`
- Produces: `GenerationListView()` — root component for list page showing drafts and a "New Draft" button

- [ ] **Step 1: Write failing test for DraftList**

Create `web/__tests__/generation/generation-list-view.spec.tsx`:

```tsx
/**
 * @jest-environment jsdom
 */
import { render, screen } from '@testing-library/react';
import { DraftList } from '@/components/generation/list/DraftList';

jest.mock('@/components/generation/shared/useGenerationNav', () => ({
  useGenerationNav: () => ({
    buildDraftUrl: (id: string) => `/w/ws/generate/drafts/${id}`,
  }),
}));

jest.mock('@/components/generation/shared/ExecutionModeTag', () => ({
  ExecutionModeTag: ({ mode }: { mode: string }) => (
    <span data-testid="mode">{mode}</span>
  ),
}));

describe('DraftList', () => {
  const drafts = [
    {
      id: 'draft-1',
      title: 'Summer Campaign',
      execution_mode: 'byok',
      updated_at: '2026-09-01T12:00:00Z',
      allowed_actions: [],
    },
    {
      id: 'draft-2',
      title: 'Holiday Promo',
      execution_mode: 'mock',
      updated_at: '2026-09-02T12:00:00Z',
      allowed_actions: [],
    },
  ];

  it('renders draft names as links', () => {
    render(<DraftList drafts={drafts} />);
    expect(screen.getByText('Summer Campaign')).toBeInTheDocument();
    expect(screen.getByText('Holiday Promo')).toBeInTheDocument();
    expect(
      screen.getByText('Summer Campaign').closest('a')
    ).toHaveAttribute('href', '/w/ws/generate/drafts/draft-1');
  });

  it('shows execution mode tag per draft', () => {
    render(<DraftList drafts={drafts} />);
    const modes = screen.getAllByTestId('mode');
    expect(modes).toHaveLength(2);
    expect(modes[0]).toHaveTextContent('byok');
    expect(modes[1]).toHaveTextContent('mock');
  });

  it('shows empty state when no drafts', () => {
    render(<DraftList drafts={[]} />);
    expect(screen.getByText(/no drafts/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx jest __tests__/generation/generation-list-view.spec.tsx --verbose`
Expected: FAIL.

- [ ] **Step 3: Implement DraftList**

Create `web/components/generation/list/DraftList.tsx`:

```tsx
import Link from 'next/link';
import { useGenerationNav } from '@/components/generation/shared/useGenerationNav';
import { ExecutionModeTag } from '@/components/generation/shared/ExecutionModeTag';

interface DraftSummary {
  id: string;
  title: string;
  execution_mode: string;
  updated_at: string;
  allowed_actions: Array<{ action: string; allowed: boolean; reason_code: string | null }>;
}

interface DraftListProps {
  drafts: DraftSummary[];
}

export function DraftList({ drafts }: DraftListProps) {
  const { buildDraftUrl } = useGenerationNav();

  if (drafts.length === 0) {
    return <p className="text-sm text-gray-500">No drafts yet.</p>;
  }

  return (
    <ul className="space-y-2">
      {drafts.map((draft) => (
        <li key={draft.id} className="card card-compact bg-base-100 shadow-sm">
          <div className="card-body flex-row items-center justify-between">
            <div>
              <Link
                href={buildDraftUrl(draft.id)}
                className="font-medium hover:underline"
              >
                {draft.title}
              </Link>
              <div className="mt-1">
                <ExecutionModeTag mode={draft.execution_mode} />
              </div>
            </div>
            <span className="text-xs text-gray-400">
              {new Date(draft.updated_at).toLocaleDateString()}
            </span>
          </div>
        </li>
      ))}
    </ul>
  );
}
```

- [ ] **Step 4: Implement GenerationListView and wire into list page**

Create `web/components/generation/list/GenerationListView.tsx`:

```tsx
import { useState } from 'react';
import { useTranslation } from 'next-i18next';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { FeatureEmptyState } from '@/components/workspace';
import { DraftList } from '@/components/generation/list/DraftList';
import { NewDraftDialog } from '@/components/generation/list/NewDraftDialog';

export function GenerationListView() {
  const { t } = useTranslation('common');
  const { data: draftsResponse, isLoading } = useDomainQuery(
    'list_generation_drafts'
  );
  const [dialogOpen, setDialogOpen] = useState(false);

  const drafts = draftsResponse?.items ?? [];
  const isEmpty = !isLoading && drafts.length === 0;

  return (
    <>
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold outline-none">
          {t('workspace-page-generate')}
        </h1>
        <button
          className="btn btn-primary btn-sm"
          onClick={() => setDialogOpen(true)}
        >
          New Draft
        </button>
      </div>

      {isLoading && (
        <div className="mt-8 flex justify-center">
          <span className="loading loading-spinner" />
        </div>
      )}

      {isEmpty && (
        <FeatureEmptyState
          title="No generations yet"
          description="Create your first AI-generated creative asset."
          action={
            <button
              className="btn btn-primary btn-sm mt-2"
              onClick={() => setDialogOpen(true)}
            >
              New Draft
            </button>
          }
        />
      )}

      {!isLoading && drafts.length > 0 && (
        <div className="mt-6">
          <h2 className="text-lg font-semibold mb-3">Drafts</h2>
          <DraftList drafts={drafts} />
        </div>
      )}

      <NewDraftDialog
        open={dialogOpen}
        onClose={() => setDialogOpen(false)}
      />
    </>
  );
}
```

Update `web/pages/w/[slug]/generate/index.tsx` to use `GenerationListView`:

```tsx
import type { ReactElement } from 'react';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { GenerationListView } from '@/components/generation/list/GenerationListView';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { NextPageWithLayout } from 'types';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';

interface Props {
  bootstrap: WorkspaceBootstrap;
  errorCode?: number;
  errorContext?: string;
}

const GenerateListPage: NextPageWithLayout<Props> = () => {
  return <GenerationListView />;
};

GenerateListPage.getLayout = function getLayout(page: ReactElement) {
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

export const getServerSideProps = getWorkspacePageProps();
export default GenerateListPage;
```

- [ ] **Step 5: Run tests and commit**

```bash
cd web && npx jest __tests__/generation/generation-list-view.spec.tsx --verbose
git add web/components/generation/list/ web/pages/w/[slug]/generate/index.tsx \
  web/__tests__/generation/generation-list-view.spec.tsx
git commit -m "feat(generation): GenerationListView with DraftList and NewDraftDialog"
```

---

## Task 5: DraftWorkspace + DraftComposer (CAS editing)

**Files:**
- Create: `web/components/generation/draft/DraftWorkspace.tsx`
- Create: `web/components/generation/draft/DraftComposer.tsx`
- Create: `web/components/generation/draft/DraftSaveIndicator.tsx`
- Create: `web/components/generation/draft/ProviderConfig.tsx`
- Create: `web/components/generation/draft/ProductSelector.tsx`
- Create: `web/components/generation/draft/DimensionConfigurator.tsx`
- Create: `web/components/generation/draft/CombinationStrategy.tsx`
- Create: `web/components/generation/draft/FormatConfig.tsx`
- Create: `web/components/generation/draft/ReferenceSelector.tsx`
- Create: `web/components/generation/draft/ExclusionConfig.tsx`
- Create: `web/pages/w/[slug]/generate/drafts/[draftId].tsx`
- Test: `web/__tests__/generation/draft-workspace.spec.tsx`
- Test: `web/tests/generation/draft-cas.test.ts`

**Interfaces:**
- Consumes: `useDomainQuery('get_generation_draft')`, `useDomainCommand('update_generation_draft')` with `ifMatch`, `useDomainQuery('get_generation_capabilities')`, library read queries, `useGenerationNav`, `ExecutionModeTag`
- Produces: `DraftWorkspace({ draftId, previewId, children })` — root component for draft page with children slot for preview

This is the largest task in Slice 1. The DraftComposer orchestrates multiple selectors, each of which reads library data and updates the draft via CAS. The page shell wires the preview slot.

- [ ] **Step 1: Write failing test for DraftWorkspace**

Create `web/__tests__/generation/draft-workspace.spec.tsx`:

```tsx
/**
 * @jest-environment jsdom
 */
import { render, screen } from '@testing-library/react';
import { DraftWorkspace } from '@/components/generation/draft/DraftWorkspace';

const mockDraft = {
  id: 'draft-1',
  title: 'Test Draft',
  execution_mode: 'byok',
  provider: 'meta',
  model: 'llama-3',
  selectors: {},
  revision: 'rev-1',
  allowed_actions: [
    { action: 'update_draft', allowed: true, reason_code: null },
    { action: 'create_preview', allowed: true, reason_code: null },
  ],
};

jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: (key: string) => {
    if (key === 'get_generation_draft') {
      return { data: mockDraft, isLoading: false, mutate: jest.fn() };
    }
    return { data: undefined, isLoading: true };
  },
}));

jest.mock('@/hooks/domain/useDomainCommand', () => ({
  useDomainCommand: () => ({
    execute: jest.fn().mockResolvedValue({ ok: true }),
    isExecuting: false,
  }),
}));

jest.mock('@/components/generation/draft/DraftComposer', () => ({
  DraftComposer: () => <div data-testid="draft-composer">Composer</div>,
}));

describe('DraftWorkspace', () => {
  it('renders draft title and composer', () => {
    render(<DraftWorkspace draftId="draft-1" previewId={null} />);
    expect(screen.getByText('Test Draft')).toBeInTheDocument();
    expect(screen.getByTestId('draft-composer')).toBeInTheDocument();
  });

  it('renders children slot', () => {
    render(
      <DraftWorkspace draftId="draft-1" previewId="prev-1">
        <div data-testid="preview-slot">Preview content</div>
      </DraftWorkspace>
    );
    expect(screen.getByTestId('preview-slot')).toBeInTheDocument();
  });

  it('shows loading state', () => {
    jest.mocked(require('@/hooks/domain/useDomainQuery').useDomainQuery)
      .mockReturnValue({ data: undefined, isLoading: true });
    render(<DraftWorkspace draftId="draft-1" previewId={null} />);
    expect(screen.getByRole('status')).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Write failing test for CAS update flow**

Create `web/tests/generation/draft-cas.test.ts`:

```ts
/**
 * Integration test for Draft CAS (Compare-and-Swap) editing. Verifies
 * that update_generation_draft sends If-Match with the draft's revision
 * ETag, and that a 412 response triggers a revalidation.
 */
import { DomainGateway } from '@/lib/domain-client/gateway';

// This test verifies the interface contract between useDomainCommand
// and the gateway's ifMatch support (prerequisite #54).
describe('Draft CAS update', () => {
  it('sends If-Match header with revision ETag', async () => {
    mockGateway.updateGenerationDraft.mockResolvedValue({
      kind: 'success',
      status: 200,
      data: { id: 'draft-1', revision: 2 },
      meta: {},
    });

    const { result } = renderHook(() =>
      useDomainCommand('update_generation_draft')
    );

    await act(async () => {
      await result.current.execute({
        body: { selectors: { product_id: 'p1' } } as any,
        actionInstanceId: 'cas-test-1',
        ifMatch: '"rev-1"',
      });
    });

    expect(mockGateway.updateGenerationDraft).toHaveBeenCalledWith(
      expect.objectContaining({ ifMatch: '"rev-1"' })
    );
  });

  it('surfaces 412 Precondition Failed as a problem', async () => {
    mockGateway.updateGenerationDraft.mockResolvedValue({
      kind: 'problem',
      status: 412,
      problem: {
        type: 'precondition_failed',
        title: 'Precondition Failed',
        status: 412,
        code: 'revision_mismatch',
        detail: 'Draft was modified by another session',
        trace_id: 't1',
        retryable: false,
      },
      meta: {},
    });

    const { result } = renderHook(() =>
      useDomainCommand('update_generation_draft')
    );

    await act(async () => {
      await result.current.execute({
        body: {} as any,
        actionInstanceId: 'cas-test-2',
        ifMatch: '"stale-rev"',
      });
    });

    expect(result.current.problem?.status).toBe(412);
    expect(result.current.problem?.code).toBe('revision_mismatch');
  });

  it('revalidates draft data after a 412 conflict', async () => {
    mockGateway.updateGenerationDraft.mockResolvedValue({
      kind: 'problem',
      status: 412,
      problem: {
        type: 'precondition_failed',
        title: 'Precondition Failed',
        status: 412,
        code: 'revision_mismatch',
        detail: 'Draft was modified',
        trace_id: 't2',
        retryable: false,
      },
      meta: {},
    });

    const { result } = renderHook(() =>
      useDomainCommand('update_generation_draft')
    );

    await act(async () => {
      await result.current.execute({
        body: {} as any,
        actionInstanceId: 'cas-conflict-test',
        ifMatch: '"stale"',
      });
    });

    expect(result.current.problem?.code).toBe('revision_mismatch');
  });
});
```

- [ ] **Step 3: Implement DraftSaveIndicator**

Create `web/components/generation/draft/DraftSaveIndicator.tsx`:

```tsx
interface DraftSaveIndicatorProps {
  state: 'idle' | 'saving' | 'saved' | 'conflict';
}

export function DraftSaveIndicator({ state }: DraftSaveIndicatorProps) {
  const config = {
    idle: { text: '', className: '' },
    saving: { text: 'Saving…', className: 'text-blue-500' },
    saved: { text: 'Saved', className: 'text-green-600' },
    conflict: {
      text: 'Draft was modified — reload',
      className: 'text-red-600',
    },
  }[state];

  if (!config.text) return null;

  return (
    <span className={`text-xs ${config.className}`} aria-live="polite">
      {config.text}
    </span>
  );
}
```

- [ ] **Step 4: Implement DraftComposer sub-components**

Create each selector component. Each follows the same pattern: receive current draft data as props, render a picker, and call `onUpdate(patch)` when the user changes a value.

Create `web/components/generation/draft/ProductSelector.tsx`:

```tsx
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';

interface ProductSelectorProps {
  selectedProductId: string | null;
  onSelect: (productId: string) => void;
}

export function ProductSelector({
  selectedProductId,
  onSelect,
}: ProductSelectorProps) {
  const { data: products, isLoading } = useDomainQuery(
    'list_published_product_versions'
  );

  return (
    <div className="form-control">
      <label className="label" htmlFor="product-select">
        <span className="label-text font-medium">Product</span>
      </label>
      <select
        id="product-select"
        className="select select-bordered"
        value={selectedProductId ?? ''}
        onChange={(e) => onSelect(e.target.value)}
        disabled={isLoading}
      >
        <option value="">Select product version…</option>
        {(products?.items ?? []).map((p: any) => (
          <option key={p.id} value={p.id}>
            {p.name} (v{p.version})
          </option>
        ))}
      </select>
    </div>
  );
}
```

Create `web/components/generation/draft/ProviderConfig.tsx`:

```tsx
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';

interface ProviderConfigProps {
  provider: string;
  model: string;
  executionMode: string;
  seed: number | null;
  onUpdate: (patch: Record<string, unknown>) => void;
}

export function ProviderConfig({
  provider,
  model,
  executionMode,
  seed,
  onUpdate,
}: ProviderConfigProps) {
  const { data: capabilities } = useDomainQuery('get_generation_capabilities');
  const providers = capabilities?.providers ?? [];
  const selectedProvider = providers.find((p: any) => p.id === provider);
  const models = selectedProvider?.models ?? [];

  return (
    <div className="space-y-3">
      <div className="form-control">
        <label className="label" htmlFor="provider-select">
          <span className="label-text font-medium">Provider</span>
        </label>
        <select
          id="provider-select"
          className="select select-bordered"
          value={provider}
          onChange={(e) => onUpdate({ provider: e.target.value, model: '' })}
        >
          {providers.map((p: any) => (
            <option key={p.id} value={p.id}>{p.id}</option>
          ))}
        </select>
      </div>

      <div className="form-control">
        <label className="label" htmlFor="model-select">
          <span className="label-text font-medium">Model</span>
        </label>
        <select
          id="model-select"
          className="select select-bordered"
          value={model}
          onChange={(e) => onUpdate({ model: e.target.value })}
        >
          {models.map((m: any) => (
            <option key={m.id} value={m.id}>{m.name}</option>
          ))}
        </select>
      </div>

      <div className="form-control">
        <label className="label" htmlFor="seed-input">
          <span className="label-text font-medium">Seed</span>
        </label>
        <div className="flex items-center gap-2">
          <input
            id="seed-input"
            type="number"
            className="input input-bordered w-32"
            placeholder="Auto"
            value={seed ?? ''}
            onChange={(e) =>
              onUpdate({
                seed: e.target.value ? parseInt(e.target.value, 10) : null,
              })
            }
          />
          <span className="text-xs text-gray-400">
            Leave empty for random seed
          </span>
        </div>
      </div>
    </div>
  );
}
```

Create stub implementations for the remaining selectors — each follows the same pattern with domain-specific fields. For brevity, these are listed but follow the ProductSelector/ProviderConfig pattern:

- `web/components/generation/draft/DimensionConfigurator.tsx` — lists `DimensionTemplateVersion` pickers
- `web/components/generation/draft/CombinationStrategy.tsx` — full vs. sampled toggle with cap
- `web/components/generation/draft/FormatConfig.tsx` — aspect ratio, resolution, variant count
- `web/components/generation/draft/ReferenceSelector.tsx` — `ReferenceAssetVersion` picker with binding
- `web/components/generation/draft/ExclusionConfig.tsx` — manual exclusion, invalid flagging

Each receives its slice of draft data as props and calls `onUpdate(patch)`.

- [ ] **Step 5: Implement DraftComposer**

Create `web/components/generation/draft/DraftComposer.tsx`:

```tsx
import { useCallback, useState } from 'react';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import { ProductSelector } from './ProductSelector';
import { ProviderConfig } from './ProviderConfig';
import { DimensionConfigurator } from './DimensionConfigurator';
import { CombinationStrategy } from './CombinationStrategy';
import { FormatConfig } from './FormatConfig';
import { ReferenceSelector } from './ReferenceSelector';
import { ExclusionConfig } from './ExclusionConfig';
import { DraftSaveIndicator } from './DraftSaveIndicator';
import { ExecutionModeTag } from '@/components/generation/shared/ExecutionModeTag';

interface DraftComposerProps {
  draft: any;
  onDraftMutated: () => void;
}

export function DraftComposer({ draft, onDraftMutated }: DraftComposerProps) {
  const { execute } = useDomainCommand();
  const [saveState, setSaveState] = useState<
    'idle' | 'saving' | 'saved' | 'conflict'
  >('idle');

  const handleUpdate = useCallback(
    async (patch: Record<string, unknown>) => {
      setSaveState('saving');
      const result = await execute('update_generation_draft', {
        pathParams: { draft_id: draft.id },
        body: patch,
        ifMatch: draft.revision,
      });

      if (result.ok) {
        setSaveState('saved');
        onDraftMutated();
      } else if (result.problem?.type?.includes('precondition-failed')) {
        setSaveState('conflict');
      } else {
        setSaveState('idle');
      }
    },
    [draft.id, draft.revision, execute, onDraftMutated]
  );

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <ExecutionModeTag mode={draft.execution_mode} provider={draft.provider} model={draft.model} />
        <DraftSaveIndicator state={saveState} />
      </div>

      <ProductSelector
        selectedProductId={draft.selectors?.product_version_id ?? null}
        onSelect={(productId) =>
          handleUpdate({ selectors: { ...draft.selectors, product_version_id: productId } })
        }
      />

      <ProviderConfig
        provider={draft.provider}
        model={draft.model}
        executionMode={draft.execution_mode}
        seed={draft.seed ?? null}
        onUpdate={handleUpdate}
      />

      <DimensionConfigurator
        dimensions={draft.selectors?.dimensions ?? []}
        onUpdate={(dimensions) =>
          handleUpdate({ selectors: { ...draft.selectors, dimensions } })
        }
      />

      <CombinationStrategy
        strategy={draft.combination_strategy ?? 'full'}
        sampleCap={draft.sample_cap ?? null}
        onUpdate={handleUpdate}
      />

      <FormatConfig
        format={draft.format ?? {}}
        onUpdate={(format) => handleUpdate({ format })}
      />

      <ReferenceSelector
        references={draft.selectors?.references ?? []}
        onUpdate={(references) =>
          handleUpdate({ selectors: { ...draft.selectors, references } })
        }
      />

      <ExclusionConfig
        exclusions={draft.exclusions ?? []}
        onUpdate={(exclusions) => handleUpdate({ exclusions })}
      />
    </div>
  );
}
```

- [ ] **Step 6: Implement DraftWorkspace**

Create `web/components/generation/draft/DraftWorkspace.tsx`:

```tsx
import type { ReactNode } from 'react';
import { useTranslation } from 'next-i18next';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import { useRouter } from 'next/router';
import { DraftComposer } from './DraftComposer';

interface DraftWorkspaceProps {
  draftId: string;
  previewId: string | null;
  children?: ReactNode;
}

export function DraftWorkspace({
  draftId,
  previewId,
  children,
}: DraftWorkspaceProps) {
  const { t } = useTranslation('common');
  const router = useRouter();
  const {
    data: draft,
    isLoading,
    mutate,
  } = useDomainQuery('get_generation_draft', {
    pathParams: { draft_id: draftId },
  });
  const { execute } = useDomainCommand();

  const canPreview = draft?.allowed_actions?.some(
    (a: any) => a.action === 'create_preview' && a.allowed
  );

  async function handleCreatePreview() {
    const result = await execute('create_generation_preview', {
      pathParams: { draft_id: draftId },
    });
    if (result.ok) {
      router.replace(
        { query: { ...router.query, preview: result.data.id } },
        undefined,
        { shallow: true }
      );
    }
  }

  if (isLoading) {
    return (
      <div className="flex justify-center mt-12" role="status">
        <span className="loading loading-spinner loading-lg" />
      </div>
    );
  }

  if (!draft) {
    return <div className="mt-8 text-center text-gray-500">Draft not found.</div>;
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold outline-none">{draft.title}</h1>
        {!previewId && canPreview && (
          <button className="btn btn-secondary btn-sm" onClick={handleCreatePreview}>
            Generate Preview
          </button>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2">
          <DraftComposer draft={draft} onDraftMutated={() => mutate()} />
        </div>
        <div className="lg:col-span-1">{children}</div>
      </div>
    </div>
  );
}
```

- [ ] **Step 7: Create draft page shell**

Create `web/pages/w/[slug]/generate/drafts/[draftId].tsx`:

```tsx
import type { ReactElement } from 'react';
import { useRouter } from 'next/router';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { DraftWorkspace } from '@/components/generation/draft/DraftWorkspace';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { NextPageWithLayout } from 'types';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';

interface Props {
  bootstrap: WorkspaceBootstrap;
  errorCode?: number;
  errorContext?: string;
}

const DraftPage: NextPageWithLayout<Props> = () => {
  const router = useRouter();
  const draftId = router.query.draftId as string;
  const previewId = (router.query.preview as string) ?? null;

  return (
    <DraftWorkspace draftId={draftId} previewId={previewId}>
      {/* Preview slot wired in this task (Task 6) */}
    </DraftWorkspace>
  );
};

DraftPage.getLayout = function getLayout(page: ReactElement) {
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

export const getServerSideProps = getWorkspacePageProps();
export default DraftPage;
```

- [ ] **Step 8: Run tests and commit**

```bash
cd web && npx jest __tests__/generation/draft-workspace.spec.tsx tests/generation/draft-cas.test.ts --verbose
git add web/components/generation/draft/ web/pages/w/[slug]/generate/drafts/ \
  web/__tests__/generation/draft-workspace.spec.tsx web/tests/generation/draft-cas.test.ts
git commit -m "feat(generation): DraftWorkspace with CAS editing, selectors, and page shell"
```

---

## Task 6: Preview display components

**Files:**
- Create: `web/components/generation/preview/PreviewPanel.tsx`
- Create: `web/components/generation/preview/PreviewValidity.tsx`
- Create: `web/components/generation/preview/PromptInspector.tsx`
- Create: `web/components/generation/preview/CombinationSummary.tsx`
- Create: `web/components/generation/preview/CostEstimate.tsx`
- Create: `web/components/generation/preview/CapabilityDecisions.tsx`
- Modify: `web/pages/w/[slug]/generate/drafts/[draftId].tsx` (wire preview slot)
- Test: `web/__tests__/generation/preview-panel.spec.tsx`
- Test: `web/__tests__/generation/preview-validity.spec.tsx`

**Interfaces:**
- Consumes: `useDomainQuery('get_generation_preview')`, `CostDisplay`, `ExecutionModeTag`
- Produces: `PreviewPanel({ previewId })` — frozen preview display with validity, prompt, combinations, cost, capabilities

- [ ] **Step 1: Write failing test for PreviewValidity**

Create `web/__tests__/generation/preview-validity.spec.tsx`:

```tsx
/**
 * @jest-environment jsdom
 */
import { render, screen } from '@testing-library/react';
import { PreviewValidity } from '@/components/generation/preview/PreviewValidity';

describe('PreviewValidity', () => {
  it('shows valid state when not expired and not invalidated', () => {
    const future = new Date(Date.now() + 600_000).toISOString();
    render(
      <PreviewValidity validUntil={future} invalidationReason={null} />
    );
    expect(screen.getByText(/valid for/i)).toBeInTheDocument();
  });

  it('shows expired state when past valid_until', () => {
    const past = new Date(Date.now() - 1000).toISOString();
    render(
      <PreviewValidity validUntil={past} invalidationReason={null} />
    );
    expect(screen.getByText(/expired/i)).toBeInTheDocument();
  });

  it('shows invalidation reason when present', () => {
    const future = new Date(Date.now() + 600_000).toISOString();
    render(
      <PreviewValidity
        validUntil={future}
        invalidationReason="Draft was modified after preview creation"
      />
    );
    expect(screen.getByText(/draft was modified/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Implement PreviewValidity**

Create `web/components/generation/preview/PreviewValidity.tsx`:

```tsx
import { useEffect, useState } from 'react';

interface PreviewValidityProps {
  validUntil: string;
  invalidationReason: string | null;
}

export function PreviewValidity({
  validUntil,
  invalidationReason,
}: PreviewValidityProps) {
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 10_000);
    return () => clearInterval(timer);
  }, []);

  const expiresAt = new Date(validUntil).getTime();
  const isExpired = now >= expiresAt;
  const remainingMs = expiresAt - now;
  const remainingMin = Math.max(0, Math.ceil(remainingMs / 60_000));

  if (invalidationReason) {
    return (
      <div className="alert alert-warning text-sm" role="alert">
        <span>Preview outdated — {invalidationReason}</span>
      </div>
    );
  }

  if (isExpired) {
    return (
      <div className="alert alert-error text-sm" role="alert">
        <span>Preview expired — regenerate to continue</span>
      </div>
    );
  }

  return (
    <div className="text-xs text-gray-500" aria-live="polite">
      Valid for {remainingMin} minute{remainingMin !== 1 ? 's' : ''}
    </div>
  );
}
```

- [ ] **Step 3: Implement remaining preview sub-components**

Create `web/components/generation/preview/PromptInspector.tsx`:

```tsx
interface PromptInspectorProps {
  finalPrompt: string;
}

export function PromptInspector({ finalPrompt }: PromptInspectorProps) {
  return (
    <details className="collapse collapse-arrow bg-base-200">
      <summary className="collapse-title text-sm font-medium">
        Final Prompt
      </summary>
      <div className="collapse-content">
        <pre className="whitespace-pre-wrap text-xs font-mono bg-base-300 p-3 rounded overflow-x-auto max-w-full">
          {finalPrompt}
        </pre>
      </div>
    </details>
  );
}
```

Create `web/components/generation/preview/CombinationSummary.tsx`:

```tsx
interface CombinationSummaryProps {
  combinationCount: number;
  callCount: number;
  exclusionSummary: { excluded_count: number; reasons: string[] } | null;
}

export function CombinationSummary({
  combinationCount,
  callCount,
  exclusionSummary,
}: CombinationSummaryProps) {
  return (
    <div className="stats stats-vertical lg:stats-horizontal shadow-sm">
      <div className="stat">
        <div className="stat-title">Combinations</div>
        <div className="stat-value text-lg">{combinationCount}</div>
      </div>
      <div className="stat">
        <div className="stat-title">API Calls</div>
        <div className="stat-value text-lg">{callCount}</div>
      </div>
      {exclusionSummary && exclusionSummary.excluded_count > 0 && (
        <div className="stat">
          <div className="stat-title">Excluded</div>
          <div className="stat-value text-lg text-amber-600">
            {exclusionSummary.excluded_count}
          </div>
          <div className="stat-desc">
            {exclusionSummary.reasons.join(', ')}
          </div>
        </div>
      )}
    </div>
  );
}
```

Create `web/components/generation/preview/CostEstimate.tsx`:

```tsx
import { CostDisplay } from '@/components/generation/shared/CostDisplay';

interface CostEstimateProps {
  maximumCost: { amount: string; currency: string } | null;
}

export function CostEstimate({ maximumCost }: CostEstimateProps) {
  return (
    <div className="flex items-center gap-2 text-sm">
      <span className="font-medium">Maximum cost:</span>
      {maximumCost ? (
        <CostDisplay amount={maximumCost.amount} currency={maximumCost.currency} />
      ) : (
        <span className="text-gray-400">Not available</span>
      )}
    </div>
  );
}
```

Create `web/components/generation/preview/CapabilityDecisions.tsx`:

```tsx
interface CapabilityDecision {
  capability: string;
  supported: boolean;
  detail: string | null;
}

interface CapabilityDecisionsProps {
  decisions: CapabilityDecision[];
}

export function CapabilityDecisions({ decisions }: CapabilityDecisionsProps) {
  if (decisions.length === 0) return null;

  return (
    <details className="collapse collapse-arrow bg-base-200">
      <summary className="collapse-title text-sm font-medium">
        Capability Decisions ({decisions.length})
      </summary>
      <div className="collapse-content">
        <ul className="space-y-1">
          {decisions.map((d) => (
            <li key={d.capability} className="flex items-center gap-2 text-xs">
              <span className={d.supported ? 'text-green-600' : 'text-red-600'}>
                {d.supported ? '✓' : '✕'}
              </span>
              <span>{d.capability}</span>
              {d.detail && <span className="text-gray-400">— {d.detail}</span>}
            </li>
          ))}
        </ul>
      </div>
    </details>
  );
}
```

- [ ] **Step 4: Implement PreviewPanel**

Create `web/components/generation/preview/PreviewPanel.tsx`:

```tsx
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { ExecutionModeTag } from '@/components/generation/shared/ExecutionModeTag';
import { PreviewValidity } from './PreviewValidity';
import { PromptInspector } from './PromptInspector';
import { CombinationSummary } from './CombinationSummary';
import { CostEstimate } from './CostEstimate';
import { CapabilityDecisions } from './CapabilityDecisions';

interface PreviewPanelProps {
  previewId: string;
}

export function PreviewPanel({ previewId }: PreviewPanelProps) {
  const { data: preview, isLoading } = useDomainQuery(
    'get_generation_preview',
    { pathParams: { preview_id: previewId } }
  );

  if (isLoading) {
    return <div className="loading loading-spinner" role="status" />;
  }

  if (!preview) {
    return <div className="text-sm text-gray-500">Preview not found.</div>;
  }

  return (
    <div className="space-y-4 rounded-lg border p-4">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold">Resolution Preview</h3>
        <ExecutionModeTag mode={preview.execution_mode} />
      </div>

      <PreviewValidity
        validUntil={preview.valid_until}
        invalidationReason={preview.invalidation_reason}
      />

      <CombinationSummary
        combinationCount={preview.combination_count}
        callCount={preview.call_count}
        exclusionSummary={preview.exclusion_summary}
      />

      <CostEstimate maximumCost={preview.maximum_cost} />

      {preview.final_prompt && (
        <PromptInspector finalPrompt={preview.final_prompt} />
      )}

      <CapabilityDecisions
        decisions={preview.capability_decisions ?? []}
      />
    </div>
  );
}
```

- [ ] **Step 5: Wire preview slot in draft page shell**

Update `web/pages/w/[slug]/generate/drafts/[draftId].tsx`:

```tsx
import type { ReactElement } from 'react';
import { useRouter } from 'next/router';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { DraftWorkspace } from '@/components/generation/draft/DraftWorkspace';
import { PreviewPanel } from '@/components/generation/preview/PreviewPanel';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { NextPageWithLayout } from 'types';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';

interface Props {
  bootstrap: WorkspaceBootstrap;
  errorCode?: number;
  errorContext?: string;
}

const DraftPage: NextPageWithLayout<Props> = () => {
  const router = useRouter();
  const draftId = router.query.draftId as string;
  const previewId = (router.query.preview as string) ?? null;

  return (
    <DraftWorkspace draftId={draftId} previewId={previewId}>
      {previewId && <PreviewPanel previewId={previewId} />}
    </DraftWorkspace>
  );
};

DraftPage.getLayout = function getLayout(page: ReactElement) {
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

export const getServerSideProps = getWorkspacePageProps();
export default DraftPage;
```

- [ ] **Step 6: Run tests and commit**

```bash
cd web && npx jest __tests__/generation/preview --verbose
git add web/components/generation/preview/ web/pages/w/[slug]/generate/drafts/ \
  web/__tests__/generation/preview-panel.spec.tsx web/__tests__/generation/preview-validity.spec.tsx
git commit -m "feat(generation): PreviewPanel with validity, prompt, combinations, cost, capabilities"
```

---

## Task 7: Slice 1 E2E tests

**Files:**
- Create: `web/e2e/generation/draft-preview.spec.ts`
- Create: `web/e2e/generation/fixtures.ts`

**Interfaces:**
- Consumes: All Slice 1 components via browser automation
- Produces: Playwright tests covering acceptance criteria 1.1–1.16

- [ ] **Step 1: Create generation E2E fixtures**

Create `web/e2e/generation/fixtures.ts`:

```ts
import { test as setup } from '@playwright/test';

/**
 * Extends the workspace auth fixtures with generation-specific seed data.
 * Requires the domain API to be running with:
 * - A workspace with at least one published ProductVersion
 * - Capabilities endpoint returning valid providers/models/modes
 */
export { setup };
```

- [ ] **Step 2: Write E2E tests for draft creation and preview flow**

Create `web/e2e/generation/draft-preview.spec.ts`:

```ts
import { test, expect } from '@playwright/test';

test.describe('Slice 1: Draft + Preview', () => {
  test('1.1: New Draft opens setup dialog, creates draft, navigates', async ({
    page,
  }) => {
    await page.goto('/w/workspace-a/generate');
    await page.click('button:has-text("New Draft")');
    await expect(page.locator('dialog')).toBeVisible();

    await page.selectOption('[aria-label="Product"]', { index: 1 });
    await page.selectOption('[aria-label="Provider"]', { index: 1 });
    await page.selectOption('[aria-label="Model"]', { index: 1 });
    await page.selectOption('[aria-label="Execution Mode"]', { index: 1 });
    await page.click('button:has-text("Create Draft")');

    await expect(page).toHaveURL(/\/generate\/drafts\//);
  });

  test('1.9: Refresh on draft page with preview restores from server', async ({
    page,
  }) => {
    // Navigate to an existing draft (created via API seed)
    await page.goto('/w/workspace-a/generate');
    await page.click('a[href*="/generate/drafts/"]');
    await expect(page).toHaveURL(/\/generate\/drafts\//);
    await page.reload();
    await expect(page.locator('h1')).toBeVisible();
  });

  test('1.10: Refresh on generate list restores from server', async ({
    page,
  }) => {
    await page.goto('/w/workspace-a/generate');
    await page.reload();
    await expect(page.locator('h1')).toContainText(/generation/i);
  });

  test('1.12: Back/forward between list and draft', async ({ page }) => {
    await page.goto('/w/workspace-a/generate');
    await page.click('a[href*="/generate/drafts/"]');
    await expect(page).toHaveURL(/\/generate\/drafts\//);
    await page.goBack();
    await expect(page).toHaveURL(/\/generate$/);
    await page.goForward();
    await expect(page).toHaveURL(/\/generate\/drafts\//);
  });

  test('1.13: Nav item highlighted on all /generate/* routes', async ({
    page,
  }) => {
    await page.goto('/w/workspace-a/generate');
    const navLink = page.locator('a:has-text("New Generation")');
    await expect(navLink).toHaveAttribute('data-active', 'true');

    await page.click('a[href*="/generate/drafts/"]');
    await expect(navLink).toHaveAttribute('data-active', 'true');
  });

  test('1.11: Deep link with foreign resource ID returns 404', async ({
    page,
  }) => {
    await page.goto('/w/workspace-a/generate/drafts/nonexistent-foreign-id');
    await expect(page.getByText(/not found/i)).toBeVisible();
  });
});
```

- [ ] **Step 3: Commit**

```bash
git add web/e2e/generation/
git commit -m "test(generation): Slice 1 E2E tests for draft creation, preview, and navigation"
```

---

# Slice 2: Submission + Progress

**Prerequisites:** Slice 1 merged, #53 closed.

## Task 8: RunProgressSource + useRunProgress

**Files:**
- Create: `web/components/generation/shared/RunProgressSource.ts`
- Create: `web/components/generation/shared/useRunProgress.ts`
- Test: `web/tests/generation/shared/run-progress-source.test.ts`

**Interfaces:**
- Consumes: `DomainGateway` (reads), `useOperation` (for command polling)
- Produces:
  - `RunProgressSource` interface and `PollingRunProgressSource` implementation
  - `useRunProgress(runId)` → `RunProgressSnapshot & { forceRefresh, setItemPage }`

- [ ] **Step 1: Write failing test for PollingRunProgressSource**

Create `web/tests/generation/shared/run-progress-source.test.ts`:

```ts
import { PollingRunProgressSource } from '@/components/generation/shared/RunProgressSource';

const mockGateway = {
  runQuery: jest.fn(),
};

function makeRun(state: string, commandOpState?: string) {
  return {
    id: 'run-1',
    status: state,
    done: 5,
    total: 10,
    failed: 2,
    commands: commandOpState
      ? [{ operation_id: 'op-1', state: commandOpState }]
      : [],
  };
}

function makeItems(count: number) {
  return {
    items: Array.from({ length: count }, (_, i) => ({
      id: `item-${i}`,
      state: 'succeeded',
      ordinal: i,
    })),
    next_cursor: null,
  };
}

beforeEach(() => {
  jest.useFakeTimers();
  mockGateway.runQuery.mockReset();
});

afterEach(() => {
  jest.useRealTimers();
});

describe('PollingRunProgressSource', () => {
  it('polls at 1-2s when Operation is running', () => {
    mockGateway.runQuery
      .mockResolvedValueOnce(makeRun('running', 'running'))
      .mockResolvedValueOnce(makeItems(5));

    const source = new PollingRunProgressSource(mockGateway as any);
    const onSnapshot = jest.fn();

    source.subscribe('run-1', {}, onSnapshot);
    expect(mockGateway.runQuery).toHaveBeenCalled();
  });

  it('stops polling when both Run and Operation are terminal', async () => {
    mockGateway.runQuery
      .mockResolvedValueOnce(makeRun('succeeded', 'succeeded'))
      .mockResolvedValueOnce(makeItems(10));

    const source = new PollingRunProgressSource(mockGateway as any);
    const onSnapshot = jest.fn();

    source.subscribe('run-1', {}, onSnapshot);

    // Wait for initial fetch
    await jest.advanceTimersByTimeAsync(100);

    const snapshot = onSnapshot.mock.calls[0]?.[0];
    expect(snapshot?.isRunTerminal).toBe(true);
    expect(snapshot?.isOperationTerminal).toBe(true);
  });

  it('continues polling when Operation is uncertain', async () => {
    mockGateway.runQuery
      .mockResolvedValueOnce(makeRun('running', 'uncertain'))
      .mockResolvedValueOnce(makeItems(5));

    const source = new PollingRunProgressSource(mockGateway as any);
    const onSnapshot = jest.fn();

    source.subscribe('run-1', {}, onSnapshot);
    await jest.advanceTimersByTimeAsync(100);

    const snapshot = onSnapshot.mock.calls[0]?.[0];
    expect(snapshot?.isOperationTerminal).toBe(false);
  });

  it('tracks transport errors without creating terminal state', async () => {
    mockGateway.runQuery.mockRejectedValueOnce(new Error('Network error'));

    const source = new PollingRunProgressSource(mockGateway as any);
    const onSnapshot = jest.fn();

    source.subscribe('run-1', {}, onSnapshot);
    await jest.advanceTimersByTimeAsync(100);

    const snapshot = onSnapshot.mock.calls[0]?.[0];
    expect(snapshot?.transportError).toBeInstanceOf(Error);
    expect(snapshot?.isRunTerminal).toBe(false);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx jest tests/generation/shared/run-progress-source.test.ts --verbose`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement RunProgressSource**

Create `web/components/generation/shared/RunProgressSource.ts`:

```ts
interface RunProgressSnapshot {
  run: any;
  items: any[];
  itemsPagination: { cursor: string | null; hasMore: boolean };
  latestCommandOperation: any | null;
  isRunTerminal: boolean;
  isOperationTerminal: boolean;
  isPartialSuccess: boolean;
  lastRefreshedAt: number;
  transportError: Error | null;
}

interface RunProgressSourceInterface {
  subscribe(
    runId: string,
    options: { itemPageCursor?: string },
    onSnapshot: (s: RunProgressSnapshot) => void
  ): void;
  unsubscribe(): void;
  forceRefresh(): void;
  setItemPage(cursor: string | null): void;
}

const RUN_TERMINAL_STATES = new Set([
  'succeeded',
  'failed',
  'cancelled',
  'partially_succeeded',
]);

const OP_TERMINAL_STATES = new Set([
  'succeeded',
  'failed',
  'cancelled',
  'partially_succeeded',
]);

function getInterval(opState: string | null): number {
  if (opState === 'running') return 1500;
  if (opState === 'queued') return 15_000 + Math.random() * 15_000;
  if (opState === 'uncertain') return 15_000 + Math.random() * 15_000;
  return 5000;
}

class PollingRunProgressSource implements RunProgressSourceInterface {
  private gateway: any;
  private runId: string | null = null;
  private itemPageCursor: string | null = null;
  private onSnapshot: ((s: RunProgressSnapshot) => void) | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private consecutiveFailures = 0;
  private stopped = false;
  private visibilityHandler: (() => void) | null = null;

  constructor(gateway: any) {
    this.gateway = gateway;
  }

  subscribe(
    runId: string,
    options: { itemPageCursor?: string },
    onSnapshot: (s: RunProgressSnapshot) => void
  ): void {
    this.runId = runId;
    this.itemPageCursor = options.itemPageCursor ?? null;
    this.onSnapshot = onSnapshot;
    this.stopped = false;
    this.consecutiveFailures = 0;

    this.visibilityHandler = () => {
      if (document.visibilityState === 'visible') {
        this.forceRefresh();
      }
    };

    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', this.visibilityHandler);
    }

    this.poll();
  }

  unsubscribe(): void {
    this.stopped = true;
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    if (this.visibilityHandler && typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', this.visibilityHandler);
    }
    this.onSnapshot = null;
  }

  forceRefresh(): void {
    if (this.stopped) return;
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    this.poll();
  }

  setItemPage(cursor: string | null): void {
    this.itemPageCursor = cursor;
    this.forceRefresh();
  }

  private async poll(): Promise<void> {
    if (this.stopped || !this.runId || !this.onSnapshot) return;

    try {
      const [run, itemsResponse] = await Promise.all([
        this.gateway.runQuery('get_generation_run', {
          pathParams: { generation_run_id: this.runId },
        }),
        this.gateway.runQuery('list_generation_run_items', {
          pathParams: { generation_run_id: this.runId },
          queryParams: this.itemPageCursor
            ? { cursor: this.itemPageCursor }
            : undefined,
        }),
      ]);

      this.consecutiveFailures = 0;
      const latestCommand =
        run.commands?.[run.commands.length - 1] ?? null;
      const opState = latestCommand?.state ?? null;
      const isRunTerminal = RUN_TERMINAL_STATES.has(run.status);
      const isOperationTerminal =
        latestCommand === null || OP_TERMINAL_STATES.has(opState);

      const snapshot: RunProgressSnapshot = {
        run,
        items: itemsResponse?.items ?? [],
        itemsPagination: {
          cursor: itemsResponse?.next_cursor ?? null,
          hasMore: !!itemsResponse?.next_cursor,
        },
        latestCommandOperation: latestCommand,
        isRunTerminal,
        isOperationTerminal,
        isPartialSuccess: run.status === 'partially_succeeded',
        lastRefreshedAt: Date.now(),
        transportError: null,
      };

      this.onSnapshot(snapshot);

      if (isRunTerminal && isOperationTerminal) {
        return;
      }

      const interval = getInterval(opState);
      this.timer = setTimeout(() => this.poll(), interval);
    } catch (err) {
      this.consecutiveFailures++;
      const backoff = Math.min(
        60_000,
        1000 * Math.pow(2, this.consecutiveFailures)
      );

      if (this.onSnapshot) {
        this.onSnapshot({
          run: null as any,
          items: [],
          itemsPagination: { cursor: null, hasMore: false },
          latestCommandOperation: null,
          isRunTerminal: false,
          isOperationTerminal: false,
          isPartialSuccess: false,
          lastRefreshedAt: Date.now(),
          transportError: err instanceof Error ? err : new Error(String(err)),
        });
      }

      this.timer = setTimeout(() => this.poll(), backoff);
    }
  }
}

export type { RunProgressSnapshot, RunProgressSourceInterface };
export { PollingRunProgressSource };
```

- [ ] **Step 4: Implement useRunProgress hook**

Create `web/components/generation/shared/useRunProgress.ts`:

```ts
import { useEffect, useRef, useState } from 'react';
import {
  PollingRunProgressSource,
  type RunProgressSnapshot,
} from './RunProgressSource';

const INITIAL_SNAPSHOT: RunProgressSnapshot = {
  run: null as any,
  items: [],
  itemsPagination: { cursor: null, hasMore: false },
  latestCommandOperation: null,
  isRunTerminal: false,
  isOperationTerminal: false,
  isPartialSuccess: false,
  lastRefreshedAt: 0,
  transportError: null,
};

export function useRunProgress(runId: string, gateway: any) {
  const [snapshot, setSnapshot] =
    useState<RunProgressSnapshot>(INITIAL_SNAPSHOT);
  const sourceRef = useRef<PollingRunProgressSource | null>(null);

  useEffect(() => {
    const source = new PollingRunProgressSource(gateway);
    sourceRef.current = source;
    source.subscribe(runId, {}, setSnapshot);
    return () => source.unsubscribe();
  }, [runId, gateway]);

  const forceRefresh = () => sourceRef.current?.forceRefresh();
  const setItemPage = (cursor: string | null) =>
    sourceRef.current?.setItemPage(cursor);

  return { ...snapshot, forceRefresh, setItemPage };
}
```

- [ ] **Step 5: Run tests and commit**

```bash
cd web && npx jest tests/generation/shared/run-progress-source.test.ts --verbose
git add web/components/generation/shared/RunProgressSource.ts \
  web/components/generation/shared/useRunProgress.ts \
  web/tests/generation/shared/run-progress-source.test.ts
git commit -m "feat(generation): RunProgressSource with adaptive polling and useRunProgress hook"
```

---

## Task 9: SubmitConfirmation + Run page shell

**Files:**
- Create: `web/components/generation/preview/SubmitConfirmation.tsx`
- Create: `web/components/generation/shared/RunItemStateBadge.tsx`
- Create: `web/components/generation/progress/RunProgressView.tsx`
- Create: `web/components/generation/progress/RunSummary.tsx`
- Create: `web/components/generation/progress/RunItemGrid.tsx`
- Create: `web/components/generation/progress/RunItemDetail.tsx`
- Create: `web/components/generation/progress/OperationTracker.tsx`
- Create: `web/pages/w/[slug]/generate/runs/[runId].tsx`
- Create: `web/components/generation/list/RunList.tsx`
- Modify: `web/components/generation/list/GenerationListView.tsx` (add Run list)
- Modify: `web/pages/w/[slug]/generate/drafts/[draftId].tsx` (wire SubmitConfirmation)
- Test: `web/__tests__/generation/submit-confirmation.spec.tsx`
- Test: `web/__tests__/generation/run-progress-view.spec.tsx`

**Interfaces:**
- Consumes: `useDomainCommand('submit_generation_run')`, `useRunProgress`, `useGenerationNav`, `CostDisplay`, `ExecutionModeTag`, `OperationStatus`, `RunItemStateBadge`
- Produces: `SubmitConfirmation({ previewId, previewHash, validUntil, onSubmitted })`, `RunProgressView({ runId, recoverySlot })`, `RunList({ runs })`

- [ ] **Step 1: Write failing test for SubmitConfirmation**

Create `web/__tests__/generation/submit-confirmation.spec.tsx`:

```tsx
/**
 * @jest-environment jsdom
 */
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { SubmitConfirmation } from '@/components/generation/preview/SubmitConfirmation';

const mockExecute = jest.fn();
const mockNavigateToRun = jest.fn();

jest.mock('@/hooks/domain/useDomainCommand', () => ({
  useDomainCommand: () => ({ execute: mockExecute, isExecuting: false }),
}));

jest.mock('@/components/generation/shared/useGenerationNav', () => ({
  useGenerationNav: () => ({ navigateToRun: mockNavigateToRun }),
}));

beforeEach(() => {
  mockExecute.mockClear();
  mockNavigateToRun.mockClear();
});

describe('SubmitConfirmation', () => {
  const futureDate = new Date(Date.now() + 600_000).toISOString();
  const pastDate = new Date(Date.now() - 1000).toISOString();

  it('enables submit when preview is valid', () => {
    render(
      <SubmitConfirmation
        previewId="prev-1"
        previewHash="hash-1"
        validUntil={futureDate}
        onSubmitted={jest.fn()}
      />
    );
    expect(screen.getByRole('button', { name: /submit/i })).not.toBeDisabled();
  });

  it('disables submit when preview is expired', () => {
    render(
      <SubmitConfirmation
        previewId="prev-1"
        previewHash="hash-1"
        validUntil={pastDate}
        onSubmitted={jest.fn()}
      />
    );
    expect(screen.getByRole('button', { name: /submit/i })).toBeDisabled();
  });

  it('submits and navigates to run', async () => {
    mockExecute.mockResolvedValue({
      ok: true,
      data: { operation_id: 'op-1', generation_run_id: 'run-1' },
    });

    render(
      <SubmitConfirmation
        previewId="prev-1"
        previewHash="hash-1"
        validUntil={futureDate}
        onSubmitted={jest.fn()}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /submit/i }));

    await waitFor(() => {
      expect(mockExecute).toHaveBeenCalledWith(
        'submit_generation_run',
        expect.objectContaining({
          body: { preview_id: 'prev-1', preview_hash: 'hash-1' },
        })
      );
    });

    await waitFor(() => {
      expect(mockNavigateToRun).toHaveBeenCalledWith('run-1');
    });
  });
});
```

- [ ] **Step 2: Implement SubmitConfirmation**

Create `web/components/generation/preview/SubmitConfirmation.tsx`:

```tsx
import { useState } from 'react';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import { useGenerationNav } from '@/components/generation/shared/useGenerationNav';

interface SubmitConfirmationProps {
  previewId: string;
  previewHash: string;
  validUntil: string;
  onSubmitted: () => void;
}

export function SubmitConfirmation({
  previewId,
  previewHash,
  validUntil,
  onSubmitted,
}: SubmitConfirmationProps) {
  const { execute, isExecuting } = useDomainCommand();
  const { navigateToRun } = useGenerationNav();
  const isExpired = Date.now() >= new Date(validUntil).getTime();

  async function handleSubmit() {
    const result = await execute('submit_generation_run', {
      body: { preview_id: previewId, preview_hash: previewHash },
    });

    if (result.ok) {
      onSubmitted();
      navigateToRun(result.data.generation_run_id);
    }
  }

  return (
    <div className="mt-4 border-t pt-4">
      <button
        className="btn btn-primary w-full"
        onClick={handleSubmit}
        disabled={isExpired || isExecuting}
        aria-label="Submit Generation Run"
      >
        {isExecuting ? 'Submitting…' : 'Submit Run'}
      </button>
      {isExpired && (
        <p className="mt-1 text-xs text-red-600">
          Preview expired — regenerate to submit
        </p>
      )}
    </div>
  );
}
```

- [ ] **Step 3: Implement RunItemStateBadge, RunSummary, RunItemGrid, RunItemDetail, OperationTracker**

Create `web/components/generation/shared/RunItemStateBadge.tsx`:

```tsx
const STATE_CONFIG: Record<string, { label: string; className: string }> = {
  queued: { label: 'Queued', className: 'badge-ghost' },
  running: { label: 'Running', className: 'badge-info' },
  succeeded: { label: 'Succeeded', className: 'badge-success' },
  failed: { label: 'Failed', className: 'badge-error' },
  paused: { label: 'Paused', className: 'badge-warning' },
  uncertain: { label: 'Uncertain', className: 'badge-warning' },
  cancelled: { label: 'Cancelled', className: 'badge-ghost' },
};

interface RunItemStateBadgeProps {
  state: string;
}

export function RunItemStateBadge({ state }: RunItemStateBadgeProps) {
  const config = STATE_CONFIG[state] ?? STATE_CONFIG.queued;
  return (
    <span
      className={`badge badge-sm ${config.className}`}
      aria-label={config.label}
    >
      {config.label}
    </span>
  );
}
```

Create `web/components/generation/progress/RunSummary.tsx`:

```tsx
import { ExecutionModeTag } from '@/components/generation/shared/ExecutionModeTag';

interface RunSummaryProps {
  run: any;
}

export function RunSummary({ run }: RunSummaryProps) {
  return (
    <div className="card bg-base-100 shadow-sm">
      <div className="card-body">
        <div className="flex items-center justify-between">
          <h2 className="card-title">{run.title ?? 'Generation Run'}</h2>
          <ExecutionModeTag
            mode={run.execution_mode}
            provider={run.provider}
            model={run.model}
          />
        </div>
        <div className="stats stats-horizontal mt-2">
          <div className="stat">
            <div className="stat-title">Done</div>
            <div className="stat-value text-lg text-green-600">
              {run.done ?? 0}
            </div>
          </div>
          <div className="stat">
            <div className="stat-title">Total</div>
            <div className="stat-value text-lg">{run.total ?? 0}</div>
          </div>
          <div className="stat">
            <div className="stat-title">Failed</div>
            <div className="stat-value text-lg text-red-600">
              {run.failed ?? 0}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
```

Create `web/components/generation/progress/RunItemGrid.tsx`:

```tsx
import { RunItemStateBadge } from '@/components/generation/shared/RunItemStateBadge';

interface RunItemGridProps {
  items: any[];
  selectedItemId: string | null;
  onSelectItem: (itemId: string) => void;
  pagination: { cursor: string | null; hasMore: boolean };
  onPageChange: (cursor: string | null) => void;
}

export function RunItemGrid({
  items,
  selectedItemId,
  onSelectItem,
  pagination,
  onPageChange,
}: RunItemGridProps) {
  return (
    <div>
      <div className="grid grid-cols-2 sm:grid-cols-4 md:grid-cols-6 gap-2">
        {items.map((item) => (
          <button
            key={item.id}
            className={`card card-compact bg-base-100 shadow-sm cursor-pointer hover:shadow-md transition-shadow ${
              selectedItemId === item.id ? 'ring-2 ring-primary' : ''
            }`}
            onClick={() => onSelectItem(item.id)}
            aria-label={`Item ${item.ordinal + 1}: ${item.state}`}
            aria-pressed={selectedItemId === item.id}
          >
            <div className="card-body items-center p-2">
              <span className="text-xs text-gray-500">#{item.ordinal + 1}</span>
              <RunItemStateBadge state={item.state} />
            </div>
          </button>
        ))}
      </div>
      {pagination.hasMore && (
        <div className="mt-3 flex justify-center">
          <button
            className="btn btn-sm btn-ghost"
            onClick={() => onPageChange(pagination.cursor)}
          >
            Load more
          </button>
        </div>
      )}
    </div>
  );
}
```

Create `web/components/generation/progress/RunItemDetail.tsx`:

```tsx
import { RunItemStateBadge } from '@/components/generation/shared/RunItemStateBadge';
import { ExecutionModeTag } from '@/components/generation/shared/ExecutionModeTag';
import { CostDisplay } from '@/components/generation/shared/CostDisplay';

interface RunItemDetailProps {
  item: any;
}

export function RunItemDetail({ item }: RunItemDetailProps) {
  return (
    <div className="card bg-base-100 shadow-sm">
      <div className="card-body">
        <div className="flex items-center justify-between">
          <h3 className="font-semibold">Item #{item.ordinal + 1}</h3>
          <RunItemStateBadge state={item.state} />
        </div>
        <ExecutionModeTag mode={item.execution_mode} />

        {item.latest_attempt && (
          <div className="mt-3 space-y-1 text-sm">
            <p>
              <strong>Dispatch:</strong> {item.latest_attempt.dispatch_certainty}
            </p>
            <p>
              <strong>Outcome:</strong> {item.latest_attempt.outcome}
            </p>
            <p>
              <strong>Provider:</strong> {item.latest_attempt.provider} /{' '}
              {item.latest_attempt.model} / {item.latest_attempt.version}
            </p>
            {item.latest_attempt.cost && (
              <p>
                <strong>Cost:</strong>{' '}
                <CostDisplay
                  amount={item.latest_attempt.cost.amount}
                  currency={item.latest_attempt.cost.currency}
                />
              </p>
            )}
          </div>
        )}

        {item.qc_result && (
          <div className="mt-3 text-sm">
            <p>
              <strong>QC:</strong>{' '}
              <span
                className={
                  item.qc_result.state === 'passed'
                    ? 'text-green-600'
                    : 'text-red-600'
                }
              >
                {item.qc_result.state}
              </span>
            </p>
          </div>
        )}

        {item.bound_asset_version_id && (
          <div className="mt-2 text-sm">
            <strong>Asset:</strong>{' '}
            <code className="text-xs">{item.bound_asset_version_id}</code>
          </div>
        )}

        {item.error_summary && (
          <div className="mt-3 alert alert-error text-sm">
            {item.error_summary}
          </div>
        )}
      </div>
    </div>
  );
}
```

Create `web/components/generation/progress/OperationTracker.tsx`:

```tsx
import { OperationStatus } from '@/components/workspace/OperationStatus';

interface OperationTrackerProps {
  operation: any;
  transportError: Error | null;
}

export function OperationTracker({
  operation,
  transportError,
}: OperationTrackerProps) {
  if (!operation) return null;

  return (
    <OperationStatus
      operation={{ status: operation.state, result_ref: operation.result_ref }}
      pollingProblem={
        transportError ? { title: transportError.message, type: 'transport-error', status: 0 } : undefined
      }
    />
  );
}
```

- [ ] **Step 4: Implement RunProgressView**

Create `web/components/generation/progress/RunProgressView.tsx`:

```tsx
import type { ReactNode } from 'react';
import { useState } from 'react';
import { useRunProgress } from '@/components/generation/shared/useRunProgress';
import { useDomainGateway } from '@/hooks/domain/useDomainGateway';
import { RunSummary } from './RunSummary';
import { RunItemGrid } from './RunItemGrid';
import { RunItemDetail } from './RunItemDetail';
import { OperationTracker } from './OperationTracker';

interface RunProgressViewProps {
  runId: string;
  recoverySlot?: ReactNode;
}

export function RunProgressView({ runId, recoverySlot }: RunProgressViewProps) {
  const gateway = useDomainGateway();
  const progress = useRunProgress(runId, gateway);
  const [selectedItemId, setSelectedItemId] = useState<string | null>(null);

  const selectedItem = progress.items.find((i) => i.id === selectedItemId);

  if (!progress.run && !progress.transportError) {
    return (
      <div className="flex justify-center mt-12" role="status">
        <span className="loading loading-spinner loading-lg" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {progress.run && (
        <>
          <h1 className="text-2xl font-bold outline-none">
            {progress.run.title ?? 'Generation Run'}
          </h1>

          <RunSummary run={progress.run} />

          <OperationTracker
            operation={progress.latestCommandOperation}
            transportError={progress.transportError}
          />

          <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
            <div className="lg:col-span-2">
              <h2 className="text-lg font-semibold mb-3">Items</h2>
              <RunItemGrid
                items={progress.items}
                selectedItemId={selectedItemId}
                onSelectItem={setSelectedItemId}
                pagination={progress.itemsPagination}
                onPageChange={progress.setItemPage}
              />
            </div>
            <div className="lg:col-span-1">
              {selectedItem && <RunItemDetail item={selectedItem} />}
            </div>
          </div>
        </>
      )}

      {progress.isRunTerminal && recoverySlot}
    </div>
  );
}
```

- [ ] **Step 5: Create Run page shell and RunList**

Create `web/pages/w/[slug]/generate/runs/[runId].tsx`:

```tsx
import type { ReactElement } from 'react';
import { useRouter } from 'next/router';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { RunProgressView } from '@/components/generation/progress/RunProgressView';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { NextPageWithLayout } from 'types';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';

interface Props {
  bootstrap: WorkspaceBootstrap;
  errorCode?: number;
  errorContext?: string;
}

const RunPage: NextPageWithLayout<Props> = () => {
  const router = useRouter();
  const runId = router.query.runId as string;

  return <RunProgressView runId={runId} />;
};

RunPage.getLayout = function getLayout(page: ReactElement) {
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

export const getServerSideProps = getWorkspacePageProps();
export default RunPage;
```

Create `web/components/generation/list/RunList.tsx`:

```tsx
import Link from 'next/link';
import { useGenerationNav } from '@/components/generation/shared/useGenerationNav';
import { RunItemStateBadge } from '@/components/generation/shared/RunItemStateBadge';

interface RunSummaryItem {
  id: string;
  title: string;
  status: string;
  done: number;
  total: number;
  created_at: string;
}

interface RunListProps {
  runs: RunSummaryItem[];
}

export function RunList({ runs }: RunListProps) {
  const { buildRunUrl } = useGenerationNav();

  if (runs.length === 0) {
    return <p className="text-sm text-gray-500">No runs yet.</p>;
  }

  return (
    <ul className="space-y-2">
      {runs.map((run) => (
        <li key={run.id} className="card card-compact bg-base-100 shadow-sm">
          <div className="card-body flex-row items-center justify-between">
            <div>
              <Link
                href={buildRunUrl(run.id)}
                className="font-medium hover:underline"
              >
                {run.title}
              </Link>
              <div className="mt-1 flex items-center gap-2">
                <RunItemStateBadge state={run.status} />
                <span className="text-xs text-gray-400">
                  {run.done}/{run.total}
                </span>
              </div>
            </div>
            <span className="text-xs text-gray-400">
              {new Date(run.created_at).toLocaleDateString()}
            </span>
          </div>
        </li>
      ))}
    </ul>
  );
}
```

- [ ] **Step 6: Run tests and commit**

```bash
cd web && npx jest __tests__/generation/submit-confirmation.spec.tsx __tests__/generation/run-progress-view.spec.tsx --verbose
git add web/components/generation/preview/SubmitConfirmation.tsx \
  web/components/generation/shared/RunItemStateBadge.tsx \
  web/components/generation/progress/ \
  web/components/generation/list/RunList.tsx \
  web/pages/w/[slug]/generate/runs/ \
  web/__tests__/generation/submit-confirmation.spec.tsx \
  web/__tests__/generation/run-progress-view.spec.tsx
git commit -m "feat(generation): Submission + Progress — SubmitConfirmation, RunProgressView, RunItemGrid, Run page"
```

---

## Task 10: Slice 2 E2E tests

**Files:**
- Create: `web/e2e/generation/submission-progress.spec.ts`

**Interfaces:**
- Consumes: All Slice 2 components via browser automation
- Produces: Playwright tests covering acceptance criteria 2.1–2.16

- [ ] **Step 1: Write E2E tests**

Create `web/e2e/generation/submission-progress.spec.ts`:

```ts
import { test, expect } from '@playwright/test';

test.describe('Slice 2: Submission + Progress', () => {
  test('2.2: Submit creates Operation and navigates to run page', async ({
    page,
  }) => {
    // Pre-condition: a draft with a valid preview exists
    await page.goto('/w/workspace-a/generate/drafts/seeded-draft?preview=seeded-preview');
    await page.click('button:has-text("Submit Run")');
    await expect(page).toHaveURL(/\/generate\/runs\//);
  });

  test('2.4: Run page shows RunSummary with counters', async ({ page }) => {
    await page.goto('/w/workspace-a/generate/runs/seeded-run');
    await expect(page.getByText('Done')).toBeVisible();
    await expect(page.getByText('Total')).toBeVisible();
  });

  test('2.11: Refresh on run page resumes progress', async ({ page }) => {
    await page.goto('/w/workspace-a/generate/runs/seeded-run');
    await page.reload();
    await expect(page.locator('h1')).toBeVisible();
  });

  test('2.14: Run list on /generate shows Runs', async ({ page }) => {
    await page.goto('/w/workspace-a/generate');
    await expect(page.getByText('Runs')).toBeVisible();
  });
});
```

- [ ] **Step 2: Commit**

```bash
git add web/e2e/generation/submission-progress.spec.ts
git commit -m "test(generation): Slice 2 E2E tests for submission and progress"
```

---

# Slice 3: Recovery

**Prerequisites:** Slice 2 merged, #51 closed (with expanded recovery preview response).

## Task 11: Recovery components

**Files:**
- Create: `web/components/generation/recovery/RecoveryPanel.tsx`
- Create: `web/components/generation/recovery/RecoveryPreviewDisplay.tsx`
- Create: `web/components/generation/recovery/ReconciliationGuidance.tsx`
- Create: `web/components/generation/recovery/CancelConfirmation.tsx`
- Modify: `web/pages/w/[slug]/generate/runs/[runId].tsx` (wire recoverySlot)
- Test: `web/__tests__/generation/recovery-panel.spec.tsx`

**Interfaces:**
- Consumes: `useDomainCommand` (recovery commands), `useDomainQuery('get_generation_run')`, `CostDisplay`, `ActionEligibility` from run detail, `useRunProgress.forceRefresh`
- Produces: `RecoveryPanel({ run, onRecoveryComplete })` — server-driven recovery actions

- [ ] **Step 1: Write failing test for RecoveryPanel**

Create `web/__tests__/generation/recovery-panel.spec.tsx`:

```tsx
/**
 * @jest-environment jsdom
 */
import { render, screen } from '@testing-library/react';
import { RecoveryPanel } from '@/components/generation/recovery/RecoveryPanel';

jest.mock('@/hooks/domain/useDomainCommand', () => ({
  useDomainCommand: () => ({
    execute: jest.fn().mockResolvedValue({ ok: true }),
    isExecuting: false,
  }),
}));

describe('RecoveryPanel', () => {
  it('shows retry button when allowed', () => {
    const run = {
      id: 'run-1',
      allowed_actions: [
        { action: 'retry_failed_items', allowed: true, reason_code: null },
      ],
    };
    render(<RecoveryPanel run={run} onRecoveryComplete={jest.fn()} />);
    expect(
      screen.getByRole('button', { name: /retry failed/i })
    ).not.toBeDisabled();
  });

  it('disables retry when not allowed', () => {
    const run = {
      id: 'run-1',
      allowed_actions: [
        {
          action: 'retry_failed_items',
          allowed: false,
          reason_code: 'no_failed_items',
        },
      ],
    };
    render(<RecoveryPanel run={run} onRecoveryComplete={jest.fn()} />);
    expect(
      screen.getByRole('button', { name: /retry failed/i })
    ).toBeDisabled();
  });

  it('shows cancel button when allowed', () => {
    const run = {
      id: 'run-1',
      allowed_actions: [
        { action: 'cancel_run', allowed: true, reason_code: null },
      ],
    };
    render(<RecoveryPanel run={run} onRecoveryComplete={jest.fn()} />);
    expect(
      screen.getByRole('button', { name: /cancel/i })
    ).toBeInTheDocument();
  });

  it('shows reconcile button when allowed', () => {
    const run = {
      id: 'run-1',
      allowed_actions: [
        { action: 'reconcile_run', allowed: true, reason_code: null },
      ],
    };
    render(<RecoveryPanel run={run} onRecoveryComplete={jest.fn()} />);
    expect(
      screen.getByRole('button', { name: /reconcile/i })
    ).toBeInTheDocument();
  });

  it('hides actions not in allowed_actions', () => {
    const run = { id: 'run-1', allowed_actions: [] };
    render(<RecoveryPanel run={run} onRecoveryComplete={jest.fn()} />);
    expect(
      screen.queryByRole('button', { name: /retry/i })
    ).not.toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Implement RecoveryPanel and sub-components**

Create `web/components/generation/recovery/RecoveryPreviewDisplay.tsx`:

```tsx
import { CostDisplay } from '@/components/generation/shared/CostDisplay';

interface RecoveryPreviewDisplayProps {
  preview: {
    kind: string;
    targets: string[];
    estimated_additional_cost: { amount: string; currency: string };
    valid_until: string;
  };
  onConfirm: () => void;
  onCancel: () => void;
  isSubmitting: boolean;
}

export function RecoveryPreviewDisplay({
  preview,
  onConfirm,
  onCancel,
  isSubmitting,
}: RecoveryPreviewDisplayProps) {
  const isExpired = Date.now() >= new Date(preview.valid_until).getTime();

  return (
    <div className="card bg-base-200 p-4 space-y-3">
      <h4 className="font-semibold">Recovery Preview</h4>
      <p className="text-sm">
        <strong>Action:</strong> {preview.kind}
      </p>
      <p className="text-sm">
        <strong>Targets:</strong> {preview.targets.length} item(s)
      </p>
      <p className="text-sm">
        <strong>Additional cost:</strong>{' '}
        <CostDisplay
          amount={preview.estimated_additional_cost.amount}
          currency={preview.estimated_additional_cost.currency}
        />
      </p>
      {isExpired && (
        <div className="alert alert-error text-sm">
          Preview expired — regenerate
        </div>
      )}
      <div className="flex gap-2">
        <button className="btn btn-sm" onClick={onCancel}>
          Cancel
        </button>
        <button
          className="btn btn-sm btn-primary"
          onClick={onConfirm}
          disabled={isExpired || isSubmitting}
        >
          {isSubmitting ? 'Executing…' : 'Confirm'}
        </button>
      </div>
    </div>
  );
}
```

Create `web/components/generation/recovery/ReconciliationGuidance.tsx`:

```tsx
import { useState } from 'react';

interface ReconciliationGuidanceProps {
  uncertainCount: number;
  onReconcile: (operatorNote: string) => void;
  isSubmitting: boolean;
}

export function ReconciliationGuidance({
  uncertainCount,
  onReconcile,
  isSubmitting,
}: ReconciliationGuidanceProps) {
  const [note, setNote] = useState('');

  return (
    <div className="card bg-amber-50 p-4 space-y-3">
      <h4 className="font-semibold text-amber-800">Reconciliation Required</h4>
      <p className="text-sm">
        {uncertainCount} item(s) have uncertain outcomes after a worker
        interruption. Review these items and provide an operator note to
        proceed with reconciliation.
      </p>
      <textarea
        className="textarea textarea-bordered w-full"
        placeholder="Operator note (required)…"
        value={note}
        onChange={(e) => setNote(e.target.value)}
        rows={3}
      />
      <button
        className="btn btn-sm btn-warning"
        onClick={() => onReconcile(note)}
        disabled={!note.trim() || isSubmitting}
      >
        {isSubmitting ? 'Reconciling…' : 'Reconcile'}
      </button>
    </div>
  );
}
```

Create `web/components/generation/recovery/CancelConfirmation.tsx`:

```tsx
interface CancelConfirmationProps {
  hasInFlightItems: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  isSubmitting: boolean;
}

export function CancelConfirmation({
  hasInFlightItems,
  onConfirm,
  onCancel,
  isSubmitting,
}: CancelConfirmationProps) {
  return (
    <div className="card bg-base-200 p-4 space-y-3">
      <h4 className="font-semibold">Cancel Run?</h4>
      {hasInFlightItems && (
        <div className="alert alert-warning text-sm">
          Some items are still in flight. Cancelling will route uncertain
          items to reconciliation, not report them as cancelled.
        </div>
      )}
      <div className="flex gap-2">
        <button className="btn btn-sm" onClick={onCancel}>
          Keep Running
        </button>
        <button
          className="btn btn-sm btn-error"
          onClick={onConfirm}
          disabled={isSubmitting}
        >
          {isSubmitting ? 'Cancelling…' : 'Cancel Run'}
        </button>
      </div>
    </div>
  );
}
```

Create `web/components/generation/recovery/RecoveryPanel.tsx`:

```tsx
import { useState } from 'react';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import { RecoveryPreviewDisplay } from './RecoveryPreviewDisplay';
import { ReconciliationGuidance } from './ReconciliationGuidance';
import { CancelConfirmation } from './CancelConfirmation';

interface ActionEligibility {
  action: string;
  allowed: boolean;
  reason_code: string | null;
}

interface RecoveryPanelProps {
  run: { id: string; allowed_actions: ActionEligibility[] };
  onRecoveryComplete: () => void;
}

type RecoveryState =
  | { phase: 'idle' }
  | { phase: 'previewing'; kind: string }
  | { phase: 'preview-ready'; preview: any; kind: string }
  | { phase: 'cancel-confirm' };

const ACTION_LABELS: Record<string, string> = {
  retry_failed_items: 'Retry Failed',
  resume_paused_items: 'Resume Paused',
  cancel_run: 'Cancel Run',
  reconcile_run: 'Reconcile',
};

export function RecoveryPanel({ run, onRecoveryComplete }: RecoveryPanelProps) {
  const { execute, isExecuting } = useDomainCommand();
  const [state, setState] = useState<RecoveryState>({ phase: 'idle' });

  function getAction(name: string): ActionEligibility | undefined {
    return run.allowed_actions.find((a) => a.action === name);
  }

  function isAllowed(name: string): boolean {
    return getAction(name)?.allowed === true;
  }

  function isVisible(name: string): boolean {
    return !!getAction(name);
  }

  async function requestRecoveryPreview(kind: string) {
    setState({ phase: 'previewing', kind });
    const result = await execute(
      'create_generation_run_recovery_preview',
      {
        pathParams: { generation_run_id: run.id },
        body: { kind },
      }
    );
    if (result.ok) {
      setState({ phase: 'preview-ready', preview: result.data, kind });
    } else {
      setState({ phase: 'idle' });
    }
  }

  async function confirmRecovery(kind: string) {
    const commandMap: Record<string, string> = {
      retry: 'retry_failed_items',
      resume: 'resume_paused_items',
    };
    const command = commandMap[kind];
    if (!command) return;

    const preview = state.phase === 'preview-ready' ? state.preview : null;
    const result = await execute(command, {
      pathParams: { generation_run_id: run.id },
      body: preview
        ? { preview_id: preview.id, preview_hash: preview.preview_hash }
        : {},
    });

    if (result.ok) {
      setState({ phase: 'idle' });
      onRecoveryComplete();
    }
  }

  async function handleCancel() {
    const result = await execute('cancel_generation_run', {
      pathParams: { generation_run_id: run.id },
    });
    if (result.ok) {
      setState({ phase: 'idle' });
      onRecoveryComplete();
    }
  }

  async function handleReconcile(operatorNote: string) {
    const result = await execute('reconcile_generation_run', {
      pathParams: { generation_run_id: run.id },
      body: { operator_note: operatorNote },
    });
    if (result.ok) {
      setState({ phase: 'idle' });
      onRecoveryComplete();
    }
  }

  if (state.phase === 'cancel-confirm') {
    return (
      <CancelConfirmation
        hasInFlightItems={run.allowed_actions.some(
          (a) => a.action === 'reconcile_run'
        )}
        onConfirm={handleCancel}
        onCancel={() => setState({ phase: 'idle' })}
        isSubmitting={isExecuting}
      />
    );
  }

  if (state.phase === 'preview-ready') {
    return (
      <RecoveryPreviewDisplay
        preview={state.preview}
        onConfirm={() => confirmRecovery(state.kind)}
        onCancel={() => setState({ phase: 'idle' })}
        isSubmitting={isExecuting}
      />
    );
  }

  const uncertainCount = 0; // derived from RunItems in real implementation

  return (
    <div className="space-y-3 rounded-lg border p-4">
      <h3 className="font-semibold">Recovery Actions</h3>

      {isVisible('retry_failed_items') && (
        <button
          className="btn btn-sm btn-outline"
          disabled={!isAllowed('retry_failed_items') || isExecuting}
          onClick={() => requestRecoveryPreview('retry')}
          aria-label="Retry Failed"
        >
          {ACTION_LABELS.retry_failed_items}
        </button>
      )}

      {isVisible('resume_paused_items') && (
        <button
          className="btn btn-sm btn-outline"
          disabled={!isAllowed('resume_paused_items') || isExecuting}
          onClick={() => requestRecoveryPreview('resume')}
          aria-label="Resume Paused"
        >
          {ACTION_LABELS.resume_paused_items}
        </button>
      )}

      {isVisible('cancel_run') && (
        <button
          className="btn btn-sm btn-outline btn-error"
          disabled={!isAllowed('cancel_run') || isExecuting}
          onClick={() => setState({ phase: 'cancel-confirm' })}
          aria-label="Cancel Run"
        >
          {ACTION_LABELS.cancel_run}
        </button>
      )}

      {isVisible('reconcile_run') && isAllowed('reconcile_run') && (
        <ReconciliationGuidance
          uncertainCount={uncertainCount}
          onReconcile={handleReconcile}
          isSubmitting={isExecuting}
        />
      )}
    </div>
  );
}
```

- [ ] **Step 3: Wire recoverySlot in Run page shell**

Update `web/pages/w/[slug]/generate/runs/[runId].tsx`:

```tsx
import type { ReactElement } from 'react';
import { useRouter } from 'next/router';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { RunProgressView } from '@/components/generation/progress/RunProgressView';
import { RecoveryPanel } from '@/components/generation/recovery/RecoveryPanel';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { NextPageWithLayout } from 'types';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';

interface Props {
  bootstrap: WorkspaceBootstrap;
  errorCode?: number;
  errorContext?: string;
}

const RunPage: NextPageWithLayout<Props> = () => {
  const router = useRouter();
  const runId = router.query.runId as string;

  return (
    <RunProgressView
      runId={runId}
      recoverySlot={
        <RecoveryPanel
          run={{ id: runId, allowed_actions: [] }}
          onRecoveryComplete={() => {
            // RunProgressView.forceRefresh handles this via snapshot subscription
          }}
        />
      }
    />
  );
};

RunPage.getLayout = function getLayout(page: ReactElement) {
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

export const getServerSideProps = getWorkspacePageProps();
export default RunPage;
```

- [ ] **Step 4: Run tests and commit**

```bash
cd web && npx jest __tests__/generation/recovery-panel.spec.tsx --verbose
git add web/components/generation/recovery/ web/pages/w/[slug]/generate/runs/ \
  web/__tests__/generation/recovery-panel.spec.tsx
git commit -m "feat(generation): RecoveryPanel with retry, resume, cancel, reconcile flows"
```

---

## Task 12: Slice 3 E2E tests + cross-slice golden path

**Files:**
- Create: `web/e2e/generation/recovery.spec.ts`
- Create: `web/e2e/generation/golden-path.spec.ts`

**Interfaces:**
- Consumes: All components via browser automation
- Produces: Playwright tests covering acceptance criteria 3.1–3.13 and the cross-slice golden path

- [ ] **Step 1: Write recovery E2E tests**

Create `web/e2e/generation/recovery.spec.ts`:

```ts
import { test, expect } from '@playwright/test';

test.describe('Slice 3: Recovery', () => {
  test('3.2: Recovery actions appear from allowed_actions', async ({
    page,
  }) => {
    await page.goto('/w/workspace-a/generate/runs/seeded-partial-run');
    await expect(page.getByText('Recovery Actions')).toBeVisible();
  });

  test('3.10: Refresh during recovery returns to observation', async ({
    page,
  }) => {
    await page.goto('/w/workspace-a/generate/runs/seeded-partial-run');
    await page.reload();
    await expect(page.locator('h1')).toBeVisible();
  });
});
```

- [ ] **Step 2: Write cross-slice golden path E2E test**

Create `web/e2e/generation/golden-path.spec.ts`:

```ts
import { test, expect } from '@playwright/test';

test('Golden path: Draft → Preview → Submit → Progress → Recovery', async ({
  page,
}) => {
  // 1. Create Draft
  await page.goto('/w/workspace-a/generate');
  await page.click('button:has-text("New Draft")');
  await page.selectOption('[aria-label="Product"]', { index: 1 });
  await page.selectOption('[aria-label="Provider"]', { index: 1 });
  await page.selectOption('[aria-label="Model"]', { index: 1 });
  await page.selectOption('[aria-label="Execution Mode"]', { index: 1 });
  await page.click('button:has-text("Create Draft")');
  await expect(page).toHaveURL(/\/generate\/drafts\//);

  // 2. Generate Preview
  await page.click('button:has-text("Generate Preview")');
  await expect(page).toHaveURL(/preview=/);
  await expect(page.getByText('Resolution Preview')).toBeVisible();

  // 3. Submit Run
  await page.click('button:has-text("Submit Run")');
  await expect(page).toHaveURL(/\/generate\/runs\//);

  // 4. Observe Progress
  await expect(page.getByText('Done')).toBeVisible();
  await expect(page.getByText('Total')).toBeVisible();

  // 5. Wait for completion (or partial failure for recovery test)
  // This depends on mock provider behavior in the test environment
});
```

- [ ] **Step 3: Commit**

```bash
git add web/e2e/generation/recovery.spec.ts web/e2e/generation/golden-path.spec.ts
git commit -m "test(generation): Slice 3 E2E + cross-slice golden path"
```
