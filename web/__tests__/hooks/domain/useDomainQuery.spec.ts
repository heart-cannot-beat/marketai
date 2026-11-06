/**
 * @jest-environment jsdom
 */
import { createElement } from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';
import { SWRConfig } from 'swr';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';

const mockGateway = {
  getIdentityProbe: jest.fn(),
};

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
  useDomainGateway: () => mockGateway,
}));

// `swr`'s default cache is a module-level singleton shared by every
// `useSWR` call in this file, including separate `it()` blocks that
// intentionally reuse the same operation+params cache key with a different
// mocked response per test. A fresh `SWRConfig` provider per render gives
// each test its own isolated cache, so a later test never observes a prior
// test's still-cached result.
function renderDomainQuery<T>(callback: () => T) {
  return renderHook(callback, {
    wrapper: ({ children }) =>
      createElement(
        SWRConfig,
        { value: { provider: () => new Map() } },
        children
      ),
  });
}

describe('useDomainQuery', () => {
  beforeEach(() => jest.clearAllMocks());

  it('returns data on successful fetch', async () => {
    mockGateway.getIdentityProbe.mockResolvedValue({
      kind: 'success',
      status: 200,
      data: { workspace_id: 'w1', user_id: 'u1' },
      meta: { etag: null, retryAt: null, location: null },
    });

    const { result } = renderDomainQuery(() =>
      useDomainQuery('get_identity_probe')
    );
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    expect(result.current.data).toEqual({ workspace_id: 'w1', user_id: 'u1' });
    expect(result.current.problem).toBeUndefined();
  });

  it('returns problem on error response', async () => {
    mockGateway.getIdentityProbe.mockResolvedValue({
      kind: 'problem',
      status: 403,
      problem: { type: 'forbidden', title: 'Forbidden', status: 403 },
      meta: { etag: null, retryAt: null, location: null },
    });

    const { result } = renderDomainQuery(() =>
      useDomainQuery('get_identity_probe')
    );
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    expect(result.current.data).toBeUndefined();
    expect(result.current.problem?.status).toBe(403);
  });

  it('tracks lastSuccessfulAt on success', async () => {
    mockGateway.getIdentityProbe.mockResolvedValue({
      kind: 'success',
      status: 200,
      data: { workspace_id: 'w1' },
      meta: { etag: null, retryAt: null, location: null },
    });

    const { result } = renderDomainQuery(() =>
      useDomainQuery('get_identity_probe')
    );
    await waitFor(() => expect(result.current.lastSuccessfulAt).not.toBeNull());
    expect(result.current.lastSuccessfulAt).toBeGreaterThan(0);
  });

  it('does not fetch when enabled is false', () => {
    renderDomainQuery(() =>
      useDomainQuery('get_identity_probe', undefined, { enabled: false })
    );
    expect(mockGateway.getIdentityProbe).not.toHaveBeenCalled();
  });

  describe('a problem the Workspace will outgrow (#309)', () => {
    const meta = { etag: null, retryAt: 5_000, location: null };
    const problem = (code: string, retryable: boolean) => ({
      kind: 'problem',
      status: 503,
      problem: { code, retryable, status: 503 },
      meta,
    });
    const syncing = problem('projection_sync_unavailable', true);
    const answered = {
      kind: 'ok',
      status: 200,
      data: { workspace_id: 'w1' },
      meta,
    };
    const reads = () => mockGateway.getIdentityProbe.mock.calls.length;
    const advance = (ms: number) =>
      act(() => jest.advanceTimersByTimeAsync(ms));

    beforeEach(() => jest.useFakeTimers());
    afterEach(() => jest.useRealTimers());

    it('polls every 5 seconds and stops once it clears', async () => {
      mockGateway.getIdentityProbe
        .mockResolvedValueOnce(syncing)
        .mockResolvedValueOnce(syncing)
        .mockResolvedValue(answered);
      const { result } = renderDomainQuery(() =>
        useDomainQuery('get_identity_probe')
      );
      await advance(0);
      expect(result.current.problem?.code).toBe('projection_sync_unavailable');
      expect(reads()).toBe(1);
      await advance(4_900);
      expect(reads()).toBe(1);
      await advance(100);
      expect(reads()).toBe(2);
      await advance(5_000);
      expect(reads()).toBe(3);
      expect(result.current.data).toEqual({ workspace_id: 'w1' });
      expect(result.current.problem).toBeUndefined();
      await advance(60_000);
      expect(reads()).toBe(3);
    });

    it('does not poll an ordinary problem', async () => {
      for (const ordinary of [
        problem('service_unavailable', true),
        problem('projection_sync_unavailable', false),
      ]) {
        mockGateway.getIdentityProbe.mockReset();
        mockGateway.getIdentityProbe.mockResolvedValue(ordinary);
        const view = renderDomainQuery(() =>
          useDomainQuery('get_identity_probe')
        );
        await advance(0);
        expect(view.result.current.problem).toBeDefined();
        await advance(60_000);
        expect(reads()).toBe(1);
        view.unmount();
      }
    });

    it('stops after ten minutes; refresh starts them again', async () => {
      mockGateway.getIdentityProbe.mockResolvedValue(syncing);
      const { result } = renderDomainQuery(() =>
        useDomainQuery('get_identity_probe')
      );
      await advance(0);
      await advance(10 * 60_000);
      // The first read and 120 polls, 5 seconds apart.
      expect(reads()).toBe(121);
      await advance(10 * 60_000);
      expect(reads()).toBe(121);

      await act(() => result.current.refresh());
      expect(reads()).toBe(122);
      await advance(5_000);
      expect(reads()).toBe(123);
    });
  });
});
