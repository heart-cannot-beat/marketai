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
import assetMessages from '../../../locales/en/assets.json';
import zhAssetMessages from '../../../locales/zh-CN/assets.json';
// #293 reads the reuse picker's scope copy in both locales; #225 its reasons
// for an ad of the ad ideas, which are the library's (`assets`).
let catalog: Record<string, string> = messages;
let assetCatalog: Record<string, string> = assetMessages;
jest.mock('next-i18next', () => ({
  useTranslation: (namespace?: string) => ({
    t: (key: string, options?: Record<string, unknown>) =>
      ((namespace === 'assets' ? assetCatalog : catalog)[key] ?? key).replace(
        /\{\{(\w+)\}\}/g,
        (_: string, k: string) => String(options?.[k] ?? '')
      ),
  }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { workspaceId: 'ws', slug: 'alpha' } }),
}));
const gateway = {
  getMarketingCapabilities: jest.fn(),
  listMarketingReuseCandidates: jest.fn(),
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
  gateway.listMarketingReuseCandidates.mockResolvedValue({
    kind: 'ok',
    data: {
      items: [
        {
          asset_version_id: 'old-version',
          asset_id: 'old-asset',
          created_at: '2026-10-17',
          provider: 'openai',
          model: 'image-model',
          aspect_ratio: '4:5',
          relation: 'this_test',
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
    <MarketingReusePicker row={row} busy={false} onAttach={onAttach} />
  );
  fireEvent.click(screen.getByText('Use an existing creative in this round'));
  await screen.findByRole('option', { name: /Creative 1/ });
  expect(gateway.listMarketingReuseCandidates).toHaveBeenCalledWith({
    workspaceSlug: 'alpha',
    roundId: row.id,
    cursor: undefined,
    limit: 20,
  });
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
  // A creative of this test needs no cross-test notice.
  expect(screen.queryByRole('note')).toBeNull();
  fireEvent.click(attach);
  await waitFor(() => expect(onAttach).toHaveBeenCalled());
  expect(onAttach.mock.calls[0][0]).toEqual({
    asset_version_id: 'old-version',
    combination_id: row.snapshot.plan.combinations[0].id,
  });
  view.unmount();
  expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:exact-history');
});
describe('reuse picker across the product card (#7)', () => {
  afterEach(() => {
    catalog = messages;
  });
  const candidate = (id: string, relation: string, extra = {}) => ({
    asset_version_id: id,
    asset_id: `${id}-asset`,
    created_at: '2026-11-10',
    provider: 'mock',
    model: 'mock-v1',
    aspect_ratio: '1:1',
    relation,
    ...extra,
  });
  test.each([
    ['en', messages],
    ['zh-CN', zhMessages],
  ])(
    'in %s, creatives are grouped by where they come from and a creative of another test says how it is reviewed',
    async (_, strings) => {
      catalog = strings;
      gateway.listMarketingReuseCandidates.mockResolvedValue({
        kind: 'ok',
        data: {
          items: [
            candidate('mine', 'this_test'),
            candidate('theirs', 'other_test', {
              source_project_id: 'mp_other',
              source_test_name: 'Alpha travel charger',
              facts_relation: 'differs',
            }),
            candidate('card', 'product_card'),
          ],
          next_cursor: null,
        },
      });
      wrap(
        <MarketingReusePicker row={row} busy={false} onAttach={jest.fn()} />
      );
      fireEvent.click(screen.getByText(strings['marketing-reuse-title']));
      const select = screen.getByLabelText(strings['marketing-reuse-creative']);
      await within(select).findByRole('option', {
        name: /Alpha travel charger/,
      });
      const groups = within(select).getAllByRole('group');
      expect(groups.map((g) => g.getAttribute('label'))).toEqual([
        strings['marketing-reuse-group-this_test'],
        strings['marketing-reuse-group-other_test'],
        strings['marketing-reuse-group-product_card'],
      ]);
      expect(
        within(groups[1])
          .getByRole('option')
          .textContent?.startsWith('Alpha travel charger · ')
      ).toBe(true);
      fireEvent.change(select, { target: { value: 'theirs' } });
      const note = await screen.findByRole('note');
      for (const key of [
        'marketing-reuse-other-review',
        'marketing-reuse-other-shared-review',
        'marketing-reuse-facts-differ',
        'marketing-reuse-not-editable',
      ])
        expect(within(note).getByText(strings[key])).toBeInTheDocument();
      // A creative of the product card itself is not revised here either, but has no other test's review.
      fireEvent.change(select, { target: { value: 'card' } });
      expect(
        within(screen.getByRole('note')).queryByText(
          strings['marketing-reuse-other-shared-review']
        )
      ).toBeNull();
      expect(
        within(screen.getByRole('note')).getByText(
          strings['marketing-reuse-not-editable']
        )
      ).toBeInTheDocument();
    }
  );
  test.each([
    [
      'en',
      messages,
      'Creatives from this test, from other tests of the same product card and from the card itself are listed here.',
      'No other creatives of this product are available in this execution mode.',
    ],
    [
      'zh-CN',
      zhMessages,
      '这里列出本测试、同一产品卡片的其他测试，以及产品卡片本身生成的素材。',
      '这个产品在这种执行方式下没有其他可用素材。',
    ],
  ])(
    'in %s, the scope names every source and an empty list says nothing is left for this product',
    async (_, strings, boundary, empty) => {
      catalog = strings;
      gateway.listMarketingReuseCandidates.mockResolvedValue({
        kind: 'ok',
        data: { items: [], next_cursor: null },
      });
      wrap(
        <MarketingReusePicker row={row} busy={false} onAttach={jest.fn()} />
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
      expect(gateway.listMarketingReuseCandidates).toHaveBeenCalledTimes(1);
    }
  );
});
describe('an ad of the ad ideas that is not checked yet (#225)', () => {
  afterEach(() => {
    catalog = messages;
    assetCatalog = assetMessages;
  });
  const ad = (id: string, extra = {}) => ({
    asset_version_id: id,
    asset_id: `${id}-asset`,
    created_at: '2026-11-18',
    provider: 'mock',
    model: 'mock-v1',
    aspect_ratio: '1:1',
    relation: 'product_card',
    reuse_block: null,
    ...extra,
  });
  test.each([
    ['en', messages, assetMessages, 'Ad draft', 'Needs remaking'],
    ['zh-CN', zhMessages, zhAssetMessages, '广告草稿', '需要重出'],
  ])(
    'in %s, is listed with why it cannot be chosen, and only a checked one can be added',
    async (_, strings, assetStrings, draftWord, redoWord) => {
      catalog = strings;
      assetCatalog = assetStrings;
      gateway.listMarketingReuseCandidates.mockResolvedValue({
        kind: 'ok',
        data: {
          items: [
            ad('draft', {
              reuse_block: {
                status: 'draft',
                reasons: [
                  { code: 'facts_unverified', fact_ids: ['f1'], finding_ids: [] },
                  { code: 'check_missing', fact_ids: [], finding_ids: [] },
                ],
              },
            }),
            ad('redo', {
              reuse_block: {
                status: 'needs_redo',
                reasons: [
                  { code: 'facts_changed', fact_ids: ['f2'], finding_ids: [] },
                  { code: 'a_reason_this_page_has_no_words_for', fact_ids: [], finding_ids: [] },
                ],
              },
            }),
            ad('checked'),
          ],
          next_cursor: null,
        },
      });
      const onAttach = jest.fn(async () => undefined);
      wrap(<MarketingReusePicker row={row} busy={false} onAttach={onAttach} />);
      fireEvent.click(screen.getByText(strings['marketing-reuse-title']));
      const select = screen.getByLabelText(strings['marketing-reuse-creative']);
      const draft = await within(select).findByRole('option', {
        name: new RegExp(draftWord),
      });
      const redo = within(select).getByRole('option', {
        name: new RegExp(redoWord),
      });
      expect(draft).toBeDisabled();
      expect(redo).toBeDisabled();
      const sep = assetStrings['export-block-reasons-separator'];
      expect(draft.textContent).toContain(
        [
          assetStrings['export-block-reason-facts_unverified'],
          assetStrings['export-block-reason-check_missing'],
        ].join(sep)
      );
      // A reason without words reads as "not checked yet", never as its code.
      expect(redo.textContent).toContain(
        assetStrings['export-block-reason-other']
      );
      expect(redo.textContent).not.toContain('a_reason_this_page');
      expect(screen.getByRole('note')).toHaveTextContent(
        strings['marketing-reuse-blocked-note'].replace('{{count}}', '2')
      );
      const attach = screen.getByRole('button', {
        name: strings['marketing-reuse-attach'],
      });
      // Even set by hand, a blocked ad is never added.
      fireEvent.change(select, { target: { value: 'draft' } });
      await waitFor(() =>
        expect(
          gateway.createGenerationAssetVersionDownloadIntent
        ).toHaveBeenCalled()
      );
      expect(attach).toBeDisabled();
      fireEvent.change(select, { target: { value: 'checked' } });
      expect(await screen.findByRole('img')).toBeInTheDocument();
      await waitFor(() => expect(attach).toBeEnabled());
      fireEvent.click(attach);
      await waitFor(() => expect(onAttach).toHaveBeenCalledTimes(1));
      expect(onAttach.mock.calls[0]).toEqual([
        {
          asset_version_id: 'checked',
          combination_id: row.snapshot.plan.combinations[0].id,
        },
      ]);
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
