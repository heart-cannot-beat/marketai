/**
 * @jest-environment jsdom
 */
import { act, renderHook } from '@testing-library/react';

const mockPreviewPublish = jest.fn();
const mockPreviewArchive = jest.fn();
const mockPreviewWithdraw = jest.fn();

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

jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => ({
    previewLibraryPublishImpact: mockPreviewPublish,
    previewLibraryArchiveImpact: mockPreviewArchive,
    previewLibraryWithdrawImpact: mockPreviewWithdraw,
  }),
}));

import { useLibraryCommandImpact } from '@/hooks/library/useLibraryCommandImpact';

const META = { etag: null, retryAt: null, location: null };

function successResult(
  projection: Record<string, unknown> = {},
  overrides: Record<string, unknown> = {}
) {
  return {
    kind: 'created',
    status: 201,
    data: {
      impact_id: 'imp-1',
      impact_hash: 'hash-abc',
      command_kind: 'publish',
      target_entry_id: 'entry-1',
      target_revision: 3,
      content_hash: 'content-hash',
      expires_at: '2026-09-05T08:00:00.000Z',
      projection,
      ...overrides,
    },
    meta: META,
  };
}

describe('useLibraryCommandImpact', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockPreviewPublish.mockResolvedValue(successResult());
    mockPreviewArchive.mockResolvedValue(successResult());
    mockPreviewWithdraw.mockResolvedValue(successResult());
  });

  it('starts with empty state', () => {
    const { result } = renderHook(() => useLibraryCommandImpact());

    expect(result.current.impact).toBeNull();
    expect(result.current.isLoading).toBe(false);
    expect(result.current.problem).toBeUndefined();
    expect(result.current.isStale).toBe(false);
    expect(result.current.confirmed).toBe(false);
  });

  it('uses the fresh publish preview endpoint and maps its projection', async () => {
    mockPreviewPublish.mockResolvedValue(
      successResult({
        blocking: [{ code: 'required', message: 'A field is required' }],
        warnings: [{ code: 'review', message: 'Review this value' }],
        dependents: [
          {
            version_id: 'version-2',
            entry_id: 'entry-2',
            entry_type: 'product',
            eligibility: 'eligible',
          },
        ],
      })
    );
    const { result } = renderHook(() => useLibraryCommandImpact());

    await act(async () => {
      await result.current.fetch('publish', 'entry-1');
    });

    expect(mockPreviewPublish).toHaveBeenCalledWith(
      expect.objectContaining({ workspaceSlug: 'acme', entryId: 'entry-1' })
    );
    expect(result.current.impact).toMatchObject({
      impactId: 'imp-1',
      impactHash: 'hash-abc',
      commandKind: 'publish',
      targetId: 'entry-1',
      targetRevision: 3,
      contentHash: 'content-hash',
      blockingReasons: ['A field is required'],
      warnings: ['Review this value'],
      // Codes travel with the messages so the publish dialog can send
      // FastAPI the exact `acknowledged_warnings` it expects.
      warningFindings: [{ code: 'review', message: 'Review this value' }],
      affectedSummaries: [
        {
          id: 'version-2',
          display_name: 'entry-2',
          entry_type: 'product',
          effect: 'eligible',
        },
      ],
    });
    expect(result.current.impact?.expiry).toBe(
      Date.parse('2026-09-05T08:00:00.000Z')
    );
  });

  it('dispatches archive and withdraw previews using the correct target', async () => {
    const { result } = renderHook(() => useLibraryCommandImpact());

    await act(async () => result.current.fetch('archive', 'entry-3'));
    expect(mockPreviewArchive).toHaveBeenCalledWith(
      expect.objectContaining({ entryId: 'entry-3' })
    );

    await act(async () => result.current.fetch('withdraw', 'version-4'));
    expect(mockPreviewWithdraw).toHaveBeenCalledWith(
      expect.objectContaining({ versionId: 'version-4' })
    );
  });

  it('sets problem on a definitive gateway error', async () => {
    mockPreviewPublish.mockResolvedValue({
      kind: 'problem',
      status: 500,
      problem: {
        type: 'server_error',
        title: 'Server Error',
        status: 500,
        detail: 'Internal failure',
        instance: '',
        code: 'internal',
        trace_id: '',
        retryable: true,
        field_errors: [],
      },
      meta: META,
    });
    const { result } = renderHook(() => useLibraryCommandImpact());

    await act(async () => result.current.fetch('publish', 'entry-1'));

    expect(result.current.impact).toBeNull();
    expect(result.current.problem?.detail).toBe('Internal failure');
  });

  it('marks an ambiguous preview outcome stale', async () => {
    mockPreviewArchive.mockResolvedValue({
      kind: 'reconciliation_required',
      actionInstanceId: 'action-1',
      staleSince: Date.now(),
    });
    const { result } = renderHook(() => useLibraryCommandImpact());

    await act(async () => result.current.fetch('archive', 'entry-1'));

    expect(result.current.impact).toBeNull();
    expect(result.current.isStale).toBe(true);
    expect(result.current.problem?.code).toBe('impact_reconciliation_required');
  });

  it('confirms, invalidates confirmation, and clears state', async () => {
    const { result } = renderHook(() => useLibraryCommandImpact());
    await act(async () => result.current.fetch('publish', 'entry-1'));

    act(() => result.current.confirm());
    expect(result.current.confirmed).toBe(true);

    act(() => result.current.resetConfirmation());
    expect(result.current.confirmed).toBe(false);
    expect(result.current.isStale).toBe(true);

    act(() => result.current.clear());
    expect(result.current.impact).toBeNull();
    expect(result.current.problem).toBeUndefined();
    expect(result.current.isStale).toBe(false);
  });

  it('handles network errors gracefully', async () => {
    mockPreviewArchive.mockRejectedValue(new Error('Network error'));
    const { result } = renderHook(() => useLibraryCommandImpact());

    await act(async () => result.current.fetch('archive', 'entry-5'));

    expect(result.current.impact).toBeNull();
    expect(result.current.problem?.code).toBe('network_error');
    expect(result.current.isLoading).toBe(false);
  });
});
