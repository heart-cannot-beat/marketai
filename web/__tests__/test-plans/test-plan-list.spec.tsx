/**
 * @jest-environment jsdom
 */
import { render, screen, within } from '@testing-library/react';
import { TestPlanList } from '@/components/test-plans/TestPlanList';

let mockList: any;

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme', role: 'MEMBER' } }),
}));
jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: () => mockList,
}));
jest.mock('@/components/test-plans/GenerationDefaultsBanner', () => ({
  GenerationDefaultsBanner: () => null,
}));
jest.mock('@/components/test-plans/GenerationDefaultsSection', () => ({
  GenerationDefaultsSection: () => <div data-testid="defaults-editor" />,
}));

const ROW = {
  status: 'ready',
  revision: 2,
  product_id: 'prod_1',
  product_name: 'VoltNest',
  goal: 'explore',
  scale: 'sample',
  direction_count: 4,
  run_stages: [],
  created_at: '2026-10-11T08:00:00Z',
};

describe('TestPlanList', () => {
  it('lists plans newest first by product, goal, scale and progress', () => {
    mockList = {
      isLoading: false,
      problem: undefined,
      refresh: jest.fn(),
      data: {
        items: [
          { ...ROW, plan_id: 'atp_2', run_stages: ['full', 'sample'] },
          { ...ROW, plan_id: 'atp_1', status: 'drafting', product_name: '' },
        ],
      },
    };
    render(<TestPlanList />);
    const rows = screen.getAllByTestId('test-plan-row');
    expect(within(rows[0]).getByRole('link')).toHaveAttribute(
      'href',
      '/w/acme/tests/atp_2'
    );
    expect(rows[0]).toHaveTextContent('VoltNest');
    expect(rows[0]).toHaveTextContent('Explore new directions');
    expect(rows[0]).toHaveTextContent('4 directions');
    expect(rows[0]).toHaveTextContent('Full test started');
    expect(rows[1]).toHaveTextContent('Unnamed product');
    expect(rows[1]).toHaveTextContent('Drafting');
    expect(document.body.textContent).not.toMatch(/atp_|prod_/);
    // View-only since #337: a notice points to the marketing test.
    const notice = screen.getByTestId('legacy-tests-notice');
    expect(notice).toHaveTextContent(
      'This older test flow has been retired: its tests can be viewed but no longer changed, generated or imported into.'
    );
    expect(
      within(notice).getByRole('link', {
        name: 'Start new tests in Marketing tests',
      })
    ).toHaveAttribute('href', '/w/acme/experiments');
  });

  it('no longer folds in the「生成默认值」editor, which lives in Settings (#337)', () => {
    mockList = {
      isLoading: false,
      problem: undefined,
      refresh: jest.fn(),
      data: { items: [{ ...ROW, plan_id: 'atp_1' }] },
    };
    render(<TestPlanList />);
    expect(screen.queryByTestId('defaults-editor')).not.toBeInTheDocument();
    expect(screen.queryByText('Generation defaults')).not.toBeInTheDocument();
    expect(document.querySelector('details')).toBeNull();
  });

  it('points an empty Workspace to the marketing test', () => {
    mockList = {
      isLoading: false,
      problem: undefined,
      refresh: jest.fn(),
      data: { items: [] },
    };
    render(<TestPlanList />);
    expect(screen.getByText('No tests yet')).toBeInTheDocument();
    expect(
      screen.getByText('New tests are set up in Marketing tests.')
    ).toBeInTheDocument();
    expect(
      screen.getByRole('link', { name: 'Go to Marketing tests' })
    ).toHaveAttribute('href', '/w/acme/experiments');
  });
});
