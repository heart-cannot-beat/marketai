/**
 * @jest-environment jsdom
 */
import { act, render, screen } from '@testing-library/react';
import { ModeProvenance } from '@/components/workspace/ModeProvenance';
import { OperationStatus } from '@/components/workspace/OperationStatus';
import { CapabilityNotice } from '@/components/workspace/CapabilityNotice';
import { StaleIndicator } from '@/components/workspace/StaleIndicator';

describe('ModeProvenance', () => {
  it('renders controlled-mock badge', () => {
    render(
      <ModeProvenance
        provenance={{ mode: 'controlled-mock', mockScenario: 'happy-path' }}
      />
    );
    expect(screen.getByText(/controlled mock/i)).toBeInTheDocument();
    expect(screen.getByText(/happy-path/i)).toBeInTheDocument();
  });

  it('renders real mode without extra badges', () => {
    render(
      <ModeProvenance provenance={{ mode: 'real', provider: 'OpenAI' }} />
    );
    expect(screen.queryByText(/mock/i)).not.toBeInTheDocument();
    expect(screen.getByText(/OpenAI/i)).toBeInTheDocument();
  });
});

describe('OperationStatus', () => {
  it('shows admitted state distinctly from queued and running', () => {
    render(<OperationStatus operation={{ status: 'admitted' }} />);
    expect(screen.getByText(/cost reserved/i)).toBeInTheDocument();
    expect(screen.queryByText(/queued/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/running/i)).not.toBeInTheDocument();
  });

  it('shows spinner for queued state', () => {
    render(<OperationStatus operation={{ status: 'queued' }} />);
    expect(screen.getByText(/queued/i)).toBeInTheDocument();
  });

  it('shows running state', () => {
    render(<OperationStatus operation={{ status: 'running' }} />);
    expect(screen.getByText(/running/i)).toBeInTheDocument();
  });

  it('shows uncertain without claiming success or failure', () => {
    render(<OperationStatus operation={{ status: 'uncertain' }} />);
    expect(screen.getByText(/checking/i)).toBeInTheDocument();
    expect(screen.queryByText(/success/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/failed/i)).not.toBeInTheDocument();
  });

  it('renders caller-provided outcomeSummary for partially_succeeded', () => {
    const outcomeSummary = '3 of 5 completed';
    render(
      <OperationStatus
        operation={{ status: 'partially_succeeded' }}
        outcomeSummary={<span>{outcomeSummary}</span>}
      />
    );
    expect(screen.getByText('3 of 5 completed')).toBeInTheDocument();
  });

  it('renders caller-provided actions for failed state', () => {
    const actionLabel = 'Retry with new intent';
    render(
      <OperationStatus
        operation={{ status: 'failed' }}
        actions={<button>{actionLabel}</button>}
      />
    );
    expect(screen.getByText('Retry with new intent')).toBeInTheDocument();
  });

  it('shows polling problem separately from operation failure', () => {
    render(
      <OperationStatus
        operation={{ status: 'running' }}
        pollingProblem={
          { type: 'network', title: 'Network error', status: 0 } as any
        }
      />
    );
    expect(screen.getByText(/running/i)).toBeInTheDocument();
    // The problem's title is a diagnostic; the interface words the issue.
    expect(
      screen.getByText('The latest status could not be loaded.')
    ).toBeInTheDocument();
    expect(screen.queryByText(/network error/i)).not.toBeInTheDocument();
  });

  it('uses aria-live polite for state changes', () => {
    const { container } = render(
      <OperationStatus operation={{ status: 'running' }} />
    );
    expect(container.querySelector('[aria-live="polite"]')).toBeInTheDocument();
  });

  it('uses role=alert for failed state', () => {
    const { container } = render(
      <OperationStatus operation={{ status: 'failed' }} />
    );
    expect(container.querySelector('[role="alert"]')).toBeInTheDocument();
  });
});

describe('CapabilityNotice', () => {
  it('renders not-available decision', () => {
    render(
      <CapabilityNotice
        decision="not-available"
        message="This feature requires a higher plan."
      />
    );
    expect(screen.getByText(/not available/i)).toBeInTheDocument();
    expect(screen.getByText(/higher plan/i)).toBeInTheDocument();
  });
});

describe('StaleIndicator', () => {
  it('renders nothing when not stale', () => {
    const { container } = render(
      <StaleIndicator isStale={false} lastSuccessfulAt={Date.now()} />
    );
    expect(container.textContent).toBe('');
  });

  it('renders age when stale', () => {
    const thirtySecsAgo = Date.now() - 30_000;
    render(<StaleIndicator isStale={true} lastSuccessfulAt={thirtySecsAgo} />);
    expect(screen.getByText(/loaded.*ago/i)).toBeInTheDocument();
  });

  it('carries the real age text on the aria-live=polite render, not empty text', () => {
    const thirtySecsAgo = Date.now() - 30_000;
    const { container } = render(
      <StaleIndicator isStale={true} lastSuccessfulAt={thirtySecsAgo} />
    );
    const politeEl = container.querySelector('[aria-live="polite"]');
    expect(politeEl).not.toBeNull();
    expect(politeEl?.textContent).toMatch(/loaded 30s ago/i);
  });

  it('falls silent (aria-live=off) for periodic updates after the initial announcement', () => {
    jest.useFakeTimers({ now: Date.now() });
    const thirtySecsAgo = Date.now() - 30_000;
    const { container } = render(
      <StaleIndicator isStale={true} lastSuccessfulAt={thirtySecsAgo} />
    );
    expect(container.querySelector('[aria-live="polite"]')).not.toBeNull();

    act(() => {
      jest.advanceTimersByTime(10_000);
    });

    const el = container.querySelector('[aria-live]');
    expect(el?.getAttribute('aria-live')).toBe('off');
    expect(el?.textContent).toMatch(/loaded 40s ago/i);
    jest.useRealTimers();
  });
});
