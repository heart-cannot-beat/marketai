/**
 * @jest-environment jsdom
 */
import { renderHook, act, waitFor } from '@testing-library/react';

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

import {
  seedReviewQueueSnapshot,
  useReviewMode,
} from '@/hooks/assets/useReviewMode';

const STORAGE_KEY = 'asset-review-queue:acme:scope';

function makeVersion(id: string, reviewHeadId: string | null = null) {
  return {
    id,
    review_head_id: reviewHeadId,
    effective_review_value: reviewHeadId ? 'approved' : null,
    allowed_actions: [{ action: 'review', allowed: true, reason_code: null }],
  };
}

function renderReviewMode(queueIds: string[]) {
  return renderHook(() =>
    useReviewMode(queueIds, {
      storageKey: STORAGE_KEY,
      isQueueLoading: false,
    })
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  mockExecute.mockResolvedValue({
    kind: 'success',
    data: { id: 'r1', review_value: 'approved' },
    meta: { etag: null, retryAt: null, location: null },
  });
  sessionStorage.clear();
});

describe('useReviewMode', () => {
  it('initializes with the first exact version id in the queue', () => {
    const { result } = renderReviewMode(['v1', 'v2', 'v3']);

    expect(result.current.currentIndex).toBe(0);
    expect(result.current.currentVersionId).toBe('v1');
    expect(result.current.isComplete).toBe(false);
  });

  it('submits against the exact AssetVersion path and advances', async () => {
    const { result } = renderReviewMode(['v1', 'v2']);

    await act(async () => {
      await result.current.submitReview('approved', makeVersion('v1'));
    });

    expect(mockExecute).toHaveBeenCalledWith(
      expect.objectContaining({
        pathParams: { version_id: 'v1' },
        body: { review_value: 'approved' },
      })
    );
    expect(result.current.currentIndex).toBe(1);
    expect(result.current.currentVersionId).toBe('v2');
  });

  it('uses the current review head for append-only correction', async () => {
    const { result } = renderReviewMode(['v1']);

    await act(async () => {
      await result.current.submitReview(
        'rejected',
        makeVersion('v1', 'review-1')
      );
    });

    expect(mockExecute).toHaveBeenCalledWith(
      expect.objectContaining({
        pathParams: { version_id: 'v1' },
        body: {
          review_value: 'rejected',
          supersedes_review_id: 'review-1',
        },
      })
    );
  });

  it('uses the review created in this session when back appends a correction', async () => {
    const { result } = renderReviewMode(['v1', 'v2']);

    await act(async () => {
      await result.current.submitReview('approved', makeVersion('v1'));
    });
    act(() => result.current.goBack());
    await act(async () => {
      await result.current.submitReview('rejected', makeVersion('v1'));
    });

    expect(mockExecute).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({
        pathParams: { version_id: 'v1' },
        body: {
          review_value: 'rejected',
          supersedes_review_id: 'r1',
        },
      })
    );
    expect(result.current.stats).toEqual({
      total: 2,
      reviewed: 1,
      skipped: 0,
      approved: 0,
      needsChanges: 0,
      rejected: 1,
    });
  });

  it('does not submit when the server denies review', async () => {
    const { result } = renderReviewMode(['v1']);
    const denied = {
      ...makeVersion('v1'),
      allowed_actions: [
        { action: 'review', allowed: false, reason_code: 'role_not_permitted' },
      ],
    };

    await act(async () => {
      await result.current.submitReview('approved', denied);
    });

    expect(mockExecute).not.toHaveBeenCalled();
    expect(result.current.currentIndex).toBe(0);
  });

  it('marks complete when all items are reviewed', async () => {
    const { result } = renderReviewMode(['v1']);

    await act(async () => {
      await result.current.submitReview('approved', makeVersion('v1'));
    });

    expect(result.current.isComplete).toBe(true);
  });

  it('goes back and skips without creating a domain record', async () => {
    const { result } = renderReviewMode(['v1', 'v2', 'v3']);

    act(() => result.current.skip());
    expect(result.current.currentIndex).toBe(1);
    expect(result.current.stats.skipped).toBe(1);

    act(() => result.current.goBack());
    expect(result.current.currentIndex).toBe(0);
    expect(mockExecute).not.toHaveBeenCalled();
  });

  it('tracks review statistics', async () => {
    const { result } = renderReviewMode(['v1', 'v2', 'v3']);

    await act(async () => {
      await result.current.submitReview('approved', makeVersion('v1'));
    });
    await act(async () => {
      await result.current.submitReview('needs_changes', makeVersion('v2'));
    });
    act(() => result.current.skip());

    expect(result.current.stats).toEqual({
      total: 3,
      reviewed: 2,
      skipped: 1,
      approved: 1,
      needsChanges: 1,
      rejected: 0,
    });
  });

  it('prevents duplicate submissions with a synchronous mutex', async () => {
    let resolveFirst: (value: unknown) => void;
    mockExecute.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveFirst = resolve;
      })
    );
    const { result } = renderReviewMode(['v1', 'v2']);

    await act(async () => {
      const p1 = result.current.submitReview('approved', makeVersion('v1'));
      const p2 = result.current.submitReview('rejected', makeVersion('v1'));

      resolveFirst!({
        kind: 'success',
        data: { id: 'r1', review_value: 'approved' },
        meta: { etag: null, retryAt: null, location: null },
      });

      await p1;
      await p2;
    });

    expect(mockExecute).toHaveBeenCalledTimes(1);
  });

  it('restores the exact queue, position, and counters after refresh', async () => {
    seedReviewQueueSnapshot(STORAGE_KEY, ['v2', 'v4']);
    const first = renderReviewMode([]);

    await act(async () => {
      await first.result.current.submitReview('approved', makeVersion('v2'));
    });
    first.unmount();

    const restored = renderReviewMode(['different-first-page-item']);

    await waitFor(() => {
      expect(restored.result.current.currentVersionId).toBe('v4');
    });
    expect(restored.result.current.stats).toEqual(
      expect.objectContaining({ total: 2, reviewed: 1, approved: 1 })
    );

    act(() => restored.result.current.goBack());
    await act(async () => {
      await restored.result.current.submitReview('rejected', makeVersion('v2'));
    });
    expect(restored.result.current.stats).toEqual(
      expect.objectContaining({
        total: 2,
        reviewed: 1,
        approved: 0,
        rejected: 1,
      })
    );
    expect(mockExecute).toHaveBeenLastCalledWith(
      expect.objectContaining({
        body: {
          review_value: 'rejected',
          supersedes_review_id: 'r1',
        },
      })
    );
  });
});
