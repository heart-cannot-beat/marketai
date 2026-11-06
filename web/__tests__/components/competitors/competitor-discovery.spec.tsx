/** @jest-environment jsdom */
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { SWRConfig } from 'swr';
import { CompetitorDiscovery } from '@/components/competitors/CompetitorDiscovery';
import { PRODUCT_ID, ok, problem, scan } from './fixtures';

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: { workspaceId: 'ws_alpha', slug: 'alpha', role: 'MEMBER' },
  }),
}));
const gateway = {
  listCompetitorScanDiscoveries: jest.fn(),
  decideCompetitorDiscovery: jest.fn(),
};
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => gateway,
}));

const NORTHPEAK = '100000000000017';
const KESTREL = '100000000011001';

function item(overrides: Record<string, unknown> = {}) {
  return {
    advertiser_page_id: NORTHPEAK,
    advertiser_name: 'Northpeak',
    categories: ['Brand'],
    observation_count: 5,
    landing_domains: ['northpeak.example'],
    keywords: ['CCS1 adapter'],
    relevance: 'relevant',
    reason: 'Sells CCS1 to NACS adapters.',
    language: 'en',
    library_url: `https://www.facebook.com/ads/library/?view_all_page_id=${NORTHPEAK}`,
    call_name: 'discover:2',
    state: 'open',
    decision: null,
    competitor_id: null,
    ...overrides,
  };
}

function listing(overrides: Record<string, unknown> = {}) {
  return {
    scan_id: 'scan_1',
    product_id: PRODUCT_ID,
    execution_mode: 'mock',
    language: 'en',
    status: 'completed',
    reason: null,
    advertisers: 12,
    judged: 12,
    recommended: 2,
    unrelated: 10,
    over_cap: 0,
    items: [
      item(),
      item({
        advertiser_page_id: KESTREL,
        advertiser_name: 'Kestrel Power',
        relevance: 'adjacent',
        landing_domains: ['backerhub.example'],
        call_name: 'discover:1',
      }),
    ],
    ...overrides,
  };
}

const keywordScan = () =>
  scan({
    execution_mode: 'mock',
    params: {
      competitor_ids: ['cmp_acme'],
      keywords: ['CCS1 adapter'],
      country: 'US',
      platform: 'meta',
      ad_status: 'active',
      max_pages_per_competitor: 3,
      keyword_pages: 2,
      max_media_refreshes: 10,
      max_tagged_ads: 60,
      max_discover_advertisers: 30,
      language: 'en',
    },
  });

function renderDiscovery() {
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <CompetitorDiscovery scan={keywordScan()} productId={PRODUCT_ID} />
    </SWRConfig>
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  gateway.listCompetitorScanDiscoveries.mockResolvedValue(ok(listing()));
});

describe('CompetitorDiscovery', () => {
  it('lists each recommendation as AI inference, with its facts, link and no avatar', async () => {
    renderDiscovery();
    const items = await screen.findAllByTestId('discovery-item');
    expect(items.map((li) => li.getAttribute('data-page-id'))).toEqual([
      NORTHPEAK,
      KESTREL,
    ]);
    const northpeak = items[0];
    expect(within(northpeak).getByText('Northpeak')).toBeInTheDocument();
    expect(within(northpeak).getByText('Same category')).toBeInTheDocument();
    expect(within(northpeak).getByText('AI inference')).toBeInTheDocument();
    expect(
      within(northpeak).getByText('5 ads found by keyword')
    ).toBeInTheDocument();
    expect(
      within(northpeak).getByText('northpeak.example')
    ).toBeInTheDocument();
    expect(
      within(northpeak).getByText('Why: Sells CCS1 to NACS adapters.')
    ).toBeInTheDocument();
    expect(
      within(northpeak).getByRole('link', { name: /Open in Meta Ad Library/ })
    ).toHaveAttribute('href', expect.stringContaining(NORTHPEAK));
    expect(within(items[1]).getByText('Adjacent')).toBeInTheDocument();
    expect(screen.getByTestId('discovery-counts')).toHaveTextContent(
      '2 recommended of 12 advertisers judged; 10 judged unrelated are not listed.'
    );
    expect(
      screen.getByTestId('competitor-simulated-badge')
    ).toBeInTheDocument();
    expect(document.querySelector('img')).toBeNull();
  });

  it('accepts through a confirmation card: page ID from the ads, no resolve', async () => {
    gateway.decideCompetitorDiscovery.mockResolvedValue(
      ok({
        discovery: item({ state: 'accepted' }),
        decision: {},
        competitor: {},
      })
    );
    renderDiscovery();
    const [northpeak] = await screen.findAllByTestId('discovery-item');
    fireEvent.click(within(northpeak).getByRole('button', { name: 'Accept' }));
    const card = within(northpeak).getByTestId('discovery-confirm-card');
    expect(
      within(card).getByText('Add Northpeak as a competitor?')
    ).toBeInTheDocument();
    expect(
      within(card).getByText(
        'Its page ID comes from these ads, so it is confirmed at once: no resolve, no charge.'
      )
    ).toBeInTheDocument();
    gateway.listCompetitorScanDiscoveries.mockResolvedValue(
      ok(
        listing({
          items: [item({ state: 'accepted', competitor_id: 'cmp_t' })],
        })
      )
    );
    fireEvent.click(
      within(card).getByRole('button', { name: 'Confirm and add' })
    );
    await waitFor(() =>
      expect(gateway.decideCompetitorDiscovery).toHaveBeenCalledWith(
        expect.objectContaining({
          workspaceSlug: 'alpha',
          scanId: 'scan_1',
          advertiserPageId: NORTHPEAK,
          body: { decision: 'accept' },
        })
      )
    );
    expect(
      await screen.findByText(
        'Added to your competitors. The next scan fetches its page.'
      )
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Accept' })).toBeNull();
  });

  it('rejects with an optional reason; a rejected one can still be accepted', async () => {
    gateway.decideCompetitorDiscovery.mockResolvedValue(
      ok({
        discovery: item({ state: 'rejected' }),
        decision: {},
        competitor: null,
      })
    );
    renderDiscovery();
    const [, kestrel] = await screen.findAllByTestId('discovery-item');
    fireEvent.click(within(kestrel).getByRole('button', { name: 'Reject' }));
    const form = within(kestrel).getByTestId('discovery-reject-form');
    fireEvent.change(within(form).getByRole('textbox'), {
      target: { value: '  sells V2L, not adapters ' },
    });
    gateway.listCompetitorScanDiscoveries.mockResolvedValue(
      ok(
        listing({
          items: [
            item({
              advertiser_page_id: KESTREL,
              advertiser_name: 'Kestrel Power',
              relevance: 'adjacent',
              state: 'rejected',
              decision: {
                decision: 'reject',
                reason: 'sells V2L, not adapters',
              },
            }),
          ],
        })
      )
    );
    fireEvent.click(within(form).getByRole('button', { name: 'Reject' }));
    await waitFor(() =>
      expect(gateway.decideCompetitorDiscovery).toHaveBeenCalledWith(
        expect.objectContaining({
          advertiserPageId: KESTREL,
          body: { decision: 'reject', reason: 'sells V2L, not adapters' },
        })
      )
    );
    const rejected = await screen.findByTestId('discovery-rejected');
    expect(rejected).toHaveTextContent(
      'Rejected (sells V2L, not adapters). Later scans will not recommend it.'
    );
    const row = screen.getByTestId('discovery-item');
    expect(
      within(row).getByRole('button', { name: 'Accept' })
    ).toBeInTheDocument();
    expect(within(row).queryByRole('button', { name: 'Reject' })).toBeNull();
  });

  it('offers an archived competitor again: accepted before, decided anew (#330)', async () => {
    const archived = item({
      state: 'open',
      competitor_id: null,
      decision: {
        decision_id: 'cdd_1',
        scan_id: 'scan_0',
        advertiser_page_id: NORTHPEAK,
        decision: 'accept',
        reason: null,
        competitor_id: 'cmp_archived',
      },
    });
    gateway.listCompetitorScanDiscoveries.mockResolvedValue(
      ok(listing({ items: [archived] }))
    );
    gateway.decideCompetitorDiscovery.mockResolvedValue(
      ok({
        discovery: item({ state: 'accepted', competitor_id: 'cmp_new' }),
        decision: {},
        competitor: {},
      })
    );
    renderDiscovery();
    const row = await screen.findByTestId('discovery-item');
    expect(row).toHaveAttribute('data-state', 'open');
    expect(within(row).getByTestId('discovery-archived')).toHaveTextContent(
      'Accepted before, but it is no longer on your competitor list. Decide again.'
    );
    expect(
      within(row).queryByText(
        'Added to your competitors. The next scan fetches its page.'
      )
    ).toBeNull();
    expect(
      within(row).getByRole('button', { name: 'Reject' })
    ).toBeInTheDocument();
    fireEvent.click(within(row).getByRole('button', { name: 'Accept' }));
    gateway.listCompetitorScanDiscoveries.mockResolvedValue(
      ok(
        listing({
          items: [
            item({
              state: 'accepted',
              competitor_id: 'cmp_new',
              decision: { decision: 'accept', competitor_id: 'cmp_new' },
            }),
          ],
        })
      )
    );
    fireEvent.click(
      within(row).getByRole('button', { name: 'Confirm and add' })
    );
    await waitFor(() =>
      expect(gateway.decideCompetitorDiscovery).toHaveBeenCalledWith(
        expect.objectContaining({
          advertiserPageId: NORTHPEAK,
          body: { decision: 'accept' },
        })
      )
    );
    expect(
      await screen.findByText(
        'Added to your competitors. The next scan fetches its page.'
      )
    ).toBeInTheDocument();
    expect(screen.queryByTestId('discovery-archived')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Accept' })).toBeNull();
  });

  it('shows a refusal in the item, worded from its code', async () => {
    gateway.decideCompetitorDiscovery.mockResolvedValue(
      problem(409, 'competitor_discovery_already_competitor', {
        competitor_id: 'cmp_t',
      })
    );
    renderDiscovery();
    const [northpeak] = await screen.findAllByTestId('discovery-item');
    fireEvent.click(within(northpeak).getByRole('button', { name: 'Accept' }));
    fireEvent.click(
      within(northpeak).getByRole('button', { name: 'Confirm and add' })
    );
    expect(
      await within(northpeak).findByText(
        "This advertiser is already on the product's competitor list."
      )
    ).toBeInTheDocument();
  });

  it('says why nothing is listed, or what is left to judge', async () => {
    gateway.listCompetitorScanDiscoveries.mockResolvedValue(
      ok(
        listing({
          status: 'skipped',
          reason: 'no_candidates',
          advertisers: 0,
          judged: 0,
          recommended: 0,
          unrelated: 0,
          items: [],
        })
      )
    );
    const { unmount } = renderDiscovery();
    expect(
      await screen.findByText(
        'The keywords found no advertiser outside your list.'
      )
    ).toBeInTheDocument();
    unmount();

    gateway.listCompetitorScanDiscoveries.mockResolvedValue(
      ok(
        listing({
          status: 'partial',
          reason: 'credential_rejected',
          over_cap: 3,
        })
      )
    );
    renderDiscovery();
    expect(await screen.findByTestId('discovery-incomplete')).toHaveTextContent(
      'Judging stopped before every advertiser was done. Resume the scan to judge the rest.'
    );
    expect(screen.getByTestId('discovery-counts')).toHaveTextContent(
      "3 more advertisers were not judged (the scan's limit)."
    );
  });
});
