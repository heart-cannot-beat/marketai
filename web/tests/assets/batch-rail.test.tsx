/**
 * @jest-environment jsdom
 */
import { render, screen, fireEvent, within } from '@testing-library/react';
import { BatchRail } from '@/components/assets/BatchRail';
import type { RunOrigin } from '@/lib/assets/run-display';

const mockOnSelectRun = jest.fn();

function makeRun(
  id: string,
  status: string,
  total: number,
  done: number,
  productId = 'prod_charger'
) {
  return {
    id,
    product_id: productId,
    provider: 'openai',
    model: 'gpt-image-1',
    status,
    total,
    done,
    failed: 0,
    execution_mode: 'byok',
    revision: 1,
    commands: [],
    created_at: '2026-10-12T06:05:00Z',
  };
}

const PLAN_ORIGIN: RunOrigin = {
  planId: 'tp_internal',
  productName: 'Grizzl-E Classic',
  stage: 'sample',
  directionCount: 4,
};

const productNameFor = (productId: string | null | undefined) =>
  productId === 'prod_charger' ? 'Grizzl-E Classic' : null;

beforeEach(() => {
  jest.clearAllMocks();
});

describe('BatchRail', () => {
  it('names a batch from a test plan by its test and stage, never by id', () => {
    const { container } = render(
      <BatchRail
        runs={[makeRun('gr_935bb7a1', 'succeeded', 4, 4)]}
        activeRunId={undefined}
        onSelectRun={mockOnSelectRun}
        isLoading={false}
        origins={new Map([['gr_935bb7a1', PLAN_ORIGIN]])}
        productNameFor={productNameFor}
      />
    );

    const run = screen.getByTestId('batch-rail-run');
    expect(
      within(run).getByText('Ad test for Grizzl-E Classic · Samples')
    ).toBeInTheDocument();
    expect(
      within(run).getByText(/^4 directions · \d\d\/\d\d \d\d:\d\d$/)
    ).toBeInTheDocument();
    expect(within(run).getByTestId('batch-rail-status')).toHaveTextContent(
      'Completed'
    );
    expect(within(run).getByText('4/4')).toBeInTheDocument();
    expect(within(run).getByText('Real')).toBeInTheDocument();

    // The id is only a tooltip: not in the text, not in the accessible name.
    expect(container).not.toHaveTextContent(/gr_|succeeded|Production/);
    expect(run).toHaveAttribute('title', 'gr_935bb7a1');
    expect(run).toHaveAccessibleName(
      'Ad test for Grizzl-E Classic · Samples, Completed, 4/4 done'
    );
  });

  it('names a workbench batch by product, source and time', () => {
    render(
      <BatchRail
        runs={[makeRun('gr_aaaa1111', 'running', 10, 5)]}
        activeRunId={undefined}
        onSelectRun={mockOnSelectRun}
        isLoading={false}
        origins={new Map()}
        productNameFor={productNameFor}
      />
    );

    const run = screen.getByTestId('batch-rail-run');
    expect(within(run).getByText('Grizzl-E Classic')).toBeInTheDocument();
    expect(
      within(run).getByText(/^Advanced workbench · \d\d\/\d\d \d\d:\d\d$/)
    ).toBeInTheDocument();
    expect(within(run).getByTestId('batch-rail-status')).toHaveTextContent(
      'Generating'
    );
    expect(within(run).getByText('5/10')).toBeInTheDocument();
    expect(run).not.toHaveTextContent('gr_');
  });

  it('falls back to neutral labels for an unknown product and status', () => {
    render(
      <BatchRail
        runs={[makeRun('gr_bbbb2222', 'brand_new_state', 2, 0, 'prod_gone')]}
        activeRunId={undefined}
        onSelectRun={mockOnSelectRun}
        isLoading={false}
        productNameFor={productNameFor}
      />
    );

    const run = screen.getByTestId('batch-rail-run');
    expect(within(run).getByText('Unnamed product')).toBeInTheDocument();
    expect(within(run).getByTestId('batch-rail-status')).toHaveTextContent(
      'Status unknown'
    );
    expect(run).not.toHaveTextContent(/brand_new_state|prod_gone|gr_/);
  });

  it('maps every Run status to a translated label', () => {
    const statuses: Record<string, string> = {
      queued: 'Queued',
      running: 'Generating',
      succeeded: 'Completed',
      partially_succeeded: 'Partly completed',
      failed: 'Failed',
      cancelled: 'Cancelled',
      paused: 'Paused',
      uncertain: 'Awaiting confirmation',
    };
    render(
      <BatchRail
        runs={Object.keys(statuses).map((status, index) =>
          makeRun(`gr_${index}`, status, 1, 0)
        )}
        activeRunId={undefined}
        onSelectRun={mockOnSelectRun}
        isLoading={false}
        productNameFor={productNameFor}
      />
    );

    expect(
      screen.getAllByTestId('batch-rail-status').map((el) => el.textContent)
    ).toEqual(Object.values(statuses));
  });

  it('highlights the active run', () => {
    render(
      <BatchRail
        runs={[makeRun('run-1', 'succeeded', 10, 10)]}
        activeRunId="run-1"
        onSelectRun={mockOnSelectRun}
        isLoading={false}
      />
    );

    const button = screen.getByTestId('batch-rail-run');
    expect(button.className).toContain('bg-primary');
    expect(button).toHaveAttribute('aria-pressed', 'true');
  });

  it('calls onSelectRun when a run is clicked', () => {
    render(
      <BatchRail
        runs={[makeRun('run-1', 'succeeded', 10, 10)]}
        activeRunId={undefined}
        onSelectRun={mockOnSelectRun}
        isLoading={false}
      />
    );

    fireEvent.click(screen.getByTestId('batch-rail-run'));
    expect(mockOnSelectRun).toHaveBeenCalledWith('run-1');
  });

  it('clears filter when clicking the active run again', () => {
    render(
      <BatchRail
        runs={[makeRun('run-1', 'succeeded', 10, 10)]}
        activeRunId="run-1"
        onSelectRun={mockOnSelectRun}
        isLoading={false}
      />
    );

    fireEvent.click(screen.getByTestId('batch-rail-run'));
    expect(mockOnSelectRun).toHaveBeenCalledWith(undefined);
  });

  it('shows loading skeleton when loading', () => {
    render(
      <BatchRail
        runs={[]}
        activeRunId={undefined}
        onSelectRun={mockOnSelectRun}
        isLoading={true}
      />
    );

    expect(screen.getAllByTestId('rail-skeleton')).toHaveLength(3);
  });

  it('shows the Chinese heading and empty state', () => {
    render(
      <BatchRail
        runs={[]}
        activeRunId={undefined}
        onSelectRun={mockOnSelectRun}
        isLoading={false}
      />
    );

    expect(
      screen.getByRole('navigation', { name: 'Generation batches' })
    ).toBeVisible();
    expect(screen.getByText('Batches')).toBeInTheDocument();
    expect(screen.getByText('No generation batches yet')).toBeInTheDocument();
  });
});
