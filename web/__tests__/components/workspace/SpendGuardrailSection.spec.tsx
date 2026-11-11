/** @jest-environment jsdom */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

let mockWorkspace: { workspaceId: string; slug: string; role: string } = {
  workspaceId: 'ws_test',
  slug: 'test-ws',
  role: 'OWNER',
};
const mockRefresh = jest.fn(async () => undefined);
const executeByOperation: Record<string, jest.Mock> = {
  preview_spend_guardrail: jest.fn(),
  confirm_spend_guardrail: jest.fn(),
};

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: mockWorkspace }),
}));

jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: jest.fn(),
}));

jest.mock('@/hooks/domain/useDomainCommand', () => ({
  useDomainCommand: jest.fn((operationId: string) => ({
    execute: executeByOperation[operationId],
    retry: jest.fn(),
    reset: jest.fn(),
    isPending: false,
    result: undefined,
    problem: undefined,
    reconciliation: undefined,
  })),
}));

import { SpendGuardrailSection } from '@/components/workspace/SpendGuardrailSection';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import {
  changeKind,
  compareDecimal,
  plainDecimal,
} from '@/lib/entitlements/spend-guardrail';

/** What provisioning creates (contract §11), as `GET /spend-guardrail` serialises it. */
const DEFAULTS = {
  workspace_id: 'ws_test',
  revision: 1,
  budget_currency: 'USD',
  per_operation_limit: '5.000000000000',
  rolling_24h_limit: '20.000000000000',
  period_limit: '100.000000000000',
  outstanding_exposure_limit: '10.000000000000',
  platform_policy_version: 1,
};

const meta = { etag: null, retryAt: null, location: null };
const success = (data: unknown) => ({ kind: 'success', data, meta });
const refusal = (code: string, params?: Record<string, unknown>) => ({
  kind: 'problem',
  problem: {
    type: `urn:marketai:error:${code}`,
    title: 'Request failed',
    status: 400,
    detail: `detail for ${code}`,
    instance: '',
    code,
    trace_id: 'trace-guardrail',
    retryable: false,
    field_errors: [],
    ...(params ? { params } : {}),
  },
  meta,
});

function preview(
  old: Record<string, string>,
  proposed: Record<string, string>
) {
  return success({
    workspace_id: 'ws_test',
    current_revision: 1,
    old_limits: old,
    proposed_limits: proposed,
    preview_token: 'sgp_token_1',
    expires_at: '2026-11-11T10:15:00+00:00',
    mfa_evidence_ref: null,
  });
}

const limitsOf = (guardrail: typeof DEFAULTS) => ({
  per_operation_limit: guardrail.per_operation_limit,
  rolling_24h_limit: guardrail.rolling_24h_limit,
  period_limit: guardrail.period_limit,
  outstanding_exposure_limit: guardrail.outstanding_exposure_limit,
});

function arrangeRead(guardrail: unknown = DEFAULTS, problem?: unknown) {
  jest.mocked(useDomainQuery).mockImplementation(((operationId: string) => {
    if (operationId !== 'get_spend_guardrail') {
      throw new Error(`unexpected read ${operationId}`);
    }
    return {
      data: problem ? undefined : guardrail,
      problem,
      isLoading: false,
      isStale: false,
      lastSuccessfulAt: null,
      refresh: mockRefresh,
    };
  }) as never);
}

const preview$ = () => executeByOperation.preview_spend_guardrail;
const confirm$ = () => executeByOperation.confirm_spend_guardrail;

function setLimit(label: RegExp, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

beforeEach(() => {
  jest.clearAllMocks();
  mockWorkspace = { workspaceId: 'ws_test', slug: 'test-ws', role: 'OWNER' };
  arrangeRead();
});

describe('SpendGuardrailSection: every member reads the limits', () => {
  it('shows a Member the four limits in the budget currency, read-only', () => {
    mockWorkspace = { ...mockWorkspace, role: 'MEMBER' };
    render(<SpendGuardrailSection />);

    expect(screen.getByText('Spending limits')).toBeInTheDocument();
    expect(
      screen.getByTestId('spend-guardrail-value-per_operation_limit')
    ).toHaveTextContent('$5');
    expect(
      screen.getByTestId('spend-guardrail-value-rolling_24h_limit')
    ).toHaveTextContent('$20');
    expect(
      screen.getByTestId('spend-guardrail-value-period_limit')
    ).toHaveTextContent('$100');
    expect(
      screen.getByTestId('spend-guardrail-value-outstanding_exposure_limit')
    ).toHaveTextContent('$10');
    expect(screen.getByTestId('spend-guardrail-owner-only')).toHaveTextContent(
      'Only the Workspace Owner can change these limits.'
    );
    expect(screen.queryByTestId('spend-guardrail-edit')).toBeNull();
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(preview$()).not.toHaveBeenCalled();
    expect(confirm$()).not.toHaveBeenCalled();
  });

  it('shows the Owner the same limits and a way to change them', () => {
    render(<SpendGuardrailSection />);
    expect(
      screen.getByTestId('spend-guardrail-value-per_operation_limit')
    ).toHaveTextContent('$5');
    expect(screen.queryByTestId('spend-guardrail-owner-only')).toBeNull();
    fireEvent.click(screen.getByTestId('spend-guardrail-edit'));
    expect(screen.getByLabelText(/Per operation/)).toHaveValue('5');
    expect(screen.getByLabelText(/Rolling 24 hours/)).toHaveValue('20');
    expect(screen.getByLabelText(/This subscription period/)).toHaveValue(
      '100'
    );
    expect(screen.getByLabelText(/Unsettled at any one time/)).toHaveValue(
      '10'
    );
    // Nothing changed yet: nothing to save.
    expect(screen.getByTestId('spend-guardrail-save')).toBeDisabled();
  });

  it('says so when the limits cannot be read', () => {
    arrangeRead(undefined, {
      code: 'spend_guardrail_not_found',
      trace_id: 'trace-read',
    });
    render(<SpendGuardrailSection />);
    expect(
      screen.getByTestId('spend-guardrail-read-problem')
    ).toHaveTextContent('The spending limits could not be loaded.');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(mockRefresh).toHaveBeenCalled();
  });
});

describe('SpendGuardrailSection: the Owner lowers a limit', () => {
  it('previews and confirms in one save, without a second confirmation', async () => {
    const lowered = { ...limitsOf(DEFAULTS), per_operation_limit: '3' };
    preview$().mockResolvedValue(preview(limitsOf(DEFAULTS), lowered));
    confirm$().mockResolvedValue(
      success({ ...DEFAULTS, revision: 2, per_operation_limit: '3' })
    );
    render(<SpendGuardrailSection />);

    fireEvent.click(screen.getByTestId('spend-guardrail-edit'));
    setLimit(/Per operation/, '3');
    expect(screen.getByTestId('spend-guardrail-kind')).toHaveTextContent(
      'Lower limits apply to new calls as soon as you save.'
    );
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() =>
      expect(screen.getByTestId('spend-guardrail-saved')).toHaveTextContent(
        'The new limits are saved.'
      )
    );
    expect(preview$()).toHaveBeenCalledTimes(1);
    expect(preview$().mock.calls[0][0].body).toEqual({
      per_operation_limit: '3',
      rolling_24h_limit: '20',
      period_limit: '100',
      outstanding_exposure_limit: '10',
      expected_revision: 1,
    });
    expect(confirm$()).toHaveBeenCalledTimes(1);
    expect(confirm$().mock.calls[0][0].body).toEqual({
      preview_token: 'sgp_token_1',
      expected_revision: 1,
    });
    // Two commands, two intents.
    expect(preview$().mock.calls[0][0].actionInstanceId).not.toEqual(
      confirm$().mock.calls[0][0].actionInstanceId
    );
    expect(screen.queryByTestId('spend-guardrail-raise-review')).toBeNull();
    expect(mockRefresh).toHaveBeenCalled();
    expect(jest.mocked(useDomainCommand)).toHaveBeenCalledWith(
      'preview_spend_guardrail'
    );
    expect(jest.mocked(useDomainCommand)).toHaveBeenCalledWith(
      'confirm_spend_guardrail'
    );
  });

  it('re-reads the limits when they moved on before the confirm', async () => {
    const lowered = { ...limitsOf(DEFAULTS), period_limit: '50' };
    preview$().mockResolvedValue(preview(limitsOf(DEFAULTS), lowered));
    confirm$().mockResolvedValue(refusal('revision_conflict'));
    render(<SpendGuardrailSection />);

    fireEvent.click(screen.getByTestId('spend-guardrail-edit'));
    setLimit(/This subscription period/, '50');
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() =>
      expect(screen.getByTestId('spend-guardrail-problem')).toHaveTextContent(
        'The new limits were not saved.'
      )
    );
    expect(screen.getByTestId('spend-guardrail-problem')).toHaveTextContent(
      'This changed while you were working. Refresh and try again.'
    );
    expect(mockRefresh).toHaveBeenCalled();
    expect(screen.queryByTestId('spend-guardrail-saved')).toBeNull();
  });
});

describe('SpendGuardrailSection: the Owner raises a limit', () => {
  const lowerFirst = {
    ...DEFAULTS,
    per_operation_limit: '2.000000000000',
    outstanding_exposure_limit: '4.000000000000',
  };

  it("shows the server's preview and confirms only on the second click", async () => {
    arrangeRead(lowerFirst);
    const raised = { ...limitsOf(lowerFirst), per_operation_limit: '4' };
    preview$().mockResolvedValue(preview(limitsOf(lowerFirst), raised));
    confirm$().mockResolvedValue(
      success({ ...lowerFirst, revision: 2, per_operation_limit: '4' })
    );
    render(<SpendGuardrailSection />);

    fireEvent.click(screen.getByTestId('spend-guardrail-edit'));
    setLimit(/Per operation/, '4');
    expect(screen.getByTestId('spend-guardrail-kind')).toHaveTextContent(
      'Raising a limit lets this Workspace spend more'
    );
    fireEvent.click(screen.getByRole('button', { name: 'Review the raise' }));

    await waitFor(() =>
      expect(
        screen.getByTestId('spend-guardrail-raise-review')
      ).toBeInTheDocument()
    );
    expect(
      screen.getByTestId('spend-guardrail-change-per_operation_limit')
    ).toHaveTextContent('Per operation$2$4');
    expect(preview$()).toHaveBeenCalledTimes(1);
    expect(confirm$()).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Confirm raise' }));
    await waitFor(() =>
      expect(screen.getByTestId('spend-guardrail-saved')).toBeInTheDocument()
    );
    expect(confirm$()).toHaveBeenCalledTimes(1);
    expect(confirm$().mock.calls[0][0].body).toEqual({
      preview_token: 'sgp_token_1',
      expected_revision: 1,
    });
  });

  it('goes back to the form without confirming', async () => {
    arrangeRead(lowerFirst);
    const raised = { ...limitsOf(lowerFirst), outstanding_exposure_limit: '8' };
    preview$().mockResolvedValue(preview(limitsOf(lowerFirst), raised));
    render(<SpendGuardrailSection />);

    fireEvent.click(screen.getByTestId('spend-guardrail-edit'));
    setLimit(/Unsettled at any one time/, '8');
    fireEvent.click(screen.getByRole('button', { name: 'Review the raise' }));
    await screen.findByTestId('spend-guardrail-raise-review');
    fireEvent.click(screen.getByRole('button', { name: 'Back' }));

    expect(screen.getByLabelText(/Unsettled at any one time/)).toHaveValue('8');
    expect(confirm$()).not.toHaveBeenCalled();
  });

  it('marks the limit the platform maximum refused, with that maximum', async () => {
    preview$().mockResolvedValue(
      refusal('spend_guardrail_limit_above_platform', {
        field: 'per_operation_limit',
        limit: '5',
      })
    );
    render(<SpendGuardrailSection />);

    fireEvent.click(screen.getByTestId('spend-guardrail-edit'));
    setLimit(/Per operation/, '50');
    fireEvent.click(screen.getByRole('button', { name: 'Review the raise' }));

    await waitFor(() =>
      expect(
        screen.getByTestId('spend-guardrail-per_operation_limit-hint')
      ).toHaveTextContent('The platform allows at most $5 here.')
    );
    expect(screen.getByLabelText(/Per operation/)).toHaveAttribute(
      'aria-invalid',
      'true'
    );
    expect(screen.getByLabelText(/Rolling 24 hours/)).toHaveAttribute(
      'aria-invalid',
      'false'
    );
    expect(screen.getByTestId('spend-guardrail-problem')).toHaveTextContent(
      'This limit is above the platform maximum.'
    );
    expect(screen.getByTestId('spend-guardrail-problem')).toHaveTextContent(
      'spend_guardrail_limit_above_platform'
    );
    expect(confirm$()).not.toHaveBeenCalled();
    expect(screen.queryByTestId('spend-guardrail-raise-review')).toBeNull();

    // Editing the refused limit clears its mark.
    setLimit(/Per operation/, '4');
    expect(
      screen.queryByTestId('spend-guardrail-per_operation_limit-hint')
    ).toBeNull();
  });

  it('refuses to send a limit that is not a number of 0 or more', () => {
    render(<SpendGuardrailSection />);
    fireEvent.click(screen.getByTestId('spend-guardrail-edit'));
    setLimit(/Rolling 24 hours/, '-1');
    expect(
      screen.getByTestId('spend-guardrail-rolling_24h_limit-hint')
    ).toHaveTextContent('Enter a number of 0 or more.');
    expect(screen.getByTestId('spend-guardrail-save')).toBeDisabled();
    expect(preview$()).not.toHaveBeenCalled();
  });
});

describe('spend-guardrail model', () => {
  it('compares decimals exactly', () => {
    expect(compareDecimal('5.000000000000', '5')).toBe(0);
    expect(compareDecimal('4.999999999999', '5')).toBe(-1);
    expect(compareDecimal('10', '9.5')).toBe(1);
    expect(compareDecimal('0.10', '0.1')).toBe(0);
    expect(compareDecimal('007', '7')).toBe(0);
  });

  it('reads a draft as lower, raise, unchanged or invalid', () => {
    const current = limitsOf(DEFAULTS);
    expect(changeKind(current, { ...current })).toBe('unchanged');
    expect(changeKind(current, { ...current, period_limit: '99.5' })).toBe(
      'lower'
    );
    // One limit up is a raise, even when another goes down.
    expect(
      changeKind(current, {
        ...current,
        period_limit: '50',
        rolling_24h_limit: '20.01',
      })
    ).toBe('raise');
    expect(changeKind(current, { ...current, period_limit: 'abc' })).toBe(
      'invalid'
    );
    expect(plainDecimal('2.500000000000')).toBe('2.5');
    expect(plainDecimal('100')).toBe('100');
  });
});
