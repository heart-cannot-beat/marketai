/** @jest-environment jsdom */
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { SWRConfig } from 'swr';
import {
  MarketingNextPlan,
  MarketingReusePicker,
  MarketingComparisonView,
} from '@/components/workspace/MarketingContinuation';
import type { MarketingRound } from '@/components/workspace/MarketingExecution';
import fixture from '../../fixtures/marketing-workflow.json';
import messages from '../../../locales/en/common.json';
import zhMessages from '../../../locales/zh-CN/common.json';
// #293 reads the reuse picker's scope copy in both locales.
let catalog: Record<string, string> = messages;
jest.mock('next-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, options?: Record<string, unknown>) =>
      (catalog[key] ?? key).replace(/\{\{(\w+)\}\}/g, (_: string, k: string) =>
        String(options?.[k] ?? '')
      ),
  }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { workspaceId: 'ws', slug: 'alpha' } }),
}));
const gateway = {
  getMarketingCapabilities: jest.fn(),
  listGenerationAssets: jest.fn(),
  createGenerationAssetVersionDownloadIntent: jest.fn(),
};
const blobs = { downloadBlob: jest.fn(async () => new Blob(['exact-image'])) };
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => gateway,
}));
jest.mock('@/lib/domain-client/upload-deps', () => ({
  useBlobTransferClient: () => blobs,
}));
function wrap(ui: React.ReactNode) {
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      {ui}
    </SWRConfig>
  );
}
const row = fixture.evaluated as unknown as MarketingRound;
beforeEach(() => {
  jest.clearAllMocks();
  URL.createObjectURL = jest.fn(() => 'blob:exact-history');
  URL.revokeObjectURL = jest.fn();
  Object.defineProperty(globalThis.crypto, 'randomUUID', {
    configurable: true,
    value: () => 'next-action',
  });
  gateway.getMarketingCapabilities.mockResolvedValue({
    kind: 'ok',
    data: {
      paid: {
        available: true,
        provider: 'openai',
        model: 'text-model',
        maximum_cost: '0.10',
        quote_hash: 'quote',
      },
    },
  });
  gateway.listGenerationAssets.mockResolvedValue({
    kind: 'ok',
    data: {
      items: [
        {
          id: 'old-version',
          created_at: '2026-10-17',
          provider: 'openai',
          model: 'image-model',
          aspect_ratio: '4:5',
        },
      ],
      next_cursor: null,
    },
  });
  gateway.createGenerationAssetVersionDownloadIntent.mockResolvedValue({
    kind: 'ok',
    data: {
      expires_at: '2026-10-17T01:00:00Z',
      download_grant: {
        url: 'https://private.test/exact',
        content_disposition: 'attachment',
      },
    },
  });
});
test('next plan uses evidence-based direction and requires an explicit fresh quote', async () => {
  const onRequest = jest.fn(async (body: any) => {
    void body;
  });
  wrap(
    <MarketingNextPlan
      row={{
        ...row,
        execution_mode: 'byok',
        evaluation: {
          ...row.evaluation!,
          next_action: 'match_expressions_for_candidates',
          qualified_asset_version_ids: ['qualified'],
        },
      }}
      busy={false}
      onRequest={onRequest}
    />
  );
  expect(screen.getByLabelText('What to explore next')).toHaveValue(
    'match_expressions'
  );
  const draft = screen.getByRole('button', {
    name: 'Draft next plan for review',
  });
  expect(draft).toBeDisabled();
  fireEvent.click(
    await screen.findByRole('checkbox', { name: /Confirm up to US\$0.10/ })
  );
  fireEvent.click(draft);
  await waitFor(() => expect(onRequest).toHaveBeenCalledTimes(1));
  expect(onRequest.mock.calls[0][0]).toMatchObject({
    kind: 'plan',
    direction: 'match_expressions',
    execution_mode: 'byok',
    quote_hash: 'quote',
  });
});
test('element optimization asks for a concrete problem, without generating combinations automatically', async () => {
  const onRequest = jest.fn(async (body: any) => {
    void body;
  });
  wrap(<MarketingNextPlan row={row} busy={false} onRequest={onRequest} />);
  fireEvent.change(screen.getByLabelText('What to explore next'), {
    target: { value: 'optimize_elements' },
  });
  const draft = screen.getByRole('button', {
    name: 'Draft next plan for review',
  });
  expect(draft).toBeDisabled();
  fireEvent.change(
    screen.getByLabelText(
      'Optional adjustment (required for element optimization)'
    ),
    { target: { value: 'Does a driver make the use case clearer?' } }
  );
  fireEvent.click(draft);
  await waitFor(() => expect(onRequest).toHaveBeenCalled());
  expect(onRequest.mock.calls[0][0].decision_question).toContain('driver');
});
test('a round frozen before the advertising language asks for the next one, starting from the inferred language (#310)', async () => {
  const onRequest = jest.fn(async (body: any) => {
    void body;
  });
  // The fixture's frozen plan predates J-02: it has no advertising language.
  expect(row.snapshot.plan.ad_language).toBeUndefined();
  wrap(
    <MarketingNextPlan
      row={row}
      inferredAdLanguage="en-US"
      busy={false}
      onRequest={onRequest}
    />
  );
  const language = screen.getByLabelText('Advertising language');
  expect(language).toHaveValue('en-US');
  expect(language).toHaveAccessibleDescription(
    messages['marketing-next-plan-ad-language']
  );
  fireEvent.change(language, { target: { value: 'zh-CN' } });
  fireEvent.click(
    screen.getByRole('button', { name: 'Draft next plan for review' })
  );
  await waitFor(() => expect(onRequest).toHaveBeenCalledTimes(1));
  expect(onRequest.mock.calls[0][0]).toMatchObject({
    kind: 'plan',
    ad_language: 'zh-CN',
  });
});
test('a round with an advertising language carries it into the next plan without asking (#310)', async () => {
  const onRequest = jest.fn(async (body: any) => {
    void body;
  });
  const withLanguage = {
    ...row,
    snapshot: {
      ...row.snapshot,
      plan: { ...row.snapshot.plan, ad_language: 'zh-CN' },
    },
  } as MarketingRound;
  wrap(
    <MarketingNextPlan
      row={withLanguage}
      inferredAdLanguage="en-US"
      busy={false}
      onRequest={onRequest}
    />
  );
  expect(screen.queryByLabelText('Advertising language')).toBeNull();
  expect(
    screen.queryByText(messages['marketing-next-plan-ad-language'])
  ).toBeNull();
  fireEvent.click(
    screen.getByRole('button', { name: 'Draft next plan for review' })
  );
  await waitFor(() => expect(onRequest).toHaveBeenCalledTimes(1));
  expect(onRequest.mock.calls[0][0].ad_language).toBe('zh-CN');
});
test('existing creative is privately previewed and attached to an explicit combination without carrying approval', async () => {
  const onAttach = jest.fn(async (body: any) => {
    void body;
  });
  const view = wrap(
    <MarketingReusePicker
      row={row}
      productId="product"
      busy={false}
      onAttach={onAttach}
    />
  );
  fireEvent.click(screen.getByText('Use an existing creative in this round'));
  await screen.findByRole('option', { name: /Creative 1/ });
  expect(gateway.listGenerationAssets).toHaveBeenCalledWith(
    expect.objectContaining({
      productId: 'product',
      executionMode: row.execution_mode,
    })
  );
  const attach = screen.getByRole('button', {
    name: 'Add to this round for fresh review · No charge',
  });
  expect(attach).toBeDisabled();
  fireEvent.change(screen.getByLabelText('Existing product creative'), {
    target: { value: 'old-version' },
  });
  expect(await screen.findByRole('img')).toHaveAttribute(
    'src',
    'blob:exact-history'
  );
  fireEvent.click(attach);
  await waitFor(() => expect(onAttach).toHaveBeenCalled());
  expect(onAttach.mock.calls[0][0]).toEqual({
    asset_version_id: 'old-version',
    combination_id: row.snapshot.plan.combinations[0].id,
  });
  view.unmount();
  expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:exact-history');
});
describe('reuse picker scope (#293)', () => {
  afterEach(() => {
    catalog = messages;
  });
  test.each([
    [
      'en',
      messages,
      'Only creatives generated in this test are listed here.',
      'This test has no other available creatives in this execution mode. Creatives from other tests of the same product cannot be used here yet.',
    ],
    [
      'zh-CN',
      zhMessages,
      '这里只列出本测试生成的素材。',
      '本测试在这种执行方式下没有其他可用素材。同一产品其他测试中的素材，目前不能在这里使用。',
    ],
  ])(
    'in %s, an empty list says only this test’s creatives can be reused, and it is still read by this test’s product',
    async (_, strings, boundary, empty) => {
      catalog = strings;
      // A first round: every creative of this test is already in it, so nothing is left to offer.
      gateway.listGenerationAssets.mockResolvedValue({
        kind: 'ok',
        data: {
          items: row.assets.map((a) => ({ id: a.asset_version_id })),
          next_cursor: null,
        },
      });
      wrap(
        <MarketingReusePicker
          row={row}
          productId="this-tests-product"
          busy={false}
          onAttach={jest.fn()}
        />
      );
      fireEvent.click(screen.getByText(strings['marketing-reuse-title']));
      expect(await screen.findByText(empty)).toBeInTheDocument();
      expect(
        screen.getByText(strings['marketing-reuse-boundary'])
      ).toHaveTextContent(boundary);
      expect(
        within(
          screen.getByLabelText(strings['marketing-reuse-creative'])
        ).getAllByRole('option')
      ).toHaveLength(1);
      expect(gateway.listGenerationAssets).toHaveBeenCalledTimes(1);
      expect(gateway.listGenerationAssets).toHaveBeenCalledWith(
        expect.objectContaining({
          productId: 'this-tests-product',
          executionMode: row.execution_mode,
        })
      );
    }
  );
});
test('optional comparison preserves all five distinct expressions and marks gaps unknown', () => {
  wrap(<MarketingComparisonView plan={row.snapshot.plan} />);
  for (const angle of row.snapshot.plan.angles)
    for (const expression of angle.expressions)
      expect(screen.getByText(expression.name)).toBeInTheDocument();
  expect(screen.getAllByText('Unplanned · unknown').length).toBeGreaterThan(0);
  expect(
    screen.getByText(/Empty pairings are unplanned and unknown/)
  ).toBeInTheDocument();
});

test('an explicitly shared expression occupies one row while same-named distinct expressions stay separate', () => {
  const plan = JSON.parse(JSON.stringify(row.snapshot.plan));
  const shared = {
    ...plan.angles[0].expressions[0],
    fit_reason: 'Fits this second angle for a different reason.',
  };
  plan.angles[1].expressions.push(shared);
  plan.angles[1].expressions[0].name = shared.name;
  plan.combinations.push({
    ...plan.combinations[0],
    id: 'shared-combo',
    angle_id: plan.angles[1].id,
    headline: 'Shared execution for second angle',
  });
  wrap(<MarketingComparisonView plan={plan} />);
  expect(screen.getAllByText(shared.name)).toHaveLength(2);
  const sharedRow = screen
    .getByText('Shared execution for second angle')
    .closest('tr')!;
  expect(sharedRow).toHaveTextContent(plan.combinations[0].headline);
  expect(screen.getAllByRole('row', { hidden: true })).toHaveLength(6);
  expect(screen.getAllByText('Unplanned · unknown').length).toBeGreaterThan(0);
});
