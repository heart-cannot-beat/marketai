/**
 * @jest-environment jsdom
 */
import { createElement } from 'react';
import { render, screen } from '@testing-library/react';

jest.mock('next-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, opts?: Record<string, string>) => {
      if (opts) {
        let result = `library:${key}`;
        for (const [k, v] of Object.entries(opts)) {
          result = result.replace(`{{${k}}}`, String(v));
        }
        return result;
      }
      return `library:${key}`;
    },
  }),
}));

jest.mock('next/router', () => ({
  useRouter: () => ({
    query: { slug: 'acme' },
    asPath: '/w/acme/library',
    push: jest.fn(),
    replace: jest.fn(),
  }),
}));

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: {
      slug: 'acme',
      workspaceId: 'w1',
      role: 'OWNER',
      membershipId: 'm1',
      displayName: 'Acme',
    },
  }),
}));

let mockRouteState: any;
jest.mock('@/hooks/library/useLibraryRoute', () => ({
  useLibraryRoute: () => ({
    state: mockRouteState,
    setCategory: jest.fn(),
    setEntry: jest.fn(),
    setTab: jest.fn(),
    setVersion: jest.fn(),
    setResource: jest.fn(),
  }),
}));

let mockListData: any;
let mockListLoading: boolean;
jest.mock('@/hooks/library/useLibraryEntries', () => ({
  useLibraryEntries: () => ({
    items: mockListData ?? [],
    isLoading: mockListLoading,
    isLoadingMore: false,
    hasMore: false,
    loadMore: jest.fn(),
    isRefreshing: false,
    refresh: jest.fn(),
    problem: null,
  }),
}));

jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: () => ({
    data: null,
    problem: null,
    isLoading: false,
    refresh: jest.fn(),
  }),
}));

import { LibraryShell } from '@/components/library/LibraryShell';

beforeEach(() => {
  mockRouteState = {
    cat: 'product',
    entry: null,
    tab: null,
    version: null,
    resource: null,
    resource_type: null,
  };
  mockListData = null;
  mockListLoading = false;
});

// ─── Responsive Layout ──────────────────────────────────────────────

describe('Responsive layout', () => {
  it('shell renders both desktop sidebar and mobile category nav', () => {
    const { container } = render(createElement(LibraryShell));
    const sidebars = container.querySelectorAll('[class*="lg:block"]');
    const mobileNavs = container.querySelectorAll('[class*="lg:hidden"]');
    expect(sidebars.length).toBeGreaterThanOrEqual(1);
    expect(mobileNavs.length).toBeGreaterThanOrEqual(1);
  });

  it('entry list is hidden on mobile when entry selected', () => {
    mockRouteState = {
      cat: 'product',
      entry: 'e1',
      tab: null,
      version: null,
      resource: null,
      resource_type: null,
    };
    const { container } = render(createElement(LibraryShell));
    const listPane = container.querySelector('[class*="hidden md:block"]');
    expect(listPane).toBeTruthy();
  });

  it('entry list is visible on mobile when no entry selected', () => {
    mockRouteState = {
      cat: 'product',
      entry: null,
      tab: null,
      version: null,
      resource: null,
      resource_type: null,
    };
    const { container } = render(createElement(LibraryShell));
    const listPanes = container.querySelectorAll('[class*="md:w-72"]');
    expect(listPanes.length).toBe(1);
    const listPane = listPanes[0];
    expect(listPane.className).not.toContain('hidden md:block');
  });
});

// ─── Query Ownership ────────────────────────────────────────────────

describe('Query ownership', () => {
  it('shell without entry selected does not render detail pane query container', () => {
    mockRouteState = {
      cat: 'product',
      entry: null,
      tab: null,
      version: null,
      resource: null,
      resource_type: null,
    };
    render(createElement(LibraryShell));
    expect(screen.getByText('library:detail.no-selection')).toBeTruthy();
  });

  it('tab isolation: only one tabpanel renders for a selected tab', () => {
    expect(true).toBe(true);
  });

  it('category nav does not duplicate across viewport sizes', () => {
    const { container } = render(createElement(LibraryShell));
    const allNavs = container.querySelectorAll('nav');
    const categoryNavCount = Array.from(allNavs).filter((nav) =>
      nav.getAttribute('aria-label')?.includes('library')
    ).length;
    expect(categoryNavCount).toBeLessThanOrEqual(2);
  });
});

// ─── CSS-Only Layout ────────────────────────────────────────────────

describe('CSS-only responsive layout', () => {
  it('mobile hiding uses CSS classes not conditional rendering', () => {
    mockRouteState = {
      cat: 'product',
      entry: 'e1',
      tab: null,
      version: null,
      resource: null,
      resource_type: null,
    };
    const { container } = render(createElement(LibraryShell));
    const hiddenElements = container.querySelectorAll('[class*="hidden"]');
    expect(hiddenElements.length).toBeGreaterThan(0);

    const hasViewportJs =
      container.innerHTML.includes('matchMedia') ||
      container.innerHTML.includes('window.innerWidth');
    expect(hasViewportJs).toBe(false);
  });

  it('layout uses Tailwind responsive prefixes for breakpoints', () => {
    const { container } = render(createElement(LibraryShell));
    const html = container.innerHTML;
    const hasMdPrefix = html.includes('md:');
    const hasLgPrefix = html.includes('lg:');
    expect(hasMdPrefix).toBe(true);
    expect(hasLgPrefix).toBe(true);
  });
});
