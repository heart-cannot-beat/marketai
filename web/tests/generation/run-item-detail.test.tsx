/**
 * @jest-environment jsdom
 */
import { render, screen } from '@testing-library/react';
import { RunItemDetail } from '@/components/generation/progress/RunItemDetail';

function item(qcState: string | null) {
  return {
    ordinal: 0,
    state: 'succeeded',
    execution_mode: 'byok',
    qc_result: qcState ? { state: qcState } : null,
  };
}

/** Issue #191: the QC result reads as its translated label, never the enum. */
describe('RunItemDetail QC state', () => {
  it.each([
    ['passed', 'Passed', 'text-success'],
    ['failed_blocking', 'Failed (blocking)', 'text-error'],
    ['failed_nonblocking', 'Failed (warning)', 'text-error'],
    ['uncertain', 'Inconclusive', 'text-error'],
    ['a_new_state', 'QC status unknown', 'text-error'],
  ])('%s -> %s', (state, label, colour) => {
    render(<RunItemDetail item={item(state)} />);
    const qc = screen.getByTestId('run-item-qc-state');
    expect(qc).toHaveTextContent(label);
    expect(qc).toHaveClass(colour);
    expect(qc).not.toHaveTextContent(state);
  });

  it('shows the execution mode as its translated badge', () => {
    render(<RunItemDetail item={item(null)} />);
    expect(screen.getByTestId('execution-mode-badge')).toHaveTextContent(
      'Real'
    );
    expect(screen.queryByTestId('run-item-qc-state')).not.toBeInTheDocument();
  });
});
