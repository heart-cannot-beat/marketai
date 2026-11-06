/**
 * @jest-environment jsdom
 */
import { renderHook } from '@testing-library/react';

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
    firstPage: undefined,
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
      firstPage: undefined,
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

  it('returns first-page facets and collection action eligibility', () => {
    const facets = {
      providers: [{ value: 'openai', count: 2 }],
      origins: [],
      modes: [],
      reviews: [],
      aspects: [],
      qc_states: [],
      tags: [],
    };
    const allowedActions = [
      { action: 'export', allowed: true, reason_code: null },
      { action: 'review', allowed: false, reason_code: 'role_not_permitted' },
    ];
    mockPaginatedQuery.mockReturnValue({
      items: [{ id: 'v1' }],
      firstPage: {
        items: [{ id: 'v1' }],
        next_cursor: null,
        facets,
        allowed_actions: allowedActions,
      },
      hasMore: false,
      isLoading: false,
      isLoadingMore: false,
      isRefreshing: false,
      problem: undefined,
      loadMore: jest.fn(),
      refresh: jest.fn(),
    });

    const { result } = renderHook(() => useAssetGallery(defaultFilters));

    expect(result.current.facets).toEqual(facets);
    expect(result.current.allowedActions).toEqual(allowedActions);
  });
});
