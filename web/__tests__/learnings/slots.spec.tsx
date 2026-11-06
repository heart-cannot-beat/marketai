/**
 * @jest-environment jsdom
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { ProductLearningsSlot } from '@/components/products/ProductLearningsSlot';
import { PlanNextRoundSlot } from '@/components/test-plans/PlanNextRoundSlot';
import {
  loadLearningsNamespace,
  noDataLearnings,
  readyLearnings,
  WIN_SENTENCE,
} from './fixtures';

const mockData: Record<string, unknown> = {};
const mockPush = jest.fn();
const mockGateway = { createTestPlan: jest.fn() };

jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: (operationId: string) => ({
    data: mockData[operationId],
    problem: undefined,
    isLoading: false,
    refresh: jest.fn(),
  }),
}));
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));
jest.mock('@/hooks/domain/useUpload', () => ({
  useUpload: () => ({ startUpload: jest.fn() }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme', role: 'MEMBER' } }),
}));
jest.mock('next/router', () => ({
  useRouter: () => ({ push: mockPush }),
}));

beforeAll(loadLearningsNamespace);
beforeEach(() => {
  mockPush.mockReset();
  mockGateway.createTestPlan.mockReset();
  for (const key of Object.keys(mockData)) delete mockData[key];
});

describe('ProductLearningsSlot (product card)', () => {
  it('says there is no ad data yet and opens the full card', () => {
    mockData.get_product_learnings = noDataLearnings();
    render(<ProductLearningsSlot productId="prod_1" productName="VoltNest" />);
    expect(
      screen.getByText(
        'No delivery data yet. Conclusions appear here once results are imported.'
      )
    ).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole('button', { name: 'What we learned about “VoltNest”' })
    );
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Import delivery results' })
    ).toBeInTheDocument();
  });

  it('shows the strongest conclusion and flags demo data', () => {
    mockData.get_product_learnings = readyLearnings({ evidence_mode: 'mock' });
    render(<ProductLearningsSlot productId="prod_1" productName="VoltNest" />);
    expect(screen.getByText(WIN_SENTENCE)).toBeInTheDocument();
    expect(screen.getByText('Demo data')).toBeInTheDocument();
  });
});

describe('PlanNextRoundSlot (plan results, read-only since #337)', () => {
  const plan = { plan_id: 'atp_1', product_id: 'prod_1' } as any;

  function expectNoWriteControls() {
    expect(
      screen.queryByRole('button', {
        name: 'Generate the next round from these conclusions',
      })
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /Import (delivery|more) results/ })
    ).not.toBeInTheDocument();
    expect(screen.queryAllByRole('button')).toEqual([]);
    expect(mockGateway.createTestPlan).not.toHaveBeenCalled();
  }

  it('shows the round with no import and no next round', () => {
    const ready = readyLearnings();
    mockData.get_test_plan_results = {
      plan_id: 'atp_1',
      product_id: 'prod_1',
      status: 'ready',
      evidence_mode: 'production',
      evidence_notice: null,
      available_modes: ['production'],
      snapshot_id: 'lrn_1',
      computed_at: null,
      thresholds: {},
      round: ready.rounds[0],
      can_seed_next_round: true,
    };
    render(<PlanNextRoundSlot plan={plan} settledStage="full" />);
    expect(screen.getByText('What this round taught us')).toBeInTheDocument();
    expect(screen.getAllByTestId('learning-row').length).toBeGreaterThan(0);
    expectNoWriteControls();
    expect(mockPush).not.toHaveBeenCalled();
  });

  it('says no ad data was imported, with nothing to import', () => {
    mockData.get_test_plan_results = {
      plan_id: 'atp_1',
      product_id: 'prod_1',
      status: 'no_data',
      snapshot_id: null,
      round: null,
      can_seed_next_round: false,
    };
    render(<PlanNextRoundSlot plan={plan} settledStage="sample" />);
    expect(
      screen.getByText('No delivery data was imported for this round.')
    ).toBeInTheDocument();
    expectNoWriteControls();
  });
});
