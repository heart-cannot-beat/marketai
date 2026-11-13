/** @jest-environment jsdom */
import { fireEvent, render, waitFor, within } from '@testing-library/react';
import { SWRConfig } from 'swr';
import { MarketingExecution } from '@/components/workspace/MarketingExecution';
import type { MarketingRound } from '@/components/workspace/MarketingExecution';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import fixture from '../../fixtures/marketing-workflow.json';
import en from '../../../locales/en/common.json';
import zhCN from '../../../locales/zh-CN/common.json';

/**
 * #162: two executions of one angle and expression, and a revision of one,
 * share the proposition, expression and headline. The export list and the
 * Exported creative picker name every entry as the package's ads.csv does
 * (`display_name`, else `ad_name`), so a Meta ad ID is bound to the right image.
 */
let mockLocale: { language: string; messages: Record<string, string> } = {
  language: 'en',
  messages: en,
};
jest.mock('next-i18next', () => ({
  useTranslation: () => ({
    i18n: { language: mockLocale.language },
    t: (key: string, options?: Record<string, unknown>) =>
      (mockLocale.messages[key] ?? key).replace(
        /\{\{(\w+)\}\}/g,
        (_: string, name: string) => String(options?.[name] ?? '')
      ),
  }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { workspaceId: 'ws', slug: 'alpha' } }),
}));
const gateway = {
  getMarketingWorkflow: jest.fn(),
  getMarketingAIQCQuote: jest.fn(),
  createGenerationAssetVersionDownloadIntent: jest.fn(),
  bindMarketingDelivery: jest.fn(),
};
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => gateway,
}));
jest.mock('@/lib/domain-client/upload-deps', () => ({
  useBlobTransferClient: () => ({
    downloadBlob: jest.fn(async () => new Blob(['image'])),
  }),
}));

type Entry = components['schemas']['HandoffEntry'];
const project =
  fixture.project as components['schemas']['MarketingProjectDetail'];
const ok = (data: unknown) => ({ kind: 'ok', data });
const SECOND = 'f00000000000000000000002';
const REVISION = 'f00000000000000000000003';

/** The bound fixture round, with a second execution and a v2 of its first entry, as exported in `language`. */
function exportedRound(language: 'en' | 'zh-CN', named = true) {
  const round = JSON.parse(
    JSON.stringify(fixture.bound)
  ) as unknown as MarketingRound;
  const handoff = round.handoff!;
  const execution = language === 'en' ? 'Execution' : '执行';
  const name = (entry: Entry, number: string) =>
    `${entry.proposition} / ${entry.expression_name} · ${execution} ${number}`;
  const ascii = (entry: Entry, suffix: string) =>
    `MA-${entry.entry_id.slice(0, 12)} Fewer doubts / Question and answer ${suffix}`;
  const [first, ...rest] = handoff.entries;
  const alike: [Entry, string, string][] = [
    [first, '#1', '1'],
    [{ ...first, entry_id: SECOND, asset_version_id: 'av_second' }, '#2', '2'],
    [
      { ...first, entry_id: REVISION, asset_version_id: 'av_revision' },
      '#1v2',
      '1 v2',
    ],
  ];
  handoff.language = language;
  handoff.entries = [
    ...alike.map(([entry, suffix, number]) => ({
      ...entry,
      ad_name: ascii(entry, suffix),
      ...(named ? { display_name: name(entry, number) } : {}),
    })),
    ...rest.map((entry) => ({
      ...entry,
      ...(named ? { display_name: name(entry, '1') } : {}),
    })),
  ];
  return round;
}

function show(round: MarketingRound) {
  gateway.getMarketingWorkflow.mockResolvedValue(
    ok({
      project_id: project.id,
      rounds: [],
      current: round,
      next_action: 'import_delivery_data',
    })
  );
  render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <MarketingExecution project={project} />
    </SWRConfig>
  );
}

/** The export list's entry titles, in order. */
async function entryTitles() {
  const handoff = await waitFor(() => {
    const found = document.getElementById('marketing-handoff');
    expect(found).not.toBeNull();
    return found!;
  });
  return within(handoff)
    .getAllByTestId('marketing-export-entry-name')
    .map((title) => title.textContent);
}

/** The Exported creative picker under the export list. */
function picker() {
  return within(document.getElementById('marketing-binding')!).getByLabelText(
    mockLocale.messages['marketing-exported-creative']
  ) as HTMLSelectElement;
}

beforeEach(() => {
  jest.clearAllMocks();
  URL.createObjectURL = jest.fn(() => 'blob:export-names');
  URL.revokeObjectURL = jest.fn();
  Object.defineProperty(globalThis.crypto, 'randomUUID', {
    configurable: true,
    value: () => 'test-action-id',
  });
  gateway.getMarketingAIQCQuote.mockResolvedValue(
    ok({
      available: false,
      unavailable_reason: 'nothing_to_check',
      provider: 'mock',
      model: 'marketing-qc-example-v1',
      execution_mode: 'mock',
      quantity: 0,
      maximum_cost: '0',
      currency: 'USD',
      quote_hash: 'q'.repeat(64),
      asset_version_ids: [],
    })
  );
  gateway.createGenerationAssetVersionDownloadIntent.mockResolvedValue(
    ok({ download_grant: { url: 'http://localhost/private.png' } })
  );
});

describe.each([
  ['en', en, ['Execution 1', 'Execution 2', 'Execution 1 v2']],
  ['zh-CN', zhCN, ['执行 1', '执行 2', '执行 1 v2']],
] as const)('%s', (language, messages, numbers) => {
  beforeEach(() => {
    mockLocale = { language, messages };
  });

  test('export entries and the Exported creative picker show the execution number and version', async () => {
    const round = exportedRound(language);
    const names = round.handoff!.entries.map((e) => e.display_name);
    show(round);

    const titles = await entryTitles();
    expect(titles).toEqual(names);
    const options = within(picker())
      .getAllByRole('option')
      .map((option) => option.textContent);
    expect(options).toEqual(names);

    const alike = round.handoff!.entries.slice(0, 3);
    expect(
      new Set(alike.map((e) => `${e.proposition}·${e.expression_name}`)).size
    ).toBe(1);
    numbers.forEach((number, index) =>
      expect(titles[index]).toMatch(new RegExp(` · ${number}$`))
    );
    expect(new Set(titles.slice(0, 3)).size).toBe(3);
    expect(new Set(options.slice(0, 3)).size).toBe(3);
  });

  test('picking the revision by its name binds the revision’s entry', async () => {
    const round = exportedRound(language);
    gateway.bindMarketingDelivery.mockResolvedValue(
      ok({ id: round.id, data: round })
    );
    show(round);
    await entryTitles();
    const revision = round.handoff!.entries.find(
      (e) => e.entry_id === REVISION
    )!;
    const option = within(picker()).getByRole('option', {
      name: revision.display_name!,
    }) as HTMLOptionElement;
    fireEvent.change(picker(), { target: { value: option.value } });
    const binding = document.getElementById('marketing-binding')!;
    fireEvent.change(
      within(binding).getByLabelText(messages['marketing-meta-ad-id']),
      { target: { value: '2001' } }
    );
    fireEvent.click(
      within(binding).getByRole('button', {
        name: messages['marketing-save-ad-match'],
      })
    );
    await waitFor(() =>
      expect(gateway.bindMarketingDelivery).toHaveBeenCalledWith(
        expect.objectContaining({
          body: expect.objectContaining({
            entry_id: REVISION,
            ad_id: '2001',
          }),
        })
      )
    );
  });

  test('a package without display names falls back to the ad name, as its ads.csv does', async () => {
    const round = exportedRound(language, false);
    const adNames = round.handoff!.entries.map((e) => e.ad_name);
    show(round);

    expect(await entryTitles()).toEqual(adNames);
    expect(
      within(picker())
        .getAllByRole('option')
        .map((option) => option.textContent)
    ).toEqual(adNames);
    expect(adNames.slice(0, 3).map((name) => name.split(' ').pop())).toEqual([
      '#1',
      '#2',
      '#1v2',
    ]);
  });
});
