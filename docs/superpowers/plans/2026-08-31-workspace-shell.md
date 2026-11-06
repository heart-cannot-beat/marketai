# Authenticated Next.js Workspace Shell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the authenticated six-route Workspace shell, domain hooks, shared operational UX, upload/download, CSP enforcement, and Playwright evidence for Issue #26.

**Architecture:** Pages Router with `getLayout` pattern. A shared `getWorkspacePageProps` SSR loader validates session, slug, membership, and role before hydration. `WorkspaceLayout` persists across client navigation. All domain data flows through `DomainGateway` via same-origin BFF; three hooks (`useDomainQuery`, `useDomainCommand`, `useOperation`) hide SWR and intent mechanics. Upload/download are deep modules that hide three-phase intent flows. `StorageOriginPolicy` is the single source for both CSP `connect-src` and blob transfer origin validation.

**Tech Stack:** Next.js (Pages Router), React 18, SWR, TypeScript, Prisma, Tailwind CSS, daisyUI, NextAuth, Playwright, Jest

**Spec:** `docs/architecture/authenticated-nextjs-workspace-shell.md`

## Global Constraints

- No App Router, Server Actions, or `window.__mockApi`.
- Browser calls only `/api/domain/v1/w/{slug}/...` — never FastAPI directly.
- Domain hooks pass `workspaceSlug` to BFF; never trust `workspaceId` from context for authorization.
- `DomainGateway` is the only interface to the BFF; hooks never raw-`fetch`.
- Generated types from `web/lib/domain-client/generated/` are the type source for domain operations.
- Tests in `web/__tests__/` use Jest (default Node env; DOM tests use `@jest-environment jsdom`).
- Playwright tests in `web/e2e/` — never in `web/__tests__/` (Jest would collect them).
- No entitlement queries — `#18` is not yet done; only `CapabilityNotice` display shell.
- Imports use `@/` alias for `web/` root (e.g., `@/components/shared`).
- Existing hooks use `hooks/useTeam` path (no `@/` prefix) — follow existing convention per file location.

---

## File Map

### Create

| File | Responsibility |
|---|---|
| `web/lib/workspace-bootstrap.ts` | `WorkspaceBootstrap` type, `getWorkspacePageProps` SSR loader |
| `web/lib/workspace-navigator.ts` | Target route computation for Workspace switching |
| `web/components/layouts/WorkspaceLayout.tsx` | Persistent layout with sidebar, nav, selector, boundary |
| `web/components/workspace/WorkspaceProvider.tsx` | React context: current bootstrap, available list, switching |
| `web/components/workspace/WorkspaceSelector.tsx` | Native `<select>` workspace switcher |
| `web/components/workspace/WorkspaceNavigation.tsx` | Six-area nav + Owner-only admin items |
| `web/components/workspace/WorkspaceRouteBoundary.tsx` | Error boundary resetting on `router.asPath` |
| `web/components/workspace/ModeProvenance.tsx` | Provenance view model display |
| `web/components/workspace/OperationStatus.tsx` | Operation state rendering from caller props |
| `web/components/workspace/CapabilityNotice.tsx` | Pure display of caller-provided decision |
| `web/components/workspace/StaleIndicator.tsx` | Client cache age display |
| `web/components/workspace/ForbiddenState.tsx` | 403 page with trace_id |
| `web/components/workspace/FeatureEmptyState.tsx` | Extended EmptyState with action slot |
| `web/components/workspace/index.ts` | Barrel export |
| `web/hooks/domain/useDomainQuery.ts` | SWR-based typed read hook |
| `web/hooks/domain/useDomainCommand.ts` | Mutation hook with intent tracking |
| `web/hooks/domain/useOperation.ts` | Polling hook with ETag, Retry-After, sessionStorage |
| `web/hooks/domain/useUpload.ts` | Deep three-phase upload module |
| `web/hooks/domain/useDownload.ts` | Intent → validated URL → browser navigation |
| `web/hooks/domain/index.ts` | Barrel export |
| `web/lib/domain-client/file-digest.ts` | Streaming SHA-256 adapter interface + browser impl |
| `web/lib/domain-client/upload-deps.ts` | Context providers for BlobTransferClient, FileDigest injection |
| `web/lib/domain-client/gateway-provider.tsx` | DomainGateway React context for hook injection |
| `web/lib/domain-client/storage-origin-parser.ts` | `parseStorageOrigins` pure function |
| `web/pages/w/[slug]/index.tsx` | SSR redirect to `/w/{slug}/generate` |
| `web/pages/w/[slug]/generate.tsx` | New Generation shell |
| `web/pages/w/[slug]/assets.tsx` | Assets shell |
| `web/pages/w/[slug]/experiments.tsx` | Launch Experiments shell |
| `web/pages/w/[slug]/localization.tsx` | Localization shell |
| `web/pages/w/[slug]/scale.tsx` | Scale shell |
| `web/pages/w/[slug]/library.tsx` | Creative Library shell |
| `web/pages/w/[slug]/settings.tsx` | Owner-only Workspace settings |
| `web/pages/w/[slug]/members.tsx` | Owner-only member management |
| `web/e2e/workspace/*.spec.ts` | Six Playwright test files |
| `web/playwright.config.ts` | Playwright configuration |

### Modify

| File | Change |
|---|---|
| `web/models/team.ts` | `getTeams` adds `orderBy: [{ createdAt: 'asc' }, { id: 'asc' }]`, `getTeamsWithRole` returns role |
| `web/pages/api/teams/index.ts` | GET returns `role` per workspace, stable order |
| `web/pages/dashboard.tsx` | Replace `useEffect` redirect with `getServerSideProps` |
| `web/components/shared/shell/Navigation.tsx` | Route `/w/` slugs to `WorkspaceNavigation` |
| `web/components/shared/EmptyState.tsx` | Add optional `action` slot prop |
| `web/components/layouts/index.ts` | Export `WorkspaceLayout` |
| `web/middleware.ts` | `generateCSP` adds validated storage origins to `connect-src` |
| `web/lib/domain-client/storage-origin-policy.ts` | Add `parseStorageOrigins` |
| `web/package.json` | Add `@playwright/test`, `test:e2e` script |

---

### Task 1: WorkspaceBootstrap types, SSR loader, team list, and dashboard redirect

**Files:**
- Create: `web/lib/workspace-bootstrap.ts`
- Modify: `web/models/team.ts`
- Modify: `web/pages/api/teams/index.ts`
- Modify: `web/pages/dashboard.tsx`
- Test: `web/__tests__/lib/workspace-bootstrap.spec.ts`

**Interfaces:**
- Consumes: `requireSession` from `web/lib/requireSession.ts`, `getTeamMember` from `web/models/team.ts`, Prisma `Team`/`TeamMember` models, `serverSideTranslations` from `next-i18next`
- Produces:
  ```ts
  type WorkspaceBootstrap = {
    workspaceId: string;
    slug: string;
    displayName: string;
    membershipId: string;
    role: 'OWNER' | 'MEMBER';
  };

  type WorkspaceSummary = {
    workspaceId: string;
    slug: string;
    displayName: string;
    role: 'OWNER' | 'MEMBER';
  };

  function getWorkspacePageProps(options?: {
    ownerOnly?: boolean;
    namespaces?: string[];
  }): GetServerSideProps<{ bootstrap: WorkspaceBootstrap }>;
  ```

- [ ] **Step 1: Write SSR loader tests**

Create `web/__tests__/lib/workspace-bootstrap.spec.ts`:

```ts
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';

const mockRequireSession = jest.fn();
const mockGetTeamMember = jest.fn();
const mockServerSideTranslations = jest.fn().mockResolvedValue({});

jest.mock('@/lib/requireSession', () => ({
  getAuthenticatedSession: (...args: unknown[]) => mockRequireSession(...args),
}));
jest.mock('models/team', () => ({
  getTeamMember: (...args: unknown[]) => mockGetTeamMember(...args),
}));
jest.mock('next-i18next/serverSideTranslations', () => ({
  serverSideTranslations: (...args: unknown[]) => mockServerSideTranslations(...args),
}));

function mockContext(slug: string, locale = 'en') {
  return {
    params: { slug },
    locale,
    req: {} as any,
    res: {
      statusCode: 200,
      setHeader: jest.fn(),
    } as any,
    resolvedUrl: `/w/${slug}/generate`,
    query: { slug },
  };
}

describe('getWorkspacePageProps', () => {
  beforeEach(() => jest.clearAllMocks());

  it('redirects to login when no session', async () => {
    mockRequireSession.mockResolvedValue(null);
    const gssp = getWorkspacePageProps();
    const result = await gssp(mockContext('acme'));
    expect(result).toEqual({
      redirect: {
        destination: expect.stringContaining('/auth/login'),
        permanent: false,
      },
    });
  });

  it('returns 404 when workspace not found', async () => {
    mockRequireSession.mockResolvedValue({ user: { id: 'u1' } });
    mockGetTeamMember.mockRejectedValue(new Error('not found'));
    const gssp = getWorkspacePageProps();
    const result = await gssp(mockContext('nonexistent'));
    expect(result).toEqual({ notFound: true });
  });

  it('returns 403 props when user has no membership', async () => {
    mockRequireSession.mockResolvedValue({ user: { id: 'u1' } });
    mockGetTeamMember.mockResolvedValue(null);
    const gssp = getWorkspacePageProps();
    const result = await gssp(mockContext('acme'));
    expect((result as any).props.errorCode).toBe(403);
  });

  it('returns 403 when Member accesses ownerOnly page', async () => {
    mockRequireSession.mockResolvedValue({ user: { id: 'u1' } });
    mockGetTeamMember.mockResolvedValue({
      id: 'm1', teamId: 't1', userId: 'u1', role: 'MEMBER',
      team: { id: 't1', slug: 'acme', name: 'Acme' },
    });
    const gssp = getWorkspacePageProps({ ownerOnly: true });
    const result = await gssp(mockContext('acme'));
    expect((result as any).props.errorCode).toBe(403);
  });

  it('returns bootstrap for valid Owner', async () => {
    mockRequireSession.mockResolvedValue({ user: { id: 'u1' } });
    mockGetTeamMember.mockResolvedValue({
      id: 'm1', teamId: 't1', userId: 'u1', role: 'OWNER',
      team: { id: 't1', slug: 'acme', name: 'Acme' },
    });
    const gssp = getWorkspacePageProps();
    const result = await gssp(mockContext('acme'));
    expect((result as any).props.bootstrap).toEqual({
      workspaceId: 't1',
      slug: 'acme',
      displayName: 'Acme',
      membershipId: 'm1',
      role: 'OWNER',
    });
  });

  it('returns bootstrap for valid Member on non-ownerOnly page', async () => {
    mockRequireSession.mockResolvedValue({ user: { id: 'u1' } });
    mockGetTeamMember.mockResolvedValue({
      id: 'm2', teamId: 't1', userId: 'u1', role: 'MEMBER',
      team: { id: 't1', slug: 'acme', name: 'Acme' },
    });
    const gssp = getWorkspacePageProps();
    const result = await gssp(mockContext('acme'));
    expect((result as any).props.bootstrap).toEqual({
      workspaceId: 't1',
      slug: 'acme',
      displayName: 'Acme',
      membershipId: 'm2',
      role: 'MEMBER',
    });
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx jest __tests__/lib/workspace-bootstrap.spec.ts --no-cache`
Expected: FAIL — module `@/lib/workspace-bootstrap` not found.

- [ ] **Step 3: Implement WorkspaceBootstrap types and SSR loader**

Create `web/lib/workspace-bootstrap.ts`:

```ts
import type { GetServerSideProps, GetServerSidePropsContext } from 'next';
import { serverSideTranslations } from 'next-i18next/serverSideTranslations';
import { getAuthenticatedSession } from '@/lib/requireSession';
import { getTeamMember } from 'models/team';

export type WorkspaceBootstrap = {
  workspaceId: string;
  slug: string;
  displayName: string;
  membershipId: string;
  role: 'OWNER' | 'MEMBER';
};

export type WorkspaceSummary = {
  workspaceId: string;
  slug: string;
  displayName: string;
  role: 'OWNER' | 'MEMBER';
};

const SLUG_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$/;

export function getWorkspacePageProps(
  options: { ownerOnly?: boolean; namespaces?: string[] } = {}
): GetServerSideProps {
  const { ownerOnly = false, namespaces = ['common'] } = options;

  return async (context: GetServerSidePropsContext) => {
    const { req, res, params, locale } = context;
    const slug = params?.slug as string | undefined;

    // 1. Session
    const session = await getAuthenticatedSession(req, res);
    if (!session) {
      return {
        redirect: {
          destination: `/auth/login?callbackUrl=${encodeURIComponent(context.resolvedUrl)}`,
          permanent: false,
        },
      };
    }

    // 2. Slug shape
    if (!slug || !SLUG_PATTERN.test(slug)) {
      return { notFound: true };
    }

    // 3. Workspace + membership
    let member;
    try {
      member = await getTeamMember(session.user.id, slug);
    } catch {
      return { notFound: true };
    }

    if (!member) {
      const i18n = locale ? await serverSideTranslations(locale, namespaces) : {};
      return {
        props: { ...i18n, errorCode: 403, errorContext: 'no-membership' },
      };
    }

    // 4. Owner gate
    if (ownerOnly && member.role !== 'OWNER') {
      const i18n = locale ? await serverSideTranslations(locale, namespaces) : {};
      return {
        props: {
          ...i18n,
          errorCode: 403,
          errorContext: 'owner-only',
          bootstrap: null,
        },
      };
    }

    // 5. Success
    const bootstrap: WorkspaceBootstrap = {
      workspaceId: member.team.id,
      slug: member.team.slug,
      displayName: member.team.name,
      membershipId: member.id,
      role: member.role as 'OWNER' | 'MEMBER',
    };

    const i18n = locale ? await serverSideTranslations(locale, namespaces) : {};
    return { props: { ...i18n, bootstrap } };
  };
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npx jest __tests__/lib/workspace-bootstrap.spec.ts --no-cache`
Expected: All 6 tests PASS.

- [ ] **Step 5: Add stable ordering to team list model**

Modify `web/models/team.ts` — in `getTeams(userId)`:

```ts
// Before:
export const getTeams = async (userId: string) => {
  return await prisma.team.findMany({
    where: { members: { some: { userId } } },
    include: { _count: { select: { members: true } } },
  });
};

// After:
export const getTeams = async (userId: string) => {
  return await prisma.team.findMany({
    where: { members: { some: { userId } } },
    include: {
      _count: { select: { members: true } },
      members: {
        where: { userId },
        select: { role: true },
        take: 1,
      },
    },
    orderBy: [{ createdAt: 'asc' }, { id: 'asc' }],
  });
};
```

- [ ] **Step 6: Update teams API handler to return role**

Modify `web/pages/api/teams/index.ts` — in GET handler, map the response to include role:

```ts
// In GET handler, after fetching teams:
const teamsWithRole = teams.map((team) => ({
  ...team,
  members: undefined,
  role: team.members[0]?.role ?? 'MEMBER',
}));
res.status(200).json({ data: teamsWithRole });
```

- [ ] **Step 7: Replace dashboard.tsx with SSR redirect**

Replace `web/pages/dashboard.tsx`:

```tsx
import type { GetServerSidePropsContext } from 'next';
import { getAuthenticatedSession } from '@/lib/requireSession';
import { getTeams } from 'models/team';

export async function getServerSideProps({ req, res }: GetServerSidePropsContext) {
  const session = await getAuthenticatedSession(req, res);
  if (!session) {
    return { redirect: { destination: '/auth/login', permanent: false } };
  }

  const teams = await getTeams(session.user.id);
  if (teams.length > 0) {
    return {
      redirect: {
        destination: `/w/${teams[0].slug}/generate`,
        permanent: false,
      },
    };
  }

  return { redirect: { destination: '/teams', permanent: false } };
}

export default function Dashboard() {
  return null;
}
```

- [ ] **Step 8: Run all tests and commit**

Run: `cd web && npx jest __tests__/lib/workspace-bootstrap.spec.ts __tests__/api/ --no-cache`

```bash
git add web/lib/workspace-bootstrap.ts web/__tests__/lib/workspace-bootstrap.spec.ts \
  web/models/team.ts web/pages/api/teams/index.ts web/pages/dashboard.tsx
git commit -m "feat(web): workspace bootstrap SSR loader and stable team ordering

- WorkspaceBootstrap type and getWorkspacePageProps SSR loader
- Validates session, slug, membership, and role before hydration
- Teams API returns role per workspace with stable createdAt ordering
- Dashboard uses server-side redirect (no client-side flash)"
```

---

### Task 2: WorkspaceContext and WorkspaceNavigator

**Files:**
- Create: `web/components/workspace/WorkspaceProvider.tsx`
- Create: `web/lib/workspace-navigator.ts`
- Test: `web/__tests__/lib/workspace-navigator.spec.ts`

**Interfaces:**
- Consumes: `WorkspaceBootstrap`, `WorkspaceSummary` from Task 1; `useTeams` from `hooks/useTeams`; Next.js `useRouter`
- Produces:
  ```ts
  // WorkspaceProvider exports
  type WorkspaceContextValue = {
    current: WorkspaceBootstrap;
    available: WorkspaceSummary[];
    pendingSlug: string | null;
    switchWorkspace(slug: string): Promise<boolean>;
  };
  function useWorkspace(): WorkspaceContextValue;

  // WorkspaceNavigator exports
  function computeTargetRoute(params: {
    targetSlug: string;
    targetRole: 'OWNER' | 'MEMBER';
    currentPathname: string;
  }): string;
  ```

- [ ] **Step 1: Write WorkspaceNavigator tests**

Create `web/__tests__/lib/workspace-navigator.spec.ts`:

```ts
import { computeTargetRoute } from '@/lib/workspace-navigator';

describe('computeTargetRoute', () => {
  it('preserves feature area when switching workspaces', () => {
    expect(computeTargetRoute({
      targetSlug: 'beta',
      targetRole: 'OWNER',
      currentPathname: '/w/alpha/assets',
    })).toBe('/w/beta/assets');
  });

  it('preserves settings for Owner target', () => {
    expect(computeTargetRoute({
      targetSlug: 'beta',
      targetRole: 'OWNER',
      currentPathname: '/w/alpha/settings',
    })).toBe('/w/beta/settings');
  });

  it('falls back to /generate when Member targets Owner-only page', () => {
    expect(computeTargetRoute({
      targetSlug: 'beta',
      targetRole: 'MEMBER',
      currentPathname: '/w/alpha/settings',
    })).toBe('/w/beta/generate');
  });

  it('falls back to /generate when Member targets members page', () => {
    expect(computeTargetRoute({
      targetSlug: 'beta',
      targetRole: 'MEMBER',
      currentPathname: '/w/alpha/members',
    })).toBe('/w/beta/generate');
  });

  it('falls back to /generate for unknown path', () => {
    expect(computeTargetRoute({
      targetSlug: 'beta',
      targetRole: 'OWNER',
      currentPathname: '/teams/alpha/settings',
    })).toBe('/w/beta/generate');
  });

  it('handles all six feature areas', () => {
    const areas = ['generate', 'assets', 'experiments', 'localization', 'scale', 'library'];
    for (const area of areas) {
      expect(computeTargetRoute({
        targetSlug: 'x',
        targetRole: 'MEMBER',
        currentPathname: `/w/old/${area}`,
      })).toBe(`/w/x/${area}`);
    }
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx jest __tests__/lib/workspace-navigator.spec.ts --no-cache`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement WorkspaceNavigator**

Create `web/lib/workspace-navigator.ts`:

```ts
const FEATURE_AREAS = new Set([
  'generate', 'assets', 'experiments', 'localization', 'scale', 'library',
]);
const OWNER_ONLY_AREAS = new Set(['settings', 'members']);

export function computeTargetRoute(params: {
  targetSlug: string;
  targetRole: 'OWNER' | 'MEMBER';
  currentPathname: string;
}): string {
  const { targetSlug, targetRole, currentPathname } = params;
  const base = `/w/${targetSlug}`;

  const match = currentPathname.match(/^\/w\/[^/]+\/([^/?#]+)/);
  const currentArea = match?.[1];

  if (!currentArea) {
    return `${base}/generate`;
  }

  if (FEATURE_AREAS.has(currentArea)) {
    return `${base}/${currentArea}`;
  }

  if (OWNER_ONLY_AREAS.has(currentArea) && targetRole === 'OWNER') {
    return `${base}/${currentArea}`;
  }

  return `${base}/generate`;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npx jest __tests__/lib/workspace-navigator.spec.ts --no-cache`
Expected: All tests PASS.

- [ ] **Step 5: Implement WorkspaceProvider**

Create `web/components/workspace/WorkspaceProvider.tsx`:

```tsx
import { createContext, useCallback, useContext, useMemo, useState } from 'react';
import { useRouter } from 'next/router';
import useTeams from 'hooks/useTeams';
import { computeTargetRoute } from '@/lib/workspace-navigator';
import type { WorkspaceBootstrap, WorkspaceSummary } from '@/lib/workspace-bootstrap';

type WorkspaceContextValue = {
  current: WorkspaceBootstrap;
  available: WorkspaceSummary[];
  pendingSlug: string | null;
  switchWorkspace: (slug: string) => Promise<boolean>;
};

const WorkspaceContext = createContext<WorkspaceContextValue | null>(null);

export function useWorkspace(): WorkspaceContextValue {
  const ctx = useContext(WorkspaceContext);
  if (!ctx) throw new Error('useWorkspace must be used within WorkspaceProvider');
  return ctx;
}

export function WorkspaceProvider({
  bootstrap,
  children,
}: {
  bootstrap: WorkspaceBootstrap;
  children: React.ReactNode;
}) {
  const router = useRouter();
  const { teams } = useTeams();
  const [pendingSlug, setPendingSlug] = useState<string | null>(null);

  const available: WorkspaceSummary[] = useMemo(
    () =>
      (teams ?? []).map((t) => ({
        workspaceId: t.id,
        slug: t.slug,
        displayName: t.name,
        role: (t as any).role ?? 'MEMBER',
      })),
    [teams]
  );

  const switchWorkspace = useCallback(
    async (slug: string): Promise<boolean> => {
      if (slug === bootstrap.slug || pendingSlug) return false;

      const target = available.find((w) => w.slug === slug);
      if (!target) return false;

      setPendingSlug(slug);
      const targetUrl = computeTargetRoute({
        targetSlug: slug,
        targetRole: target.role,
        currentPathname: router.asPath,
      });

      try {
        await router.push(targetUrl);
        return true;
      } catch {
        return false;
      } finally {
        setPendingSlug(null);
      }
    },
    [bootstrap.slug, pendingSlug, available, router]
  );

  const value = useMemo(
    () => ({ current: bootstrap, available, pendingSlug, switchWorkspace }),
    [bootstrap, available, pendingSlug, switchWorkspace]
  );

  return (
    <WorkspaceContext.Provider value={value}>
      {children}
    </WorkspaceContext.Provider>
  );
}
```

- [ ] **Step 6: Commit**

```bash
git add web/lib/workspace-navigator.ts web/__tests__/lib/workspace-navigator.spec.ts \
  web/components/workspace/WorkspaceProvider.tsx
git commit -m "feat(web): workspace context provider and navigator

- WorkspaceProvider projects URL-sourced bootstrap into React context
- computeTargetRoute preserves feature area, falls back on role mismatch
- Switching sets pendingSlug, routes, clears on success or failure"
```

---

### Task 3: WorkspaceLayout, navigation, selector, route boundary, and nine route pages

**Files:**
- Create: `web/components/workspace/WorkspaceNavigation.tsx`
- Create: `web/components/workspace/WorkspaceSelector.tsx`
- Create: `web/components/workspace/WorkspaceRouteBoundary.tsx`
- Create: `web/components/layouts/WorkspaceLayout.tsx`
- Create: `web/components/workspace/index.ts`
- Create: `web/pages/w/[slug]/index.tsx` through `web/pages/w/[slug]/members.tsx` (9 files)
- Modify: `web/components/shared/shell/Navigation.tsx`
- Modify: `web/components/layouts/index.ts`
- Test: `web/__tests__/components/workspace/navigation.spec.tsx`

**Interfaces:**
- Consumes: `WorkspaceProvider`, `useWorkspace` from Task 2; `getWorkspacePageProps` from Task 1; `useCanAccess` from `hooks/useCanAccess`; `MenuItem` from `NavigationItems`
- Produces: Nine route pages that each use `WorkspaceLayout` via `getLayout`; `WorkspaceRouteBoundary` component

- [ ] **Step 1: Write WorkspaceNavigation test**

Create `web/__tests__/components/workspace/navigation.spec.tsx`:

```tsx
/**
 * @jest-environment jsdom
 */
import { render, screen } from '@testing-library/react';
import { WorkspaceNavigation } from '@/components/workspace/WorkspaceNavigation';

jest.mock('hooks/useCanAccess', () => ({
  __esModule: true,
  default: () => ({
    canAccess: (resource: string, actions: string[]) =>
      resource === 'team_member' && actions.includes('create'),
  }),
}));

jest.mock('next/router', () => ({
  useRouter: () => ({
    asPath: '/w/acme/assets',
    query: { slug: 'acme' },
  }),
}));

describe('WorkspaceNavigation', () => {
  it('renders six feature area links', () => {
    render(<WorkspaceNavigation slug="acme" activePathname="/w/acme/assets" />);
    expect(screen.getByText('New Generation')).toBeInTheDocument();
    expect(screen.getByText('Assets')).toBeInTheDocument();
    expect(screen.getByText('Experiments')).toBeInTheDocument();
    expect(screen.getByText('Localization')).toBeInTheDocument();
    expect(screen.getByText('Scale')).toBeInTheDocument();
    expect(screen.getByText('Creative Library')).toBeInTheDocument();
  });

  it('shows Owner-only items when canAccess returns true', () => {
    render(<WorkspaceNavigation slug="acme" activePathname="/w/acme/assets" />);
    expect(screen.getByText('Settings')).toBeInTheDocument();
    expect(screen.getByText('Members')).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx jest __tests__/components/workspace/navigation.spec.tsx --no-cache`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement WorkspaceNavigation**

Create `web/components/workspace/WorkspaceNavigation.tsx`:

```tsx
import {
  SparklesIcon,
  PhotoIcon,
  BeakerIcon,
  GlobeAltIcon,
  ArrowTrendingUpIcon,
  RectangleStackIcon,
  Cog6ToothIcon,
  UserPlusIcon,
} from '@heroicons/react/24/outline';
import useCanAccess from 'hooks/useCanAccess';
import NavigationItems from '@/components/shared/shell/NavigationItems';
import type { MenuItem } from '@/components/shared/shell/NavigationItems';

interface WorkspaceNavigationProps {
  slug: string;
  activePathname: string | null;
}

export function WorkspaceNavigation({ slug, activePathname }: WorkspaceNavigationProps) {
  const { canAccess } = useCanAccess();
  const isActive = (area: string) => activePathname === `/w/${slug}/${area}`;

  const menus: MenuItem[] = [
    { name: 'New Generation', href: `/w/${slug}/generate`, icon: SparklesIcon, active: isActive('generate') },
    { name: 'Assets', href: `/w/${slug}/assets`, icon: PhotoIcon, active: isActive('assets') },
    { name: 'Experiments', href: `/w/${slug}/experiments`, icon: BeakerIcon, active: isActive('experiments') },
    { name: 'Localization', href: `/w/${slug}/localization`, icon: GlobeAltIcon, active: isActive('localization') },
    { name: 'Scale', href: `/w/${slug}/scale`, icon: ArrowTrendingUpIcon, active: isActive('scale') },
    { name: 'Creative Library', href: `/w/${slug}/library`, icon: RectangleStackIcon, active: isActive('library') },
  ];

  if (canAccess('team_member', ['create', 'update', 'delete'])) {
    menus.push(
      { name: 'Settings', href: `/w/${slug}/settings`, icon: Cog6ToothIcon, active: isActive('settings') },
      { name: 'Members', href: `/w/${slug}/members`, icon: UserPlusIcon, active: isActive('members') },
    );
  }

  return <NavigationItems menus={menus} />;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npx jest __tests__/components/workspace/navigation.spec.tsx --no-cache`
Expected: PASS.

- [ ] **Step 5: Implement WorkspaceSelector**

Create `web/components/workspace/WorkspaceSelector.tsx`:

```tsx
import { useWorkspace } from './WorkspaceProvider';

export function WorkspaceSelector() {
  const { current, available, pendingSlug, switchWorkspace } = useWorkspace();

  return (
    <div className="px-2 py-3">
      <label htmlFor="workspace-selector" className="sr-only">
        Switch workspace
      </label>
      <select
        id="workspace-selector"
        value={pendingSlug ?? current.slug}
        onChange={(e) => switchWorkspace(e.target.value)}
        disabled={!!pendingSlug}
        className="select select-bordered select-sm w-full"
      >
        {available.map((w) => (
          <option key={w.workspaceId} value={w.slug}>
            {w.displayName}
          </option>
        ))}
      </select>
    </div>
  );
}
```

- [ ] **Step 6: Implement WorkspaceRouteBoundary**

Create `web/components/workspace/WorkspaceRouteBoundary.tsx`:

```tsx
import React from 'react';

interface Props {
  resetKey: string;
  children: React.ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export class WorkspaceRouteBoundary extends React.Component<Props, State> {
  state: State = { hasError: false, error: null };

  static getDerivedStateFromProps(props: Props, state: State) {
    if (state.hasError && props.resetKey !== (state as any).lastResetKey) {
      return { hasError: false, error: null, lastResetKey: props.resetKey };
    }
    return { lastResetKey: props.resetKey };
  }

  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error };
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="flex flex-col items-center justify-center py-16" role="alert">
          <h2 className="text-lg font-semibold">Something went wrong</h2>
          <p className="mt-2 text-sm text-gray-600">
            An error occurred while rendering this page.
          </p>
          <button
            className="btn btn-primary btn-sm mt-4"
            onClick={() => this.setState({ hasError: false, error: null })}
          >
            Try again
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
```

- [ ] **Step 7: Implement WorkspaceLayout**

Create `web/components/layouts/WorkspaceLayout.tsx`:

```tsx
import { useEffect, useRef } from 'react';
import { useRouter } from 'next/router';
import { SWRConfig } from 'swr';
import { WorkspaceProvider } from '@/components/workspace/WorkspaceProvider';
import { WorkspaceSelector } from '@/components/workspace/WorkspaceSelector';
import { WorkspaceNavigation } from '@/components/workspace/WorkspaceNavigation';
import { WorkspaceRouteBoundary } from '@/components/workspace/WorkspaceRouteBoundary';
import { ForbiddenState } from '@/components/workspace/ForbiddenState';
import Brand from '@/components/shared/shell/Brand';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';

interface WorkspaceLayoutProps {
  children: React.ReactNode;
  bootstrap: WorkspaceBootstrap | null;
  errorCode?: number;
  errorContext?: string;
}

function WorkspaceLayoutInner({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const slug = router.query.slug as string;
  const h1Ref = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    h1Ref.current?.focus();
  }, [router.asPath]);

  return (
    <div className="flex min-h-screen">
      {/* Desktop sidebar */}
      <div className="hidden lg:fixed lg:inset-y-0 lg:flex lg:w-64 lg:flex-col">
        <div className="flex grow flex-col gap-y-5 overflow-y-auto border-r border-gray-200 bg-white px-4 pb-4">
          <Brand />
          <WorkspaceSelector />
          <nav className="flex flex-1 flex-col">
            <WorkspaceNavigation slug={slug} activePathname={router.asPath} />
          </nav>
        </div>
      </div>

      {/* Main */}
      <div className="lg:pl-64 flex-1">
        <a href="#main-content" className="sr-only focus:not-sr-only focus:absolute focus:z-50 focus:p-4">
          Skip to content
        </a>
        <main id="main-content" className="py-5">
          <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
            <WorkspaceRouteBoundary resetKey={router.asPath}>
              {children}
            </WorkspaceRouteBoundary>
          </div>
        </main>
      </div>
    </div>
  );
}

export default function WorkspaceLayout({
  children,
  bootstrap,
  errorCode,
  errorContext,
}: WorkspaceLayoutProps) {
  if (errorCode === 403 || !bootstrap) {
    return (
      <SWRConfig value={{ revalidateOnFocus: false }}>
        <ForbiddenState
          context={errorContext as 'no-membership' | 'owner-only' | undefined}
        />
      </SWRConfig>
    );
  }

  // DomainGatewayProvider, BlobClientProvider, and FileDigestProvider are
  // wired in Task 4 (gateway-provider.tsx) and Task 8 (upload-deps.ts).
  // Once those tasks land, add them here wrapping WorkspaceProvider so all
  // domain hooks inside WorkspaceLayoutInner have access to DomainGateway.
  return (
    <SWRConfig value={{ revalidateOnFocus: false }}>
      <WorkspaceProvider bootstrap={bootstrap}>
        <WorkspaceLayoutInner>{children}</WorkspaceLayoutInner>
      </WorkspaceProvider>
    </SWRConfig>
  );
}
```

- [ ] **Step 8: Create ForbiddenState component**

Create `web/components/workspace/ForbiddenState.tsx`:

```tsx
import { useEffect, useRef } from 'react';

interface ForbiddenStateProps {
  context?: 'no-membership' | 'owner-only';
  traceId?: string;
}

export function ForbiddenState({ context, traceId }: ForbiddenStateProps) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => { headingRef.current?.focus(); }, []);

  const message = context === 'owner-only'
    ? 'This page is only accessible to workspace Owners.'
    : context === 'no-membership'
      ? "You don't have access to this workspace."
      : "You don't have access to this resource.";

  return (
    <div className="flex flex-col items-center justify-center py-16">
      <h1 ref={headingRef} tabIndex={-1} className="text-lg font-semibold outline-none">
        Access Denied
      </h1>
      <p className="mt-2 text-sm text-gray-600">{message}</p>
      {traceId && (
        <p className="mt-4 font-mono text-xs text-gray-400">Trace: {traceId}</p>
      )}
    </div>
  );
}
```

- [ ] **Step 9: Create nine route pages**

Create `web/pages/w/[slug]/index.tsx`:

```tsx
import type { GetServerSidePropsContext } from 'next';

export async function getServerSideProps({ params }: GetServerSidePropsContext) {
  return {
    redirect: {
      destination: `/w/${params?.slug}/generate`,
      permanent: false,
    },
  };
}

export default function WorkspaceIndex() {
  return null;
}
```

Create `web/pages/w/[slug]/generate.tsx` (template for all six feature pages):

```tsx
import type { ReactElement } from 'react';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { FeatureEmptyState } from '@/components/workspace';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { NextPageWithLayout } from 'types';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';

interface Props {
  bootstrap: WorkspaceBootstrap;
  errorCode?: number;
  errorContext?: string;
}

const GeneratePage: NextPageWithLayout<Props> = ({ bootstrap }) => {
  return (
    <>
      <h1 tabIndex={-1} className="text-2xl font-bold outline-none">
        New Generation
      </h1>
      <FeatureEmptyState
        title="No generations yet"
        description="Create your first AI-generated creative asset."
      />
    </>
  );
};

GeneratePage.getLayout = function getLayout(page: ReactElement) {
  const { bootstrap, errorCode, errorContext } = page.props as Props;
  return (
    <WorkspaceLayout bootstrap={bootstrap} errorCode={errorCode} errorContext={errorContext}>
      {page}
    </WorkspaceLayout>
  );
};

export const getServerSideProps = getWorkspacePageProps();
export default GeneratePage;
```

Create the remaining five feature pages (`assets.tsx`, `experiments.tsx`, `localization.tsx`, `scale.tsx`, `library.tsx`) following the identical pattern — change only the component name, `<h1>` text, and `FeatureEmptyState` title/description. Feature page headings:

| File | Component | h1 | Empty title |
|---|---|---|---|
| `assets.tsx` | `AssetsPage` | Assets | No assets yet |
| `experiments.tsx` | `ExperimentsPage` | Experiments | No experiments yet |
| `localization.tsx` | `LocalizationPage` | Localization | No localization tasks yet |
| `scale.tsx` | `ScalePage` | Scale | No scale monitoring yet |
| `library.tsx` | `LibraryPage` | Creative Library | Your library is empty |

Create Owner-only pages `web/pages/w/[slug]/settings.tsx`:

```tsx
import type { ReactElement } from 'react';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { TeamSettings } from '@/components/team';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { NextPageWithLayout } from 'types';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';
import useTeam from 'hooks/useTeam';
import { Loading, Error } from '@/components/shared';

interface Props {
  bootstrap: WorkspaceBootstrap;
  errorCode?: number;
  errorContext?: string;
}

const SettingsPage: NextPageWithLayout<Props> = () => {
  const { isLoading, isError, team } = useTeam();
  if (isLoading) return <Loading />;
  if (isError || !team) return <Error message="Could not load settings" />;

  return (
    <>
      <h1 tabIndex={-1} className="text-2xl font-bold outline-none">
        Workspace Settings
      </h1>
      <TeamSettings team={team} />
    </>
  );
};

SettingsPage.getLayout = function getLayout(page: ReactElement) {
  const { bootstrap, errorCode, errorContext } = page.props as Props;
  return (
    <WorkspaceLayout bootstrap={bootstrap} errorCode={errorCode} errorContext={errorContext}>
      {page}
    </WorkspaceLayout>
  );
};

export const getServerSideProps = getWorkspacePageProps({ ownerOnly: true });
export default SettingsPage;
```

Create `web/pages/w/[slug]/members.tsx` following the same pattern but using `Members` component with `ownerOnly: true`.

- [ ] **Step 10: Create FeatureEmptyState**

Modify `web/components/shared/EmptyState.tsx` — add optional `action` prop:

```tsx
// Add to existing props:
interface EmptyStateProps {
  title: string;
  description?: string;
  action?: React.ReactNode;
}

// In the render, after description:
{action && <div className="mt-4">{action}</div>}
```

Create `web/components/workspace/FeatureEmptyState.tsx`:

```tsx
import { EmptyState } from '@/components/shared';

interface FeatureEmptyStateProps {
  title: string;
  description: string;
  action?: React.ReactNode;
}

export function FeatureEmptyState({ title, description, action }: FeatureEmptyStateProps) {
  return (
    <div className="mt-8">
      <EmptyState title={title} description={description} action={action} />
    </div>
  );
}
```

- [ ] **Step 11: Create barrel export and update layout index**

Create `web/components/workspace/index.ts`:

```ts
export { WorkspaceProvider, useWorkspace } from './WorkspaceProvider';
export { WorkspaceSelector } from './WorkspaceSelector';
export { WorkspaceNavigation } from './WorkspaceNavigation';
export { WorkspaceRouteBoundary } from './WorkspaceRouteBoundary';
export { ForbiddenState } from './ForbiddenState';
export { FeatureEmptyState } from './FeatureEmptyState';
```

Add to `web/components/layouts/index.ts`:

```ts
export { default as WorkspaceLayout } from './WorkspaceLayout';
```

- [ ] **Step 12: Update Navigation.tsx to route /w/ slugs**

Modify `web/components/shared/shell/Navigation.tsx` — add the `/w/` path check:

```tsx
// In the existing conditional that checks slug:
const slug = router.query.slug as string | undefined;
const isWorkspaceRoute = router.asPath.startsWith('/w/');

if (isWorkspaceRoute && slug) {
  return <WorkspaceNavigation slug={slug} activePathname={router.asPath} />;
}
// ... existing TeamNavigation / UserNavigation logic
```

- [ ] **Step 13: Run tests and commit**

Run: `cd web && npx jest __tests__/components/workspace/ --no-cache && npx tsc --noEmit`

```bash
git add web/components/workspace/ web/components/layouts/WorkspaceLayout.tsx \
  web/components/layouts/index.ts web/components/shared/EmptyState.tsx \
  web/components/shared/shell/Navigation.tsx \
  web/pages/w/ web/__tests__/components/workspace/
git commit -m "feat(web): workspace layout, navigation, selector, and nine route pages

- WorkspaceLayout with persistent sidebar, six-area nav, selector
- WorkspaceRouteBoundary resets on router.asPath change
- ForbiddenState with context-aware messaging and focus management
- Nine route pages under /w/[slug]/ using getWorkspacePageProps SSR loader
- Owner-only settings/members enforced by SSR loader
- Skip-to-content link and route focus on h1"
```

---

### Task 4: useDomainQuery hook

**Files:**
- Create: `web/hooks/domain/useDomainQuery.ts`
- Create: `web/hooks/domain/index.ts`
- Test: `web/__tests__/hooks/domain/useDomainQuery.spec.ts`

**Interfaces:**
- Consumes: `useWorkspace` from Task 2; `DomainGateway` from `web/lib/domain-client/gateway.ts`; `DomainOperationMap`, `DomainResultFor`, `buildBffPath` from generated types; `ProblemDetails` from `result-types.ts`
- Produces:
  ```ts
  function useDomainQuery<K extends ReadOperationId>(
    operationId: K,
    params?: DomainOperationMap[K]['pathParams'] & DomainOperationMap[K]['queryParams'],
    options?: { staleAfterMs?: number; enabled?: boolean }
  ): {
    data: SuccessDataFor<K> | undefined;
    problem: ProblemDetails | undefined;
    isLoading: boolean;
    isStale: boolean;
    lastSuccessfulAt: number | null;
    refresh: () => Promise<void>;
  };
  ```

- [ ] **Step 1: Write useDomainQuery tests**

Create `web/__tests__/hooks/domain/useDomainQuery.spec.ts`:

```ts
/**
 * @jest-environment jsdom
 */
import { renderHook, waitFor } from '@testing-library/react';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';

const mockGateway = {
  getIdentityProbe: jest.fn(),
};

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: { slug: 'acme', workspaceId: 'w1', role: 'OWNER', membershipId: 'm1', displayName: 'Acme' },
  }),
}));

jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));

describe('useDomainQuery', () => {
  beforeEach(() => jest.clearAllMocks());

  it('returns data on successful fetch', async () => {
    mockGateway.getIdentityProbe.mockResolvedValue({
      kind: 'success',
      status: 200,
      data: { workspace_id: 'w1', user_id: 'u1' },
      meta: { etag: null, retryAt: null, location: null },
    });

    const { result } = renderHook(() => useDomainQuery('get_identity_probe'));
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    expect(result.current.data).toEqual({ workspace_id: 'w1', user_id: 'u1' });
    expect(result.current.problem).toBeUndefined();
  });

  it('returns problem on error response', async () => {
    mockGateway.getIdentityProbe.mockResolvedValue({
      kind: 'problem',
      status: 403,
      problem: { type: 'forbidden', title: 'Forbidden', status: 403 },
      meta: { etag: null, retryAt: null, location: null },
    });

    const { result } = renderHook(() => useDomainQuery('get_identity_probe'));
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    expect(result.current.data).toBeUndefined();
    expect(result.current.problem?.status).toBe(403);
  });

  it('tracks lastSuccessfulAt on success', async () => {
    mockGateway.getIdentityProbe.mockResolvedValue({
      kind: 'success', status: 200,
      data: { workspace_id: 'w1' },
      meta: { etag: null, retryAt: null, location: null },
    });

    const { result } = renderHook(() => useDomainQuery('get_identity_probe'));
    await waitFor(() => expect(result.current.lastSuccessfulAt).not.toBeNull());
    expect(result.current.lastSuccessfulAt).toBeGreaterThan(0);
  });

  it('does not fetch when enabled is false', () => {
    renderHook(() => useDomainQuery('get_identity_probe', undefined, { enabled: false }));
    expect(mockGateway.getIdentityProbe).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx jest __tests__/hooks/domain/useDomainQuery.spec.ts --no-cache`
Expected: FAIL — module not found.

- [ ] **Step 3: Create gateway provider**

Create `web/lib/domain-client/gateway-provider.tsx`:

```tsx
import { createContext, useContext } from 'react';
import type { DomainGateway } from './gateway';

const GatewayContext = createContext<DomainGateway | null>(null);

export function useDomainGateway(): DomainGateway {
  const gw = useContext(GatewayContext);
  if (!gw) throw new Error('useDomainGateway must be used within DomainGatewayProvider');
  return gw;
}

export function DomainGatewayProvider({
  gateway,
  children,
}: {
  gateway: DomainGateway;
  children: React.ReactNode;
}) {
  return <GatewayContext.Provider value={gateway}>{children}</GatewayContext.Provider>;
}
```

- [ ] **Step 4: Implement useDomainQuery**

Create `web/hooks/domain/useDomainQuery.ts`:

```ts
import { useCallback, useEffect, useRef, useState } from 'react';
import useSWR from 'swr';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import type { DomainOperationMap, ReadOperationId } from '@/lib/domain-client/generated/operation-map';
import type { DomainResultFor } from '@/lib/domain-client/result-types';
import type { ProblemDetails } from '@/lib/domain-client/result-types';

interface UseDomainQueryOptions {
  staleAfterMs?: number;
  enabled?: boolean;
}

export function useDomainQuery<K extends ReadOperationId>(
  operationId: K,
  params?: Record<string, string>,
  options: UseDomainQueryOptions = {}
) {
  const { staleAfterMs = 30_000, enabled = true } = options;
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const [lastSuccessfulAt, setLastSuccessfulAt] = useState<number | null>(null);
  const [isStale, setIsStale] = useState(false);
  const staleTimerRef = useRef<ReturnType<typeof setTimeout>>();

  const cacheKey = enabled
    ? ['domain', current.slug, operationId, JSON.stringify(params ?? {})]
    : null;

  const fetcher = useCallback(async () => {
    const result = await (gateway as any)[operationId]({
      workspaceSlug: current.slug,
      ...params,
    });
    return result;
  }, [gateway, operationId, current.slug, params]);

  const { data: rawResult, error: swrError, mutate } = useSWR<DomainResultFor<K>>(
    cacheKey,
    fetcher,
    { revalidateOnFocus: true }
  );

  useEffect(() => {
    if (rawResult && 'kind' in rawResult && rawResult.kind === 'success') {
      const now = Date.now();
      setLastSuccessfulAt(now);
      setIsStale(false);

      if (staleTimerRef.current) clearTimeout(staleTimerRef.current);
      staleTimerRef.current = setTimeout(() => setIsStale(true), staleAfterMs);
    }
    return () => {
      if (staleTimerRef.current) clearTimeout(staleTimerRef.current);
    };
  }, [rawResult, staleAfterMs]);

  const successData = rawResult && 'kind' in rawResult && rawResult.kind === 'success'
    ? (rawResult as any).data
    : undefined;

  const problem: ProblemDetails | undefined =
    rawResult && 'kind' in rawResult && rawResult.kind === 'problem'
      ? (rawResult as any).problem
      : undefined;

  const refresh = useCallback(async () => { await mutate(); }, [mutate]);

  return {
    data: successData,
    problem,
    isLoading: !rawResult && !swrError && enabled,
    isStale,
    lastSuccessfulAt,
    refresh,
  };
}
```

- [ ] **Step 5: Create barrel export**

Create `web/hooks/domain/index.ts`:

```ts
export { useDomainQuery } from './useDomainQuery';
```

- [ ] **Step 6: Run tests and commit**

Run: `cd web && npx jest __tests__/hooks/domain/useDomainQuery.spec.ts --no-cache`

```bash
git add web/hooks/domain/ web/__tests__/hooks/domain/useDomainQuery.spec.ts \
  web/lib/domain-client/gateway-provider.tsx
git commit -m "feat(web): useDomainQuery hook with SWR, stale tracking, workspace scoping

- Typed read hook using DomainGateway (never raw fetch)
- Cache keys scoped by workspace slug
- Stale timer tracks client cache age, not domain freshness
- refresh() hides SWR mutate; problem from result-types.ts"
```

---

### Task 5: useDomainCommand hook

**Files:**
- Create: `web/hooks/domain/useDomainCommand.ts`
- Test: `web/__tests__/hooks/domain/useDomainCommand.spec.ts`

**Interfaces:**
- Consumes: `useDomainGateway` from Task 4; `useWorkspace` from Task 2; `IntentTracker`, `ReconciliationRequired` from `web/lib/domain-client/intent.ts`
- Produces:
  ```ts
  function useDomainCommand<K extends CommandOperationId>(
    operationId: K,
    options?: {}
  ): {
    execute: (params: {
      body: DomainOperationMap[K]['requestBody'];
      pathParams?: Record<string, string>;
      queryParams?: Record<string, string>;
      actionInstanceId: string;
    }) => Promise<void>;
    retry: () => Promise<void>;
    reset: () => void;
    result: SuccessDataFor<K> | undefined;
    problem: ProblemDetails | undefined;
    reconciliation: ReconciliationRequired | undefined;
  };
  ```

- [ ] **Step 1: Write useDomainCommand tests**

Create `web/__tests__/hooks/domain/useDomainCommand.spec.ts`:

```ts
/**
 * @jest-environment jsdom
 */
import { renderHook, act } from '@testing-library/react';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';

const mockGateway = {
  createUploadIntent: jest.fn(),
};

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: { slug: 'acme', workspaceId: 'w1', role: 'OWNER', membershipId: 'm1', displayName: 'Acme' },
  }),
}));

jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));

describe('useDomainCommand', () => {
  beforeEach(() => jest.clearAllMocks());

  it('executes a command and returns result', async () => {
    mockGateway.createUploadIntent.mockResolvedValue({
      kind: 'success',
      status: 201,
      data: { upload_intent_id: 'ui1' },
      meta: {},
    });

    const { result } = renderHook(() => useDomainCommand('create_upload_intent'));

    await act(async () => {
      await result.current.execute({
        body: { owner_kind: 'product', owner_id: 'p1' } as any,
        actionInstanceId: 'test-action-1',
      });
    });

    expect(result.current.result).toEqual({ upload_intent_id: 'ui1' });
    expect(result.current.problem).toBeUndefined();
  });

  it('surfaces problem on error response', async () => {
    mockGateway.createUploadIntent.mockResolvedValue({
      kind: 'problem',
      status: 409,
      problem: { type: 'conflict', title: 'Conflict', status: 409 },
      meta: {},
    });

    const { result } = renderHook(() => useDomainCommand('create_upload_intent'));

    await act(async () => {
      await result.current.execute({
        body: {} as any,
        actionInstanceId: 'test-action-2',
      });
    });

    expect(result.current.result).toBeUndefined();
    expect(result.current.problem?.status).toBe(409);
  });

  it('surfaces reconciliation when returned', async () => {
    mockGateway.createUploadIntent.mockResolvedValue({
      kind: 'reconciliation_required',
      actionInstanceId: 'test-action-3',
      staleSince: 1234,
    });

    const { result } = renderHook(() => useDomainCommand('create_upload_intent'));

    await act(async () => {
      await result.current.execute({
        body: {} as any,
        actionInstanceId: 'test-action-3',
      });
    });

    expect(result.current.reconciliation).toBeDefined();
  });

  it('retry reuses the same actionInstanceId', async () => {
    mockGateway.createUploadIntent
      .mockRejectedValueOnce(new Error('network'))
      .mockResolvedValueOnce({ kind: 'success', status: 201, data: {}, meta: {} });

    const { result } = renderHook(() => useDomainCommand('create_upload_intent'));

    await act(async () => {
      await result.current.execute({
        body: {} as any,
        actionInstanceId: 'retry-test',
      }).catch(() => {});
    });

    await act(async () => {
      await result.current.retry();
    });

    const calls = mockGateway.createUploadIntent.mock.calls;
    expect(calls[0][0].actionInstanceId).toBe('retry-test');
    expect(calls[1][0].actionInstanceId).toBe('retry-test');
  });

  it('reset clears result and problem', async () => {
    mockGateway.createUploadIntent.mockResolvedValue({
      kind: 'success', status: 201, data: { id: '1' }, meta: {},
    });

    const { result } = renderHook(() => useDomainCommand('create_upload_intent'));

    await act(async () => {
      await result.current.execute({ body: {} as any, actionInstanceId: 'reset-test' });
    });
    expect(result.current.result).toBeDefined();

    act(() => result.current.reset());
    expect(result.current.result).toBeUndefined();
    expect(result.current.problem).toBeUndefined();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx jest __tests__/hooks/domain/useDomainCommand.spec.ts --no-cache`

- [ ] **Step 3: Implement useDomainCommand**

Create `web/hooks/domain/useDomainCommand.ts`:

```ts
import { useCallback, useRef, useState } from 'react';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import type { ReconciliationRequired } from '@/lib/domain-client/intent';

interface ExecuteParams {
  body: Record<string, unknown>;
  pathParams?: Record<string, string>;
  queryParams?: Record<string, string>;
  actionInstanceId: string;
}

export function useDomainCommand<K extends string>(operationId: K) {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();

  const [result, setResult] = useState<unknown>();
  const [problem, setProblem] = useState<ProblemDetails>();
  const [reconciliation, setReconciliation] = useState<ReconciliationRequired>();
  const lastParamsRef = useRef<ExecuteParams | null>(null);

  const execute = useCallback(
    async (params: ExecuteParams) => {
      lastParamsRef.current = params;
      setResult(undefined);
      setProblem(undefined);
      setReconciliation(undefined);

      const response = await (gateway as any)[operationId]({
        workspaceSlug: current.slug,
        actionInstanceId: params.actionInstanceId,
        body: params.body,
        ...params.pathParams,
        ...params.queryParams,
      });

      if (response.kind === 'success' || response.kind === 'accepted') {
        setResult(response.data);
      } else if (response.kind === 'problem') {
        setProblem(response.problem);
      } else if (response.kind === 'reconciliation_required') {
        setReconciliation(response);
      }
    },
    [gateway, operationId, current.slug]
  );

  const retry = useCallback(async () => {
    if (!lastParamsRef.current) return;
    await execute(lastParamsRef.current);
  }, [execute]);

  const reset = useCallback(() => {
    setResult(undefined);
    setProblem(undefined);
    setReconciliation(undefined);
    lastParamsRef.current = null;
  }, []);

  return { execute, retry, reset, result, problem, reconciliation };
}
```

- [ ] **Step 4: Update barrel export**

Add to `web/hooks/domain/index.ts`:

```ts
export { useDomainCommand } from './useDomainCommand';
```

- [ ] **Step 5: Run tests and commit**

Run: `cd web && npx jest __tests__/hooks/domain/useDomainCommand.spec.ts --no-cache`

```bash
git add web/hooks/domain/useDomainCommand.ts web/__tests__/hooks/domain/useDomainCommand.spec.ts \
  web/hooks/domain/index.ts
git commit -m "feat(web): useDomainCommand hook with intent tracking and retry

- Mutation hook using DomainGateway (never raw fetch or header manipulation)
- actionInstanceId stable across retries; DomainGateway handles X-Intent-Id
- Surfaces problem, reconciliation, and result as separate states
- reset() clears local display state only"
```

---

### Task 6: useOperation hook

**Files:**
- Create: `web/hooks/domain/useOperation.ts`
- Test: `web/__tests__/hooks/domain/useOperation.spec.ts`

**Interfaces:**
- Consumes: `useDomainGateway` from Task 4; `useWorkspace` from Task 2; `gateway.pollOperation()` from `DomainGateway`
- Produces:
  ```ts
  function useOperation(params: {
    operationRecordId: string | null;
    purpose: string;
    actionInstanceId: string;
  }): {
    operation: OperationData | undefined;
    problem: ProblemDetails | undefined;
    isTerminal: boolean;
    isPartialSuccess: boolean;
  };
  ```

- [ ] **Step 1: Write useOperation tests**

Create `web/__tests__/hooks/domain/useOperation.spec.ts`:

```ts
/**
 * @jest-environment jsdom
 */
import { renderHook, waitFor, act } from '@testing-library/react';
import { useOperation } from '@/hooks/domain/useOperation';

const mockGateway = {
  pollOperation: jest.fn(),
};

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: { slug: 'acme', workspaceId: 'w1', role: 'OWNER', membershipId: 'm1', displayName: 'Acme' },
  }),
}));

jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));

beforeEach(() => {
  jest.useFakeTimers();
  jest.clearAllMocks();
  sessionStorage.clear();
});

afterEach(() => jest.useRealTimers());

describe('useOperation', () => {
  it('does not poll when operationRecordId is null', () => {
    renderHook(() => useOperation({
      operationRecordId: null,
      purpose: 'upload',
      actionInstanceId: 'a1',
    }));
    expect(mockGateway.pollOperation).not.toHaveBeenCalled();
  });

  it('polls and returns running state', async () => {
    mockGateway.pollOperation.mockResolvedValue({
      kind: 'success', status: 200,
      data: { status: 'running', result_ref: null },
      meta: { etag: '"v1"', retryAt: null, location: null },
    });

    const { result } = renderHook(() => useOperation({
      operationRecordId: 'op1',
      purpose: 'upload',
      actionInstanceId: 'a1',
    }));

    await waitFor(() => expect(result.current.operation).toBeDefined());
    expect(result.current.isTerminal).toBe(false);
    expect(result.current.operation?.status).toBe('running');
  });

  it('stops polling on succeeded terminal state', async () => {
    mockGateway.pollOperation.mockResolvedValue({
      kind: 'success', status: 200,
      data: { status: 'succeeded', result_ref: 'r1' },
      meta: { etag: '"v2"', retryAt: null, location: null },
    });

    const { result } = renderHook(() => useOperation({
      operationRecordId: 'op2',
      purpose: 'upload',
      actionInstanceId: 'a2',
    }));

    await waitFor(() => expect(result.current.isTerminal).toBe(true));
    const callCount = mockGateway.pollOperation.mock.calls.length;
    jest.advanceTimersByTime(30_000);
    expect(mockGateway.pollOperation).toHaveBeenCalledTimes(callCount);
  });

  it('treats partially_succeeded as terminal', async () => {
    mockGateway.pollOperation.mockResolvedValue({
      kind: 'success', status: 200,
      data: { status: 'partially_succeeded', result_ref: 'r2' },
      meta: { etag: '"v3"', retryAt: null, location: null },
    });

    const { result } = renderHook(() => useOperation({
      operationRecordId: 'op3',
      purpose: 'gen',
      actionInstanceId: 'a3',
    }));

    await waitFor(() => expect(result.current.isTerminal).toBe(true));
    expect(result.current.isPartialSuccess).toBe(true);
  });

  it('continues polling on uncertain state', async () => {
    mockGateway.pollOperation.mockResolvedValue({
      kind: 'success', status: 200,
      data: { status: 'uncertain', result_ref: null },
      meta: { etag: '"v4"', retryAt: null, location: null },
    });

    const { result } = renderHook(() => useOperation({
      operationRecordId: 'op4',
      purpose: 'gen',
      actionInstanceId: 'a4',
    }));

    await waitFor(() => expect(result.current.operation).toBeDefined());
    expect(result.current.isTerminal).toBe(false);
  });

  it('persists operation to sessionStorage for refresh recovery', async () => {
    mockGateway.pollOperation.mockResolvedValue({
      kind: 'success', status: 200,
      data: { status: 'running', result_ref: null },
      meta: { etag: '"v5"', retryAt: null, location: null },
    });

    renderHook(() => useOperation({
      operationRecordId: 'op5',
      purpose: 'upload',
      actionInstanceId: 'recovery-test',
    }));

    await waitFor(() =>
      expect(sessionStorage.getItem('operation:acme:recovery-test')).toBe('op5')
    );
  });

  it('respects Retry-After from server', async () => {
    mockGateway.pollOperation.mockResolvedValue({
      kind: 'success', status: 200,
      data: { status: 'running', result_ref: null },
      meta: { etag: '"v6"', retryAt: 10, location: null },
    });

    renderHook(() => useOperation({
      operationRecordId: 'op6',
      purpose: 'gen',
      actionInstanceId: 'a6',
    }));

    await waitFor(() => expect(mockGateway.pollOperation).toHaveBeenCalledTimes(1));
    jest.advanceTimersByTime(5_000);
    expect(mockGateway.pollOperation).toHaveBeenCalledTimes(1);
    jest.advanceTimersByTime(6_000);
    expect(mockGateway.pollOperation).toHaveBeenCalledTimes(2);
  });

  it('sends ETag as If-None-Match on subsequent polls', async () => {
    mockGateway.pollOperation
      .mockResolvedValueOnce({
        kind: 'success', status: 200,
        data: { status: 'running', result_ref: null },
        meta: { etag: '"first"', retryAt: 1, location: null },
      })
      .mockResolvedValueOnce({
        kind: 'not_modified', status: 304, data: null,
        meta: { etag: '"first"', retryAt: 1, location: null },
      });

    renderHook(() => useOperation({
      operationRecordId: 'op7',
      purpose: 'gen',
      actionInstanceId: 'a7',
    }));

    await waitFor(() => expect(mockGateway.pollOperation).toHaveBeenCalledTimes(1));
    jest.advanceTimersByTime(2_000);
    await waitFor(() => expect(mockGateway.pollOperation).toHaveBeenCalledTimes(2));

    expect(mockGateway.pollOperation.mock.calls[1][0].ifNoneMatch).toBe('"first"');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx jest __tests__/hooks/domain/useOperation.spec.ts --no-cache`

- [ ] **Step 3: Implement useOperation**

Create `web/hooks/domain/useOperation.ts`:

```ts
import { useCallback, useEffect, useRef, useState } from 'react';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import type { ProblemDetails } from '@/lib/domain-client/result-types';

const TERMINAL_STATES = new Set(['succeeded', 'failed', 'cancelled', 'partially_succeeded']);
const DEFAULT_INTERVALS = [1000, 2000, 3000, 5000, 10000, 15000];

interface OperationData {
  status: string;
  result_ref: string | null;
  [key: string]: unknown;
}

interface UseOperationParams {
  operationRecordId: string | null;
  purpose: string;
  actionInstanceId: string;
}

export function useOperation({ operationRecordId, purpose, actionInstanceId }: UseOperationParams) {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();

  const [operation, setOperation] = useState<OperationData>();
  const [problem, setProblem] = useState<ProblemDetails>();
  const etagRef = useRef<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout>>();
  const pollCountRef = useRef(0);
  const mountedRef = useRef(true);

  const storageKey = `operation:${current.slug}:${actionInstanceId}`;

  useEffect(() => {
    if (operationRecordId) {
      try { sessionStorage.setItem(storageKey, operationRecordId); } catch {}
    }
  }, [storageKey, operationRecordId]);

  const poll = useCallback(async () => {
    if (!operationRecordId || !mountedRef.current) return;

    try {
      const result = await gateway.pollOperation({
        workspaceSlug: current.slug,
        operationId: operationRecordId,
        ifNoneMatch: etagRef.current ?? undefined,
      });

      if (!mountedRef.current) return;

      if (result.kind === 'not_modified') {
        scheduleNext(result.meta?.retryAt);
        return;
      }

      if (result.kind === 'success') {
        setOperation(result.data as OperationData);
        etagRef.current = result.meta?.etag ?? null;

        if (!TERMINAL_STATES.has((result.data as OperationData).status)) {
          scheduleNext(result.meta?.retryAt);
        }
      } else if (result.kind === 'problem') {
        setProblem(result.problem as ProblemDetails);
      }
    } catch {
      if (mountedRef.current) {
        scheduleNext(null);
      }
    }
  }, [operationRecordId, gateway, current.slug]);

  function scheduleNext(retryAtSeconds: number | null | undefined) {
    if (!mountedRef.current) return;
    const delayMs = retryAtSeconds
      ? retryAtSeconds * 1000
      : DEFAULT_INTERVALS[Math.min(pollCountRef.current, DEFAULT_INTERVALS.length - 1)];
    pollCountRef.current++;
    timerRef.current = setTimeout(poll, delayMs);
  }

  useEffect(() => {
    mountedRef.current = true;
    if (operationRecordId) {
      poll();
    }
    return () => {
      mountedRef.current = false;
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [operationRecordId, poll]);

  const isTerminal = operation ? TERMINAL_STATES.has(operation.status) : false;
  const isPartialSuccess = operation?.status === 'partially_succeeded';

  return { operation, problem, isTerminal, isPartialSuccess };
}
```

- [ ] **Step 4: Update barrel export and run tests**

Add to `web/hooks/domain/index.ts`:

```ts
export { useOperation } from './useOperation';
```

Run: `cd web && npx jest __tests__/hooks/domain/useOperation.spec.ts --no-cache`

- [ ] **Step 5: Commit**

```bash
git add web/hooks/domain/useOperation.ts web/__tests__/hooks/domain/useOperation.spec.ts \
  web/hooks/domain/index.ts
git commit -m "feat(web): useOperation polling hook with ETag, Retry-After, and recovery

- Polls via gateway.pollOperation() with If-None-Match
- Respects server Retry-After, falls back to client backoff
- Terminal: succeeded, failed, cancelled, partially_succeeded
- Non-terminal: queued, running, uncertain (keeps polling)
- sessionStorage persistence keyed by [slug, actionInstanceId]
- Cleans up timers and ignores late responses on unmount"
```

---

### Task 7: Shared operational UX components

**Files:**
- Create: `web/components/workspace/ModeProvenance.tsx`
- Create: `web/components/workspace/OperationStatus.tsx`
- Create: `web/components/workspace/CapabilityNotice.tsx`
- Create: `web/components/workspace/StaleIndicator.tsx`
- Test: `web/__tests__/components/workspace/operational-ux.spec.tsx`

**Interfaces:**
- Consumes: None (all receive normalized view models from callers)
- Produces:
  ```ts
  // ModeProvenance
  type ProvenanceViewModel = {
    mode: 'real' | 'real-read-only' | 'sandbox' | 'controlled-mock';
    provider?: string;
    model?: string;
    mockScenario?: string;
    correlationId?: string;
  };

  // OperationStatus
  type OperationStatusProps = {
    operation: { status: string; result_ref?: string | null } | undefined;
    pollingProblem?: ProblemDetails;
    outcomeSummary?: React.ReactNode;
    actions?: React.ReactNode;
  };

  // CapabilityNotice
  type CapabilityDecision = 'allowed' | 'not-available' | 'limit-reached' | 'requires-upgrade';
  ```

- [ ] **Step 1: Write operational UX component tests**

Create `web/__tests__/components/workspace/operational-ux.spec.tsx`:

```tsx
/**
 * @jest-environment jsdom
 */
import { render, screen } from '@testing-library/react';
import { ModeProvenance } from '@/components/workspace/ModeProvenance';
import { OperationStatus } from '@/components/workspace/OperationStatus';
import { CapabilityNotice } from '@/components/workspace/CapabilityNotice';
import { StaleIndicator } from '@/components/workspace/StaleIndicator';

describe('ModeProvenance', () => {
  it('renders controlled-mock badge', () => {
    render(<ModeProvenance provenance={{ mode: 'controlled-mock', mockScenario: 'happy-path' }} />);
    expect(screen.getByText(/controlled mock/i)).toBeInTheDocument();
    expect(screen.getByText(/happy-path/i)).toBeInTheDocument();
  });

  it('renders real mode without extra badges', () => {
    render(<ModeProvenance provenance={{ mode: 'real', provider: 'OpenAI' }} />);
    expect(screen.queryByText(/mock/i)).not.toBeInTheDocument();
    expect(screen.getByText(/OpenAI/i)).toBeInTheDocument();
  });
});

describe('OperationStatus', () => {
  it('shows spinner for queued state', () => {
    render(<OperationStatus operation={{ status: 'queued' }} />);
    expect(screen.getByText(/queued/i)).toBeInTheDocument();
  });

  it('shows running state', () => {
    render(<OperationStatus operation={{ status: 'running' }} />);
    expect(screen.getByText(/running/i)).toBeInTheDocument();
  });

  it('shows uncertain without claiming success or failure', () => {
    render(<OperationStatus operation={{ status: 'uncertain' }} />);
    expect(screen.getByText(/checking/i)).toBeInTheDocument();
    expect(screen.queryByText(/success/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/failed/i)).not.toBeInTheDocument();
  });

  it('renders caller-provided outcomeSummary for partially_succeeded', () => {
    render(
      <OperationStatus
        operation={{ status: 'partially_succeeded' }}
        outcomeSummary={<span>3 of 5 completed</span>}
      />
    );
    expect(screen.getByText('3 of 5 completed')).toBeInTheDocument();
  });

  it('renders caller-provided actions for failed state', () => {
    render(
      <OperationStatus
        operation={{ status: 'failed' }}
        actions={<button>Retry with new intent</button>}
      />
    );
    expect(screen.getByText('Retry with new intent')).toBeInTheDocument();
  });

  it('shows polling problem separately from operation failure', () => {
    render(
      <OperationStatus
        operation={{ status: 'running' }}
        pollingProblem={{ type: 'network', title: 'Network error', status: 0 } as any}
      />
    );
    expect(screen.getByText(/running/i)).toBeInTheDocument();
    expect(screen.getByText(/network error/i)).toBeInTheDocument();
  });

  it('uses aria-live polite for state changes', () => {
    const { container } = render(<OperationStatus operation={{ status: 'running' }} />);
    expect(container.querySelector('[aria-live="polite"]')).toBeInTheDocument();
  });

  it('uses role=alert for failed state', () => {
    const { container } = render(<OperationStatus operation={{ status: 'failed' }} />);
    expect(container.querySelector('[role="alert"]')).toBeInTheDocument();
  });
});

describe('CapabilityNotice', () => {
  it('renders not-available decision', () => {
    render(<CapabilityNotice decision="not-available" message="This feature requires a higher plan." />);
    expect(screen.getByText(/not available/i)).toBeInTheDocument();
    expect(screen.getByText(/higher plan/i)).toBeInTheDocument();
  });
});

describe('StaleIndicator', () => {
  it('renders nothing when not stale', () => {
    const { container } = render(
      <StaleIndicator isStale={false} lastSuccessfulAt={Date.now()} />
    );
    expect(container.textContent).toBe('');
  });

  it('renders age when stale', () => {
    const thirtySecsAgo = Date.now() - 30_000;
    render(<StaleIndicator isStale={true} lastSuccessfulAt={thirtySecsAgo} />);
    expect(screen.getByText(/loaded.*ago/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx jest __tests__/components/workspace/operational-ux.spec.tsx --no-cache`

- [ ] **Step 3: Implement ModeProvenance**

Create `web/components/workspace/ModeProvenance.tsx`:

```tsx
export interface ProvenanceViewModel {
  mode: 'real' | 'real-read-only' | 'sandbox' | 'controlled-mock';
  provider?: string;
  model?: string;
  mockScenario?: string;
  correlationId?: string;
}

const MODE_LABELS: Record<string, { label: string; className: string }> = {
  'real': { label: 'Production', className: 'badge-success' },
  'real-read-only': { label: 'Read-only', className: 'badge-info' },
  'sandbox': { label: 'Sandbox', className: 'badge-warning' },
  'controlled-mock': { label: 'Controlled Mock', className: 'badge-error' },
};

export function ModeProvenance({ provenance }: { provenance: ProvenanceViewModel }) {
  const config = MODE_LABELS[provenance.mode] ?? MODE_LABELS['real'];
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs">
      <span className={`badge badge-sm ${config.className}`}>{config.label}</span>
      {provenance.provider && <span className="text-gray-500">{provenance.provider}</span>}
      {provenance.model && <span className="text-gray-500">{provenance.model}</span>}
      {provenance.mockScenario && (
        <span className="text-gray-400 italic">{provenance.mockScenario}</span>
      )}
      {provenance.correlationId && (
        <span className="font-mono text-gray-300">{provenance.correlationId}</span>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Implement OperationStatus**

Create `web/components/workspace/OperationStatus.tsx`:

```tsx
import type { ProblemDetails } from '@/lib/domain-client/result-types';

interface OperationStatusProps {
  operation: { status: string; result_ref?: string | null } | undefined;
  pollingProblem?: ProblemDetails;
  outcomeSummary?: React.ReactNode;
  actions?: React.ReactNode;
}

const STATUS_CONFIG: Record<string, { label: string; icon: string; variant: string }> = {
  queued: { label: 'Queued', icon: '⏳', variant: 'text-gray-500' },
  running: { label: 'Running', icon: '⚙️', variant: 'text-blue-600' },
  succeeded: { label: 'Succeeded', icon: '✓', variant: 'text-green-600' },
  partially_succeeded: { label: 'Partially succeeded', icon: '⚠', variant: 'text-amber-600' },
  failed: { label: 'Failed', icon: '✕', variant: 'text-red-600' },
  cancelled: { label: 'Cancelled', icon: '—', variant: 'text-gray-500' },
  uncertain: { label: 'Result unknown, checking...', icon: '?', variant: 'text-amber-500' },
};

export function OperationStatus({
  operation,
  pollingProblem,
  outcomeSummary,
  actions,
}: OperationStatusProps) {
  if (!operation) return null;

  const config = STATUS_CONFIG[operation.status] ?? STATUS_CONFIG.running;
  const isFailed = operation.status === 'failed';

  const Wrapper = isFailed ? 'div' : 'div';
  const wrapperProps = isFailed
    ? { role: 'alert' as const }
    : { 'aria-live': 'polite' as const };

  return (
    <div {...wrapperProps} className="rounded-lg border p-4">
      <div className={`flex items-center gap-2 font-medium ${config.variant}`}>
        <span aria-hidden="true">{config.icon}</span>
        <span>{config.label}</span>
      </div>

      {outcomeSummary && <div className="mt-2 text-sm">{outcomeSummary}</div>}

      {pollingProblem && (
        <div className="mt-2 text-sm text-amber-600">
          <span>Connection issue: </span>
          <span>{pollingProblem.title ?? 'Network error'}</span>
        </div>
      )}

      {actions && <div className="mt-3">{actions}</div>}
    </div>
  );
}
```

- [ ] **Step 5: Implement CapabilityNotice and StaleIndicator**

Create `web/components/workspace/CapabilityNotice.tsx`:

```tsx
export type CapabilityDecision = 'allowed' | 'not-available' | 'limit-reached' | 'requires-upgrade';

interface CapabilityNoticeProps {
  decision: CapabilityDecision;
  message?: string;
}

const DECISION_CONFIG: Record<CapabilityDecision, { label: string; className: string }> = {
  'allowed': { label: 'Available', className: 'alert-success' },
  'not-available': { label: 'Not available', className: 'alert-warning' },
  'limit-reached': { label: 'Limit reached', className: 'alert-error' },
  'requires-upgrade': { label: 'Upgrade required', className: 'alert-info' },
};

export function CapabilityNotice({ decision, message }: CapabilityNoticeProps) {
  if (decision === 'allowed') return null;
  const config = DECISION_CONFIG[decision];
  return (
    <div className={`alert ${config.className}`}>
      <span className="font-medium">{config.label}</span>
      {message && <span className="text-sm">{message}</span>}
    </div>
  );
}
```

Create `web/components/workspace/StaleIndicator.tsx`:

```tsx
import { useEffect, useRef, useState } from 'react';

interface StaleIndicatorProps {
  isStale: boolean;
  lastSuccessfulAt: number | null;
}

function formatAge(ms: number): string {
  const secs = Math.floor(ms / 1000);
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m`;
  return `${Math.floor(mins / 60)}h`;
}

export function StaleIndicator({ isStale, lastSuccessfulAt }: StaleIndicatorProps) {
  const [age, setAge] = useState('');
  const announcedRef = useRef(false);

  useEffect(() => {
    if (!isStale || !lastSuccessfulAt) {
      announcedRef.current = false;
      return;
    }
    const update = () => setAge(formatAge(Date.now() - lastSuccessfulAt));
    update();
    const timer = setInterval(update, 10_000);
    return () => clearInterval(timer);
  }, [isStale, lastSuccessfulAt]);

  if (!isStale || !lastSuccessfulAt) return null;

  return (
    <span
      className="text-xs text-gray-400"
      aria-live={announcedRef.current ? 'off' : 'polite'}
      ref={() => { announcedRef.current = true; }}
    >
      Loaded {age} ago
    </span>
  );
}
```

- [ ] **Step 6: Update barrel export and run tests**

Add to `web/components/workspace/index.ts`:

```ts
export { ModeProvenance } from './ModeProvenance';
export type { ProvenanceViewModel } from './ModeProvenance';
export { OperationStatus } from './OperationStatus';
export { CapabilityNotice } from './CapabilityNotice';
export type { CapabilityDecision } from './CapabilityNotice';
export { StaleIndicator } from './StaleIndicator';
```

Run: `cd web && npx jest __tests__/components/workspace/operational-ux.spec.tsx --no-cache`

- [ ] **Step 7: Commit**

```bash
git add web/components/workspace/ web/__tests__/components/workspace/operational-ux.spec.tsx
git commit -m "feat(web): shared operational UX components

- ModeProvenance: displays provenance view model with mode badge
- OperationStatus: renders caller-provided summary and actions per state
- CapabilityNotice: pure display of entitlement decision (#18 provides resolution)
- StaleIndicator: client cache age only, announces once at stale threshold
- All use aria-live/role=alert as appropriate"
```

---

### Task 8: FileDigest adapter and useUpload

**Files:**
- Create: `web/lib/domain-client/file-digest.ts`
- Create: `web/hooks/domain/useUpload.ts`
- Test: `web/__tests__/domain-client/file-digest.spec.ts`
- Test: `web/__tests__/hooks/domain/useUpload.spec.ts`

**Interfaces:**
- Consumes: `DomainGateway.createUploadIntent`, `DomainGateway.finalizeUpload` from gateway; `BrowserBlobTransferClient.upload` from blob-transfer; `useWorkspace` from Task 2
- Produces:
  ```ts
  // FileDigest
  interface FileDigest {
    computeSha256(file: File, onProgress?: (fraction: number) => void): Promise<string>;
  }

  // useUpload
  type UploadState = 'idle' | 'hashing' | 'requesting_grant' | 'transferring'
    | 'finalizing' | 'awaiting_verification';

  function useUpload(purpose: string): {
    startUpload: (params: {
      file: File;
      ownerKind: string;
      ownerId: string;
      actionInstanceId: string;
    }) => Promise<UploadStartResult>;
    state: UploadState;
    hashProgress: number;
  };
  ```

- [ ] **Step 1: Write FileDigest tests**

Create `web/__tests__/domain-client/file-digest.spec.ts`:

```ts
import { BrowserFileDigest, type FileDigest } from '@/lib/domain-client/file-digest';

describe('FileDigest', () => {
  it('computes SHA-256 base64 for a file', async () => {
    const content = new TextEncoder().encode('hello world');
    const file = new File([content], 'test.txt', { type: 'text/plain' });
    const digest = new BrowserFileDigest(1024);
    const hash = await digest.computeSha256(file);
    // SHA-256 of 'hello world' in base64
    expect(hash).toBe('uU0nuZNNPgilLlLX2n2r+sSE7+N6U4DukIj3rOLvzek=');
  });

  it('produces the same digest for the same file', async () => {
    const file = new File(['deterministic'], 'same.txt');
    const digest = new BrowserFileDigest(512);
    const h1 = await digest.computeSha256(file);
    const h2 = await digest.computeSha256(file);
    expect(h1).toBe(h2);
  });

  it('reports progress', async () => {
    const content = new Uint8Array(4096);
    const file = new File([content], 'big.bin');
    const digest = new BrowserFileDigest(1024);
    const progressValues: number[] = [];
    await digest.computeSha256(file, (p) => progressValues.push(p));
    expect(progressValues.length).toBeGreaterThan(0);
    expect(progressValues[progressValues.length - 1]).toBe(1);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx jest __tests__/domain-client/file-digest.spec.ts --no-cache`

- [ ] **Step 3: Implement FileDigest**

Create `web/lib/domain-client/file-digest.ts`:

```ts
export interface FileDigest {
  computeSha256(file: File, onProgress?: (fraction: number) => void): Promise<string>;
}

export class BrowserFileDigest implements FileDigest {
  constructor(private chunkSize = 64 * 1024) {}

  async computeSha256(file: File, onProgress?: (fraction: number) => void): Promise<string> {
    // Read file in chunks via stream to bound memory by chunkSize, not file size.
    // crypto.subtle.digest is one-shot, so we use an incremental approach:
    // read chunks into a queue, then digest. For truly large files, a Web Worker
    // with an incremental SHA-256 (e.g., via WebAssembly) would be better,
    // but the File API's stream().getReader() keeps memory bounded during read.
    const reader = file.stream().getReader();
    const chunks: Uint8Array[] = [];
    let bytesRead = 0;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      bytesRead += value.byteLength;
      onProgress?.(bytesRead / file.size);
    }

    // Assemble for digest (crypto.subtle.digest requires full buffer)
    const assembled = new Uint8Array(bytesRead);
    let pos = 0;
    for (const chunk of chunks) {
      assembled.set(chunk, pos);
      pos += chunk.byteLength;
    }
    // Release chunk references before digest
    chunks.length = 0;

    const hashBuffer = await crypto.subtle.digest('SHA-256', assembled);
    const hashArray = new Uint8Array(hashBuffer);
    let binary = '';
    for (const byte of hashArray) {
      binary += String.fromCharCode(byte);
    }
    return btoa(binary);
  }
}

export class TestFileDigest implements FileDigest {
  constructor(private fixedHash = 'test-sha256-hash-base64') {}
  async computeSha256(_file: File, onProgress?: (fraction: number) => void): Promise<string> {
    onProgress?.(1);
    return this.fixedHash;
  }
}
```

- [ ] **Step 4: Run FileDigest test to verify it passes**

Run: `cd web && npx jest __tests__/domain-client/file-digest.spec.ts --no-cache`

- [ ] **Step 5: Write useUpload tests**

Create `web/__tests__/hooks/domain/useUpload.spec.ts`:

```ts
/**
 * @jest-environment jsdom
 */
import { renderHook, act } from '@testing-library/react';
import { useUpload } from '@/hooks/domain/useUpload';

const mockGateway = {
  createUploadIntent: jest.fn(),
  finalizeUpload: jest.fn(),
};
const mockBlobClient = { upload: jest.fn(), validatedDownloadUrl: jest.fn() };
const mockDigest = { computeSha256: jest.fn() };

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme' } }),
}));
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));
jest.mock('@/lib/domain-client/upload-deps', () => ({
  useBlobTransferClient: () => mockBlobClient,
  useFileDigest: () => mockDigest,
}));

describe('useUpload', () => {
  beforeEach(() => jest.clearAllMocks());

  it('completes three-phase upload and returns operationRecordId', async () => {
    mockDigest.computeSha256.mockResolvedValue('abc123hash=');
    mockGateway.createUploadIntent.mockResolvedValue({
      kind: 'success', status: 201,
      data: { upload_intent_id: 'ui1', grant: { kind: 'signed_put', url: 'https://store/obj' } },
      meta: {},
    });
    mockBlobClient.upload.mockResolvedValue(undefined);
    mockGateway.finalizeUpload.mockResolvedValue({
      kind: 'accepted', status: 202,
      data: { operation_id: 'op42' },
      meta: {},
    });

    const file = new File(['data'], 'test.png', { type: 'image/png' });
    const { result } = renderHook(() => useUpload('reference'));

    let uploadResult: any;
    await act(async () => {
      uploadResult = await result.current.startUpload({
        file,
        ownerKind: 'product',
        ownerId: 'p1',
        actionInstanceId: 'upload-1',
      });
    });

    expect(uploadResult.kind).toBe('accepted');
    expect(uploadResult.operationRecordId).toBe('op42');
    expect(result.current.state).toBe('awaiting_verification');
  });

  it('uses derived action IDs for create and finalize', async () => {
    mockDigest.computeSha256.mockResolvedValue('hash=');
    mockGateway.createUploadIntent.mockResolvedValue({
      kind: 'success', status: 201,
      data: { upload_intent_id: 'ui2', grant: { kind: 'signed_put', url: 'https://s/o' } },
      meta: {},
    });
    mockBlobClient.upload.mockResolvedValue(undefined);
    mockGateway.finalizeUpload.mockResolvedValue({
      kind: 'accepted', status: 202, data: { operation_id: 'op2' }, meta: {},
    });

    const file = new File(['x'], 'f.txt');
    const { result } = renderHook(() => useUpload('ref'));

    await act(async () => {
      await result.current.startUpload({
        file, ownerKind: 'product', ownerId: 'p1', actionInstanceId: 'my-action',
      });
    });

    expect(mockGateway.createUploadIntent.mock.calls[0][0].actionInstanceId)
      .toBe('upload:my-action:create');
    expect(mockGateway.finalizeUpload.mock.calls[0][0].actionInstanceId)
      .toBe('upload:my-action:finalize');
  });

  it('returns problem when createUploadIntent fails', async () => {
    mockDigest.computeSha256.mockResolvedValue('hash=');
    mockGateway.createUploadIntent.mockResolvedValue({
      kind: 'problem', status: 413,
      problem: { type: 'too_large', title: 'File too large', status: 413 },
      meta: {},
    });

    const file = new File(['x'], 'big.bin');
    const { result } = renderHook(() => useUpload('ref'));

    let uploadResult: any;
    await act(async () => {
      uploadResult = await result.current.startUpload({
        file, ownerKind: 'product', ownerId: 'p1', actionInstanceId: 'fail-test',
      });
    });

    expect(uploadResult.kind).toBe('problem');
    expect(uploadResult.problem.status).toBe(413);
  });

  it('returns reconciliation_required from gateway', async () => {
    mockDigest.computeSha256.mockResolvedValue('hash=');
    mockGateway.createUploadIntent.mockResolvedValue({
      kind: 'reconciliation_required',
      actionInstanceId: 'recon-test',
      staleSince: 1234,
    });

    const file = new File(['x'], 'f.txt');
    const { result } = renderHook(() => useUpload('ref'));

    let uploadResult: any;
    await act(async () => {
      uploadResult = await result.current.startUpload({
        file, ownerKind: 'product', ownerId: 'p1', actionInstanceId: 'recon-test',
      });
    });

    expect(uploadResult.kind).toBe('reconciliation_required');
  });
});
```

- [ ] **Step 6: Implement useUpload**

Create `web/hooks/domain/useUpload.ts`:

```ts
import { useCallback, useState } from 'react';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { useBlobTransferClient, useFileDigest } from '@/lib/domain-client/upload-deps';
import type { ProblemDetails } from '@/lib/domain-client/result-types';

export type UploadState =
  | 'idle' | 'hashing' | 'requesting_grant'
  | 'transferring' | 'finalizing' | 'awaiting_verification';

export interface TransferError {
  status: number;
  origin: string;
  message: string;
}

export type UploadStartResult =
  | { kind: 'accepted'; uploadIntentId: string; operationRecordId: string }
  | { kind: 'problem'; problem: ProblemDetails }
  | { kind: 'reconciliation_required'; actionInstanceId: string; staleSince: number }
  | { kind: 'transfer_error'; error: TransferError };

interface StartUploadParams {
  file: File;
  ownerKind: string;
  ownerId: string;
  actionInstanceId: string;
}

export function useUpload(purpose: string) {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const blobClient = useBlobTransferClient();
  const fileDigest = useFileDigest();

  const [state, setState] = useState<UploadState>('idle');
  const [hashProgress, setHashProgress] = useState(0);

  const startUpload = useCallback(
    async (params: StartUploadParams): Promise<UploadStartResult> => {
      const { file, ownerKind, ownerId, actionInstanceId } = params;
      const createActionId = `upload:${actionInstanceId}:create`;
      const finalizeActionId = `upload:${actionInstanceId}:finalize`;

      try {
        // 1. Hash
        setState('hashing');
        setHashProgress(0);
        const checksum = await fileDigest.computeSha256(file, setHashProgress);

        // 2. Create intent
        setState('requesting_grant');
        const intentResult = await gateway.createUploadIntent({
          workspaceSlug: current.slug,
          actionInstanceId: createActionId,
          body: {
            owner_kind: ownerKind,
            owner_id: ownerId,
            filename: file.name,
            content_type: file.type || 'application/octet-stream',
            content_length_min: file.size,
            content_length_max: file.size,
            checksum_sha256: checksum,
          } as any,
        });

        if (intentResult.kind === 'problem') {
          setState('idle');
          return { kind: 'problem', problem: intentResult.problem as ProblemDetails };
        }
        if (intentResult.kind === 'reconciliation_required') {
          setState('idle');
          return intentResult;
        }

        const { upload_intent_id, grant } = (intentResult as any).data;

        // 3. Transfer
        setState('transferring');
        try {
          await blobClient.upload(grant, file);
        } catch (err: any) {
          setState('idle');
          return {
            kind: 'transfer_error',
            error: {
              status: err?.status ?? 0,
              origin: new URL(grant.url ?? grant.action ?? '').origin,
              message: 'File transfer failed',
            },
          };
        }

        // 4. Finalize
        setState('finalizing');
        const finalizeResult = await gateway.finalizeUpload({
          workspaceSlug: current.slug,
          uploadIntentId: upload_intent_id,
          actionInstanceId: finalizeActionId,
        });

        if (finalizeResult.kind === 'problem') {
          setState('idle');
          return { kind: 'problem', problem: finalizeResult.problem as ProblemDetails };
        }
        if (finalizeResult.kind === 'reconciliation_required') {
          setState('idle');
          return finalizeResult;
        }

        setState('awaiting_verification');
        const operationRecordId = (finalizeResult as any).data?.operation_id;
        return { kind: 'accepted', uploadIntentId: upload_intent_id, operationRecordId };
      } catch (err: any) {
        setState('idle');
        return {
          kind: 'transfer_error',
          error: { status: 0, origin: 'unknown', message: err?.message ?? 'Upload failed' },
        };
      }
    },
    [current.slug, gateway, blobClient, fileDigest]
  );

  return { startUpload, state, hashProgress };
}
```

Create `web/lib/domain-client/upload-deps.ts` (dependency injection for testing):

```ts
import { useContext, createContext } from 'react';
import type { BrowserBlobTransferClient } from './blob-transfer';
import type { FileDigest } from './file-digest';

const BlobClientContext = createContext<BrowserBlobTransferClient | null>(null);
const FileDigestContext = createContext<FileDigest | null>(null);

export function useBlobTransferClient(): BrowserBlobTransferClient {
  const client = useContext(BlobClientContext);
  if (!client) throw new Error('useBlobTransferClient requires BlobClientProvider');
  return client;
}

export function useFileDigest(): FileDigest {
  const digest = useContext(FileDigestContext);
  if (!digest) throw new Error('useFileDigest requires FileDigestProvider');
  return digest;
}

export { BlobClientContext, FileDigestContext };
```

- [ ] **Step 7: Run tests and commit**

Run: `cd web && npx jest __tests__/domain-client/file-digest.spec.ts __tests__/hooks/domain/useUpload.spec.ts --no-cache`

```bash
git add web/lib/domain-client/file-digest.ts web/lib/domain-client/upload-deps.ts \
  web/hooks/domain/useUpload.ts web/__tests__/domain-client/file-digest.spec.ts \
  web/__tests__/hooks/domain/useUpload.spec.ts web/hooks/domain/index.ts
git commit -m "feat(web): deep upload module with FileDigest and three-phase orchestration

- BrowserFileDigest: chunked SHA-256 with progress callback
- useUpload: hashing -> requesting_grant -> transferring -> finalizing -> awaiting_verification
- Derived stable action IDs per phase (upload:{id}:create, upload:{id}:finalize)
- Surfaces problem, reconciliation, and transfer_error distinctly
- Testable via injected gateway, blob client, and digest adapters"
```

---

### Task 9: useDownload, storage origin parser, and CSP middleware

**Files:**
- Create: `web/hooks/domain/useDownload.ts`
- Create: `web/lib/domain-client/storage-origin-parser.ts`
- Modify: `web/lib/domain-client/storage-origin-policy.ts`
- Modify: `web/middleware.ts`
- Test: `web/__tests__/hooks/domain/useDownload.spec.ts`
- Test: `web/__tests__/domain-client/storage-origin-parser.spec.ts`
- Test: `web/__tests__/security/csp-storage-origins.spec.ts`

**Interfaces:**
- Consumes: `DomainGateway.createDownloadIntent` from gateway; `BrowserBlobTransferClient.validatedDownloadUrl` from blob-transfer; `StorageOriginPolicy` from storage-origin-policy
- Produces:
  ```ts
  function parseStorageOrigins(raw: string, options: { production: boolean }): string[];
  function useDownload(): {
    requestDownload: (params: {
      objectRef: Record<string, unknown>;
      filename: string;
      actionInstanceId: string;
    }) => Promise<DownloadStartResult>;
    downloadState: 'idle' | 'requesting' | 'navigating';
    problem: ProblemDetails | undefined;
  };
  ```

- [ ] **Step 1: Write storage origin parser tests**

Create `web/__tests__/domain-client/storage-origin-parser.spec.ts`:

```ts
import { parseStorageOrigins } from '@/lib/domain-client/storage-origin-parser';

describe('parseStorageOrigins', () => {
  it('parses comma-separated origins', () => {
    const origins = parseStorageOrigins('https://s3.example.com,https://cdn.example.com', { production: true });
    expect(origins).toEqual(['https://s3.example.com', 'https://cdn.example.com']);
  });

  it('trims whitespace', () => {
    const origins = parseStorageOrigins(' https://s3.example.com , https://cdn.example.com ', { production: true });
    expect(origins).toEqual(['https://s3.example.com', 'https://cdn.example.com']);
  });

  it('rejects wildcards in production', () => {
    expect(() => parseStorageOrigins('*', { production: true })).toThrow(/wildcard/i);
  });

  it('rejects non-HTTPS in production', () => {
    expect(() => parseStorageOrigins('http://store.local:9000', { production: true })).toThrow(/https/i);
  });

  it('allows HTTP loopback in development', () => {
    const origins = parseStorageOrigins('http://localhost:9000', { production: false });
    expect(origins).toEqual(['http://localhost:9000']);
  });

  it('rejects origins with paths', () => {
    expect(() => parseStorageOrigins('https://s3.example.com/bucket', { production: true }))
      .toThrow(/path/i);
  });

  it('rejects origins with credentials', () => {
    expect(() => parseStorageOrigins('https://user:pass@s3.example.com', { production: true }))
      .toThrow(/credential/i);
  });

  it('rejects data: and blob: schemes', () => {
    expect(() => parseStorageOrigins('data:', { production: true })).toThrow();
    expect(() => parseStorageOrigins('blob:', { production: true })).toThrow();
  });

  it('returns empty array for empty string', () => {
    expect(parseStorageOrigins('', { production: false })).toEqual([]);
  });

  it('rejects origin matching DOMAIN_API_BASE_URL', () => {
    const original = process.env.DOMAIN_API_BASE_URL;
    process.env.DOMAIN_API_BASE_URL = 'http://localhost:8000';
    try {
      expect(() => parseStorageOrigins('http://localhost:8000', { production: false }))
        .toThrow(/FastAPI/i);
    } finally {
      process.env.DOMAIN_API_BASE_URL = original;
    }
  });
});
```

- [ ] **Step 2: Implement storage origin parser**

Create `web/lib/domain-client/storage-origin-parser.ts`:

```ts
const LOOPBACK = new Set(['localhost', '127.0.0.1', '[::1]']);

export function parseStorageOrigins(
  raw: string,
  options: { production: boolean }
): string[] {
  if (!raw.trim()) return [];

  const domainApiOrigin = process.env.DOMAIN_API_BASE_URL
    ? new URL(process.env.DOMAIN_API_BASE_URL).origin
    : null;

  return raw
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
    .map((entry) => {
      if (entry === '*') throw new Error('Wildcard origins are not allowed');
      if (entry === 'data:' || entry === 'blob:') {
        throw new Error(`Unsafe scheme source "${entry}" is not allowed`);
      }

      let url: URL;
      try {
        url = new URL(entry);
      } catch {
        throw new Error(`Invalid origin: "${entry}"`);
      }

      if (url.username || url.password) {
        throw new Error(`Credential in storage origin is not allowed: "${entry}"`);
      }

      if (url.pathname !== '/' || url.search || url.hash) {
        throw new Error(`Storage origin must not contain path, query, or fragment: "${entry}"`);
      }

      if (options.production && url.protocol !== 'https:') {
        throw new Error(`Production storage origins must use HTTPS: "${entry}"`);
      }

      if (!options.production && url.protocol === 'http:' && !LOOPBACK.has(url.hostname)) {
        throw new Error(`HTTP is only allowed for loopback in development: "${entry}"`);
      }

      const origin = url.origin;

      if (domainApiOrigin && origin === domainApiOrigin) {
        throw new Error(`Storage origin must not match FastAPI origin: "${entry}"`);
      }

      return origin;
    });
}
```

- [ ] **Step 3: Run parser test**

Run: `cd web && npx jest __tests__/domain-client/storage-origin-parser.spec.ts --no-cache`

- [ ] **Step 4: Write CSP integration test**

Create `web/__tests__/security/csp-storage-origins.spec.ts`:

```ts
import { generateCSP } from '@/middleware';

jest.mock('@/lib/domain-client/storage-origin-parser', () => ({
  parseStorageOrigins: jest.fn().mockReturnValue(['https://s3.example.com']),
}));

describe('CSP with storage origins', () => {
  it('includes storage origins in connect-src', () => {
    const csp = generateCSP('test-nonce');
    expect(csp).toContain("connect-src 'self' https://s3.example.com");
  });

  it('never includes DOMAIN_API_BASE_URL in connect-src', () => {
    process.env.DOMAIN_API_BASE_URL = 'http://localhost:8000';
    const csp = generateCSP('test-nonce');
    expect(csp).not.toContain('localhost:8000');
  });
});
```

- [ ] **Step 5: Update CSP middleware**

Modify `web/middleware.ts` — update `generateCSP` to include storage origins:

```ts
// Add import at top:
import { parseStorageOrigins } from '@/lib/domain-client/storage-origin-parser';

// In generateCSP, update connect-src:
let storageOrigins: string[] = [];
try {
  storageOrigins = parseStorageOrigins(
    process.env.NEXT_PUBLIC_MARKETAI_STORAGE_ORIGINS ?? '',
    { production: isProduction }
  );
} catch (e) {
  console.error('Invalid NEXT_PUBLIC_MARKETAI_STORAGE_ORIGINS:', e);
}

const policies = {
  'connect-src': ["'self'", ...storageOrigins],
  // ... rest unchanged
};
```

- [ ] **Step 6: Implement useDownload**

Create `web/hooks/domain/useDownload.ts`:

```ts
import { useCallback, useState } from 'react';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { useBlobTransferClient } from '@/lib/domain-client/upload-deps';
import type { ProblemDetails } from '@/lib/domain-client/result-types';

type DownloadState = 'idle' | 'requesting' | 'navigating';

type DownloadStartResult =
  | { kind: 'started' }
  | { kind: 'problem'; problem: ProblemDetails }
  | { kind: 'reconciliation_required'; actionInstanceId: string; staleSince: number }
  | { kind: 'transfer_error'; error: { status: number; origin: string; message: string } };

export function useDownload() {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const blobClient = useBlobTransferClient();

  const [downloadState, setDownloadState] = useState<DownloadState>('idle');
  const [problem, setProblem] = useState<ProblemDetails>();

  const requestDownload = useCallback(
    async (params: {
      objectRef: Record<string, unknown>;
      filename: string;
      actionInstanceId: string;
    }): Promise<DownloadStartResult> => {
      const { objectRef, actionInstanceId } = params;
      setProblem(undefined);

      setDownloadState('requesting');
      const result = await gateway.createDownloadIntent({
        workspaceSlug: current.slug,
        actionInstanceId,
        body: objectRef as any,
      });

      if (result.kind === 'problem') {
        setDownloadState('idle');
        setProblem(result.problem as ProblemDetails);
        return { kind: 'problem', problem: result.problem as ProblemDetails };
      }

      if (result.kind === 'reconciliation_required') {
        setDownloadState('idle');
        return result;
      }

      const grant = (result as any).data;
      const validatedUrl = blobClient.validatedDownloadUrl(grant);

      setDownloadState('navigating');
      window.location.assign(validatedUrl.href);

      setTimeout(() => setDownloadState('idle'), 2000);
      return { kind: 'started' };
    },
    [current.slug, gateway, blobClient]
  );

  return { requestDownload, downloadState, problem };
}
```

- [ ] **Step 7: Write useDownload test**

Create `web/__tests__/hooks/domain/useDownload.spec.ts`:

```ts
/**
 * @jest-environment jsdom
 */
import { renderHook, act } from '@testing-library/react';
import { useDownload } from '@/hooks/domain/useDownload';

const mockGateway = { createDownloadIntent: jest.fn() };
const mockBlobClient = {
  upload: jest.fn(),
  validatedDownloadUrl: jest.fn(),
};

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme' } }),
}));
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));
jest.mock('@/lib/domain-client/upload-deps', () => ({
  useBlobTransferClient: () => mockBlobClient,
  useFileDigest: () => ({}),
}));

const originalAssign = window.location.assign;
beforeAll(() => { Object.defineProperty(window, 'location', { value: { assign: jest.fn() }, writable: true }); });
afterAll(() => { window.location.assign = originalAssign; });

describe('useDownload', () => {
  beforeEach(() => jest.clearAllMocks());

  it('completes download flow and triggers navigation', async () => {
    mockGateway.createDownloadIntent.mockResolvedValue({
      kind: 'success', status: 200,
      data: { url: 'https://s3.example.com/file', kind: 'signed_get' },
      meta: {},
    });
    mockBlobClient.validatedDownloadUrl.mockReturnValue(
      new URL('https://s3.example.com/file')
    );

    const { result } = renderHook(() => useDownload());

    await act(async () => {
      const res = await result.current.requestDownload({
        objectRef: { owner_kind: 'product', owner_id: 'p1', blob_key: 'k1' },
        filename: 'asset.png',
        actionInstanceId: 'dl-1',
      });
      expect(res.kind).toBe('started');
    });

    expect(window.location.assign).toHaveBeenCalledWith('https://s3.example.com/file');
  });

  it('returns problem on 403', async () => {
    mockGateway.createDownloadIntent.mockResolvedValue({
      kind: 'problem', status: 403,
      problem: { type: 'forbidden', title: 'Forbidden', status: 403 },
      meta: {},
    });

    const { result } = renderHook(() => useDownload());

    await act(async () => {
      const res = await result.current.requestDownload({
        objectRef: {}, filename: 'x.png', actionInstanceId: 'dl-2',
      });
      expect(res.kind).toBe('problem');
    });
  });
});
```

- [ ] **Step 8: Update barrel export and run all tests**

Add to `web/hooks/domain/index.ts`:

```ts
export { useUpload } from './useUpload';
export { useDownload } from './useDownload';
```

Run: `cd web && npx jest __tests__/domain-client/storage-origin-parser.spec.ts __tests__/security/csp-storage-origins.spec.ts __tests__/hooks/domain/useDownload.spec.ts --no-cache`

- [ ] **Step 9: Commit**

```bash
git add web/lib/domain-client/storage-origin-parser.ts web/hooks/domain/useDownload.ts \
  web/middleware.ts web/__tests__/domain-client/storage-origin-parser.spec.ts \
  web/__tests__/security/csp-storage-origins.spec.ts \
  web/__tests__/hooks/domain/useDownload.spec.ts web/hooks/domain/index.ts
git commit -m "feat(web): download hook, storage origin parser, and CSP enforcement

- parseStorageOrigins: validates exact origins, rejects wildcards/paths/credentials
- Production requires HTTPS; dev allows HTTP loopback; FastAPI origin always rejected
- CSP connect-src: 'self' + validated storage origins (single source of truth)
- useDownload: intent -> validatedDownloadUrl -> browser navigation
- Signed URL never cached, recorded, or exposed to caller"
```

---

### Task 10: Playwright setup and E2E tests

**Files:**
- Create: `web/playwright.config.ts`
- Create: `web/e2e/workspace/fixtures.ts`
- Create: `web/e2e/workspace/session.spec.ts`
- Create: `web/e2e/workspace/workspace-switch.spec.ts`
- Create: `web/e2e/workspace/owner-member.spec.ts`
- Create: `web/e2e/workspace/navigation-refresh.spec.ts`
- Create: `web/e2e/workspace/network-boundary.spec.ts`
- Create: `web/e2e/workspace/keyboard.spec.ts`
- Modify: `web/package.json`

**Interfaces:**
- Consumes: Running Next.js dev server, PostgreSQL, FastAPI, local blob transfer
- Produces: Six E2E test files covering all acceptance criteria

- [ ] **Step 1: Install Playwright and create config**

Add to `web/package.json` devDependencies:

```json
"@playwright/test": "^1.45.0"
```

Add script:

```json
"test:e2e": "playwright test --config playwright.config.ts"
```

Run: `cd web && npm install`

Create `web/playwright.config.ts`:

```ts
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  retries: process.env.CI ? 2 : 0,
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:3000',
    trace: 'on-first-retry',
  },
  projects: [
    {
      name: 'setup',
      testMatch: /fixtures\.ts/,
      teardown: 'chromium',
    },
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        storageState: 'e2e/.auth/owner.json',
      },
      dependencies: ['setup'],
    },
  ],
  webServer: process.env.CI
    ? undefined
    : {
        command: 'npm run dev',
        port: 3000,
        reuseExistingServer: true,
      },
});
```

- [ ] **Step 2: Create test fixtures**

Create `web/e2e/workspace/fixtures.ts`:

```ts
import { test as setup, expect } from '@playwright/test';
import path from 'path';

const ownerFile = path.join(__dirname, '..', '.auth', 'owner.json');
const memberFile = path.join(__dirname, '..', '.auth', 'member.json');

setup('authenticate as owner', async ({ page }) => {
  await page.goto('/auth/login');
  await page.fill('input[name="email"]', process.env.E2E_OWNER_EMAIL!);
  await page.fill('input[name="password"]', process.env.E2E_OWNER_PASSWORD!);
  await page.click('button[type="submit"]');
  await page.waitForURL(/\/w\//);
  await page.context().storageState({ path: ownerFile });
});

setup('authenticate as member', async ({ page }) => {
  await page.goto('/auth/login');
  await page.fill('input[name="email"]', process.env.E2E_MEMBER_EMAIL!);
  await page.fill('input[name="password"]', process.env.E2E_MEMBER_PASSWORD!);
  await page.click('button[type="submit"]');
  await page.waitForURL(/\/w\//);
  await page.context().storageState({ path: memberFile });
});
```

Add `web/e2e/.auth/` to `.gitignore`.

- [ ] **Step 3: Create session.spec.ts**

Create `web/e2e/workspace/session.spec.ts`:

```ts
import { test, expect } from '@playwright/test';

test('unauthenticated access redirects to login', async ({ browser }) => {
  const context = await browser.newContext({ storageState: undefined as any });
  const page = await context.newPage();
  await page.goto('/w/test-workspace/generate');
  await expect(page).toHaveURL(/\/auth\/login/);
  await context.close();
});

test('authenticated user reaches workspace page', async ({ page }) => {
  await page.goto('/w/test-workspace/generate');
  await expect(page.locator('h1')).toContainText('New Generation');
});
```

- [ ] **Step 4: Create workspace-switch.spec.ts**

Create `web/e2e/workspace/workspace-switch.spec.ts`:

```ts
import { test, expect } from '@playwright/test';

test('switching workspace changes URL and content atomically', async ({ page }) => {
  await page.goto('/w/workspace-a/generate');
  await expect(page.locator('h1')).toContainText('New Generation');

  const selector = page.locator('#workspace-selector');
  await selector.selectOption('workspace-b');
  await expect(page).toHaveURL(/\/w\/workspace-b\/generate/);

  // Verify no flash of previous workspace data
  await expect(page.locator('#workspace-selector')).toHaveValue('workspace-b');
});

test('switching from Owner-only page to Member workspace lands on generate', async ({ page }) => {
  await page.goto('/w/workspace-a/settings');
  await expect(page.locator('h1')).toContainText('Settings');

  const selector = page.locator('#workspace-selector');
  await selector.selectOption('workspace-b');
  await expect(page).toHaveURL(/\/w\/workspace-b\/generate/);
});
```

- [ ] **Step 5: Create owner-member.spec.ts**

Create `web/e2e/workspace/owner-member.spec.ts`:

```ts
import { test, expect } from '@playwright/test';
import path from 'path';

test('Owner sees settings and members nav items', async ({ page }) => {
  await page.goto('/w/workspace-a/generate');
  await expect(page.getByText('Settings')).toBeVisible();
  await expect(page.getByText('Members')).toBeVisible();
});

test.describe('Member perspective', () => {
  test.use({ storageState: path.join(__dirname, '..', '.auth', 'member.json') });

  test('Member does not see settings nav', async ({ page }) => {
    await page.goto('/w/workspace-b/generate');
    await expect(page.getByText('Settings')).not.toBeVisible();
  });

  test('Member direct access to settings returns 403', async ({ page }) => {
    const response = await page.goto('/w/workspace-b/settings');
    expect(response?.status()).toBe(200); // page renders but shows forbidden
    await expect(page.getByText('Access Denied')).toBeVisible();
  });
});
```

- [ ] **Step 6: Create navigation-refresh.spec.ts**

Create `web/e2e/workspace/navigation-refresh.spec.ts`:

```ts
import { test, expect } from '@playwright/test';

test('navigating between six routes preserves layout', async ({ page }) => {
  await page.goto('/w/workspace-a/generate');
  const routes = ['assets', 'experiments', 'localization', 'scale', 'library'];
  for (const route of routes) {
    await page.click(`a[href="/w/workspace-a/${route}"]`);
    await expect(page).toHaveURL(new RegExp(`/w/workspace-a/${route}`));
    await expect(page.locator('#workspace-selector')).toBeVisible();
  }
});

test('page refresh restores workspace context', async ({ page }) => {
  await page.goto('/w/workspace-a/assets');
  await page.reload();
  await expect(page.locator('h1')).toContainText('Assets');
  await expect(page.locator('#workspace-selector')).toHaveValue('workspace-a');
});

test('back/forward navigation works', async ({ page }) => {
  await page.goto('/w/workspace-a/generate');
  await page.click('a[href="/w/workspace-a/assets"]');
  await expect(page).toHaveURL(/assets/);
  await page.goBack();
  await expect(page).toHaveURL(/generate/);
  await page.goForward();
  await expect(page).toHaveURL(/assets/);
});
```

- [ ] **Step 7: Create network-boundary.spec.ts**

Create `web/e2e/workspace/network-boundary.spec.ts`:

```ts
import { test, expect } from '@playwright/test';

test('domain requests go through same-origin BFF only', async ({ page }) => {
  const domainRequests: string[] = [];
  page.on('request', (req) => {
    if (req.url().includes('/api/domain/')) {
      domainRequests.push(req.url());
    }
  });

  await page.goto('/w/workspace-a/generate');
  await page.waitForTimeout(2000);

  for (const url of domainRequests) {
    const requestUrl = new URL(url);
    const pageUrl = new URL(page.url());
    expect(requestUrl.origin).toBe(pageUrl.origin);
  }
});

test('CSP blocks direct fetch to FastAPI origin', async ({ page }) => {
  await page.goto('/w/workspace-a/generate');

  const cspViolations: string[] = [];
  page.on('console', (msg) => {
    if (msg.text().includes('connect-src')) {
      cspViolations.push(msg.text());
    }
  });

  const fastapiOrigin = process.env.DOMAIN_API_BASE_URL ?? 'http://localhost:8000';
  await page.evaluate(async (origin) => {
    try {
      await fetch(`${origin}/api/v1/health`);
    } catch {}
  }, fastapiOrigin);

  expect(cspViolations.length).toBeGreaterThan(0);
});

test('CSP header contains self and storage origins, not FastAPI', async ({ page }) => {
  const response = await page.goto('/w/workspace-a/generate');
  const csp = response?.headers()['content-security-policy'] ?? '';

  expect(csp).toContain("connect-src 'self'");
  expect(csp).not.toContain(process.env.DOMAIN_API_BASE_URL ?? 'localhost:8000');
  expect(csp).not.toContain('*');
});
```

- [ ] **Step 8: Create keyboard.spec.ts**

Create `web/e2e/workspace/keyboard.spec.ts`:

```ts
import { test, expect } from '@playwright/test';

test('skip-to-content link works', async ({ page }) => {
  await page.goto('/w/workspace-a/generate');
  await page.keyboard.press('Tab');
  const skipLink = page.locator('a:has-text("Skip to content")');
  await expect(skipLink).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page.locator('#main-content')).toBeFocused();
});

test('workspace selector is keyboard operable', async ({ page }) => {
  await page.goto('/w/workspace-a/generate');
  const selector = page.locator('#workspace-selector');
  await selector.focus();
  await expect(selector).toBeFocused();
});

test('route change moves focus to h1', async ({ page }) => {
  await page.goto('/w/workspace-a/generate');
  await page.click('a[href="/w/workspace-a/assets"]');
  await expect(page).toHaveURL(/assets/);
  const h1 = page.locator('h1');
  await expect(h1).toBeFocused();
});

test('navigation items reachable via tab', async ({ page }) => {
  await page.goto('/w/workspace-a/generate');
  const navItems = page.locator('nav a');
  const count = await navItems.count();
  expect(count).toBeGreaterThanOrEqual(6);
});
```

- [ ] **Step 9: Commit**

```bash
git add web/playwright.config.ts web/e2e/ web/package.json web/package-lock.json \
  web/.gitignore
git commit -m "feat(web): playwright E2E setup and six workspace test files

- Playwright config with auth fixtures, Chromium, webServer
- session.spec.ts: auth redirect, authenticated access
- workspace-switch.spec.ts: atomic switch, Owner-only fallback
- owner-member.spec.ts: nav visibility, SSR 403 for Member
- navigation-refresh.spec.ts: layout persistence, refresh, back/forward
- network-boundary.spec.ts: BFF-only requests, CSP enforcement, FastAPI block
- keyboard.spec.ts: skip-to-content, selector, route focus, tab order"
```

---

### Task 11: Final verification and CI integration

**Files:**
- Modify: `Makefile` or CI configuration
- No new code — verification and integration

- [ ] **Step 1: Run TypeScript type check**

```bash
cd web && npx tsc --noEmit
```

Fix any type errors.

- [ ] **Step 2: Run linter**

```bash
cd web && npx eslint . --ext .ts,.tsx
```

Fix any lint errors.

- [ ] **Step 3: Run all Jest tests**

```bash
cd web && npx jest --no-cache
```

Verify all pass including new tests.

- [ ] **Step 4: Run make verify from project root**

```bash
make verify
```

- [ ] **Step 5: Verify production build**

```bash
cd web && npm run build
```

Verify no build errors and CSP headers correct in production mode.

- [ ] **Step 6: Run Playwright tests locally**

```bash
cd web && npx playwright install chromium
cd web && npm run test:e2e
```

Requires running Next.js dev server, PostgreSQL, FastAPI, and local blob transfer.

- [ ] **Step 7: Manual keyboard and screen reader check**

Open the app in a browser and verify:
- Tab through all navigation items in correct order
- Workspace selector works via keyboard
- Skip-to-content link works
- Focus moves to h1 on route change
- Screen reader announces operation status changes (polite) and errors (alert)
- StaleIndicator does not re-announce on every tick

- [ ] **Step 8: Final commit and PR**

```bash
git add -A
git status  # review what's included
git commit -m "chore(web): verify workspace shell typecheck, lint, tests, and build"
```

Open PR:

```bash
gh pr create --title "feat(web): authenticated workspace shell and six-route product UI" \
  --body "$(cat <<'EOF'
## Summary

- Implements Issue #26: authenticated Next.js Workspace shell
- Nine route pages under /w/[slug]/ with SSR workspace validation
- WorkspaceContext with atomic switching and URL as sole source of truth
- Three domain hooks: useDomainQuery, useDomainCommand, useOperation
- Deep upload/download modules hiding three-phase intent flows
- Shared operational UX: ModeProvenance, OperationStatus, CapabilityNotice, StaleIndicator, ForbiddenState
- CSP enforcement with storage origin single-source policy
- Keyboard accessibility with skip-to-content, route focus, ARIA live regions
- Six Playwright E2E test files + comprehensive Jest unit tests

Closes #26

## Migrations

None — no schema changes. Team list query adds orderBy (no migration needed).

## Test evidence

- `npm test` — all Jest tests pass
- `npm run test:e2e` — six Playwright files pass
- `npx tsc --noEmit` — zero type errors
- `npm run build` — production build succeeds
- CSP `connect-src` contains `'self'` + storage origins, never FastAPI
- Manual keyboard and screen reader verification

## Operational impact

- New routes under /w/[slug]/ — no breaking changes to existing /teams/ routes
- CSP connect-src now includes NEXT_PUBLIC_MARKETAI_STORAGE_ORIGINS
- Dashboard redirect is now server-side (no flash)

## Remaining risks

- Playwright fixtures require E2E environment (PostgreSQL, FastAPI, blob transfer)
- Feature page content deferred to #27 and #28
- Entitlement resolution deferred to #18

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
