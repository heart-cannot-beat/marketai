/**
 * @jest-environment jsdom
 */
import type { ReactNode } from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';
import { SWRConfig, useSWRConfig } from 'swr';
import { useWorkspaceProducts } from '@/hooks/products/useWorkspaceProducts';
import { refreshProductTests } from '@/hooks/tests/useTestHistory';
import env from '@/lib/env';

/**
 * The overview's and the sidebar's test steps (#258): one
 * `list_marketing_tests` per product with ad ideas, the round's result only
 * where a report is in, nothing at all with the switch off. A product whose
 * list cannot be read keeps the step its ads give it.
 */
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: { slug: 'acme', workspaceId: 'ws_1', role: 'MEMBER' },
  }),
}));
const mockLists: Record<string, unknown> = {};
jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: (operation: string) => ({
    data: mockLists[operation],
    problem: undefined,
    refresh: jest.fn().mockResolvedValue(undefined),
  }),
}));
const mockGateway = {
  listMarketingTests: jest.fn(),
  getMarketingTestResult: jest.fn(),
};
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));

const ok = <T,>(data: T) => ({ kind: 'ok' as const, status: 200, data });

const ideasProject = (id: string, productId: string, kept: number) => ({
  id,
  revision: 1,
  state: 'ideas_ready',
  product_id: productId,
  product_name: `Acme Charge ${productId}`,
  ad_count: 6,
  kept_count: kept,
  updated_at: '2026-11-20T00:00:00Z',
});

const round = (extra: Record<string, unknown> = {}) => ({
  id: 'mr_1',
  ordinal: 1,
  state: 'handoff',
  start_date: '2026-01-01',
  end_date: '2026-01-07',
  effective_start_date: '2026-01-01',
  effective_end_date: '2026-01-07',
  reports: [],
  ...extra,
});

const running = (productId: string, roundExtra: Record<string, unknown>) => ({
  product_id: productId,
  tests: [
    {
      id: `mt_${productId}`,
      source_project_id: 'mp_x',
      comparison: 'claims',
      plan_summary: {
        target_cost: '30.00',
        round_budget: '1200.00',
        duration_days: 7,
        currency: 'USD',
        landing_page: 'https://acmecharge.example/home',
      },
      rounds: [round(roundExtra)],
      current_round_id: 'mr_1',
      created_at: '2026-01-01T00:00:00Z',
    },
  ],
  earlier_tests: [],
});

const REPORT = {
  batch_id: 'mmb_1',
  kind: 'upload',
  replaces_batch_id: null,
  uploaded_at: '2026-01-03T00:00:00Z',
  uploaded_by: 'user_1',
  row_count: 3,
};

function wrapper({ children }: { children: ReactNode }) {
  return (
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      {children}
    </SWRConfig>
  );
}

beforeEach(() => {
  env.adsFirstEnabled = true;
  mockGateway.listMarketingTests.mockReset();
  mockGateway.getMarketingTestResult.mockReset();
  mockLists.list_products = {
    items: ['prod_a', 'prod_b', 'prod_c', 'prod_d'].map((id) => ({
      product_id: id,
      name: `Acme Charge ${id}`,
      ready: true,
      updated_at: '2026-11-01T00:00:00Z',
    })),
  };
  mockLists.list_marketing_ideas = {
    items: [
      ideasProject('mp_a', 'prod_a', 2),
      ideasProject('mp_b', 'prod_b', 2),
      ideasProject('mp_c', 'prod_c', 2),
    ],
  };
});
afterEach(() => {
  env.adsFirstEnabled = false;
});

describe('useWorkspaceProducts with tests (#258)', () => {
  it('reads each product with ad ideas once, and a result only where a report is in', async () => {
    mockGateway.listMarketingTests.mockImplementation(
      ({ productId }: { productId: string }) =>
        Promise.resolve(
          productId === 'prod_a'
            ? ok(running('prod_a', {}))
            : productId === 'prod_b'
              ? ok(
                  running('prod_b', {
                    state: 'awaiting_data',
                    reports: [REPORT],
                  })
                )
              : { kind: 'problem', status: 503, problem: { code: 'x' } }
        )
    );
    mockGateway.getMarketingTestResult.mockResolvedValue(
      ok({
        test_id: 'mt_prod_b',
        round_id: 'mr_1',
        mature: true,
        evaluation_id: null,
        current: false,
      })
    );

    const { result } = renderHook(() => useWorkspaceProducts(), { wrapper });
    expect(result.current.testsLoading).toBe(true);
    await waitFor(() => expect(result.current.testsLoading).toBe(false));

    // prod_d has no ad ideas, so it cannot have a test: never read.
    expect(
      mockGateway.listMarketingTests.mock.calls.map(([p]) => p.productId)
    ).toEqual(['prod_a', 'prod_b', 'prod_c']);
    expect(mockGateway.getMarketingTestResult).toHaveBeenCalledTimes(1);
    expect(mockGateway.getMarketingTestResult).toHaveBeenCalledWith({
      workspaceSlug: 'acme',
      testId: 'mt_prod_b',
      roundId: 'mr_1',
    });

    const step = (id: string) => result.current.find(id)?.step;
    // Package made, its first day long gone, no report.
    expect(step('prod_a')).toBe('report_missing');
    expect(result.current.find('prod_a')?.test).toEqual({
      id: 'mt_prod_a',
      packageMade: true,
    });
    // A report in, the result due and never read.
    expect(step('prod_b')).toBe('conclusion_unseen');
    // Its list could not be read: the step its ads give it.
    expect(step('prod_c')).toBe('kept_untested');
    expect(step('prod_d')).toBe('no_ideas');
  });

  it('a test command elsewhere reads the overview again', async () => {
    mockGateway.listMarketingTests.mockResolvedValue(
      ok({ product_id: 'prod_a', tests: [], earlier_tests: [] })
    );
    const { result } = renderHook(
      () => ({ products: useWorkspaceProducts(), config: useSWRConfig() }),
      { wrapper }
    );
    await waitFor(() =>
      expect(result.current.products.testsLoading).toBe(false)
    );
    expect(result.current.products.find('prod_a')?.step).toBe('kept_untested');
    expect(mockGateway.listMarketingTests).toHaveBeenCalledTimes(3);

    mockGateway.listMarketingTests.mockImplementation(
      ({ productId }: { productId: string }) =>
        Promise.resolve(
          ok(
            productId === 'prod_a'
              ? running('prod_a', { state: 'reviewing' })
              : { product_id: productId, tests: [], earlier_tests: [] }
          )
        )
    );
    await act(async () => {
      await refreshProductTests(result.current.config.mutate, 'ws_1');
    });
    await waitFor(() =>
      expect(result.current.products.find('prod_a')?.step).toBe('testing')
    );
    expect(result.current.products.find('prod_a')?.signals.keptUntested).toBe(
      0
    );
  });

  it('reads no test with the switch off', async () => {
    env.adsFirstEnabled = false;
    const { result } = renderHook(() => useWorkspaceProducts(), { wrapper });
    expect(result.current.testsLoading).toBe(false);
    expect(result.current.find('prod_a')?.step).toBe('kept_untested');
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(mockGateway.listMarketingTests).not.toHaveBeenCalled();
  });
});
