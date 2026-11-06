/**
 * @jest-environment jsdom
 */
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { SubmitConfirmation } from '@/components/generation/preview/SubmitConfirmation';

const mockExecute = jest.fn();
const mockNavigateToRun = jest.fn();

/**
 * `useDomainCommand` resolves the operationId at hook-construction time
 * (`useDomainCommand('submit_generation_run')`), not per-call, and
 * `execute()` takes a single `{ actionInstanceId, body, ... }` params object
 * rather than `execute(operationId, params)` -- this mirrors the real
 * `web/hooks/domain/useDomainCommand.ts` contract (see the identical
 * correction documented in `new-draft-dialog.spec.tsx`) instead of the
 * task-9 brief's `{ execute, isExecuting }` / `execute(op, { body })` /
 * `{ ok: true, data }` sketch, which doesn't match the hook that actually
 * ships in this codebase.
 */
jest.mock('@/hooks/domain/useDomainCommand', () => ({
  useDomainCommand: () => ({
    execute: mockExecute,
    retry: jest.fn(),
    reset: jest.fn(),
    result: undefined,
    problem: undefined,
    reconciliation: undefined,
    isPending: false,
  }),
}));

jest.mock('@/components/generation/shared/useGenerationNav', () => ({
  useGenerationNav: () => ({ navigateToRun: mockNavigateToRun }),
}));

beforeEach(() => {
  mockExecute.mockClear();
  mockNavigateToRun.mockClear();
});

describe('SubmitConfirmation', () => {
  const futureDate = new Date(Date.now() + 600_000).toISOString();
  const pastDate = new Date(Date.now() - 1000).toISOString();

  it('enables submit when preview is valid', () => {
    render(
      <SubmitConfirmation
        previewId="prev-1"
        previewHash="hash-1"
        draftRevision={1}
        validUntil={futureDate}
        onSubmitted={jest.fn()}
      />
    );
    expect(screen.getByRole('button', { name: /submit/i })).not.toBeDisabled();
  });

  it('disables submit when preview is expired', () => {
    render(
      <SubmitConfirmation
        previewId="prev-1"
        previewHash="hash-1"
        draftRevision={1}
        validUntil={pastDate}
        onSubmitted={jest.fn()}
      />
    );
    expect(screen.getByRole('button', { name: /submit/i })).toBeDisabled();
    expect(screen.getByText(/expired/i)).toBeInTheDocument();
  });

  it('treats a missing valid_until as not expired', () => {
    render(
      <SubmitConfirmation
        previewId="prev-1"
        previewHash="hash-1"
        draftRevision={1}
        validUntil=""
        onSubmitted={jest.fn()}
      />
    );
    expect(screen.getByRole('button', { name: /submit/i })).not.toBeDisabled();
  });

  it('disables submit when ActionEligibility marks submit as not allowed', () => {
    render(
      <SubmitConfirmation
        previewId="prev-1"
        previewHash="hash-1"
        draftRevision={1}
        validUntil={futureDate}
        allowedActions={[
          {
            action: 'submit',
            allowed: false,
            reason_code: 'preview_not_submittable',
          },
        ]}
        onSubmitted={jest.fn()}
      />
    );
    expect(screen.getByRole('button', { name: /submit/i })).toBeDisabled();
  });

  it('enables submit when ActionEligibility marks submit as allowed', () => {
    render(
      <SubmitConfirmation
        previewId="prev-1"
        previewHash="hash-1"
        draftRevision={1}
        validUntil={futureDate}
        allowedActions={[
          { action: 'submit', allowed: true, reason_code: null },
        ]}
        onSubmitted={jest.fn()}
      />
    );
    expect(screen.getByRole('button', { name: /submit/i })).not.toBeDisabled();
  });

  it('submits and navigates to run', async () => {
    const onSubmitted = jest.fn();
    mockExecute.mockResolvedValue({
      kind: 'success',
      data: { operation_id: 'op-1', generation_run_id: 'run-1' },
      meta: { etag: null, retryAt: null, location: null },
    });

    render(
      <SubmitConfirmation
        previewId="prev-1"
        previewHash="hash-1"
        draftRevision={1}
        validUntil={futureDate}
        onSubmitted={onSubmitted}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /submit/i }));

    await waitFor(() => {
      expect(mockExecute).toHaveBeenCalledWith(
        expect.objectContaining({
          actionInstanceId: expect.any(String),
          body: { preview_id: 'prev-1', preview_hash: 'hash-1' },
          ifMatch: 1,
        })
      );
    });

    await waitFor(() => {
      expect(mockNavigateToRun).toHaveBeenCalledWith('run-1');
    });
    expect(onSubmitted).toHaveBeenCalled();
  });

  it('explains the refusal by its code and does not navigate when the command fails', async () => {
    mockExecute.mockResolvedValue({
      kind: 'problem',
      problem: {
        type: 'client_error',
        title: 'Conflict',
        status: 409,
        detail: 'Preview already submitted',
        instance: '',
        code: 'preview_already_submitted',
        trace_id: 't1',
        retryable: false,
        field_errors: [],
      },
      meta: { etag: null, retryAt: null, location: null },
    });

    render(
      <SubmitConfirmation
        previewId="prev-1"
        previewHash="hash-1"
        draftRevision={1}
        validUntil={futureDate}
        onSubmitted={jest.fn()}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /submit/i }));

    await waitFor(() => {
      expect(
        screen.getByText('This preview was already submitted.')
      ).toBeInTheDocument();
    });

    expect(mockNavigateToRun).not.toHaveBeenCalled();
  });
});
