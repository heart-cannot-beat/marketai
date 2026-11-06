/** @jest-environment jsdom */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { SWRConfig } from 'swr';
import {
  MarketingExplanationPanel,
  MarketingFeedbackForm,
} from '@/components/workspace/MarketingFollowup';
import messages from '../../../locales/en/common.json';
jest.mock('next-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, o?: Record<string, unknown>) =>
      ((messages as Record<string, string>)[key] || key).replace(
        /\{\{(\w+)\}\}/g,
        (_, k) => String(o?.[k] || '')
      ),
  }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { workspaceId: 'ws', slug: 'alpha' } }),
}));
const gateway = {
  getMarketingCapabilities: jest.fn(),
  getGenerationCapabilities: jest.fn(),
};
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => gateway,
}));
const ok = (data: unknown) => ({ kind: 'ok', data });
const wrap = (child: React.ReactNode) => (
  <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
    {child}
  </SWRConfig>
);
beforeEach(() => {
  jest.clearAllMocks();
  gateway.getMarketingCapabilities.mockResolvedValue(
    ok({
      paid: {
        available: true,
        provider: 'openai',
        model: 'configured-model',
        maximum_cost: '0.10',
        quote_hash: 'quote-a',
      },
    })
  );
  gateway.getGenerationCapabilities.mockResolvedValue(
    ok({
      provider_options: [
        {
          id: 'mock',
          label: 'Mock',
          available: true,
          execution_mode: 'mock',
          default_model: 'mock-v1',
        },
      ],
    })
  );
});
test('paid interpretation requires explicit cost confirmation and never auto-dispatches', async () => {
  const onRequest = jest.fn();
  render(
    wrap(
      <MarketingExplanationPanel
        mode="byok"
        busy={false}
        canRequest
        onRequest={onRequest}
      />
    )
  );
  const button = screen.getByRole('button', {
    name: 'Interpret these results with AI',
  });
  expect(button).toBeDisabled();
  fireEvent.click(await screen.findByRole('checkbox'));
  expect(button).toBeEnabled();
  expect(onRequest).not.toHaveBeenCalled();
  fireEvent.click(button);
  expect(onRequest).toHaveBeenCalledWith({
    execution_mode: 'byok',
    quote_hash: 'quote-a',
    strategy_language: 'en-US',
  });
});
test('an explanation written in another language is labelled and shown as recorded', () => {
  const paragraph = {
    evidence_id: 'comparison',
    object_id: 'eval',
    observation: '相对表现：尚无明确差异。',
    explanation: '只比较实际投放的组合。',
  };
  render(
    wrap(
      <MarketingExplanationPanel
        mode="mock"
        busy={false}
        canRequest
        onRequest={jest.fn()}
        call={{
          provider: 'mock',
          model: 'marketing-example-v1',
          maximum_cost: '0',
          id: 'call',
          state: 'succeeded',
          language: 'zh-CN',
          current: true,
          execution_mode: 'mock',
          error_code: null,
          data: {
            paragraphs: [paragraph],
            evidence_status: 'simulated',
          } as never,
        }}
      />
    )
  );
  expect(screen.getByRole('note')).toHaveTextContent(
    'Written in Chinese (Simplified) and shown as recorded, without translation.'
  );
  expect(screen.getByText(paragraph.observation)).toBeInTheDocument();
  expect(screen.getByText(paragraph.explanation)).toBeInTheDocument();
});
test('uncertain calls cannot be resent and stale text is hidden', async () => {
  render(
    wrap(
      <MarketingExplanationPanel
        mode="byok"
        busy={false}
        canRequest
        onRequest={jest.fn()}
        call={{
          provider: 'gemini',
          model: 'test',
          maximum_cost: '0',
          id: 'call',
          state: 'uncertain',
          current: false,
          execution_mode: 'byok',
          error_code: 'unknown',
          data: null,
        }}
      />
    )
  );
  expect(screen.getByText(/out of date and is hidden/)).toBeInTheDocument();
  fireEvent.click(await screen.findByRole('checkbox'));
  expect(
    screen.getByRole('button', { name: 'Interpret these results with AI' })
  ).toBeDisabled();
});
test('correction prepares a free draft with optional edits before any generation', async () => {
  const onPrepare = jest.fn();
  render(
    wrap(
      <MarketingFeedbackForm
        assetVersionId="version-a"
        mode="mock"
        busy={false}
        onPrepare={onPrepare}
      />
    )
  );
  const details = screen.getByText('Modify with AI').closest('details')!;
  details.open = true;
  fireEvent(details, new Event('toggle'));
  fireEvent.change(screen.getByLabelText('Additional adjustments (optional)'), {
    target: { value: 'Make the proposition visible' },
  });
  const button = screen.getByRole('button', {
    name: 'Prepare correction draft',
  });
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
  expect(onPrepare).toHaveBeenCalledWith({
    asset_version_id: 'version-a',
    note: 'Make the proposition visible',
    provider: 'mock',
    model: 'mock-v1',
  });
});
