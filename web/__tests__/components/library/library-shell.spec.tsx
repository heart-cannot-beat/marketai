/**
 * @jest-environment jsdom
 */
import { createElement } from 'react';
import { render, screen, fireEvent } from '@testing-library/react';

let mockRouteState = {
  cat: 'product' as const,
  entry: null as string | null,
  tab: null,
  version: null,
  resource_type: null,
  resource: null,
  resource_tab: null,
  resource_version: null,
  parent_type: null,
  parent: null,
};
const mockSetCategory = jest.fn();
const mockSetEntry = jest.fn();

jest.mock('@/hooks/library/useLibraryRoute', () => ({
  useLibraryRoute: () => ({
    state: mockRouteState,
    setCategory: mockSetCategory,
    setEntry: mockSetEntry,
    setTab: jest.fn(),
    setVersion: jest.fn(),
    setResource: jest.fn(),
    setResourceTab: jest.fn(),
    setResourceVersion: jest.fn(),
  }),
}));

let mockListResult = {
  items: [] as unknown[],
  hasMore: false,
  isLoading: false,
  isLoadingMore: false,
  isRefreshing: false,
  problem: undefined as unknown,
  loadMore: jest.fn(),
  refresh: jest.fn(),
};

jest.mock('@/hooks/library/useLibraryEntries', () => ({
  useLibraryEntries: () => mockListResult,
}));

let mockDetailResult = {
  data: undefined as unknown,
  problem: undefined as unknown,
  isLoading: false,
  isStale: false,
  lastSuccessfulAt: null,
  refresh: jest.fn(),
};

jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: () => mockDetailResult,
}));

jest.mock('next-i18next', () => ({
  useTranslation: (ns: string) => ({
    t: (key: string, opts?: Record<string, string>) => {
      if (opts?.defaultValue) return opts.defaultValue;
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

import { LibraryShell } from '@/components/library/LibraryShell';

describe('LibraryShell', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockRouteState = {
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
    mockListResult = {
      items: [],
      hasMore: false,
      isLoading: false,
      isLoadingMore: false,
      isRefreshing: false,
      problem: undefined,
      loadMore: jest.fn(),
      refresh: jest.fn(),
    };
    mockDetailResult = {
      data: undefined,
      problem: undefined,
      isLoading: false,
      isStale: false,
      lastSuccessfulAt: null,
      refresh: jest.fn(),
    };
  });

  it('renders the title', () => {
    render(createElement(LibraryShell));
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(
      'library:title'
    );
  });

  it('renders all 5 category buttons', () => {
    render(createElement(LibraryShell));
    expect(
      screen.getAllByText('library:category.product').length
    ).toBeGreaterThanOrEqual(1);
    expect(
      screen.getAllByText('library:category.person').length
    ).toBeGreaterThanOrEqual(1);
    expect(
      screen.getAllByText('library:category.template').length
    ).toBeGreaterThanOrEqual(1);
    expect(
      screen.getAllByText('library:category.prompt_policy').length
    ).toBeGreaterThanOrEqual(1);
    expect(
      screen.getAllByText('library:category.datasource').length
    ).toBeGreaterThanOrEqual(1);
  });

  it('calls setCategory when clicking a different category', () => {
    render(createElement(LibraryShell));
    const personButtons = screen.getAllByText('library:category.person');
    fireEvent.click(personButtons[0]);
    expect(mockSetCategory).toHaveBeenCalledWith('person');
  });

  it('shows no-selection prompt when no entry is selected', () => {
    render(createElement(LibraryShell));
    expect(screen.getByText('library:detail.no-selection')).toBeInTheDocument();
  });

  it('shows empty state when list has no items', () => {
    render(createElement(LibraryShell));
    expect(
      screen.getAllByText(/library:list\.empty/).length
    ).toBeGreaterThanOrEqual(1);
  });

  it('shows loading state for list', () => {
    mockListResult.isLoading = true;
    render(createElement(LibraryShell));
    expect(screen.getByText('library:list.loading')).toBeInTheDocument();
  });

  it('shows problem/retry state for list', () => {
    mockListResult.problem = {
      type: 'server_error',
      title: 'Error',
      status: 500,
      detail: '',
      instance: '',
      code: 'server_error',
      trace_id: '',
      retryable: true,
      field_errors: [],
    };
    render(createElement(LibraryShell));
    expect(screen.getByText('library:problem.title')).toBeInTheDocument();
    expect(screen.getByText('library:problem.retry')).toBeInTheDocument();
  });

  it('calls refresh when retry is clicked on problem state', () => {
    mockListResult.problem = {
      type: 'server_error',
      title: 'Error',
      status: 500,
      detail: '',
      instance: '',
      code: 'server_error',
      trace_id: '',
      retryable: true,
      field_errors: [],
    };
    render(createElement(LibraryShell));
    fireEvent.click(screen.getByText('library:problem.retry'));
    expect(mockListResult.refresh).toHaveBeenCalled();
  });

  it('renders entry rows and calls setEntry on click', () => {
    mockListResult.items = [
      {
        id: 'e1',
        entry_type: 'product',
        display_name: 'Widget',
        lifecycle: 'active',
        draft_state: 'editing',
        current_version_number: 1,
        current_version_state: 'effective',
        eligibility: null,
        updated_at: '2026-01-01',
        created_at: '2026-01-01',
      },
      {
        id: 'e2',
        entry_type: 'product',
        display_name: 'Gadget',
        lifecycle: 'active',
        draft_state: null,
        current_version_number: 2,
        current_version_state: 'effective',
        eligibility: null,
        updated_at: '2026-01-02',
        created_at: '2026-01-02',
      },
    ];
    render(createElement(LibraryShell));
    expect(screen.getByText('Widget')).toBeInTheDocument();
    expect(screen.getByText('Gadget')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Gadget'));
    expect(mockSetEntry).toHaveBeenCalledWith('e2');
  });

  it('shows end-of-list when hasMore is false and items exist', () => {
    mockListResult.items = [
      {
        id: 'e1',
        entry_type: 'product',
        display_name: 'Widget',
        lifecycle: 'active',
        draft_state: null,
        current_version_number: 1,
        current_version_state: 'effective',
        eligibility: null,
        updated_at: '2026-01-01',
        created_at: '2026-01-01',
      },
    ];
    render(createElement(LibraryShell));
    expect(screen.getByText('library:list.end-of-list')).toBeInTheDocument();
  });

  it('shows load-more button when hasMore is true', () => {
    mockListResult.items = [
      {
        id: 'e1',
        entry_type: 'product',
        display_name: 'Widget',
        lifecycle: 'active',
        draft_state: null,
        current_version_number: 1,
        current_version_state: 'effective',
        eligibility: null,
        updated_at: '2026-01-01',
        created_at: '2026-01-01',
      },
    ];
    mockListResult.hasMore = true;
    render(createElement(LibraryShell));
    const loadMore = screen.getByText('library:list.load-more');
    expect(loadMore).toBeInTheDocument();
    fireEvent.click(loadMore);
    expect(mockListResult.loadMore).toHaveBeenCalled();
  });

  it('shows refreshing indicator', () => {
    mockListResult.items = [
      {
        id: 'e1',
        entry_type: 'product',
        display_name: 'Widget',
        lifecycle: 'active',
        draft_state: null,
        current_version_number: 1,
        current_version_state: 'effective',
        eligibility: null,
        updated_at: '2026-01-01',
        created_at: '2026-01-01',
      },
    ];
    mockListResult.isRefreshing = true;
    render(createElement(LibraryShell));
    expect(screen.getByText('library:list.refreshing')).toBeInTheDocument();
  });
});

describe('LibraryShell — detail pane', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockRouteState = {
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
    mockListResult = {
      items: [
        {
          id: 'e1',
          entry_type: 'product',
          display_name: 'Widget',
          lifecycle: 'active',
          draft_state: 'editing',
          current_version_number: 1,
          current_version_state: 'effective',
          eligibility: null,
          updated_at: '2026-01-01',
          created_at: '2026-01-01',
        },
      ],
      hasMore: false,
      isLoading: false,
      isLoadingMore: false,
      isRefreshing: false,
      problem: undefined,
      loadMore: jest.fn(),
      refresh: jest.fn(),
    };
    mockDetailResult = {
      data: undefined,
      problem: undefined,
      isLoading: false,
      isStale: false,
      lastSuccessfulAt: null,
      refresh: jest.fn(),
    };
  });

  it('shows loading state in detail pane', () => {
    mockRouteState = { ...mockRouteState, entry: 'e1' };
    mockDetailResult = { ...mockDetailResult, isLoading: true };
    render(createElement(LibraryShell));
    expect(screen.getByText('library:detail.loading')).toBeInTheDocument();
  });

  it('shows not-found for 404 in detail', () => {
    mockRouteState = { ...mockRouteState, entry: 'e-missing' };
    mockDetailResult = {
      ...mockDetailResult,
      problem: {
        type: 'not_found',
        title: 'Not Found',
        status: 404,
        detail: '',
        instance: '',
        code: 'not_found',
        trace_id: '',
        retryable: false,
        field_errors: [],
      },
    };
    render(createElement(LibraryShell));
    expect(screen.getByText('library:detail.not-found')).toBeInTheDocument();
  });

  it('shows problem/retry for non-404 detail error', () => {
    mockRouteState = { ...mockRouteState, entry: 'e1' };
    mockDetailResult = {
      ...mockDetailResult,
      problem: {
        type: 'server_error',
        title: 'Error',
        status: 500,
        detail: '',
        instance: '',
        code: 'server_error',
        trace_id: '',
        retryable: true,
        field_errors: [],
      },
    };
    render(createElement(LibraryShell));
    expect(screen.getByText('library:problem.title')).toBeInTheDocument();
  });

  it('renders entry header when detail loads successfully', () => {
    mockRouteState = { ...mockRouteState, entry: 'e1' };
    mockDetailResult = {
      ...mockDetailResult,
      data: {
        id: 'e1',
        entry_type: 'product',
        display_name: 'Widget Pro',
        lifecycle: 'active',
        draft_state: 'editing',
        current_version_number: 3,
        current_version_state: 'effective',
        eligibility: null,
        updated_at: '2026-01-01',
        created_at: '2026-01-01',
      },
    };
    render(createElement(LibraryShell));
    expect(screen.getByText('Widget Pro')).toBeInTheDocument();
  });
});
