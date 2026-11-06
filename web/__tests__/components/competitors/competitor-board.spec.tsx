/** @jest-environment jsdom */
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { I18nextProvider, getI18n } from 'react-i18next';
import useSWR, { SWRConfig } from 'swr';
import type { ReactElement } from 'react';
import {
  CompetitorBoard,
  adQueryOf,
} from '@/components/competitors/CompetitorBoard';
import { libraryUrlOf } from '@/components/competitors/AdThumbnail';
import competitorsZh from '../../../locales/zh-CN/competitors.json';
import commonZh from '../../../locales/zh-CN/common.json';
import {
  PRODUCT_ID,
  accepted,
  ad,
  ok,
  problem,
  scan,
  snapshotSummary,
} from './fixtures';

let query: Record<string, string> = {};
jest.mock('next/router', () => ({
  useRouter: () => ({
    query,
    replace: jest.fn(),
    pathname: '/w/[slug]/experiments',
  }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: { workspaceId: 'ws_alpha', slug: 'alpha', role: 'MEMBER' },
  }),
}));
const gateway = {
  listCompetitorScanAds: jest.fn(),
  flipCompetitorAdRelevance: jest.fn(),
  listCompetitorAdAssessments: jest.fn(),
  confirmCompetitorAdTags: jest.fn(),
  listCompetitorAdTags: jest.fn(),
  refreezeCompetitorScan: jest.fn(),
  getCompetitorScanSummary: jest.fn(),
  quoteCompetitorScanRecluster: jest.fn(),
  reclusterCompetitorScan: jest.fn(),
  getProductCompetitorSettings: jest.fn(),
  updateProductCompetitorSettings: jest.fn(),
};
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => gateway,
}));
const blobClient = { downloadBlob: jest.fn() };
jest.mock('@/lib/domain-client/upload-deps', () => ({
  useBlobTransferClient: () => blobClient,
}));

const onScanChanged = jest.fn().mockResolvedValue(undefined);
const finished = () =>
  scan({
    execution_mode: 'mock',
    snapshot_id: 'snap_1',
    targets: [
      {
        origin: 'page:cmp_acme',
        kind: 'page',
        competitor_id: 'cmp_acme',
        role: 'competitor',
        page_id: '100000000000010',
        page_name: 'Acme Charge',
        status: 'completed',
        pages_planned: 3,
        pages_fetched: 3,
        ads_seen: 2,
      },
      {
        origin: 'page:cmp_self',
        kind: 'page',
        competitor_id: 'cmp_self',
        role: 'self',
        page_id: '100000000000016',
        page_name: 'Brightvolt',
        status: 'completed',
        pages_planned: 1,
        pages_fetched: 1,
        ads_seen: 1,
      },
    ],
  });

/** The marketing project's projection, as the workspace loaded it. */
function Project({ stale }: { stale?: boolean }) {
  useSWR(['marketing-project', 'ws_alpha', 'mp_1'], async () =>
    stale === undefined
      ? { id: 'mp_1' }
      : { id: 'mp_1', competitor_snapshot_stale: stale }
  );
  return null;
}

function renderBoard(
  current = finished(),
  options: { stale?: boolean; ui?: (node: ReactElement) => ReactElement } = {}
) {
  const node = (
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <Project stale={options.stale} />
      <CompetitorBoard
        scan={current}
        productId={PRODUCT_ID}
        onScanChanged={onScanChanged}
      />
    </SWRConfig>
  );
  return render(options.ui ? options.ui(node) : node);
}

const competitorAd = ad();
const ruleAd = ad({
  ad_id: 'ad_2',
  ad_archive_id: '2207',
  advertiser_name: 'Big EV Dealer',
  competitor_ids: [],
  role: null,
  days_running: 12,
  display_format: 'VIDEO',
  media_status: 'unavailable',
  thumbnail: null,
  library_url: null,
  cards: [{ index: 0, title: 'Visit our lot', video_poster: null }],
  relevance: {
    relevance: 'unrelated',
    source: 'rule',
    revision: 1,
    rule: 'dealer',
  },
});
const selfAd = ad({
  ad_id: 'ad_self',
  ad_archive_id: '3300',
  advertiser_name: 'Brightvolt',
  competitor_ids: ['cmp_self'],
  role: 'self',
  relevance: { relevance: 'relevant', source: 'user', revision: 2 },
});

beforeEach(() => {
  jest.clearAllMocks();
  query = { test: 'mp_1' };
  global.URL.createObjectURL = jest.fn(() => 'blob:thumb');
  global.URL.revokeObjectURL = jest.fn();
  blobClient.downloadBlob.mockResolvedValue(new Blob(['x']));
  gateway.listCompetitorScanAds.mockImplementation(async ({ role }) =>
    role === 'self'
      ? ok({ items: [selfAd], total: 1, next_cursor: null })
      : ok({
          items: [competitorAd, ruleAd, selfAd],
          total: 3,
          next_cursor: null,
        })
  );
  gateway.getCompetitorScanSummary.mockResolvedValue(ok(snapshotSummary()));
  gateway.getProductCompetitorSettings.mockResolvedValue(
    ok({ product_id: PRODUCT_ID, competitor_snapshot_in_drafts: true })
  );
});

const card = async (adId: string) =>
  (await screen.findAllByTestId('competitor-ad-card')).find(
    (node) => node.getAttribute('data-ad-id') === adId
  )!;

describe('observation cards', () => {
  it('shows the signed thumbnail with its source and the original link under it', async () => {
    renderBoard();
    const acme = await card('ad_1');
    const image = await within(acme).findByTestId('competitor-ad-thumbnail');
    expect(image).toHaveAttribute('src', 'blob:thumb');
    expect(image).toHaveAttribute('draggable', 'false');
    expect(blobClient.downloadBlob).toHaveBeenCalledWith(
      expect.objectContaining({
        url: 'https://storage.example/thumb/ad_1.jpg?sig=1',
        content_disposition: 'inline',
      }),
      expect.anything()
    );
    const source = within(acme).getByTestId('competitor-ad-source');
    expect(source).toHaveTextContent('Source: Meta Ad Library');
    expect(
      within(source).getByRole('link', { name: 'View the original ad' })
    ).toHaveAttribute('href', 'https://www.facebook.com/ads/library/?id=1203');
    expect(within(acme).getByText('Running 64 days · Image')).toBeVisible();
    expect(
      within(acme).getByText('One adapter for every charger')
    ).toBeVisible();
    expect(within(acme).getByText('Button: Shop now')).toBeVisible();
    // No download control anywhere on the board.
    expect(document.querySelector('a[download]')).toBeNull();
  });

  it('keeps the source and a link when the archive has no image, and marks video', async () => {
    renderBoard();
    const dealer = await card('ad_2');
    expect(
      within(dealer).getByTestId('competitor-ad-thumbnail-placeholder')
    ).toHaveTextContent('Image unavailable');
    expect(
      within(dealer).getByTestId('competitor-ad-video-badge')
    ).toHaveTextContent('Video');
    expect(
      within(dealer).getByRole('link', { name: 'View the original ad' })
    ).toHaveAttribute('href', 'https://www.facebook.com/ads/library/?id=2207');
    expect(libraryUrlOf({ library_url: null, ad_archive_id: null })).toBeNull();
  });

  it('labels only the AI step as inference; a rule and a member are not', async () => {
    renderBoard();
    const sourceOf = async (adId: string) =>
      within(await card(adId)).getByTestId('competitor-judgment-source');
    expect(await sourceOf('ad_1')).toHaveTextContent('AI inference');
    expect(await sourceOf('ad_2')).toHaveTextContent('Rule · dealer');
    expect(await sourceOf('ad_2')).not.toHaveTextContent('AI inference');
    expect(await sourceOf('ad_self')).toHaveTextContent("Member's judgment");
  });

  it('flips relevance with an Idempotency-Key and refreshes the lists, summary and scan', async () => {
    gateway.flipCompetitorAdRelevance.mockResolvedValue(
      ok({ assessment_id: 'as_2', relevance: 'relevant', source: 'user' })
    );
    renderBoard();
    const dealer = await card('ad_2');
    const calls = gateway.listCompetitorScanAds.mock.calls.length;
    fireEvent.click(within(dealer).getByTestId('competitor-ad-flip-relevant'));
    await waitFor(() =>
      expect(gateway.flipCompetitorAdRelevance).toHaveBeenCalledWith(
        expect.objectContaining({
          scanId: 'scan_1',
          adId: 'ad_2',
          body: { relevance: 'relevant' },
          actionInstanceId: expect.any(String),
        })
      )
    );
    expect(
      await within(dealer).findByText(
        'Changed. Refreeze the snapshot to use it in the summary.'
      )
    ).toBeVisible();
    expect(gateway.listCompetitorScanAds.mock.calls.length).toBeGreaterThan(
      calls
    );
    expect(gateway.getCompetitorScanSummary).toHaveBeenCalledTimes(2);
    expect(onScanChanged).toHaveBeenCalledWith();
    // The value in effect is not offered as a flip.
    expect(
      within(await card('ad_1')).getByTestId('competitor-ad-flip-relevant')
    ).toBeDisabled();
  });

  it('shows the relevance history with who judged each revision', async () => {
    gateway.listCompetitorAdAssessments.mockResolvedValue(
      ok({
        scan_id: 'scan_1',
        ad_id: 'ad_self',
        kind: 'relevance',
        items: [
          {
            assessment_id: 'as_1',
            ad_id: 'ad_self',
            scan_id: 'scan_1',
            kind: 'relevance',
            revision: 1,
            source: 'ai',
            relevance: 'unrelated',
            reason: 'Not the same product.',
            created_at: '2026-10-30T09:04:00Z',
          },
          {
            assessment_id: 'as_2',
            ad_id: 'ad_self',
            scan_id: 'scan_1',
            kind: 'relevance',
            revision: 2,
            source: 'user',
            relevance: 'relevant',
            created_at: '2026-10-30T09:10:00Z',
          },
        ],
      })
    );
    renderBoard();
    const own = await card('ad_self');
    fireEvent.click(within(own).getByText('History (2 revisions)'));
    const entries = await within(own).findAllByTestId(
      'competitor-ad-history-entry'
    );
    expect(entries.map((e) => e.getAttribute('data-source'))).toEqual([
      'user',
      'ai',
    ]);
    expect(entries[0]).toHaveTextContent("Revision 2RelevantMember's judgment");
    expect(entries[1]).toHaveTextContent('AI inference');
    expect(entries[1]).toHaveTextContent('Not the same product.');
  });

  it('confirms tags against the scan vocabulary, keeping AI suggestion and confirmation apart', async () => {
    const tagged = ad({
      tags: {
        source: 'ai',
        revision: 1,
        ai_suggestion: true,
        factors: [{ factor_id: 'angle', value_id: 'v1', confidence: 0.7 }],
        summary: 'Talks about charging access.',
      },
    });
    gateway.listCompetitorScanAds.mockImplementation(async ({ role }) =>
      ok({
        items: role === 'self' ? [] : [tagged],
        total: role === 'self' ? 0 : 1,
      })
    );
    gateway.listCompetitorAdTags.mockResolvedValue(
      ok({
        scan_id: 'scan_1',
        ad_id: 'ad_1',
        kind: 'tags',
        vocabulary: {
          factors: [
            {
              factor_id: 'angle',
              label: 'Angle',
              values: [
                { value_id: 'v1', label: 'Charging access' },
                { value_id: 'v2', label: 'Safety' },
              ],
            },
          ],
        },
        items: [
          {
            assessment_id: 'tg_1',
            ad_id: 'ad_1',
            scan_id: 'scan_1',
            kind: 'tags',
            revision: 1,
            source: 'ai',
            ai_suggestion: true,
            factors: [
              {
                factor_id: 'angle',
                factor_label: 'Angle',
                value_id: 'v1',
                value_label: 'Charging access',
              },
            ],
          },
        ],
        current: {
          assessment_id: 'tg_1',
          revision: 1,
          source: 'ai',
          ai_suggestion: true,
          factors: [{ factor_id: 'angle', value_id: 'v1' }],
        },
      })
    );
    gateway.confirmCompetitorAdTags.mockResolvedValue(
      ok({ assessment_id: 'tg_2', source: 'user', ai_suggestion: false })
    );
    renderBoard();
    const acme = await card('ad_1');
    const tags = within(acme).getByTestId('competitor-ad-tags');
    expect(tags).toHaveTextContent('AI suggestion');
    expect(
      await within(tags).findByText('Angle: Charging access')
    ).toBeVisible();

    fireEvent.click(within(tags).getByText('Review the suggestion'));
    const editor = await within(tags).findByTestId('competitor-tag-editor');
    expect(
      within(editor).getByTestId('competitor-tag-history')
    ).toHaveTextContent('Revision 1AI suggestion');
    fireEvent.change(within(editor).getByLabelText('Angle'), {
      target: { value: 'v2' },
    });
    fireEvent.click(within(editor).getByTestId('competitor-tag-confirm'));
    await waitFor(() =>
      expect(gateway.confirmCompetitorAdTags).toHaveBeenCalledWith(
        expect.objectContaining({
          adId: 'ad_1',
          body: { tags: [{ factor_id: 'angle', value_id: 'v2' }] },
        })
      )
    );
    await waitFor(() =>
      expect(within(tags).queryByTestId('competitor-tag-editor')).toBeNull()
    );
  });
});

describe('filters', () => {
  it('maps every filter to the list query, the own page on its own', () => {
    const base = {
      competitor: 'all',
      relevance: '',
      minDays: null,
      format: '',
    } as const;
    expect(adQueryOf({ ...base }, 'main')).toEqual({});
    expect(adQueryOf({ ...base }, 'self')).toEqual({ role: 'self' });
    expect(
      adQueryOf(
        {
          competitor: 'cmp_acme',
          relevance: 'unassessed',
          minDays: 90,
          format: 'VIDEO',
        },
        'main'
      )
    ).toEqual({
      competitorId: 'cmp_acme',
      relevance: 'unassessed',
      minDaysRunning: 90,
      displayFormat: 'VIDEO',
    });
    expect(adQueryOf({ ...base, competitor: 'self' }, 'main')).toEqual({
      role: 'self',
    });
  });

  it('groups the own page apart and passes filters to the list', async () => {
    renderBoard();
    const main = await screen.findByTestId('competitor-board-main');
    const own = await screen.findByTestId('competitor-board-self');
    expect(within(main).queryByText('Brightvolt')).toBeNull();
    expect(within(own).getByText('Brightvolt')).toBeVisible();
    expect(screen.getByTestId('competitor-board-main-count')).toHaveTextContent(
      '2 of 2 shown'
    );

    expect(
      within(screen.getByTestId('competitor-board-filter-days'))
        .getAllByRole('option')
        .map((o) => o.textContent)
    ).toEqual(['All', '30+ days', '60+ days', '90+ days']);
    fireEvent.change(screen.getByTestId('competitor-board-filter-relevance'), {
      target: { value: 'relevant' },
    });
    fireEvent.change(screen.getByTestId('competitor-board-filter-days'), {
      target: { value: '60' },
    });
    fireEvent.change(screen.getByTestId('competitor-board-filter-format'), {
      target: { value: 'IMAGE' },
    });
    await waitFor(() =>
      expect(gateway.listCompetitorScanAds).toHaveBeenCalledWith(
        expect.objectContaining({
          relevance: 'relevant',
          minDaysRunning: 60,
          displayFormat: 'IMAGE',
        })
      )
    );
    const competitor = screen.getByTestId('competitor-board-filter-competitor');
    expect(
      within(competitor)
        .getAllByRole('option')
        .map((o) => o.textContent)
    ).toEqual(['All', 'Acme Charge', 'Your own page']);
    fireEvent.change(competitor, { target: { value: 'cmp_acme' } });
    await waitFor(() =>
      expect(gateway.listCompetitorScanAds).toHaveBeenLastCalledWith(
        expect.objectContaining({ competitorId: 'cmp_acme' })
      )
    );
    expect(screen.queryByTestId('competitor-board-self')).toBeNull();
  });
});

describe('summary', () => {
  it('shows clusters with advertisers, ads and longest run, and the AI conclusion as inference', async () => {
    gateway.getCompetitorScanSummary.mockResolvedValue(
      ok(
        snapshotSummary({
          unclustered: [
            {
              ad_id: 'ad_fsd',
              ad_archive_id: '9',
              advertiser_name: 'Halcyon Motors',
              days_running: 120,
              title: 'Full Self-Driving',
              relevance: 'relevant',
              relevance_source: 'user',
              ai_inferred: false,
            },
          ],
        })
      )
    );
    renderBoard();
    const summary = await screen.findByTestId('competitor-summary');
    const cluster = await within(summary).findByTestId(
      'competitor-summary-cluster'
    );
    expect(cluster).toHaveTextContent('One adapter covers every charger');
    expect(cluster).toHaveTextContent(
      'Advertisers: 1 · Ads: 1 · longest running 64 days'
    );
    expect(
      within(cluster).getByTestId('competitor-ai-inference')
    ).toHaveTextContent('AI inference');
    const conclusion = within(summary).getByTestId(
      'competitor-summary-conclusion'
    );
    expect(conclusion).toHaveTextContent('AI inference');
    expect(conclusion).toHaveTextContent(
      'Most competitors promise one adapter for every charger.'
    );
    const unclustered = within(summary).getByTestId(
      'competitor-summary-unclustered'
    );
    expect(unclustered).toHaveAttribute('data-count', '1');
    expect(unclustered).toHaveTextContent(
      'Halcyon Motors · Running 120 days · Full Self-Driving'
    );
    expect(unclustered).not.toHaveTextContent('AI inference');
    expect(
      within(summary).getByTestId('competitor-summary-count-relevant')
    ).toHaveTextContent('1');
  });

  it('refreezes for free and explains a 409 when there is nothing to freeze', async () => {
    gateway.refreezeCompetitorScan
      .mockResolvedValueOnce(
        problem(409, 'competitor_scan_nothing_to_freeze', {
          reason: 'no_judgments',
        })
      )
      .mockResolvedValueOnce(ok({ snapshot_id: 'snap_2', created: true }))
      .mockResolvedValueOnce(ok({ snapshot_id: 'snap_2', created: false }));
    renderBoard();
    const refreeze = await screen.findByTestId('competitor-refreeze');
    expect(refreeze).toHaveTextContent('Refreeze (free)');
    fireEvent.click(refreeze);
    expect(
      await screen.findByTestId('competitor-refreeze-error')
    ).toHaveTextContent(
      'This scan has no judged observation yet, so there is nothing to freeze.'
    );
    fireEvent.click(refreeze);
    expect(await screen.findByText('A new snapshot was frozen.')).toBeVisible();
    expect(onScanChanged).toHaveBeenCalled();
    fireEvent.click(refreeze);
    expect(
      await screen.findByText('Nothing changed; the snapshot stays the same.')
    ).toBeVisible();
  });

  it('quotes a recluster before it runs, and waits while one is running', async () => {
    gateway.quoteCompetitorScanRecluster.mockResolvedValue(
      ok({
        kind: 'recluster',
        scan_id: 'scan_1',
        product_id: PRODUCT_ID,
        execution_mode: 'mock',
        relevant_count: 3,
        llm: {
          provider: 'openai',
          model: 'gpt-4.1-mini',
          calls: 1,
          max_usd: '0',
          simulated_usd: '0.0412',
          price_rule_version: 'v1',
        },
        max_usd: '0',
        simulated_usd: '0.0412',
      })
    );
    const running = {
      ...finished(),
      reclusters: [
        {
          operation_id: 'op_r',
          job_id: 'job_r',
          status: 'queued',
          max_usd: '0',
        },
      ],
    };
    gateway.reclusterCompetitorScan.mockResolvedValue(accepted(running));
    const view = renderBoard();
    fireEvent.click(await screen.findByTestId('competitor-recluster'));
    const quote = await screen.findByTestId('competitor-recluster-quote');
    expect(gateway.reclusterCompetitorScan).not.toHaveBeenCalled();
    expect(
      await within(quote).findByTestId('competitor-recluster-quote-total')
    ).toHaveTextContent('no charge (estimate $0.0412)');
    expect(quote).toHaveTextContent(
      'Up to 1 calls · gpt-4.1-mini · 3 relevant ads'
    );
    fireEvent.click(within(quote).getByTestId('competitor-recluster-confirm'));
    await waitFor(() =>
      expect(gateway.reclusterCompetitorScan).toHaveBeenCalledWith(
        expect.objectContaining({ scanId: 'scan_1' })
      )
    );
    expect(onScanChanged).toHaveBeenCalledWith(running);

    view.rerender(
      <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
        <CompetitorBoard
          scan={running as never}
          productId={PRODUCT_ID}
          onScanChanged={onScanChanged}
        />
      </SWRConfig>
    );
    expect(
      await screen.findByTestId('competitor-recluster-status')
    ).toHaveTextContent('Recluster queued…');
    expect(screen.getByTestId('competitor-refreeze')).toBeDisabled();
    expect(screen.getByTestId('competitor-recluster')).toBeDisabled();
  });

  it('shows the first five ads of a long list until asked for all', async () => {
    const adjacent = Array.from({ length: 7 }, (_, index) => ({
      ad_id: `ad_adj_${index}`,
      advertiser_name: `Adjacent ${index}`,
      relevance: 'adjacent',
      relevance_source: 'ai',
      ai_inferred: true,
    }));
    gateway.getCompetitorScanSummary.mockResolvedValue(
      ok(snapshotSummary({ adjacent }))
    );
    renderBoard();
    const section = await screen.findByTestId('competitor-summary-adjacent');
    expect(section).toHaveAttribute('data-count', '7');
    expect(
      within(section).getAllByTestId('competitor-summary-ad')
    ).toHaveLength(5);
    fireEvent.click(within(section).getByText('Show all 7'));
    expect(
      within(section).getAllByTestId('competitor-summary-ad')
    ).toHaveLength(7);
    fireEvent.click(within(section).getByText('Show fewer'));
    expect(
      within(section).getAllByTestId('competitor-summary-ad')
    ).toHaveLength(5);
  });

  it('says the summary and the setting are loading, never "no snapshot", while they load', async () => {
    gateway.getCompetitorScanSummary.mockReturnValue(new Promise(() => {}));
    gateway.getProductCompetitorSettings.mockReturnValue(new Promise(() => {}));
    renderBoard();
    expect(await screen.findByText('Loading the summary…')).toBeVisible();
    expect(screen.getByText('Reading the setting…')).toBeVisible();
    expect(screen.queryByTestId('competitor-summary-empty')).toBeNull();
    expect(screen.getByRole('switch')).toBeDisabled();
  });

  it('says there is no snapshot yet and does not ask for a summary', async () => {
    renderBoard(scan({ snapshot_id: null }));
    expect(
      await screen.findByTestId('competitor-summary-empty')
    ).toHaveTextContent('No snapshot yet.');
    expect(gateway.getCompetitorScanSummary).not.toHaveBeenCalled();
  });

  it('reads and switches "Use the competitor snapshot when drafting"', async () => {
    gateway.updateProductCompetitorSettings.mockResolvedValue(
      ok({ product_id: PRODUCT_ID, competitor_snapshot_in_drafts: false })
    );
    renderBoard();
    const toggle = await screen.findByRole('switch', {
      name: 'Use the competitor snapshot when drafting',
    });
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'true'));
    fireEvent.click(toggle);
    await waitFor(() =>
      expect(gateway.updateProductCompetitorSettings).toHaveBeenCalledWith(
        expect.objectContaining({
          productId: PRODUCT_ID,
          body: { competitor_snapshot_in_drafts: false },
        })
      )
    );
    await waitFor(() =>
      expect(toggle).toHaveAttribute('aria-checked', 'false')
    );
    expect(
      screen.getByText(
        'Off: a new draft cites no competitor snapshot. Plans drafted before keep theirs.'
      )
    ).toBeVisible();
  });

  it.each([
    [true, true],
    [false, false],
    [undefined, false],
  ])(
    'shows the new-snapshot hint only when the projection says stale=%s',
    async (stale, shown) => {
      renderBoard(finished(), { stale });
      await screen.findByTestId('competitor-summary-cluster');
      if (shown)
        expect(
          await screen.findByTestId('competitor-snapshot-stale')
        ).toHaveTextContent(
          'A newer competitor snapshot is available; redraft to use it.'
        );
      else expect(screen.queryByTestId('competitor-snapshot-stale')).toBeNull();
    }
  );
});

describe('in zh-CN', () => {
  it('leaves no English copy on the board and summary', async () => {
    const i18n = getI18n().cloneInstance({
      lng: 'zh-CN',
      fallbackLng: false,
      forkResourceStore: true,
    });
    i18n.addResourceBundle('zh-CN', 'competitors', competitorsZh);
    i18n.addResourceBundle('zh-CN', 'common', commonZh);
    void i18n.changeLanguage('zh-CN');
    renderBoard(finished(), {
      stale: true,
      ui: (node) => <I18nextProvider i18n={i18n}>{node}</I18nextProvider>,
    });
    await screen.findByTestId('competitor-summary-cluster');
    await screen.findByTestId('competitor-snapshot-stale');
    await screen.findAllByTestId('competitor-ad-card');
    await waitFor(() =>
      expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'true')
    );
    const sources = screen.getAllByTestId('competitor-ad-source');
    expect(sources).toHaveLength(3);
    for (const source of sources)
      expect(source).toHaveTextContent('来源：Meta Ad Library · 查看原始广告');
    expect(screen.getByText('有新的竞品快照，重新起草可采用。')).toBeVisible();
    expect(screen.getByText('起草时使用竞品快照')).toBeVisible();
    expect(screen.getAllByText('AI 推测').length).toBeGreaterThan(0);
    expect(screen.getByText('规则判定 · 经销商 / 车行')).toBeVisible();
    // Everything left is data (advertisers, ad copy, claims) or brand names.
    const copy = (document.body.textContent ?? '')
      .replace(/Meta Ad Library|Acme Charge|Brightvolt|Big EV Dealer/g, '')
      .replace(/One adapter (for|covers) every charger|Visit our lot/g, '')
      .replace(
        /Shop now|Sells the same adapter\.|Talks about charging access\./g,
        ''
      )
      .replace(/Most competitors promise one adapter for every charger\./g, '')
      .replace(/Angle|Charging access|Safety/g, '');
    expect(copy).not.toMatch(/[A-Za-z]{3,}/);
  });
});
