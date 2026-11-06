/**
 * @jest-environment jsdom
 */
import { renderHook } from '@testing-library/react';

let mockQueryResult = {
  data: undefined as unknown,
  problem: undefined as unknown,
  isLoading: false,
  isStale: false,
  lastSuccessfulAt: null,
  refresh: jest.fn(),
};

let lastQueryCall: {
  operationId: string;
  params: unknown;
  options: unknown;
} | null = null;

jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: (operationId: string, params: unknown, options: unknown) => {
    lastQueryCall = { operationId, params, options };
    return mockQueryResult;
  },
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

import { useResolvedLibraryResource } from '@/hooks/library/useResolvedLibraryResource';

describe('useResolvedLibraryResource', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    lastQueryCall = null;
    mockQueryResult = {
      data: undefined,
      problem: undefined,
      isLoading: false,
      isStale: false,
      lastSuccessfulAt: null,
      refresh: jest.fn(),
    };
  });

  it('does not query when rootEntryId is null', () => {
    renderHook(() => useResolvedLibraryResource(null, 'offer', 'r-1'));
    expect(lastQueryCall?.options).toEqual({ enabled: false });
  });

  it('does not query when resourceType is null', () => {
    renderHook(() => useResolvedLibraryResource('root-1', null, 'r-1'));
    expect(lastQueryCall?.options).toEqual({ enabled: false });
  });

  it('does not query when resourceId is null', () => {
    renderHook(() => useResolvedLibraryResource('root-1', 'offer', null));
    expect(lastQueryCall?.options).toEqual({ enabled: false });
  });

  it('queries get_library_internal_entry_context with correct params', () => {
    renderHook(() => useResolvedLibraryResource('root-1', 'offer', 'r-1'));
    expect(lastQueryCall?.operationId).toBe(
      'get_library_internal_entry_context'
    );
    expect(lastQueryCall?.options).toEqual({ enabled: true });
  });

  it('returns resource when resolved', () => {
    mockQueryResult.data = {
      id: 'r-1',
      entry_type: 'offer',
      display_name: 'Summer Offer',
      lifecycle: 'active',
      current_version_id: 'v-1',
      root_entry_id: 'root-1',
      root_entry_type: 'product',
      parent_entry_id: null,
      relation_kind: 'child',
      creation_evidence: 'manual',
    };
    const { result } = renderHook(() =>
      useResolvedLibraryResource('root-1', 'offer', 'r-1')
    );
    expect(result.current.resource).toBeDefined();
    expect(result.current.resource?.id).toBe('r-1');
    expect(result.current.notFound).toBe(false);
  });

  it('sets notFound for 404 and clears problem', () => {
    mockQueryResult.problem = {
      type: 'not_found',
      status: 404,
      detail: '',
      instance: '',
      code: 'not_found',
      trace_id: '',
      retryable: false,
      field_errors: [],
    };
    const { result } = renderHook(() =>
      useResolvedLibraryResource('root-1', 'offer', 'r-missing')
    );
    expect(result.current.notFound).toBe(true);
    expect(result.current.problem).toBeUndefined();
    expect(result.current.resource).toBeUndefined();
  });

  it('returns problem for non-404 errors', () => {
    mockQueryResult.problem = {
      type: 'server_error',
      status: 500,
      detail: '',
      instance: '',
      code: 'server_error',
      trace_id: '',
      retryable: true,
      field_errors: [],
    };
    const { result } = renderHook(() =>
      useResolvedLibraryResource('root-1', 'offer', 'r-1')
    );
    expect(result.current.notFound).toBe(false);
    expect(result.current.problem).toBeDefined();
    expect(result.current.problem?.status).toBe(500);
  });

  it('returns isLoading when query is loading', () => {
    mockQueryResult.isLoading = true;
    const { result } = renderHook(() =>
      useResolvedLibraryResource('root-1', 'offer', 'r-1')
    );
    expect(result.current.isLoading).toBe(true);
  });
});
