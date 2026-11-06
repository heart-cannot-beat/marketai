/**
 * @jest-environment jsdom
 */
import { render, screen, fireEvent } from '@testing-library/react';
import { RunProgressView } from '@/components/generation/progress/RunProgressView';

const mockUseRunProgress = jest.fn();

jest.mock('@/components/generation/shared/useRunProgress', () => ({
  useRunProgress: (...args: unknown[]) => mockUseRunProgress(...args),
}));

// The heading and summary show the product's name from `list_products`
// (Issue #162), never the `prod_` id.
jest.mock('@/hooks/products/useProductDirectory', () => ({
  useProductDirectory: () => ({
    nameFor: (id: string) => (id === 'prod-1' ? 'VoltNest 壁挂充电桩' : null),
  }),
}));

const EMPTY_SNAPSHOT = {
  run: null,
  items: [],
  itemsPagination: { cursor: null, hasMore: false },
  latestCommandOperation: null,
  isRunTerminal: false,
  isOperationTerminal: false,
  isPartialSuccess: false,
  lastRefreshedAt: 0,
  transportError: null,
  problem: null,
  forceRefresh: jest.fn(),
  setItemPage: jest.fn(),
};

/** `GenerationRunDetail` (`lib/domain-client/generated/domain-v1.d.ts`) --
 * real field names (`product_id`, `done`/`total`/`failed`), not the
 * `title`/idealized shape sketched in the task-9 brief's `RunSummary`
 * pseudocode. */
const baseRun = {
  id: 'run-1',
  product_id: 'prod-1',
  provider: 'meta',
  model: 'llama-3',
  status: 'running',
  total: 4,
  done: 1,
  failed: 0,
  execution_mode: 'byok',
  created_at: '2026-09-01T00:00:00Z',
  revision: 1,
  recipe_snapshot_id: null,
  commands: [],
  allowed_actions: [],
};

const baseItems = [
  {
    id: 'item-1',
    ordinal: 0,
    state: 'succeeded',
    combination_fingerprint: 'fp1',
    variant_index: 0,
    execution_mode: 'byok',
    error_summary: '',
  },
  {
    id: 'item-2',
    ordinal: 1,
    state: 'running',
    combination_fingerprint: 'fp2',
    variant_index: 0,
    execution_mode: 'byok',
    error_summary: '',
  },
];

beforeEach(() => {
  mockUseRunProgress.mockReset();
  mockUseRunProgress.mockReturnValue(EMPTY_SNAPSHOT);
});

describe('RunProgressView', () => {
  it('shows a loading spinner before the first successful fetch', () => {
    render(<RunProgressView runId="run-1" />);
    expect(screen.getByRole('status')).toBeInTheDocument();
  });

  it('shows a connection-issue message when the initial fetch fails at the transport level', () => {
    mockUseRunProgress.mockReturnValue({
      ...EMPTY_SNAPSHOT,
      transportError: new Error('network down'),
    });
    render(<RunProgressView runId="run-1" />);
    expect(screen.getByRole('alert')).toHaveTextContent(/connection issue/i);
  });

  it('shows a load-error message when the run does not exist', () => {
    mockUseRunProgress.mockReturnValue({
      ...EMPTY_SNAPSHOT,
      problem: {
        type: 'not_found',
        title: 'Not Found',
        status: 404,
        detail: 'run not found',
        instance: '',
        code: 'not_found',
        trace_id: 't1',
        retryable: false,
        field_errors: [],
      },
    });
    render(<RunProgressView runId="run-1" />);
    expect(screen.getByText(/couldn't load run/i)).toBeInTheDocument();
  });

  it('renders the run summary and item grid once loaded', () => {
    mockUseRunProgress.mockReturnValue({
      ...EMPTY_SNAPSHOT,
      run: baseRun,
      items: baseItems,
    });
    render(<RunProgressView runId="run-1" />);

    expect(screen.getAllByText('VoltNest 壁挂充电桩').length).toBe(2);
    expect(screen.queryByText('prod-1')).not.toBeInTheDocument();
    // The 4 total / 1 done / 0 failed stat values from RunSummary.
    expect(screen.getByText('4')).toBeInTheDocument();
    expect(screen.getByText('1')).toBeInTheDocument();
    // Two item tiles rendered by RunItemGrid.
    expect(screen.getByText('#1')).toBeInTheDocument();
    expect(screen.getByText('#2')).toBeInTheDocument();
  });

  it('shows item detail once an item tile is selected', () => {
    mockUseRunProgress.mockReturnValue({
      ...EMPTY_SNAPSHOT,
      run: baseRun,
      items: baseItems,
    });
    render(<RunProgressView runId="run-1" />);

    expect(screen.getByText(/select an item/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /item 1: succeeded/i }));

    expect(screen.getByText('Item #1')).toBeInTheDocument();
    expect(screen.queryByText(/select an item/i)).not.toBeInTheDocument();
  });

  it('renders the recovery slot once the run reaches a terminal state', () => {
    mockUseRunProgress.mockReturnValue({
      ...EMPTY_SNAPSHOT,
      run: { ...baseRun, status: 'failed' },
      items: baseItems,
      isRunTerminal: true,
    });
    render(
      <RunProgressView
        runId="run-1"
        recoverySlot={<div data-testid="recovery-slot" />}
      />
    );

    expect(screen.getByTestId('recovery-slot')).toBeInTheDocument();
  });

  it('renders the recovery slot while the run is still active (cancel is only eligible then)', () => {
    mockUseRunProgress.mockReturnValue({
      ...EMPTY_SNAPSHOT,
      run: baseRun,
      items: baseItems,
      isRunTerminal: false,
    });
    render(
      <RunProgressView
        runId="run-1"
        recoverySlot={<div data-testid="recovery-slot" />}
      />
    );

    expect(screen.getByTestId('recovery-slot')).toBeInTheDocument();
  });

  it('renders the recovery slot for an uncertain run (reconcile is owed, not terminal)', () => {
    mockUseRunProgress.mockReturnValue({
      ...EMPTY_SNAPSHOT,
      run: { ...baseRun, status: 'uncertain' },
      items: baseItems,
      isRunTerminal: false,
    });
    render(
      <RunProgressView
        runId="run-1"
        recoverySlot={<div data-testid="recovery-slot" />}
      />
    );

    expect(screen.getByTestId('recovery-slot')).toBeInTheDocument();
  });

  it('renders the command operation status when a latest command operation is present', () => {
    mockUseRunProgress.mockReturnValue({
      ...EMPTY_SNAPSHOT,
      run: baseRun,
      items: baseItems,
      latestCommandOperation: {
        operation_id: 'op-1',
        command_type: 'submit_generation_run',
        state: 'running',
        result: null,
        created_at: '2026-09-01T00:00:00Z',
        updated_at: '2026-09-01T00:00:00Z',
      },
    });
    const { container } = render(<RunProgressView runId="run-1" />);

    // "Running" appears twice (the OperationStatus label and RunItemGrid's
    // item-2 state badge, since item-2's state is also "running") -- assert
    // on the `aria-live` region OperationTracker/OperationStatus renders
    // rather than a bare text match, which is ambiguous between the two.
    const operationRegion = container.querySelector('[aria-live="polite"]');
    expect(operationRegion).not.toBeNull();
    expect(operationRegion).toHaveTextContent('Running');
  });

  it('passes runId through to useRunProgress', () => {
    render(<RunProgressView runId="run-42" />);
    expect(mockUseRunProgress).toHaveBeenCalledWith('run-42');
  });
});
