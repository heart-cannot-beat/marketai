/**
 * @jest-environment jsdom
 */
import { renderHook, waitFor } from '@testing-library/react';
import { useDomainPaginatedQuery } from '@/hooks/domain/useDomainPaginatedQuery';

const mockGateway = {
  listGenerationAssets: jest.fn(),
};

const mockCurrentWorkspace = {
  slug: 'acme',
  workspaceId: 'w1',
  role: 'OWNER',
  membershipId: 'm1',
  displayName: 'Acme',
};

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: mockCurrentWorkspace }),
}));

jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));

function okResult<T>(data: T) {
  return {
    kind: 'ok' as const,
    status: 200,
    data,
    meta: { etag: null, retryAt: null, location: null },
  };
}

function makePage(
  items: Array<{ id: string }>,
  nextCursor: string | null = null
) {
  return {
    items,
    next_cursor: nextCursor,
    facets: {
      providers: [],
      origins: [],
      modes: [],
      reviews: [],
      aspects: [],
      qc_states: [],
      tags: [],
    },
    allowed_actions: [
      { action: 'download', allowed: true, reason_code: null },
      { action: 'export', allowed: true, reason_code: null },
    ],
  };
}

beforeEach(() => {
  jest.clearAllMocks();
});

describe('useDomainPaginatedQuery with list_generation_assets', () => {
  it('fetches the first page of assets', async () => {
    mockGateway.listGenerationAssets.mockResolvedValue(
      okResult(makePage([{ id: 'v1' }, { id: 'v2' }]))
    );

    const { result } = renderHook(() =>
      useDomainPaginatedQuery('list_generation_assets', {
        product_id: 'prod-1',
      })
    );

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });

    expect(result.current.items).toHaveLength(2);
    expect(result.current.items[0]).toEqual({ id: 'v1' });
    expect(result.current.firstPage).toEqual(
      expect.objectContaining({
        facets: expect.any(Object),
        allowed_actions: expect.arrayContaining([
          expect.objectContaining({ action: 'export', allowed: true }),
        ]),
      })
    );
    expect(mockGateway.listGenerationAssets).toHaveBeenCalledWith(
      expect.objectContaining({
        workspaceSlug: 'acme',
        productId: 'prod-1',
        cursor: undefined,
        limit: 50,
      })
    );
  });

  it('passes all asset filter params to the gateway', async () => {
    mockGateway.listGenerationAssets.mockResolvedValue(okResult(makePage([])));

    renderHook(() =>
      useDomainPaginatedQuery('list_generation_assets', {
        product_id: 'prod-1',
        run_id: 'run-1',
        provider: 'openai',
        execution_mode: 'byok',
        origin_kind: 'original',
        review_value: 'approved',
        aspect_ratio: '1:1',
        qc_state: 'passed',
        tag: 'hero',
        experiment_context: 'experiment-1',
        conclusion: 'winner',
      })
    );

    await waitFor(() => {
      expect(mockGateway.listGenerationAssets).toHaveBeenCalledWith(
        expect.objectContaining({
          workspaceSlug: 'acme',
          productId: 'prod-1',
          runId: 'run-1',
          provider: 'openai',
          executionMode: 'byok',
          originKind: 'original',
          reviewValue: 'approved',
          aspectRatio: '1:1',
          qcState: 'passed',
          tag: 'hero',
          experimentContext: 'experiment-1',
          conclusion: 'winner',
        })
      );
    });
  });

  it('deduplicates items across pages', async () => {
    mockGateway.listGenerationAssets.mockImplementation((params: any) => {
      if (!params.cursor) {
        return Promise.resolve(
          okResult(makePage([{ id: 'v1' }, { id: 'v2' }], 'cursor-1'))
        );
      }
      return Promise.resolve(okResult(makePage([{ id: 'v2' }, { id: 'v3' }])));
    });

    const { result } = renderHook(() =>
      useDomainPaginatedQuery('list_generation_assets', {})
    );

    await waitFor(() => {
      expect(result.current.hasMore).toBe(true);
    });

    result.current.loadMore();

    await waitFor(() => {
      expect(result.current.items).toHaveLength(3);
    });

    expect(result.current.items.map((i: any) => i.id)).toEqual([
      'v1',
      'v2',
      'v3',
    ]);
  });
});
