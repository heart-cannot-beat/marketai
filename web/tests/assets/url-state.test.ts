/**
 * @jest-environment jsdom
 */
import { renderHook, act } from '@testing-library/react';

const mockPush = jest.fn();
const mockReplace = jest.fn();
let mockQuery: Record<string, string | string[]> = {};

jest.mock('next/router', () => ({
  useRouter: () => ({
    query: mockQuery,
    pathname: '/w/[slug]/assets',
    push: mockPush,
    replace: mockReplace,
    isReady: true,
  }),
}));

import {
  runOnlyAssetFilters,
  useAssetsUrlState,
} from '@/hooks/assets/useAssetsUrlState';
import { reviewQueueStorageKey } from '@/hooks/assets/useReviewMode';

beforeEach(() => {
  jest.clearAllMocks();
  mockQuery = { slug: 'acme' };
});

describe('useAssetsUrlState', () => {
  it('parses empty query into default filters', () => {
    const { result } = renderHook(() => useAssetsUrlState());
    expect(result.current.filters).toEqual({
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
    });
    expect(result.current.overlay).toBeUndefined();
    expect(result.current.exportId).toBeUndefined();
    expect(result.current.reviewMode).toBe(false);
  });

  it('parses filter params from URL query', () => {
    mockQuery = {
      slug: 'acme',
      product: 'prod-1',
      run: 'run-1',
      provider: 'openai',
      origin: 'original',
      mode: 'byok',
      review: 'approved',
      aspect: '1:1',
      qc: 'passed',
      tag: 'hero',
      context: 'ctx-1',
      conclusion: 'winner',
    };

    const { result } = renderHook(() => useAssetsUrlState());
    expect(result.current.filters).toEqual({
      productId: 'prod-1',
      runId: 'run-1',
      provider: 'openai',
      originKind: 'original',
      executionMode: 'byok',
      reviewValue: 'approved',
      aspectRatio: '1:1',
      qcState: 'passed',
      tag: 'hero',
      experimentContext: 'ctx-1',
      conclusion: 'winner',
    });
  });

  it('parses overlay version ID from URL', () => {
    mockQuery = { slug: 'acme', overlay: 'version-123' };
    const { result } = renderHook(() => useAssetsUrlState());
    expect(result.current.overlay).toBe('version-123');
  });

  it('parses export package ID from URL', () => {
    mockQuery = { slug: 'acme', export: 'pkg-456' };
    const { result } = renderHook(() => useAssetsUrlState());
    expect(result.current.exportId).toBe('pkg-456');
  });

  it('parses review mode flag from URL', () => {
    mockQuery = { slug: 'acme', reviewMode: '1' };
    const { result } = renderHook(() => useAssetsUrlState());
    expect(result.current.reviewMode).toBe(true);
  });

  it('updates filters via setFilter and pushes to router', () => {
    const { result } = renderHook(() => useAssetsUrlState());

    act(() => {
      result.current.setFilter('provider', 'openai');
    });

    expect(mockPush).toHaveBeenCalledWith(
      expect.objectContaining({
        query: expect.objectContaining({ slug: 'acme', provider: 'openai' }),
      }),
      undefined,
      { shallow: true }
    );
  });

  it('removes a filter when set to undefined', () => {
    mockQuery = { slug: 'acme', provider: 'openai' };
    const { result } = renderHook(() => useAssetsUrlState());

    act(() => {
      result.current.setFilter('provider', undefined);
    });

    expect(mockPush).toHaveBeenCalledWith(
      expect.objectContaining({
        query: { slug: 'acme' },
      }),
      undefined,
      { shallow: true }
    );
  });

  it('sets overlay via setOverlay with replace', () => {
    const { result } = renderHook(() => useAssetsUrlState());

    act(() => {
      result.current.setOverlay('v-789');
    });

    expect(mockReplace).toHaveBeenCalledWith(
      expect.objectContaining({
        query: expect.objectContaining({ slug: 'acme', overlay: 'v-789' }),
      }),
      undefined,
      { shallow: true }
    );
  });

  it('parses the 按方向 view and keeps it across overlay changes', () => {
    mockQuery = { slug: 'acme', run: 'run-1', view: 'directions' };
    const { result } = renderHook(() => useAssetsUrlState());
    expect(result.current.view).toBe('directions');

    act(() => {
      result.current.setOverlay('v-1');
    });
    expect(mockReplace).toHaveBeenLastCalledWith(
      expect.objectContaining({
        query: {
          slug: 'acme',
          run: 'run-1',
          view: 'directions',
          overlay: 'v-1',
        },
      }),
      undefined,
      { shallow: true }
    );

    act(() => {
      result.current.setView('all');
    });
    expect(mockReplace).toHaveBeenLastCalledWith(
      expect.objectContaining({
        query: { slug: 'acme', run: 'run-1', view: 'all' },
      }),
      undefined,
      { shallow: true }
    );
  });

  it('ignores an unknown view', () => {
    mockQuery = { slug: 'acme', view: 'grid' };
    const { result } = renderHook(() => useAssetsUrlState());
    expect(result.current.view).toBeUndefined();
  });

  it('keys a review queue for ?run= the same way another page seeds it', () => {
    mockQuery = { slug: 'acme', run: 'run-1', view: 'directions' };
    const { result } = renderHook(() => useAssetsUrlState());
    expect(reviewQueueStorageKey('acme', result.current.filters)).toBe(
      reviewQueueStorageKey('acme', runOnlyAssetFilters('run-1'))
    );
  });
});
