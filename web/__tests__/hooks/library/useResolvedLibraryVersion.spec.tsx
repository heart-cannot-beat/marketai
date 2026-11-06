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

import { useResolvedLibraryVersion } from '@/hooks/library/useResolvedLibraryVersion';

describe('useResolvedLibraryVersion', () => {
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

  it('does not query when versionId is null', () => {
    renderHook(() => useResolvedLibraryVersion(null, 'entry-1'));
    expect(lastQueryCall?.options).toEqual({ enabled: false });
  });

  it('queries get_library_version with correct params', () => {
    renderHook(() => useResolvedLibraryVersion('v-1', 'entry-1'));
    expect(lastQueryCall?.operationId).toBe('get_library_version');
    expect(lastQueryCall?.options).toEqual({ enabled: true });
  });

  it('returns version when entry_id matches', () => {
    mockQueryResult.data = {
      id: 'v-1',
      entry_id: 'entry-1',
      entry_type: 'product',
      version_no: 1,
      state: 'effective',
      eligibility: 'eligible',
    };
    const { result } = renderHook(() =>
      useResolvedLibraryVersion('v-1', 'entry-1')
    );
    expect(result.current.version).toBeDefined();
    expect(result.current.version?.id).toBe('v-1');
    expect(result.current.mismatch).toBe(false);
  });

  it('detects mismatch when entry_id differs', () => {
    mockQueryResult.data = {
      id: 'v-1',
      entry_id: 'entry-other',
      entry_type: 'product',
      version_no: 1,
      state: 'effective',
      eligibility: 'eligible',
    };
    const { result } = renderHook(() =>
      useResolvedLibraryVersion('v-1', 'entry-1')
    );
    expect(result.current.mismatch).toBe(true);
    expect(result.current.version).toBeUndefined();
  });

  it('returns problem when query fails', () => {
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
      useResolvedLibraryVersion('v-1', 'entry-1')
    );
    expect(result.current.problem).toBeDefined();
    expect(result.current.problem?.status).toBe(404);
  });

  it('returns isLoading when query is loading', () => {
    mockQueryResult.isLoading = true;
    const { result } = renderHook(() =>
      useResolvedLibraryVersion('v-1', 'entry-1')
    );
    expect(result.current.isLoading).toBe(true);
  });

  it('does not report mismatch when expectedEntryId is null', () => {
    mockQueryResult.data = {
      id: 'v-1',
      entry_id: 'entry-1',
      entry_type: 'product',
      version_no: 1,
      state: 'effective',
      eligibility: 'eligible',
    };
    const { result } = renderHook(() => useResolvedLibraryVersion('v-1', null));
    expect(result.current.mismatch).toBe(false);
    expect(result.current.version).toBeDefined();
  });
});
