/**
 * @jest-environment jsdom
 */
import { createElement, type ReactNode } from 'react';
import { renderHook, waitFor, act } from '@testing-library/react';
import { SWRConfig } from 'swr';
import { useDomainPaginatedQuery } from '@/hooks/domain/useDomainPaginatedQuery';

let mockSlug = 'acme';

const mockGateway = {
  listLibraryEntries: jest.fn(),
  listLibraryEntryVersions: jest.fn(),
  listLibraryEntryEvents: jest.fn(),
  listLibraryVersionBindings: jest.fn(),
  listLibraryInternalEntries: jest.fn(),
};

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: {
      slug: mockSlug,
      workspaceId: 'w1',
      role: 'OWNER',
      membershipId: 'm1',
      displayName: 'Acme',
    },
  }),
}));

jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));

function wrapper({ children }: { children: ReactNode }) {
  return createElement(
    SWRConfig,
    { value: { provider: () => new Map(), dedupingInterval: 0 } },
    children
  );
}

function makePage(items: Array<{ id: string }>, nextCursor?: string) {
  return {
    kind: 'ok' as const,
    status: 200,
    data: { items, next_cursor: nextCursor ?? null },
    meta: { etag: null, retryAt: null, location: null },
  };
}

function makeProblem(status: number, code: string) {
  return {
    kind: 'problem' as const,
    status,
    problem: {
      type: code,
      title: code,
      status,
      detail: '',
      instance: '',
      code,
      trace_id: '',
      retryable: false,
      field_errors: [],
    },
    meta: { etag: null, retryAt: null, location: null },
  };
}

describe('useDomainPaginatedQuery', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockSlug = 'acme';
  });

  it('loads first page and exposes items', async () => {
    mockGateway.listLibraryEntries.mockResolvedValue(
      makePage([{ id: 'e1' }, { id: 'e2' }])
    );

    const { result } = renderHook(
      () =>
        useDomainPaginatedQuery('list_library_entries', {
          entry_type: 'product',
        }),
      { wrapper }
    );

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.items).toEqual([{ id: 'e1' }, { id: 'e2' }]);
    expect(result.current.hasMore).toBe(false);
    expect(result.current.problem).toBeUndefined();
  });

  it('revalidates the first page after remounting a hydrated scope', async () => {
    const cache = new Map();
    const persistentWrapper = ({ children }: { children: ReactNode }) =>
      createElement(
        SWRConfig,
        { value: { provider: () => cache, dedupingInterval: 0 } },
        children
      );
    mockGateway.listLibraryEntries.mockResolvedValue(
      makePage([{ id: 'stale' }])
    );

    const first = renderHook(
      () =>
        useDomainPaginatedQuery('list_library_entries', {
          entry_type: 'product',
        }),
      { wrapper: persistentWrapper }
    );
    await waitFor(() =>
      expect(first.result.current.items).toEqual([{ id: 'stale' }])
    );
    first.unmount();
    const callsBeforeRemount = mockGateway.listLibraryEntries.mock.calls.length;

    mockGateway.listLibraryEntries.mockResolvedValue(
      makePage([{ id: 'fresh' }])
    );
    const second = renderHook(
      () =>
        useDomainPaginatedQuery('list_library_entries', {
          entry_type: 'product',
        }),
      { wrapper: persistentWrapper }
    );

    await waitFor(() =>
      expect(second.result.current.items).toEqual([{ id: 'fresh' }])
    );
    expect(mockGateway.listLibraryEntries.mock.calls.length).toBeGreaterThan(
      callsBeforeRemount
    );
  });

  it('refreshes through the direct first-page path', async () => {
    mockGateway.listLibraryEntries.mockResolvedValue(
      makePage([{ id: 'before-refresh' }])
    );
    const { result } = renderHook(
      () =>
        useDomainPaginatedQuery('list_library_entries', {
          entry_type: 'product',
        }),
      { wrapper }
    );
    await waitFor(() =>
      expect(result.current.items).toEqual([{ id: 'before-refresh' }])
    );

    mockGateway.listLibraryEntries.mockResolvedValue(
      makePage([{ id: 'after-refresh' }])
    );
    await act(async () => result.current.refresh());

    expect(result.current.items).toEqual([{ id: 'after-refresh' }]);
  });

  it('reports hasMore when next_cursor is present', async () => {
    mockGateway.listLibraryEntries.mockResolvedValue(
      makePage([{ id: 'e1' }], 'cursor-2')
    );

    const { result } = renderHook(
      () =>
        useDomainPaginatedQuery('list_library_entries', {
          entry_type: 'product',
        }),
      { wrapper }
    );

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.hasMore).toBe(true);
  });

  it('deduplicates items by id across pages', async () => {
    mockGateway.listLibraryEntries.mockImplementation(
      (args: { cursor?: string }) => {
        if (args.cursor === 'c2') {
          return Promise.resolve(makePage([{ id: 'e2' }, { id: 'e3' }]));
        }
        return Promise.resolve(makePage([{ id: 'e1' }, { id: 'e2' }], 'c2'));
      }
    );

    const { result } = renderHook(
      () =>
        useDomainPaginatedQuery('list_library_entries', {
          entry_type: 'product',
        }),
      { wrapper }
    );

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.hasMore).toBe(true);

    await act(async () => {
      result.current.loadMore();
    });

    await waitFor(() =>
      expect(result.current.items.length).toBeGreaterThanOrEqual(3)
    );
    expect(result.current.items).toEqual([
      { id: 'e1' },
      { id: 'e2' },
      { id: 'e3' },
    ]);
  });

  it('does not fetch when enabled is false', () => {
    renderHook(
      () =>
        useDomainPaginatedQuery(
          'list_library_entries',
          { entry_type: 'product' },
          { enabled: false }
        ),
      { wrapper }
    );
    expect(mockGateway.listLibraryEntries).not.toHaveBeenCalled();
  });

  it('surfaces problem from page-two error', async () => {
    mockGateway.listLibraryEntries.mockImplementation(
      (args: { cursor?: string }) => {
        if (args.cursor === 'c2') {
          return Promise.resolve(makeProblem(500, 'internal_error'));
        }
        return Promise.resolve(makePage([{ id: 'e1' }], 'c2'));
      }
    );

    const { result } = renderHook(
      () =>
        useDomainPaginatedQuery('list_library_entries', {
          entry_type: 'product',
        }),
      { wrapper }
    );

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    await act(async () => {
      result.current.loadMore();
    });

    await waitFor(() => expect(result.current.problem).toBeDefined());
    expect(result.current.problem?.code).toBe('internal_error');
    expect(result.current.items).toEqual([{ id: 'e1' }]);
  });

  it('handles empty page with null next_cursor', async () => {
    mockGateway.listLibraryEntries.mockResolvedValue(makePage([]));

    const { result } = renderHook(
      () =>
        useDomainPaginatedQuery('list_library_entries', {
          entry_type: 'product',
        }),
      { wrapper }
    );

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.items).toEqual([]);
    expect(result.current.hasMore).toBe(false);
  });

  it('respects custom pageSize', async () => {
    mockGateway.listLibraryEntries.mockResolvedValue(makePage([{ id: 'e1' }]));

    renderHook(
      () =>
        useDomainPaginatedQuery(
          'list_library_entries',
          { entry_type: 'product' },
          { pageSize: 25 }
        ),
      { wrapper }
    );

    await waitFor(() =>
      expect(mockGateway.listLibraryEntries).toHaveBeenCalledWith(
        expect.objectContaining({ limit: 25 })
      )
    );
  });

  it('coalesces duplicate loadMore calls', async () => {
    let callCount = 0;
    mockGateway.listLibraryEntries.mockImplementation(
      (args: { cursor?: string }) => {
        callCount++;
        if (args.cursor === 'c2') {
          return Promise.resolve(makePage([{ id: 'e2' }]));
        }
        return Promise.resolve(makePage([{ id: 'e1' }], 'c2'));
      }
    );

    const { result } = renderHook(
      () =>
        useDomainPaginatedQuery('list_library_entries', {
          entry_type: 'product',
        }),
      { wrapper }
    );

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const callsBeforeLoad = callCount;

    await act(async () => {
      result.current.loadMore();
      result.current.loadMore();
      result.current.loadMore();
    });

    await waitFor(() => expect(result.current.isLoadingMore).toBe(false));
    const additionalCalls = callCount - callsBeforeLoad;
    expect(additionalCalls).toBeLessThanOrEqual(2);
  });

  it('works with list_library_entry_versions', async () => {
    mockGateway.listLibraryEntryVersions.mockResolvedValue(
      makePage([{ id: 'v1' }])
    );

    const { result } = renderHook(
      () =>
        useDomainPaginatedQuery('list_library_entry_versions', {
          entry_id: 'e1',
        }),
      { wrapper }
    );

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.items).toEqual([{ id: 'v1' }]);
  });

  it('works with list_library_entry_events', async () => {
    mockGateway.listLibraryEntryEvents.mockResolvedValue(
      makePage([{ id: 'ev1' }])
    );

    const { result } = renderHook(
      () =>
        useDomainPaginatedQuery('list_library_entry_events', {
          entry_id: 'e1',
        }),
      { wrapper }
    );

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.items).toEqual([{ id: 'ev1' }]);
  });

  it('works with list_library_version_bindings', async () => {
    mockGateway.listLibraryVersionBindings.mockResolvedValue(
      makePage([{ id: 'b1' }])
    );

    const { result } = renderHook(
      () =>
        useDomainPaginatedQuery('list_library_version_bindings', {
          version_id: 'v1',
        }),
      { wrapper }
    );

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.items).toEqual([{ id: 'b1' }]);
  });

  it('works with list_library_internal_entries', async () => {
    mockGateway.listLibraryInternalEntries.mockResolvedValue(
      makePage([{ id: 'ie1' }])
    );

    const { result } = renderHook(
      () =>
        useDomainPaginatedQuery('list_library_internal_entries', {
          root_entry_id: 'e1',
          entry_type: 'ingredient',
        }),
      { wrapper }
    );

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.items).toEqual([{ id: 'ie1' }]);
  });
});
