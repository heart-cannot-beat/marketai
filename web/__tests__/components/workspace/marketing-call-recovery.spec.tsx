/** @jest-environment jsdom */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MarketingCallRecovery } from '@/components/workspace/MarketingCallRecovery';
import messages from '../../../locales/en/common.json';

let role = 'OWNER';
const gateway = { resolveMarketingCall: jest.fn(), pollOperation: jest.fn() };
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { workspaceId: 'ws', slug: 'alpha', role } }),
}));
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => gateway,
}));
jest.mock('next-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, options?: Record<string, unknown>) =>
      ((messages as Record<string, string>)[key] ?? key).replace(
        /\{\{(\w+)\}\}/g,
        (_, name: string) => String(options?.[name] ?? '')
      ),
  }),
}));
const call = { id: 'pc_missing', state: 'uncertain', maximum_cost: '0.10' };
beforeEach(() => {
  role = 'OWNER';
  jest.clearAllMocks();
  sessionStorage.clear();
  gateway.resolveMarketingCall.mockResolvedValue({
    kind: 'accepted',
    data: { operation_id: 'op_resolve' },
  });
  gateway.pollOperation.mockResolvedValue({
    kind: 'ok',
    data: { state: 'succeeded' },
    meta: {},
  });
  Object.defineProperty(globalThis.crypto, 'randomUUID', {
    configurable: true,
    value: () => 'recovery-action',
  });
});

test('owner explicitly accepts the estimate and note, then follows the durable recovery operation', async () => {
  const onResolved = jest.fn();
  render(
    <MarketingCallRecovery
      call={call}
      kind="planning"
      onResolved={onResolved}
    />
  );
  const button = screen.getByRole('button', { name: 'Close without a result' });
  expect(button).toBeDisabled();
  fireEvent.change(screen.getByRole('textbox', { name: 'Review note' }), {
    target: { value: 'Checked the missing response.' },
  });
  expect(button).toBeDisabled();
  fireEvent.click(screen.getByRole('checkbox'));
  fireEvent.click(button);
  await waitFor(() => expect(onResolved).toHaveBeenCalledTimes(1));
  expect(gateway.resolveMarketingCall).toHaveBeenCalledWith({
    workspaceSlug: 'alpha',
    callId: call.id,
    actionInstanceId: 'recovery-action',
    body: {
      kind: 'planning',
      maximum_cost: '0.10',
      acknowledge_estimated_cost: true,
      note: 'Checked the missing response.',
    },
  });
  expect(gateway.pollOperation).toHaveBeenCalledWith(
    expect.objectContaining({ operationId: 'op_resolve' })
  );
});

test('refresh restores reconciliation tracking without sending a second request', async () => {
  sessionStorage.setItem(`marketing-recovery:ws:${call.id}`, 'op_saved');
  const onResolved = jest.fn();
  render(
    <MarketingCallRecovery call={call} kind="ai_qc" onResolved={onResolved} />
  );
  await waitFor(() => expect(onResolved).toHaveBeenCalledTimes(1));
  expect(gateway.resolveMarketingCall).not.toHaveBeenCalled();
  expect(gateway.pollOperation).toHaveBeenCalledWith(
    expect.objectContaining({ operationId: 'op_saved' })
  );
});

test('members see the owner requirement and cannot admit recovery', () => {
  role = 'MEMBER';
  render(
    <MarketingCallRecovery call={call} kind="planning" onResolved={jest.fn()} />
  );
  expect(screen.getByRole('note')).toHaveTextContent('Owner');
  expect(screen.queryByRole('button')).not.toBeInTheDocument();
  expect(gateway.resolveMarketingCall).not.toHaveBeenCalled();
});
