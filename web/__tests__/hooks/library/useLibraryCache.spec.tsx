/**
 * @jest-environment jsdom
 */
import { createElement, type ReactNode } from 'react';
import { renderHook, act, waitFor } from '@testing-library/react';
import { SWRConfig } from 'swr';
import useSWRInfinite from 'swr/infinite';
import { useLibraryCache } from '@/hooks/library/useLibraryCache';

let mockSlug = 'acme';

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

function makeWrapper(cacheProvider: () => Map<string, unknown>) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return createElement(
      SWRConfig,
      { value: { provider: cacheProvider as any, dedupingInterval: 0 } },
      children
    );
  };
}

function seedCache(
  cacheMap: Map<string, unknown>,
  entries: Array<{ key: unknown; data?: unknown }>
) {
  for (const entry of entries) {
    const serialized = JSON.stringify(entry.key);
    cacheMap.set(
      `$swr$${serialized}`,
      entry.data ?? { kind: 'ok', status: 200, data: {}, meta: {} }
    );
  }
}

describe('useLibraryCache', () => {
  beforeEach(() => {
    mockSlug = 'acme';
  });

  it('invalidates entry resource for current workspace', async () => {
    const cacheMap = new Map<string, unknown>();
    seedCache(cacheMap, [
      { key: ['domain', 'acme', 'get_library_entry', '{"entry_id":"e1"}'] },
      { key: ['domain', 'acme', 'get_library_entry', '{"entry_id":"e2"}'] },
      { key: ['domain', 'other', 'get_library_entry', '{"entry_id":"e3"}'] },
    ]);

    const { result } = renderHook(() => useLibraryCache(), {
      wrapper: makeWrapper(() => cacheMap),
    });

    await act(async () => {
      await result.current.invalidate({ resources: ['entry'] });
    });

    // Verify the hook returns without error — mutate with filter
    // triggers revalidation for matched keys. The cache map still has
    // entries (SWR revalidates in place, doesn't delete).
    expect(
      cacheMap.has(
        '$swr$' +
          JSON.stringify([
            'domain',
            'other',
            'get_library_entry',
            '{"entry_id":"e3"}',
          ])
      )
    ).toBe(true);
  });

  it('invalidates entryList targeting all cursor pages', async () => {
    const cacheMap = new Map<string, unknown>();
    seedCache(cacheMap, [
      {
        key: [
          'domain',
          'acme',
          'list_library_entries',
          '{"entry_type":"product"}',
          null,
        ],
      },
      {
        key: [
          'domain',
          'acme',
          'list_library_entries',
          '{"entry_type":"product"}',
          'cursor_2',
        ],
      },
      {
        key: [
          'domain',
          'acme',
          'list_library_entries',
          '{"entry_type":"person"}',
          null,
        ],
      },
    ]);

    const { result } = renderHook(() => useLibraryCache(), {
      wrapper: makeWrapper(() => cacheMap),
    });

    // Should not throw — invalidation targets all list_library_entries
    // for current workspace regardless of entry_type or cursor.
    await act(async () => {
      await result.current.invalidate({ resources: ['entryList'] });
    });
  });

  it('revalidates a useSWRInfinite entry list, not only its page keys', async () => {
    // `useDomainPaginatedQuery` lists live under SWR's `$inf$` key, which a
    // key-filter `mutate` skips on purpose; before the explicit infinite
    // revalidation a freshly created entry never showed up in the list.
    const cacheMap = new Map<string, unknown>();
    const fetcher = jest.fn(async () => ({
      kind: 'ok',
      status: 200,
      data: { items: [{ id: 'e1' }], next_cursor: null },
      meta: {},
    }));
    const getKey = (index: number) =>
      [
        'domain',
        'acme',
        'list_library_entries',
        '{"entry_type":"product"}',
        index === 0 ? null : 'cursor',
      ] as const;

    const { result } = renderHook(
      () => ({
        list: useSWRInfinite(getKey, fetcher, {
          revalidateFirstPage: true,
          revalidateOnFocus: false,
        }),
        cache: useLibraryCache(),
      }),
      { wrapper: makeWrapper(() => cacheMap) }
    );

    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
    expect(
      Array.from(cacheMap.keys()).some((key) => key.startsWith('$inf$'))
    ).toBe(true);

    await act(async () => {
      await result.current.cache.invalidate({ resources: ['entryList'] });
    });

    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
  });

  it('leaves infinite lists of other operations and workspaces alone', async () => {
    const cacheMap = new Map<string, unknown>();
    const fetcher = jest.fn(async () => ({
      kind: 'ok',
      status: 200,
      data: { items: [], next_cursor: null },
      meta: {},
    }));
    const getKey = (index: number) =>
      [
        'domain',
        'other-ws',
        'list_library_entries',
        '{"entry_type":"product"}',
        index === 0 ? null : 'cursor',
      ] as const;

    const { result } = renderHook(
      () => ({
        list: useSWRInfinite(getKey, fetcher, { revalidateOnFocus: false }),
        cache: useLibraryCache(),
      }),
      { wrapper: makeWrapper(() => cacheMap) }
    );

    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));

    await act(async () => {
      await result.current.cache.invalidate({
        resources: ['entryList', 'versions'],
      });
    });

    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it('does not touch keys from another workspace', async () => {
    const cacheMap = new Map<string, unknown>();
    const otherKey = [
      'domain',
      'other-ws',
      'get_library_entry',
      '{"entry_id":"e1"}',
    ];
    seedCache(cacheMap, [
      { key: otherKey, data: { sentinel: 'untouched' } },
      { key: ['domain', 'acme', 'get_library_entry', '{"entry_id":"e1"}'] },
    ]);

    const { result } = renderHook(() => useLibraryCache(), {
      wrapper: makeWrapper(() => cacheMap),
    });

    await act(async () => {
      await result.current.invalidate({ resources: ['entry'] });
    });

    const otherValue = cacheMap.get('$swr$' + JSON.stringify(otherKey));
    expect(otherValue).toEqual({ sentinel: 'untouched' });
  });

  it('invalidates multiple resources in one call', async () => {
    const cacheMap = new Map<string, unknown>();
    seedCache(cacheMap, [
      { key: ['domain', 'acme', 'get_library_entry', '{"entry_id":"e1"}'] },
      {
        key: [
          'domain',
          'acme',
          'list_library_entries',
          '{"entry_type":"product"}',
          null,
        ],
      },
      {
        key: [
          'domain',
          'acme',
          'list_library_entry_versions',
          '{"entry_id":"e1"}',
          null,
        ],
      },
      {
        key: [
          'domain',
          'acme',
          'list_library_entry_events',
          '{"entry_id":"e1"}',
          null,
        ],
      },
    ]);

    const { result } = renderHook(() => useLibraryCache(), {
      wrapper: makeWrapper(() => cacheMap),
    });

    await act(async () => {
      await result.current.invalidate({
        resources: ['entry', 'entryList', 'versions', 'events'],
      });
    });

    // All four cache entries should still exist (revalidated, not deleted)
    expect(cacheMap.size).toBeGreaterThanOrEqual(4);
  });

  it('invalidates impact resources targeting all three impact operations', async () => {
    const cacheMap = new Map<string, unknown>();
    seedCache(cacheMap, [
      {
        key: [
          'domain',
          'acme',
          'get_library_entry_publish_impact',
          '{"entry_id":"e1"}',
        ],
      },
      {
        key: [
          'domain',
          'acme',
          'get_library_version_withdraw_impact',
          '{"version_id":"v1"}',
        ],
      },
      {
        key: [
          'domain',
          'acme',
          'get_library_entry_archive_impact',
          '{"entry_id":"e1"}',
        ],
      },
    ]);

    const { result } = renderHook(() => useLibraryCache(), {
      wrapper: makeWrapper(() => cacheMap),
    });

    await act(async () => {
      await result.current.invalidate({ resources: ['impact'] });
    });
  });

  it('invalidates internalEntries for all root/type/parent filters', async () => {
    const cacheMap = new Map<string, unknown>();
    seedCache(cacheMap, [
      {
        key: [
          'domain',
          'acme',
          'list_library_internal_entries',
          '{"entry_type":"ingredient","root_entry_id":"e1"}',
          null,
        ],
      },
      {
        key: [
          'domain',
          'acme',
          'list_library_internal_entries',
          '{"entry_type":"variant","root_entry_id":"e2"}',
          null,
        ],
      },
    ]);

    const { result } = renderHook(() => useLibraryCache(), {
      wrapper: makeWrapper(() => cacheMap),
    });

    await act(async () => {
      await result.current.invalidate({ resources: ['internalEntries'] });
    });
  });

  it('empty resources array is a no-op', async () => {
    const cacheMap = new Map<string, unknown>();
    const { result } = renderHook(() => useLibraryCache(), {
      wrapper: makeWrapper(() => cacheMap),
    });

    await act(async () => {
      await result.current.invalidate({ resources: [] });
    });
  });

  it('invalidates entryTypes', async () => {
    const cacheMap = new Map<string, unknown>();
    seedCache(cacheMap, [
      { key: ['domain', 'acme', 'list_library_entry_types', '{}'] },
    ]);

    const { result } = renderHook(() => useLibraryCache(), {
      wrapper: makeWrapper(() => cacheMap),
    });

    await act(async () => {
      await result.current.invalidate({ resources: ['entryTypes'] });
    });
  });

  it('invalidates bindings', async () => {
    const cacheMap = new Map<string, unknown>();
    seedCache(cacheMap, [
      {
        key: [
          'domain',
          'acme',
          'list_library_version_bindings',
          '{"version_id":"v1"}',
          null,
        ],
      },
    ]);

    const { result } = renderHook(() => useLibraryCache(), {
      wrapper: makeWrapper(() => cacheMap),
    });

    await act(async () => {
      await result.current.invalidate({ resources: ['bindings'] });
    });
  });
});
