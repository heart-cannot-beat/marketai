/**
 * @jest-environment jsdom
 */
import { renderHook, waitFor } from '@testing-library/react';
import { useRunDirectionGroups } from '@/hooks/test-plans/useRunDirectionGroups';
import { direction, item, version } from './direction-groups-fixtures';

const mockGateway = {
  listTestPlans: jest.fn(),
  getTestPlan: jest.fn(),
  listGenerationRunItems: jest.fn(),
  listGenerationAssets: jest.fn(),
};

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme', role: 'MEMBER' } }),
}));
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));

const meta = { etag: null, retryAt: null, location: null };
const ok = (data: unknown) => ({ kind: 'ok', status: 200, data, meta });

beforeEach(() => {
  jest.clearAllMocks();
  mockGateway.listGenerationRunItems.mockResolvedValue(
    ok({
      items: [item('i1', 0, 'dir-1'), item('i2', 1, 'dir-9')],
      next_cursor: null,
    })
  );
  mockGateway.listGenerationAssets.mockResolvedValueOnce(
    ok({ items: [version('av_i1')], next_cursor: 'c2' })
  );
  mockGateway.listGenerationAssets.mockResolvedValueOnce(
    ok({ items: [version('av_i2')], next_cursor: null })
  );
});

describe('useRunDirectionGroups', () => {
  it('finds the plan behind the Run and groups every page of items and assets', async () => {
    mockGateway.listTestPlans.mockResolvedValue(
      ok({ items: [{ plan_id: 'atp_1' }] })
    );
    mockGateway.getTestPlan.mockResolvedValue(
      ok({
        plan_id: 'atp_1',
        directions: [direction('dir-1', '雨夜车库')],
        runs: [{ stage: 'full', run_id: 'gr_1' }],
      })
    );

    const { result } = renderHook(() => useRunDirectionGroups('gr_1', 'v1'));
    await waitFor(() => expect(result.current.groups).toHaveLength(2));

    expect(mockGateway.listTestPlans).toHaveBeenCalledWith({
      workspaceSlug: 'acme',
      runId: 'gr_1',
      limit: 1,
    });
    expect(result.current.status).toBe('found');
    expect(result.current.stage).toBe('full');
    expect(mockGateway.listGenerationAssets).toHaveBeenCalledTimes(2);
    expect(mockGateway.listGenerationAssets).toHaveBeenLastCalledWith(
      expect.objectContaining({ runId: 'gr_1', cursor: 'c2', limit: 100 })
    );
    const [rain, unmatched] = result.current.groups;
    expect(rain.versions.map((v) => v.id)).toEqual(['av_i1']);
    expect(unmatched.direction).toBeNull();
    expect(unmatched.versions.map((v) => v.id)).toEqual(['av_i2']);
  });

  it('is none for a Run without a plan and reads no items', async () => {
    mockGateway.listTestPlans.mockResolvedValue(ok({ items: [] }));
    const { result } = renderHook(() => useRunDirectionGroups('gr_1', 'v1'));
    await waitFor(() => expect(result.current.status).toBe('none'));
    expect(result.current.groups).toEqual([]);
    expect(mockGateway.getTestPlan).not.toHaveBeenCalled();
    expect(mockGateway.listGenerationRunItems).not.toHaveBeenCalled();
  });

  it('falls back to none when the lookup fails', async () => {
    mockGateway.listTestPlans.mockResolvedValue(
      ok({ items: [{ plan_id: 'atp_1' }] })
    );
    mockGateway.getTestPlan.mockResolvedValue({
      kind: 'problem',
      problem: { status: 404, code: 'test_plan_not_found' },
      meta,
    });
    const { result } = renderHook(() => useRunDirectionGroups('gr_1', 'v1'));
    await waitFor(() => expect(result.current.status).toBe('none'));

    mockGateway.listTestPlans.mockRejectedValue(new Error('offline'));
    const second = renderHook(() => useRunDirectionGroups('gr_2', 'v1'));
    await waitFor(() => expect(second.result.current.status).toBe('none'));
  });

  it('stays idle without a Run', () => {
    const { result } = renderHook(() => useRunDirectionGroups(undefined, ''));
    expect(result.current.status).toBe('idle');
    expect(mockGateway.listTestPlans).not.toHaveBeenCalled();
  });
});
