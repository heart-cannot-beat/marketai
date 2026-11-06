/**
 * @jest-environment jsdom
 */
import { renderHook, act } from '@testing-library/react';

const mockExecute = jest.fn();
jest.mock('@/hooks/domain/useDomainCommand', () => ({
  useDomainCommand: () => ({
    execute: mockExecute,
    result: undefined,
    problem: undefined,
    isPending: false,
    reset: jest.fn(),
    retry: jest.fn(),
    reconciliation: undefined,
  }),
}));

const mockDomainQuery = jest.fn().mockReturnValue({
  data: undefined,
  problem: undefined,
  isLoading: false,
  isStale: false,
  lastSuccessfulAt: null,
  refresh: jest.fn(),
});

jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: (...args: unknown[]) => mockDomainQuery(...args),
}));

jest.mock('@/hooks/domain/useOperation', () => ({
  useOperation: () => ({
    operation: undefined,
    problem: undefined,
    isTerminal: false,
    isPartialSuccess: false,
  }),
}));

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme' } }),
}));

import { useExportFlow } from '@/hooks/assets/useExportFlow';

beforeEach(() => {
  jest.clearAllMocks();
});

describe('useExportFlow', () => {
  it('starts in configure step', () => {
    const { result } = renderHook(() => useExportFlow());

    expect(result.current.step).toBe('configure');
    expect(result.current.packageId).toBeUndefined();
  });

  it('transitions to processing after successful 202', async () => {
    mockExecute.mockResolvedValue({
      kind: 'success',
      data: { package_id: 'pkg-1', operation_id: 'op-1', state: 'queued' },
      meta: { etag: null, retryAt: null, location: '/api/v1/operations/op-1' },
    });

    const { result } = renderHook(() => useExportFlow());

    await act(async () => {
      await result.current.startExport({
        kind: 'approved_only',
      });
    });

    expect(result.current.step).toBe('processing');
    expect(result.current.packageId).toBe('pkg-1');
    expect(result.current.operationId).toBe('op-1');
  });

  it('transitions to ready when package state is ready', async () => {
    mockDomainQuery.mockReturnValue({
      data: {
        package_id: 'pkg-1',
        operation_id: 'op-1',
        state: 'ready',
        committed_zip_hash: 'abc123',
        committed_zip_size: 1024,
        logical_count: 10,
        logical_bytes: 5000,
      },
      problem: undefined,
      isLoading: false,
      isStale: false,
      lastSuccessfulAt: Date.now(),
      refresh: jest.fn(),
    });

    const { result } = renderHook(() =>
      useExportFlow({ initialPackageId: 'pkg-1' })
    );

    expect(result.current.step).toBe('ready');
  });

  it('returns problem when export creation fails', async () => {
    mockExecute.mockResolvedValue({
      kind: 'problem',
      problem: {
        code: 'mixed_execution_modes',
        title: 'Mixed modes',
        status: 422,
        detail: '',
        instance: '',
        type: '',
        trace_id: '',
        retryable: false,
        field_errors: [],
      },
      meta: { etag: null, retryAt: null, location: null },
    });

    const { result } = renderHook(() => useExportFlow());

    await act(async () => {
      await result.current.startExport({
        kind: 'explicit',
        version_ids: ['v1', 'v2'],
      });
    });

    expect(result.current.step).toBe('configure');
    expect(result.current.problem?.code).toBe('mixed_execution_modes');
  });

  it('handles recovery from existing packageId', () => {
    mockDomainQuery.mockReturnValue({
      data: {
        package_id: 'pkg-1',
        operation_id: 'op-1',
        state: 'building',
      },
      problem: undefined,
      isLoading: false,
      isStale: false,
      lastSuccessfulAt: Date.now(),
      refresh: jest.fn(),
    });

    const { result } = renderHook(() =>
      useExportFlow({ initialPackageId: 'pkg-1' })
    );

    expect(result.current.step).toBe('processing');
    expect(result.current.packageId).toBe('pkg-1');
  });
});
