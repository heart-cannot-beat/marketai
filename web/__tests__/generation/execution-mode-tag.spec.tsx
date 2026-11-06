/**
 * @jest-environment jsdom
 */
import { render, screen } from '@testing-library/react';
import { ExecutionModeTag } from '@/components/generation/shared/ExecutionModeTag';

describe('ExecutionModeTag', () => {
  it('renders the Real badge for byok mode', () => {
    render(<ExecutionModeTag mode="byok" provider="meta" model="llama-3" />);
    expect(screen.getByText('Real')).toHaveClass('badge-success');
    expect(screen.getByText('meta')).toBeInTheDocument();
  });

  it('renders the controlled Mock example badge for mock mode', () => {
    render(
      <ExecutionModeTag
        mode="mock"
        provider="meta"
        model="llama-3"
        scenario="golden-path"
      />
    );
    expect(screen.getByText('Example (controlled Mock)')).toHaveClass(
      'badge-error'
    );
    expect(screen.getByText('golden-path')).toBeInTheDocument();
  });

  it('treats the persisted controlled_mock alias as Controlled Mock', () => {
    render(<ExecutionModeTag mode="controlled_mock" />);
    expect(screen.getByTestId('execution-mode-badge')).toHaveTextContent(
      'Example (controlled Mock)'
    );
  });

  it('renders the Sandbox badge for sandbox mode', () => {
    render(<ExecutionModeTag mode="sandbox" />);
    expect(screen.getByText('Sandbox')).toHaveClass('badge-warning');
  });

  it('never shows the raw mode values', () => {
    const { container } = render(
      <>
        <ExecutionModeTag mode="byok" />
        <ExecutionModeTag mode="sandbox" />
        <ExecutionModeTag mode="mock" />
      </>
    );
    expect(container).toHaveTextContent('Real');
    expect(container).not.toHaveTextContent(/byok|controlled_mock|\bmock\b/);
  });
});
