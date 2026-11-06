/**
 * @jest-environment jsdom
 */
import { createElement } from 'react';
import { render, screen, fireEvent } from '@testing-library/react';

const mockOnTabChange = jest.fn();
const mockOnVersionChange = jest.fn();
const mockOnDismissResource = jest.fn();

let mockRouteState = {
  cat: 'product' as const,
  entry: 'e1' as string | null,
  tab: null as string | null,
  version: null as string | null,
  resource_type: null as string | null,
  resource: null as string | null,
  resource_tab: null,
  resource_version: null,
  parent_type: null,
  parent: null,
};

jest.mock('@/hooks/library/useLibraryRoute', () => ({
  useLibraryRoute: () => ({
    state: mockRouteState,
    setCategory: jest.fn(),
    setEntry: jest.fn(),
    setTab: mockOnTabChange,
    setVersion: mockOnVersionChange,
    setResource: jest.fn(),
    setResourceTab: jest.fn(),
    setResourceVersion: jest.fn(),
  }),
}));

let mockDetailResult = {
  data: undefined as unknown,
  problem: undefined as unknown,
  isLoading: false,
  isStale: false,
  lastSuccessfulAt: null,
  refresh: jest.fn(),
};

const useDomainQueryCalls: Array<{
  operationId: string;
  params: unknown;
  options: unknown;
}> = [];

let mockQueryOverrides: Record<string, unknown> = {};

jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: (operationId: string, params: unknown, options: unknown) => {
    useDomainQueryCalls.push({ operationId, params, options });
    if (mockQueryOverrides[operationId]) return mockQueryOverrides[operationId];
    return mockDetailResult;
  },
}));

let mockPaginatedResult = {
  items: [] as unknown[],
  hasMore: false,
  isLoading: false,
  isLoadingMore: false,
  isRefreshing: false,
  problem: undefined as unknown,
  loadMore: jest.fn(),
  refresh: jest.fn(),
};

jest.mock('@/hooks/domain/useDomainPaginatedQuery', () => ({
  useDomainPaginatedQuery: () => mockPaginatedResult,
}));

jest.mock('next-i18next', () => ({
  useTranslation: (ns: string) => ({
    t: (key: string, opts?: Record<string, string>) => {
      if (opts?.defaultValue) return opts.defaultValue;
      if (opts?.tab) return `${ns}:${key}`.replace('{{tab}}', opts.tab);
      if (opts?.no) return `${ns}:${key}`.replace('{{no}}', opts.no);
      if (opts?.date) return `${ns}:${key}`.replace('{{date}}', opts.date);
      if (opts?.reason)
        return `${ns}:${key}`.replace('{{reason}}', opts.reason);
      return `${ns}:${key}`;
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

jest.mock('@/components/workspace/NavigationBlocker', () => ({
  useOptionalNavigationBlocker: () => null,
}));

// The Draft tab mounts the editor over `useDraftSession`; its save/validate
// commands and navigation blocking are covered by the hook's own spec.
jest.mock('@/hooks/library/useDraftSession', () => ({
  useDraftSession: () => ({
    isSupported: true,
    buffer: { fields: {}, _unknown: {} },
    isDirty: false,
    isSaving: false,
    saveProblem: undefined,
    isValidating: false,
    validation: null,
    conflict: null,
    updateField: jest.fn(),
    updateFields: jest.fn(),
    replaceContent: jest.fn(),
    save: jest.fn(async () => true),
    validate: jest.fn(async () => true),
    acknowledgeWarning: jest.fn(),
    resolveConflictWithServer: jest.fn(),
    resolveConflictWithLocal: jest.fn(),
    cancelConflict: jest.fn(),
    discard: jest.fn(),
    canPublish: jest.fn(() => false),
  }),
}));

jest.mock('@/components/library/commands/PublishDialog', () => ({
  PublishDialog: ({ open }: { open: boolean }) =>
    open ? <div data-testid="publish-dialog" /> : null,
}));

import { LibraryEntryDetail } from '@/components/library/detail/LibraryEntryDetail';

function makeEntryData(overrides?: Partial<Record<string, unknown>>) {
  return {
    id: 'e1',
    entry_type: 'product',
    display_name: 'Widget Pro',
    lifecycle: 'active',
    current_version_id: 'v-current',
    draft: {
      id: 'd1',
      entry_id: 'e1',
      revision: 2,
      state: 'editing',
      content_schema_version: 1,
      created_at: '2026-01-01',
      updated_at: '2026-01-15',
    },
    revision: 3,
    allowed_actions: [
      { action: 'publish', allowed: true, reason_code: null },
      { action: 'archive', allowed: true, reason_code: null },
    ],
    ...overrides,
  };
}

describe('LibraryEntryDetail — tabs', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    useDomainQueryCalls.length = 0;
    mockQueryOverrides = {};
    mockRouteState = {
      cat: 'product',
      entry: 'e1',
      tab: null,
      version: null,
      resource_type: null,
      resource: null,
      resource_tab: null,
      resource_version: null,
      parent_type: null,
      parent: null,
    };
    mockDetailResult = {
      data: makeEntryData(),
      problem: undefined,
      isLoading: false,
      isStale: false,
      lastSuccessfulAt: null,
      refresh: jest.fn(),
    };
    mockPaginatedResult = {
      items: [],
      hasMore: false,
      isLoading: false,
      isLoadingMore: false,
      isRefreshing: false,
      problem: undefined,
      loadMore: jest.fn(),
      refresh: jest.fn(),
    };
  });

  it('renders tab bar with supported tabs for product', () => {
    render(
      createElement(LibraryEntryDetail, {
        route: mockRouteState as any,
        onTabChange: mockOnTabChange,
        onVersionChange: mockOnVersionChange,
        onDismissResource: mockOnDismissResource,
      })
    );
    expect(screen.getByRole('tablist')).toBeInTheDocument();
    expect(
      screen.getByRole('tab', { name: 'library:tabs.overview' })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('tab', { name: 'library:tabs.draft.label' })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('tab', { name: 'library:tabs.versions.label' })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('tab', { name: 'library:tabs.events.label' })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('tab', { name: 'library:tabs.impact.label' })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('tab', { name: 'library:tabs.bindings.label' })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('tab', { name: 'library:tabs.references' })
    ).toBeInTheDocument();
  });

  it('marks the active tab as selected', () => {
    render(
      createElement(LibraryEntryDetail, {
        route: mockRouteState as any,
        onTabChange: mockOnTabChange,
        onVersionChange: mockOnVersionChange,
        onDismissResource: mockOnDismissResource,
      })
    );
    const overviewTab = screen.getByRole('tab', {
      name: 'library:tabs.overview',
    });
    expect(overviewTab).toHaveAttribute('aria-selected', 'true');
  });

  it('calls onTabChange when clicking a different tab', () => {
    render(
      createElement(LibraryEntryDetail, {
        route: mockRouteState as any,
        onTabChange: mockOnTabChange,
        onVersionChange: mockOnVersionChange,
        onDismissResource: mockOnDismissResource,
      })
    );
    fireEvent.click(
      screen.getByRole('tab', { name: 'library:tabs.versions.label' })
    );
    expect(mockOnTabChange).toHaveBeenCalledWith('versions');
  });

  it('defaults to overview tab when route.tab is null', () => {
    render(
      createElement(LibraryEntryDetail, {
        route: mockRouteState as any,
        onTabChange: mockOnTabChange,
        onVersionChange: mockOnVersionChange,
        onDismissResource: mockOnDismissResource,
      })
    );
    expect(
      screen.getByRole('tabpanel', { name: 'library:tabs.overview' })
    ).toBeInTheDocument();
  });

  it('shows "Go to Draft" when no current version on overview tab', () => {
    mockDetailResult.data = makeEntryData({ current_version_id: null });
    render(
      createElement(LibraryEntryDetail, {
        route: mockRouteState as any,
        onTabChange: mockOnTabChange,
        onVersionChange: mockOnVersionChange,
        onDismissResource: mockOnDismissResource,
      })
    );
    expect(
      screen.getByText('library:tabs.current.no-version')
    ).toBeInTheDocument();
    expect(
      screen.getByText('library:tabs.current.go-to-draft')
    ).toBeInTheDocument();
  });

  it('renders server-authorized entry commands and opens publish', () => {
    mockDetailResult.data = makeEntryData({ current_version_id: null });
    render(
      createElement(LibraryEntryDetail, {
        route: mockRouteState as any,
        onEntryChange: jest.fn(),
        onTabChange: mockOnTabChange,
        onVersionChange: mockOnVersionChange,
        onDismissResource: mockOnDismissResource,
      })
    );

    const publish = document.querySelector('[data-command="publish"]');
    expect(publish).toBeInTheDocument();
    fireEvent.click(publish!);
    expect(screen.getByTestId('publish-dialog')).toBeInTheDocument();
    expect(
      screen.getAllByRole('button', { name: 'library:contextual-create.new' })
        .length
    ).toBeGreaterThan(0);
  });

  it('shows draft info on draft tab', () => {
    mockRouteState = { ...mockRouteState, tab: 'draft' };
    render(
      createElement(LibraryEntryDetail, {
        route: mockRouteState as any,
        onTabChange: mockOnTabChange,
        onVersionChange: mockOnVersionChange,
        onDismissResource: mockOnDismissResource,
      })
    );
    expect(screen.getByText('library:tabs.draft.title')).toBeInTheDocument();
    expect(screen.getByText('2')).toBeInTheDocument();
    // The editor is mounted, not a metadata-only panel.
    expect(screen.getByText('library:draft.editor-title')).toBeInTheDocument();
    expect(screen.getByText('library:draft.save')).toBeInTheDocument();
  });

  it('shows "no draft" when entry has no draft on draft tab', () => {
    mockRouteState = { ...mockRouteState, tab: 'draft' };
    mockDetailResult.data = makeEntryData({ draft: null });
    render(
      createElement(LibraryEntryDetail, {
        route: mockRouteState as any,
        onTabChange: mockOnTabChange,
        onVersionChange: mockOnVersionChange,
        onDismissResource: mockOnDismissResource,
      })
    );
    expect(screen.getByText('library:tabs.draft.no-draft')).toBeInTheDocument();
  });

  it('shows versions list on versions tab', () => {
    mockRouteState = { ...mockRouteState, tab: 'versions' };
    mockPaginatedResult.items = [
      {
        id: 'v1',
        entry_id: 'e1',
        entry_type: 'product',
        version_no: 1,
        state: 'effective',
        eligibility: 'eligible',
      },
    ];
    render(
      createElement(LibraryEntryDetail, {
        route: mockRouteState as any,
        onTabChange: mockOnTabChange,
        onVersionChange: mockOnVersionChange,
        onDismissResource: mockOnDismissResource,
      })
    );
    expect(
      screen.getByText('library:tabs.versions.version-label')
    ).toBeInTheDocument();
  });

  it('shows events tab', () => {
    mockRouteState = { ...mockRouteState, tab: 'events' };
    render(
      createElement(LibraryEntryDetail, {
        route: mockRouteState as any,
        onTabChange: mockOnTabChange,
        onVersionChange: mockOnVersionChange,
        onDismissResource: mockOnDismissResource,
      })
    );
    expect(screen.getByText('library:tabs.events.empty')).toBeInTheDocument();
  });

  it('shows placeholder for tabs without specific content (schema, facts, etc.)', () => {
    mockRouteState = { ...mockRouteState, tab: 'schema' };
    render(
      createElement(LibraryEntryDetail, {
        route: mockRouteState as any,
        onTabChange: mockOnTabChange,
        onVersionChange: mockOnVersionChange,
        onDismissResource: mockOnDismissResource,
      })
    );
    expect(screen.getByText('library:tabs.placeholder')).toBeInTheDocument();
  });

  it('renders nested resource panel when resource state is present', () => {
    mockQueryOverrides['get_library_internal_entry_context'] = {
      data: {
        id: 'r-1',
        entry_type: 'offer',
        display_name: 'Summer Offer',
        lifecycle: 'active',
        current_version_id: 'v-r1',
        root_entry_id: 'e1',
        root_entry_type: 'product',
        parent_entry_id: null,
        relation_kind: 'child',
        creation_evidence: 'manual',
      },
      problem: undefined,
      isLoading: false,
      isStale: false,
      lastSuccessfulAt: null,
      refresh: jest.fn(),
    };
    mockRouteState = {
      ...mockRouteState,
      resource_type: 'offer' as any,
      resource: 'r-1',
    };
    render(
      createElement(LibraryEntryDetail, {
        route: mockRouteState as any,
        onTabChange: mockOnTabChange,
        onVersionChange: mockOnVersionChange,
        onDismissResource: mockOnDismissResource,
      })
    );
    expect(screen.getByText('Summer Offer')).toBeInTheDocument();
    expect(screen.getByText('library:nested.dismiss')).toBeInTheDocument();
  });
});

describe('LibraryEntryDetail — no entry selected', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    useDomainQueryCalls.length = 0;
    mockQueryOverrides = {};
    mockDetailResult = {
      data: undefined,
      problem: undefined,
      isLoading: false,
      isStale: false,
      lastSuccessfulAt: null,
      refresh: jest.fn(),
    };
  });

  it('shows no-selection state when entry is null', () => {
    const route = {
      cat: 'product',
      entry: null,
      tab: null,
      version: null,
      resource_type: null,
      resource: null,
      resource_tab: null,
      resource_version: null,
      parent_type: null,
      parent: null,
    };
    render(
      createElement(LibraryEntryDetail, {
        route: route as any,
        onTabChange: mockOnTabChange,
        onVersionChange: mockOnVersionChange,
        onDismissResource: mockOnDismissResource,
      })
    );
    expect(screen.getByText('library:detail.no-selection')).toBeInTheDocument();
  });
});

describe('LibraryEntryDetail — hidden tabs do not fire queries', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    useDomainQueryCalls.length = 0;
    mockQueryOverrides = {};
    mockRouteState = {
      cat: 'product',
      entry: 'e1',
      tab: 'draft',
      version: null,
      resource_type: null,
      resource: null,
      resource_tab: null,
      resource_version: null,
      parent_type: null,
      parent: null,
    };
    mockDetailResult = {
      data: makeEntryData(),
      problem: undefined,
      isLoading: false,
      isStale: false,
      lastSuccessfulAt: null,
      refresh: jest.fn(),
    };
    mockPaginatedResult = {
      items: [],
      hasMore: false,
      isLoading: false,
      isLoadingMore: false,
      isRefreshing: false,
      problem: undefined,
      loadMore: jest.fn(),
      refresh: jest.fn(),
    };
  });

  it('draft tab does not fire version or events queries', () => {
    render(
      createElement(LibraryEntryDetail, {
        route: mockRouteState as any,
        onTabChange: mockOnTabChange,
        onVersionChange: mockOnVersionChange,
        onDismissResource: mockOnDismissResource,
      })
    );

    const operationIds = useDomainQueryCalls.map((c) => c.operationId);
    expect(operationIds).toContain('get_library_entry');
    expect(operationIds).not.toContain('get_library_version');
    expect(operationIds).not.toContain('get_library_version_impact');
  });
});
