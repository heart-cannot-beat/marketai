/**
 * @jest-environment jsdom
 */
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { RecoveryPanel } from '@/components/generation/recovery/RecoveryPanel';

/**
 * `useDomainCommand` resolves its `CommandOperationId` at hook-construction
 * time and `execute()` takes a single `{ actionInstanceId, pathParams, body,
 * ifMatch, ... }` params object, returning a `CommandOutcome` discriminated
 * by `kind: 'success' | 'problem' | 'reconciliation'` -- this mirrors the
 * real `web/hooks/domain/useDomainCommand.ts` contract (see the identical
 * correction already documented in `submit-confirmation.spec.tsx` and
 * `new-draft-dialog.spec.tsx`) rather than the task-11 brief's
 * `{ execute, isExecuting }` / `execute(op, params)` / `{ ok: true }` sketch.
 *
 * The task-11 brief's `RecoveryPanel` test fixtures also used action ids
 * `retry_failed_items` / `cancel_run` / `reconcile_run`, which don't match
 * this repo's actual `ActionEligibility.action` values -- `run_queries.py`'s
 * `_RUN_ACTIONS = ["cancel", "retry_failed", "resume_paused", "reconcile"]`
 * (spec: `app/generation/run_queries.py::run_actions`) is the source of
 * truth, so the fixtures below use those instead.
 *
 * `RecoveryPanel` calls `useDomainCommand` once per run-mutating operation
 * (preview/retry/resume/cancel/reconcile) rather than once overall, so the
 * mock below dispatches per-`operationId` to a shared `mockExecute` --
 * letting individual tests assert which command actually fired.
 */
const mockExecute = jest.fn();

jest.mock('@/hooks/domain/useDomainCommand', () => ({
  useDomainCommand: (operationId: string) => ({
    execute: (params: unknown) => mockExecute(operationId, params),
    retry: jest.fn(),
    reset: jest.fn(),
    result: undefined,
    problem: undefined,
    reconciliation: undefined,
    isPending: false,
  }),
}));

beforeEach(() => {
  mockExecute.mockReset();
  mockExecute.mockResolvedValue({
    kind: 'success',
    data: {},
    meta: { etag: null, retryAt: null, location: null },
  });
});

describe('RecoveryPanel', () => {
  it('shows retry button when allowed', () => {
    const run = {
      id: 'run-1',
      allowed_actions: [
        { action: 'retry_failed', allowed: true, reason_code: null },
      ],
    };
    render(<RecoveryPanel run={run} onRecoveryComplete={jest.fn()} />);
    expect(
      screen.getByRole('button', { name: /retry failed/i })
    ).not.toBeDisabled();
  });

  it('disables retry when not allowed', () => {
    const run = {
      id: 'run-1',
      allowed_actions: [
        {
          action: 'retry_failed',
          allowed: false,
          reason_code: 'no_failed_items',
        },
      ],
    };
    render(<RecoveryPanel run={run} onRecoveryComplete={jest.fn()} />);
    expect(
      screen.getByRole('button', { name: /retry failed/i })
    ).toBeDisabled();
    expect(screen.getByText(/no_failed_items/i)).toBeInTheDocument();
  });

  it('shows cancel button when allowed', () => {
    const run = {
      id: 'run-1',
      allowed_actions: [{ action: 'cancel', allowed: true, reason_code: null }],
    };
    render(<RecoveryPanel run={run} onRecoveryComplete={jest.fn()} />);
    expect(screen.getByRole('button', { name: /cancel/i })).not.toBeDisabled();
  });

  it('shows reconcile button when allowed', () => {
    const run = {
      id: 'run-1',
      allowed_actions: [
        { action: 'reconcile', allowed: true, reason_code: null },
      ],
    };
    render(<RecoveryPanel run={run} onRecoveryComplete={jest.fn()} />);
    expect(
      screen.getByRole('button', { name: /reconcile/i })
    ).toBeInTheDocument();
  });

  it('hides the reconciliation guidance when reconcile is not owed', () => {
    // A succeeded Run still lists `reconcile` (not allowed,
    // `no_uncertain_items`): the warning box must not announce "0 item(s)
    // have uncertain outcomes" under it.
    const run = {
      id: 'run-1',
      status: 'succeeded',
      total: 4,
      done: 4,
      failed: 0,
      allowed_actions: [
        { action: 'cancel', allowed: false, reason_code: 'run_not_active' },
        {
          action: 'reconcile',
          allowed: false,
          reason_code: 'no_uncertain_items',
        },
      ],
    };
    render(<RecoveryPanel run={run} onRecoveryComplete={jest.fn()} />);
    expect(
      screen.queryByRole('button', { name: /reconcile/i })
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/uncertain outcomes/i)).not.toBeInTheDocument();
    // The ineligible cancel button still renders, disabled with its reason.
    expect(screen.getByRole('button', { name: /cancel/i })).toBeDisabled();
    expect(screen.getByText(/run_not_active/i)).toBeInTheDocument();
  });

  it('shows the reconciliation guidance with a count for an uncertain run', () => {
    const run = {
      id: 'run-1',
      status: 'uncertain',
      total: 4,
      done: 1,
      failed: 1,
      allowed_actions: [
        { action: 'reconcile', allowed: true, reason_code: null },
      ],
    };
    render(<RecoveryPanel run={run} onRecoveryComplete={jest.fn()} />);
    expect(
      screen.getByRole('button', { name: /reconcile/i })
    ).toBeInTheDocument();
    expect(
      screen.getByText(/2 item\(s\) have uncertain outcomes/i)
    ).toBeInTheDocument();
  });

  it('keeps cancel reachable while the run is still active', () => {
    const run = {
      id: 'run-1',
      status: 'running',
      total: 4,
      done: 1,
      failed: 0,
      allowed_actions: [
        { action: 'cancel', allowed: true, reason_code: null },
        {
          action: 'retry_failed',
          allowed: false,
          reason_code: 'no_failed_items',
        },
        {
          action: 'reconcile',
          allowed: false,
          reason_code: 'no_uncertain_items',
        },
      ],
    };
    render(<RecoveryPanel run={run} onRecoveryComplete={jest.fn()} />);
    expect(screen.getByRole('button', { name: /cancel/i })).not.toBeDisabled();
    // In-flight items are not "uncertain": no reconciliation box.
    expect(screen.queryByText(/uncertain outcomes/i)).not.toBeInTheDocument();
  });

  it('hides actions not in allowed_actions', () => {
    const run = { id: 'run-1', allowed_actions: [] };
    render(<RecoveryPanel run={run} onRecoveryComplete={jest.fn()} />);
    expect(
      screen.queryByRole('button', { name: /retry/i })
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /resume/i })
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /cancel/i })
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /reconcile/i })
    ).not.toBeInTheDocument();
  });

  it('requests a recovery preview and shows it when retry is confirmed', async () => {
    mockExecute.mockImplementation((operationId: string) => {
      if (operationId === 'create_generation_run_recovery_preview') {
        return Promise.resolve({
          kind: 'success',
          data: {
            purpose: 'run_recovery',
            id: 'preview-1',
            preview_hash: 'hash-1',
            command: 'retry_failed_items',
            targets: ['item-1', 'item-2'],
            estimated_additional_cost: { amount: '1.50', currency: 'USD' },
            valid_until: new Date(Date.now() + 600_000).toISOString(),
            run_id: 'run-1',
            expected_run_revision: 3,
            allowed_actions: [
              { action: 'confirm', allowed: true, reason_code: null },
            ],
          },
          meta: { etag: null, retryAt: null, location: null },
        });
      }
      return Promise.resolve({
        kind: 'success',
        data: {},
        meta: { etag: null, retryAt: null, location: null },
      });
    });

    const run = {
      id: 'run-1',
      revision: 3,
      allowed_actions: [
        { action: 'retry_failed', allowed: true, reason_code: null },
      ],
    };
    render(<RecoveryPanel run={run} onRecoveryComplete={jest.fn()} />);

    fireEvent.click(screen.getByRole('button', { name: /retry failed/i }));

    await waitFor(() => {
      expect(mockExecute).toHaveBeenCalledWith(
        'create_generation_run_recovery_preview',
        expect.objectContaining({
          pathParams: { run_id: 'run-1' },
          body: {
            command: 'retry_failed_items',
            selection: { kind: 'all_eligible_failed' },
          },
          ifMatch: 3,
        })
      );
    });

    expect(await screen.findByText(/2 item/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /confirm/i })).not.toBeDisabled();
  });

  it('submits the recovery run and calls onRecoveryComplete after confirming the preview', async () => {
    mockExecute.mockImplementation((operationId: string) => {
      if (operationId === 'create_generation_run_recovery_preview') {
        return Promise.resolve({
          kind: 'success',
          data: {
            purpose: 'run_recovery',
            id: 'preview-1',
            preview_hash: 'hash-1',
            command: 'retry_failed_items',
            targets: ['item-1'],
            estimated_additional_cost: { amount: '1.50', currency: 'USD' },
            valid_until: new Date(Date.now() + 600_000).toISOString(),
            run_id: 'run-1',
            expected_run_revision: 3,
            allowed_actions: [
              { action: 'confirm', allowed: true, reason_code: null },
            ],
          },
          meta: { etag: null, retryAt: null, location: null },
        });
      }
      if (operationId === 'retry_generation_run_failed_items') {
        return Promise.resolve({
          kind: 'success',
          data: { operation_id: 'op-1', run_id: 'run-1' },
          meta: { etag: null, retryAt: null, location: null },
        });
      }
      return Promise.resolve({
        kind: 'success',
        data: {},
        meta: { etag: null, retryAt: null, location: null },
      });
    });

    const onRecoveryComplete = jest.fn();
    const run = {
      id: 'run-1',
      revision: 3,
      allowed_actions: [
        { action: 'retry_failed', allowed: true, reason_code: null },
      ],
    };
    render(<RecoveryPanel run={run} onRecoveryComplete={onRecoveryComplete} />);

    fireEvent.click(screen.getByRole('button', { name: /retry failed/i }));
    await screen.findByRole('button', { name: /confirm/i });

    fireEvent.click(screen.getByRole('button', { name: /confirm/i }));

    await waitFor(() => {
      expect(mockExecute).toHaveBeenCalledWith(
        'retry_generation_run_failed_items',
        expect.objectContaining({
          pathParams: { run_id: 'run-1' },
          body: { preview_id: 'preview-1', preview_hash: 'hash-1' },
          ifMatch: 3,
        })
      );
    });

    await waitFor(() => {
      expect(onRecoveryComplete).toHaveBeenCalled();
    });
  });

  it('shows a warning and confirm step before cancelling a run', async () => {
    mockExecute.mockResolvedValue({
      kind: 'success',
      data: { operation_id: 'op-2', run_id: 'run-1' },
      meta: { etag: null, retryAt: null, location: null },
    });

    const onRecoveryComplete = jest.fn();
    const run = {
      id: 'run-1',
      revision: 5,
      allowed_actions: [{ action: 'cancel', allowed: true, reason_code: null }],
    };
    render(<RecoveryPanel run={run} onRecoveryComplete={onRecoveryComplete} />);

    fireEvent.click(screen.getByRole('button', { name: /cancel/i }));

    expect(
      screen.getByRole('button', { name: /keep running/i })
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /cancel run/i }));

    await waitFor(() => {
      expect(mockExecute).toHaveBeenCalledWith(
        'cancel_generation_run',
        expect.objectContaining({
          pathParams: { run_id: 'run-1' },
          ifMatch: 5,
        })
      );
    });
    await waitFor(() => {
      expect(onRecoveryComplete).toHaveBeenCalled();
    });
  });

  it('submits an operator note through reconciliation guidance', async () => {
    mockExecute.mockResolvedValue({
      kind: 'success',
      data: { operation_id: 'op-3', run_id: 'run-1' },
      meta: { etag: null, retryAt: null, location: null },
    });

    const onRecoveryComplete = jest.fn();
    const run = {
      id: 'run-1',
      allowed_actions: [
        { action: 'reconcile', allowed: true, reason_code: null },
      ],
    };
    render(<RecoveryPanel run={run} onRecoveryComplete={onRecoveryComplete} />);

    const reconcileButton = screen.getByRole('button', { name: /reconcile/i });
    expect(reconcileButton).toBeDisabled();

    fireEvent.change(screen.getByPlaceholderText(/operator note/i), {
      target: { value: 'Verified with provider dashboard.' },
    });
    expect(reconcileButton).not.toBeDisabled();

    fireEvent.click(reconcileButton);

    await waitFor(() => {
      expect(mockExecute).toHaveBeenCalledWith(
        'reconcile_generation_run',
        expect.objectContaining({
          pathParams: { run_id: 'run-1' },
          body: { operator_note: 'Verified with provider dashboard.' },
        })
      );
    });
    await waitFor(() => {
      expect(onRecoveryComplete).toHaveBeenCalled();
    });
  });
});
