/** @jest-environment jsdom */
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { I18nextProvider, getI18n } from 'react-i18next';
import { SWRConfig } from 'swr';
import type { ReactElement } from 'react';
import { CompetitorsArea } from '@/components/competitors/CompetitorsArea';
import { inputKindOf } from '@/components/competitors/AddCompetitorForm';
import { ScanProgress } from '@/components/competitors/ScanProgress';
import { keywordsOf } from '@/components/competitors/ScanSetup';
import { countryFromMarket } from '@/components/competitors/market';
import competitorsZh from '../../../locales/zh-CN/competitors.json';
import commonZh from '../../../locales/zh-CN/common.json';
import {
  PRODUCT_ID,
  accepted,
  candidate,
  competitor,
  confirmed,
  created,
  ok,
  problem,
  resolution,
  resolveQuote,
  scan,
  scanQuote,
  snapshotSummary,
} from './fixtures';

let query: Record<string, string> = {};
const replace = jest.fn().mockImplementation(async ({ query: next }) => {
  query = next;
  return true;
});
jest.mock('next/router', () => ({
  useRouter: () => ({ query, replace, pathname: '/w/[slug]/experiments' }),
}));
let mockRole = 'OWNER';
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: { workspaceId: 'ws_alpha', slug: 'alpha', role: mockRole },
  }),
}));
const gateway = {
  listCompetitors: jest.fn(),
  createCompetitor: jest.fn(),
  quoteCompetitorResolve: jest.fn(),
  resolveCompetitor: jest.fn(),
  getCompetitorResolution: jest.fn(),
  confirmCompetitor: jest.fn(),
  archiveCompetitor: jest.fn(),
  listCompetitorScans: jest.fn(),
  getCompetitorScan: jest.fn(),
  previewCompetitorScan: jest.fn(),
  createCompetitorScan: jest.fn(),
  quoteCompetitorScanResume: jest.fn(),
  resumeCompetitorScan: jest.fn(),
  reconcileCompetitorScan: jest.fn(),
  quoteCompetitorScanRecluster: jest.fn(),
  reclusterCompetitorScan: jest.fn(),
  // The board under the scan (C-32 #275); its own suite covers it.
  listCompetitorScanAds: jest.fn(),
  getCompetitorScanSummary: jest.fn(),
  getProductCompetitorSettings: jest.fn(),
};
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => gateway,
}));
const blobClient = { downloadBlob: jest.fn() };
jest.mock('@/lib/domain-client/upload-deps', () => ({
  useBlobTransferClient: () => blobClient,
}));

function renderArea(
  planMarket: string | null = 'US',
  ui?: (node: ReactElement) => ReactElement
) {
  const node = (
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <CompetitorsArea
        productId={PRODUCT_ID}
        planMarket={planMarket}
        defaultOpen
      />
    </SWRConfig>
  );
  return render(ui ? ui(node) : node);
}

/**
 * S1 (C-50 #277): the scan's own clustering failed (#345); a paid recluster
 * failed too, then two completed as clusters revisions 1 and 2 (#353).
 */
function reclusteredScan(overrides: Parameters<typeof scan>[0] = {}) {
  return scan({
    steps: [
      { step: 'fetch', status: 'completed' },
      { step: 'media', status: 'completed' },
      { step: 'filter', status: 'completed' },
      { step: 'tag', status: 'completed' },
      {
        step: 'cluster',
        status: 'failed',
        reason: 'competitor_clusters_items_mismatch',
      },
      { step: 'freeze', status: 'completed' },
    ],
    reclusters: [
      {
        operation_id: 'op_r1',
        job_id: 'job_r1',
        status: 'failed',
        max_usd: '0.082',
        cluster_revision: null,
        failure: { code: 'competitor_clusters_items_mismatch' },
      },
      {
        operation_id: 'op_r2',
        job_id: 'job_r2',
        status: 'completed',
        max_usd: '0.082',
        cluster_revision: 1,
      },
      {
        operation_id: 'op_r3',
        job_id: 'job_r3',
        status: 'completed',
        max_usd: '0.082',
        cluster_revision: 2,
      },
    ],
    ...overrides,
  });
}

beforeEach(() => {
  jest.clearAllMocks();
  query = {};
  mockRole = 'OWNER';
  global.URL.createObjectURL = jest.fn(() => 'blob:avatar');
  global.URL.revokeObjectURL = jest.fn();
  blobClient.downloadBlob.mockRejectedValue(new Error('Download failed: 403'));
  gateway.listCompetitors.mockResolvedValue(ok({ items: [] }));
  gateway.listCompetitorScans.mockResolvedValue(ok({ items: [] }));
  gateway.listCompetitorScanAds.mockResolvedValue(ok({ items: [], total: 0 }));
  gateway.getCompetitorScanSummary.mockResolvedValue(
    problem(404, 'competitor_snapshot_not_found')
  );
  gateway.getProductCompetitorSettings.mockResolvedValue(
    ok({ product_id: PRODUCT_ID, competitor_snapshot_in_drafts: true })
  );
  gateway.quoteCompetitorResolve.mockResolvedValue(ok(resolveQuote()));
  gateway.previewCompetitorScan.mockImplementation(async ({ body }) =>
    ok(scanQuote({ execution_mode: body.execution_mode ?? 'byok' }))
  );
});

describe('input helpers', () => {
  it('tells the four inputs apart', () => {
    expect(
      inputKindOf(
        'https://www.facebook.com/ads/library/?active_status=all&view_all_page_id=100000000000010'
      )
    ).toBe('ad_library');
    expect(inputKindOf('facebook.com/profile.php?id=100000000000016')).toBe(
      'facebook_id'
    );
    expect(inputKindOf('https://www.facebook.com/acmecharge')).toBe(
      'facebook_alias'
    );
    expect(inputKindOf('acme-charge.example')).toBe('website');
    expect(inputKindOf('https://brightvolt.example/shop')).toBe('website');
    expect(inputKindOf('Brightvolt')).toBe('name');
    expect(inputKindOf('   ')).toBeNull();
  });

  it('maps a plan market to a country only when it can tell', () => {
    expect(countryFromMarket('us')).toBe('US');
    expect(countryFromMarket('North America')).toBe('US');
    expect(countryFromMarket('北美')).toBe('US');
    expect(countryFromMarket('Southeast Asia')).toBeNull();
    expect(countryFromMarket(null)).toBeNull();
  });

  it('splits keywords on commas and newlines, without duplicates', () => {
    expect(keywordsOf('NACS adapter, CCS1 adapter\nNACS adapter,')).toEqual([
      'NACS adapter',
      'CCS1 adapter',
    ]);
    expect(keywordsOf('a、b，c')).toEqual(['a', 'b', 'c']);
  });
});

describe('competitor list', () => {
  it('confirms an Ad Library link on create without a resolve (scenario 1)', async () => {
    const added = confirmed({
      competitor_id: 'cmp_brightvolt',
      name: null,
      input_kind: 'ad_library',
      can_resolve: false,
      confirmation: {
        revision: 1,
        platform: 'meta',
        page_id: '100000000000016',
        page_name: 'Brightvolt',
        source: 'link',
        link_kind: 'ad_library',
      },
    });
    gateway.createCompetitor.mockResolvedValue(created(added));
    renderArea();
    await screen.findByText(/No competitors yet/);
    const link =
      'https://www.facebook.com/ads/library/?view_all_page_id=100000000000016';
    fireEvent.change(
      screen.getByLabelText('Page link, Ad Library link or name'),
      { target: { value: link } }
    );
    expect(
      screen.getByText('Ad Library link: the page is confirmed at once, free.')
    ).toBeInTheDocument();
    gateway.listCompetitors.mockResolvedValue(ok({ items: [added] }));
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));

    const row = await screen.findByTestId('competitor-row');
    expect(gateway.createCompetitor).toHaveBeenCalledWith(
      expect.objectContaining({
        workspaceSlug: 'alpha',
        body: {
          product_id: PRODUCT_ID,
          input: link,
          website: null,
          role: 'competitor',
        },
      })
    );
    expect(within(row).getByText('Confirmed by link')).toBeInTheDocument();
    expect(within(row).getByText('Not scanned yet')).toBeInTheDocument();
    expect(gateway.quoteCompetitorResolve).not.toHaveBeenCalled();
    expect(gateway.resolveCompetitor).not.toHaveBeenCalled();
  });

  it('names a link-confirmed page by what its scans observed and shows its own last scan (#332)', async () => {
    const linked = confirmed({
      competitor_id: 'cmp_brightvolt',
      name: null,
      input_kind: 'ad_library',
      can_resolve: false,
      confirmation: {
        revision: 1,
        platform: 'meta',
        page_id: '100000000000016',
        page_name: null,
        source: 'link',
        link_kind: 'ad_library',
      },
      observed_page_name: null,
      last_scanned_at: null,
    });
    gateway.listCompetitors.mockResolvedValue(ok({ items: [linked] }));
    // The product's scan list (at most 50) no longer decides "last scanned".
    gateway.listCompetitorScans.mockResolvedValue(
      ok({
        items: [
          {
            scan_id: 'scan_other',
            product_id: PRODUCT_ID,
            operation_id: 'o',
            job_id: 'j',
            status: 'completed',
            execution_mode: 'mock',
            country: 'US',
            competitor_ids: ['cmp_brightvolt'],
            observation_count: 0,
            created_at: '2026-10-29T10:00:00Z',
            finished_at: '2026-10-29T10:05:00Z',
          },
        ],
      })
    );
    gateway.getCompetitorScan.mockResolvedValue(
      ok(scan({ scan_id: 'scan_other', observation_count: 0 }))
    );
    const { unmount } = renderArea();
    let row = await screen.findByTestId('competitor-row');
    // Before any scan: the page ID, and why there is no name yet.
    expect(within(row).getAllByText(/100000000000016/).length).toBeGreaterThan(
      0
    );
    expect(
      within(row).getByTestId('competitor-name-after-scan')
    ).toHaveTextContent(
      'A link confirms the page without its name; it shows after the first scan.'
    );
    expect(within(row).getByTestId('competitor-last-scan')).toHaveTextContent(
      'Not scanned yet'
    );
    unmount();

    gateway.listCompetitors.mockResolvedValue(
      ok({
        items: [
          {
            ...linked,
            observed_page_name: 'Brightvolt',
            last_scanned_at: '2026-10-30T09:30:00Z',
          },
        ],
      })
    );
    renderArea();
    row = await screen.findByTestId('competitor-row');
    expect(within(row).getByText('Brightvolt')).toBeInTheDocument();
    expect(
      within(row).queryByTestId('competitor-name-after-scan')
    ).not.toBeInTheDocument();
    const lastScan = within(row).getByTestId('competitor-last-scan');
    expect(lastScan).toHaveTextContent(/Last scanned .*Oct 30, 2026/);
    expect(lastScan).not.toHaveTextContent(/Oct 29/);
  });

  it('names a scan target by its observed page name when the confirmation has none (#332)', async () => {
    const linked = confirmed({
      confirmation: {
        revision: 1,
        platform: 'meta',
        page_id: '100000000000010',
        page_name: null,
        source: 'link',
        link_kind: 'facebook_page',
      },
      name: null,
      observed_page_name: 'Acme Charge',
    });
    gateway.listCompetitors.mockResolvedValue(ok({ items: [linked] }));
    const shown = scan();
    shown.targets = (shown.targets ?? []).map((target) => ({
      ...target,
      page_name: null,
      observed_page_name: 'Acme Charge',
    }));
    gateway.listCompetitorScans.mockResolvedValue(
      ok({
        items: [
          {
            scan_id: shown.scan_id,
            product_id: PRODUCT_ID,
            operation_id: 'o',
            job_id: 'j',
            status: shown.status,
            execution_mode: shown.execution_mode,
            country: 'US',
            competitor_ids: ['cmp_acme'],
            observation_count: 0,
            created_at: '2026-10-30T09:00:00Z',
          },
        ],
      })
    );
    gateway.getCompetitorScan.mockResolvedValue(ok(shown));
    renderArea();
    const area = await screen.findByTestId('competitor-scan-area');
    await waitFor(() =>
      expect(within(area).getAllByText('Acme Charge').length).toBeGreaterThan(0)
    );
    expect(within(area).queryByText('100000000000010')).not.toBeInTheDocument();
  });

  it('shows a candidate’s archived avatar through a blob URL, never a provider link (#332)', async () => {
    const added = competitor();
    gateway.listCompetitors.mockResolvedValue(ok({ items: [added] }));
    blobClient.downloadBlob.mockResolvedValue(new Blob(['png']));
    gateway.resolveCompetitor.mockResolvedValue(accepted(resolution()));
    gateway.getCompetitorResolution.mockResolvedValue(
      ok(
        resolution({
          candidates: [
            candidate({
              avatar: {
                url: 'https://storage.example/avatar-acme.jpg?sig=1',
                expires_at: '2026-10-30T09:00:00Z',
                mime_type: 'image/jpeg',
                width: 200,
                height: 200,
              },
            }),
          ],
        })
      )
    );
    renderArea();
    fireEvent.click(await screen.findByRole('button', { name: 'Resolve' }));
    fireEvent.click(
      await screen.findByRole('button', { name: 'Resolve', hidden: false })
    );
    const card = await screen.findByTestId('competitor-candidate-card');
    const avatar = await within(card).findByTestId('competitor-avatar');
    expect(avatar).toHaveAttribute('src', 'blob:avatar');
    expect(avatar).toHaveAttribute('draggable', 'false');
    expect(
      within(card).queryByTestId('competitor-avatar-placeholder')
    ).not.toBeInTheDocument();
    expect(blobClient.downloadBlob).toHaveBeenCalledTimes(1);
  });

  it('moves a bare website into its field and asks for the name', async () => {
    renderArea();
    await screen.findByText(/No competitors yet/);
    const input = screen.getByLabelText('Page link, Ad Library link or name');
    fireEvent.change(input, { target: { value: 'acme-charge.example' } });
    fireEvent.click(screen.getByRole('button', { name: 'Use as website' }));
    expect(screen.getByLabelText('Official website (optional)')).toHaveValue(
      'acme-charge.example'
    );
    expect(input).toHaveValue('');
    expect(gateway.createCompetitor).not.toHaveBeenCalled();
  });

  it('shows the resolve cost before resolving a name, then lets the user switch candidates and confirm (scenario 2)', async () => {
    const added = competitor();
    gateway.createCompetitor.mockResolvedValue(created(added));
    renderArea();
    await screen.findByText(/No competitors yet/);
    fireEvent.change(
      screen.getByLabelText('Page link, Ad Library link or name'),
      { target: { value: 'Acme Charge' } }
    );
    fireEvent.change(screen.getByLabelText('Official website (optional)'), {
      target: { value: 'acme-charge.example' },
    });
    gateway.listCompetitors.mockResolvedValue(ok({ items: [added] }));
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));

    // The new unconfirmed row opens on its paid resolve, with the cost first.
    const cost = await screen.findByTestId('competitor-resolve-cost');
    expect(cost).toHaveTextContent('Up to 4 credits · $0.0075');
    expect(
      within(cost).getByText('Name search: 1 call · 1 credit')
    ).toBeInTheDocument();
    expect(
      within(cost).getByText('Candidate ad checks: up to 3 calls · 3 credits')
    ).toBeInTheDocument();
    expect(cost).not.toHaveTextContent('×');
    expect(gateway.quoteCompetitorResolve).toHaveBeenCalledWith(
      expect.objectContaining({ competitorId: 'cmp_acme', country: 'US' })
    );

    gateway.resolveCompetitor.mockResolvedValue(
      accepted(resolution({ status: 'queued', candidates: [] }))
    );
    gateway.getCompetitorResolution.mockResolvedValue(ok(resolution()));
    fireEvent.click(screen.getByRole('button', { name: 'Resolve' }));
    await waitFor(() =>
      expect(gateway.resolveCompetitor).toHaveBeenCalledWith(
        expect.objectContaining({
          competitorId: 'cmp_acme',
          body: { execution_mode: 'byok', country: 'US' },
        })
      )
    );

    const card = await screen.findByTestId('competitor-candidate-card');
    expect(card).toHaveAttribute('data-page-id', '100000000000010');
    expect(within(card).getByText('@acmecharge')).toBeInTheDocument();
    expect(
      within(card).getByText('Automotive Parts Store')
    ).toBeInTheDocument();
    expect(within(card).getByText('3,509')).toBeInTheDocument();
    expect(
      within(card).getByText('@acme_charge_ev · 1,200 followers')
    ).toBeInTheDocument();
    expect(within(card).getByText('58')).toBeInTheDocument();
    expect(
      within(card).getByText('Ads link to acme-charge.example')
    ).toBeInTheDocument();
    // A page whose avatar the resolve could not archive: the placeholder.
    expect(
      within(card).getByTestId('competitor-avatar-placeholder')
    ).toBeInTheDocument();
    expect(screen.getByText('Candidate 1 of 2')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    const second = screen.getByTestId('competitor-candidate-card');
    expect(second).toHaveAttribute('data-page-id', '200');
    expect(
      within(second).getByText('Ads do not link to acme-charge.example')
    ).toBeInTheDocument();
    // Its archived copy is read from the signed link, which failed here:
    // the placeholder stays, and no provider link ever reaches an <img>.
    await waitFor(() =>
      expect(blobClient.downloadBlob).toHaveBeenCalledWith(
        expect.objectContaining({
          url: 'https://storage.example/avatar-200.jpg?sig=1',
          content_disposition: 'inline',
        }),
        expect.anything()
      )
    );
    expect(
      within(second).getByTestId('competitor-avatar-placeholder')
    ).toBeInTheDocument();
    expect(second.querySelector('img')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: /Acme Charge ✓/ }));
    gateway.confirmCompetitor.mockResolvedValue(ok(confirmed()));
    gateway.listCompetitors.mockResolvedValue(ok({ items: [confirmed()] }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm this page' }));
    await waitFor(() =>
      expect(gateway.confirmCompetitor).toHaveBeenCalledWith(
        expect.objectContaining({
          competitorId: 'cmp_acme',
          body: {
            resolve_job_id: 'job_resolve_1',
            page_id: '100000000000010',
          },
        })
      )
    );
    expect(
      await screen.findByText('Confirmed', {
        selector: '[data-testid="competitor-confirmation-status"]',
      })
    ).toBeInTheDocument();
  });

  it('quotes an alias-only page link as a paid resolve (#269)', async () => {
    gateway.listCompetitors.mockResolvedValue(
      ok({
        items: [
          competitor({
            name: null,
            input_kind: 'facebook_page',
            input_page_alias: 'acmecharge',
          }),
        ],
      })
    );
    renderArea();
    fireEvent.click(await screen.findByRole('button', { name: 'Resolve' }));
    expect(
      await screen.findByTestId('competitor-resolve-cost')
    ).toHaveTextContent('Up to 4 credits');
    expect(
      screen.getByText(/The link names the page only as “acmecharge”/)
    ).toBeInTheDocument();
  });

  it('offers only Controlled Mock without a market-data key and names the missing price operations', async () => {
    gateway.quoteCompetitorResolve.mockResolvedValue(
      ok(
        resolveQuote({
          execution_mode: 'mock',
          available_modes: ['mock'],
          byok_unavailable_code: 'market_data_price_operation_missing',
          byok_unavailable_params: { operations: ['page_search', 'probe'] },
          max_usd: '0',
          simulated_usd: '0.00752',
        })
      )
    );
    gateway.listCompetitors.mockResolvedValue(ok({ items: [competitor()] }));
    renderArea();
    fireEvent.click(await screen.findByRole('button', { name: 'Resolve' }));
    const hint = await screen.findByTestId('competitor-credential-hint');
    expect(hint).toHaveTextContent('lacks page_search, probe');
    expect(screen.queryByRole('radio')).not.toBeInTheDocument();
    expect(screen.getByTestId('competitor-resolve-cost')).toHaveTextContent(
      'Controlled Mock: no charge (estimate $0.0075)'
    );
    expect(screen.getAllByText('Simulated').length).toBeGreaterThan(0);
  });

  it('confirms by a pasted Ad Library link for free', async () => {
    gateway.listCompetitors.mockResolvedValue(ok({ items: [competitor()] }));
    gateway.confirmCompetitor.mockResolvedValue(ok(confirmed()));
    renderArea();
    fireEvent.click(await screen.findByRole('button', { name: 'Resolve' }));
    const link =
      'https://www.facebook.com/ads/library/?view_all_page_id=100000000000010';
    fireEvent.change(screen.getByLabelText('Or confirm with a link'), {
      target: { value: link },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Confirm link' }));
    await waitFor(() =>
      expect(gateway.confirmCompetitor).toHaveBeenCalledWith(
        expect.objectContaining({ body: { page_url: link } })
      )
    );
    expect(gateway.resolveCompetitor).not.toHaveBeenCalled();
  });

  it('shows archive to the Owner only', async () => {
    gateway.listCompetitors.mockResolvedValue(ok({ items: [confirmed()] }));
    gateway.archiveCompetitor.mockResolvedValue(
      ok(confirmed({ status: 'archived' }))
    );
    const { unmount } = renderArea();
    fireEvent.click(await screen.findByRole('button', { name: 'Archive' }));
    await waitFor(() =>
      expect(gateway.archiveCompetitor).toHaveBeenCalledWith(
        expect.objectContaining({ competitorId: 'cmp_acme' })
      )
    );
    unmount();

    mockRole = 'MEMBER';
    renderArea();
    await screen.findByTestId('competitor-row');
    expect(
      screen.queryByRole('button', { name: 'Archive' })
    ).not.toBeInTheDocument();
  });
});

describe('scan', () => {
  it('quotes both lines, defaults the market to the plan and starts a real scan (scenario 3)', async () => {
    gateway.listCompetitors.mockResolvedValue(ok({ items: [confirmed()] }));
    gateway.createCompetitorScan.mockResolvedValue(
      accepted(scan({ status: 'queued', stage: null, steps: [], cost: null }))
    );
    gateway.getCompetitorScan.mockResolvedValue(ok(scan()));
    renderArea('north america');
    const quote = await screen.findByTestId('competitor-scan-quote');
    expect(screen.getByLabelText('Market')).toHaveValue('US');
    expect(
      screen.getByText("Taken from the plan's market “north america”.")
    ).toBeInTheDocument();
    const provider = within(quote).getByTestId(
      'competitor-scan-quote-provider'
    );
    expect(provider).toHaveTextContent('up to 13 credits · $0.0244');
    expect(
      within(provider).getByText('Competitor pages: 3 calls · 3 credits')
    ).toBeInTheDocument();
    expect(
      within(provider).getByText(
        'Expired image links: up to 10 calls · 10 credits'
      )
    ).toBeInTheDocument();
    expect(provider).not.toHaveTextContent('×');
    expect(
      within(quote).getByTestId('competitor-scan-quote-llm')
    ).toHaveTextContent('up to $0.84');
    expect(
      within(quote).getByTestId('competitor-scan-quote-total')
    ).toHaveTextContent('$0.8644');
    expect(gateway.previewCompetitorScan).toHaveBeenLastCalledWith(
      expect.objectContaining({
        body: expect.objectContaining({
          product_id: PRODUCT_ID,
          competitor_ids: ['cmp_acme'],
          country: 'US',
          max_pages_per_competitor: 3,
          max_tagged_ads: 60,
          keywords: [],
        }),
      })
    );

    fireEvent.change(screen.getByLabelText('Keywords (optional)'), {
      target: { value: 'NACS adapter, CCS1 adapter' },
    });
    fireEvent.change(screen.getByLabelText('Pages per competitor'), {
      target: { value: '2' },
    });
    await waitFor(() =>
      expect(gateway.previewCompetitorScan).toHaveBeenLastCalledWith(
        expect.objectContaining({
          body: expect.objectContaining({
            keywords: ['NACS adapter', 'CCS1 adapter'],
            max_pages_per_competitor: 2,
          }),
        })
      )
    );
    fireEvent.click(
      await screen.findByRole('button', { name: 'Start scan · up to $0.8644' })
    );
    await waitFor(() =>
      expect(gateway.createCompetitorScan).toHaveBeenCalledWith(
        expect.objectContaining({
          body: expect.objectContaining({
            execution_mode: 'byok',
            keywords: ['NACS adapter', 'CCS1 adapter'],
          }),
        })
      )
    );
    expect(replace).toHaveBeenCalledWith(
      expect.objectContaining({
        query: expect.objectContaining({ competitor_scan: 'scan_1' }),
      }),
      undefined,
      expect.objectContaining({ shallow: true })
    );
    const progress = await screen.findByTestId('competitor-scan-progress');
    await waitFor(() =>
      expect(progress).toHaveAttribute('data-status', 'completed')
    );
    expect(
      within(progress).getByText('Scan completed: 50 ads observed.')
    ).toBeInTheDocument();
    expect(
      within(progress).getByTestId('competitor-scan-step-filter')
    ).toHaveTextContent('Not available yet');
  });

  it('asks for the market when the plan market cannot be told', async () => {
    gateway.listCompetitors.mockResolvedValue(ok({ items: [confirmed()] }));
    renderArea('Southeast Asia');
    await screen.findByTestId('competitor-scan-quote');
    expect(
      screen.getByText(/The plan's market “Southeast Asia” is not a country/)
    ).toBeInTheDocument();
  });

  it('offers only a simulated scan when a key is missing, with the Owner sent to Settings (scenario 10)', async () => {
    gateway.listCompetitors.mockResolvedValue(ok({ items: [confirmed()] }));
    gateway.previewCompetitorScan.mockResolvedValue(
      ok(
        scanQuote({
          execution_mode: 'mock',
          available_modes: ['mock'],
          byok_unavailable_code: 'market_data_credential_missing',
          credentials: [
            {
              binding_scope: 'market_data',
              provider: 'scrapecreators',
              ready: false,
              code: 'market_data_credential_missing',
            },
            { binding_scope: 'generation', provider: 'openai', ready: true },
          ],
          max_usd: '0',
          simulated_usd: '0.86',
          market_data: {
            provider: 'scrapecreators',
            max_credits: 13,
            max_usd: '0',
            simulated_usd: '0.0244',
            lines: [],
          },
          llm: {
            provider: 'openai',
            model: 'gpt-4.1-mini',
            calls: 66,
            max_usd: '0',
            simulated_usd: '0.84',
            price_rule_version: 'v1',
          },
        })
      )
    );
    gateway.createCompetitorScan.mockResolvedValue(
      accepted(scan({ execution_mode: 'mock' }))
    );
    gateway.getCompetitorScan.mockResolvedValue(
      ok(scan({ execution_mode: 'mock' }))
    );
    const { unmount } = renderArea();
    const hint = await screen.findByTestId('competitor-credential-hint');
    expect(hint).toHaveTextContent('ScrapeCreators key');
    expect(
      within(hint).getByTestId('competitor-settings-link')
    ).toHaveAttribute('href', '/w/alpha/settings#provider-credentials');
    expect(screen.queryByRole('radio')).not.toBeInTheDocument();
    expect(screen.getByTestId('competitor-scan-quote')).toHaveAttribute(
      'data-mode',
      'mock'
    );
    fireEvent.click(screen.getByTestId('competitor-scan-start'));
    await waitFor(() =>
      expect(gateway.createCompetitorScan).toHaveBeenCalledWith(
        expect.objectContaining({
          body: expect.objectContaining({ execution_mode: 'mock' }),
        })
      )
    );
    const progress = await screen.findByTestId('competitor-scan-progress');
    expect(
      within(progress).getByTestId('competitor-simulated-badge')
    ).toBeInTheDocument();
    unmount();

    mockRole = 'MEMBER';
    query = {};
    gateway.listCompetitorScans.mockResolvedValue(ok({ items: [] }));
    renderArea();
    const memberHint = await screen.findByTestId('competitor-credential-hint');
    expect(
      within(memberHint).queryByTestId('competitor-settings-link')
    ).not.toBeInTheDocument();
    expect(memberHint).toHaveTextContent(
      'Ask the Workspace Owner to add the key in Settings.'
    );
  });

  it('shows a partial scan with its reason, the remaining quote and resume (scenarios 4, 13)', async () => {
    query = { competitor_scan: 'scan_1' };
    gateway.listCompetitors.mockResolvedValue(ok({ items: [confirmed()] }));
    const partial = scan({
      status: 'partial',
      can_resume: true,
      failure: { code: 'market_data_insufficient_credits' },
      targets: [
        {
          origin: 'page:cmp_acme',
          kind: 'page',
          competitor_id: 'cmp_acme',
          page_name: 'Acme Charge',
          status: 'failed',
          pages_planned: 3,
          pages_fetched: 1,
          ads_seen: 30,
          failure: { code: 'market_data_insufficient_credits' },
        },
      ],
      steps: [
        { step: 'fetch', status: 'partial' },
        { step: 'media', status: 'completed' },
      ],
      observation_count: 30,
    });
    gateway.getCompetitorScan.mockResolvedValue(ok(partial));
    gateway.quoteCompetitorScanResume.mockResolvedValue(
      ok(
        scanQuote({
          kind: 'resume',
          scan_id: 'scan_1',
          market_data: {
            provider: 'scrapecreators',
            max_credits: 2,
            max_usd: '0.00376',
            lines: [
              { operation: 'page', calls: 2, credits: 2, usd: '0.00376' },
            ],
          },
          max_usd: '0.84376',
        })
      )
    );
    const resumed = scan({
      status: 'completed',
      resumes: [
        {
          operation_id: 'op_resume_1',
          job_id: 'job_resume_1',
          max_credits: 2,
          max_usd: '0.84376',
        },
      ],
    });
    gateway.resumeCompetitorScan.mockResolvedValue(accepted(resumed));
    renderArea();

    const progress = await screen.findByTestId('competitor-scan-progress');
    expect(progress).toHaveAttribute('data-status', 'partial');
    expect(
      within(progress).getByText(
        'Some pages were not fetched. The ads already fetched are kept.'
      )
    ).toBeInTheDocument();
    expect(
      within(progress).getAllByText(
        'The ScrapeCreators account is out of credits. Top it up, then try again.'
      ).length
    ).toBeGreaterThan(0);
    expect(
      within(progress).getByText(/1\/3 pages · 30 ads/)
    ).toBeInTheDocument();
    const resume = await within(progress).findByTestId(
      'competitor-scan-resume'
    );
    expect(
      await within(resume).findByTestId('competitor-scan-quote')
    ).toHaveAttribute('data-kind', 'resume');
    expect(
      within(resume).getByText('Competitor pages: 2 calls · 2 credits')
    ).toBeInTheDocument();
    gateway.getCompetitorScan.mockResolvedValue(ok(resumed));
    fireEvent.click(
      within(resume).getByRole('button', { name: 'Resume · up to $0.8438' })
    );
    await waitFor(() =>
      expect(gateway.resumeCompetitorScan).toHaveBeenCalledWith(
        expect.objectContaining({ scanId: 'scan_1' })
      )
    );
    await waitFor(() =>
      expect(screen.getByTestId('competitor-scan-progress')).toHaveAttribute(
        'data-status',
        'completed'
      )
    );
    expect(screen.getByText('Resumed 1 time.')).toBeInTheDocument();
  });

  it('lists pages awaiting reconciliation with the automatic result; only the Owner marks them (scenario 5)', async () => {
    query = { competitor_scan: 'scan_1' };
    gateway.listCompetitors.mockResolvedValue(ok({ items: [confirmed()] }));
    const uncertain = scan({
      status: 'uncertain',
      can_resume: true,
      reconciliation: [
        {
          attempt_id: 'att_2',
          call_name: 'page:cmp_acme:2',
          origin: 'page:cmp_acme',
          page: 2,
          status: 'pending',
          source: null,
          balance_check: {
            outcome: 'manual_required',
            reason: 'difference',
            difference: 3,
          },
        },
      ],
    });
    gateway.getCompetitorScan.mockResolvedValue(ok(uncertain));
    gateway.quoteCompetitorScanResume.mockResolvedValue(
      ok(scanQuote({ kind: 'resume', scan_id: 'scan_1' }))
    );
    gateway.reconcileCompetitorScan.mockResolvedValue(
      ok(
        scan({
          status: 'uncertain',
          can_resume: true,
          reconciliation: [
            {
              ...uncertain.reconciliation![0],
              status: 'charged',
              source: 'owner',
            },
          ],
        })
      )
    );
    const { unmount } = renderArea();
    const box = await screen.findByTestId('competitor-scan-reconciliation');
    expect(box).toHaveTextContent('Acme Charge, page 2');
    expect(
      within(box).getByTestId('competitor-scan-balance-check')
    ).toHaveTextContent(
      'Automatic check could not tell. The credit balance moved by 3, not 0 or 1.'
    );
    fireEvent.click(within(box).getByRole('button', { name: 'Mark charged' }));
    await waitFor(() =>
      expect(gateway.reconcileCompetitorScan).toHaveBeenCalledWith(
        expect.objectContaining({
          scanId: 'scan_1',
          body: { attempt_id: 'att_2', outcome: 'charged' },
        })
      )
    );
    expect(
      await screen.findByText(/Settled as charged · by the Owner/)
    ).toBeInTheDocument();
    unmount();

    mockRole = 'MEMBER';
    gateway.getCompetitorScan.mockResolvedValue(ok(uncertain));
    renderArea();
    const memberBox = await screen.findByTestId(
      'competitor-scan-reconciliation'
    );
    expect(
      within(memberBox).queryByRole('button', { name: 'Mark charged' })
    ).not.toBeInTheDocument();
    expect(memberBox).toHaveTextContent(
      'Only the Owner can mark whether this page was charged.'
    );
  });

  it('names the step and reason of a failed scan', async () => {
    query = { competitor_scan: 'scan_1' };
    gateway.listCompetitors.mockResolvedValue(ok({ items: [confirmed()] }));
    gateway.getCompetitorScan.mockResolvedValue(
      ok(
        scan({
          status: 'failed',
          stage: null,
          failure: { code: 'market_data_credential_rejected' },
          steps: [{ step: 'fetch', status: 'failed' }],
          observation_count: 0,
        })
      )
    );
    renderArea();
    expect(
      await screen.findByTestId('competitor-scan-failed-step')
    ).toHaveTextContent('The scan failed at “Fetch ads”.');
    expect(
      screen.getByText(
        "ScrapeCreators rejected the Workspace's key. The Owner can replace it in Settings."
      )
    ).toBeInTheDocument();
    expect(
      screen.queryByTestId('competitor-scan-resume')
    ).not.toBeInTheDocument();
  });

  it('shows a cluster step a recluster superseded as done by the latest completed revision, with no resume (#353)', async () => {
    query = { competitor_scan: 'scan_1' };
    gateway.listCompetitors.mockResolvedValue(ok({ items: [confirmed()] }));
    gateway.getCompetitorScan.mockResolvedValue(ok(reclusteredScan()));
    renderArea();

    const step = await screen.findByTestId('competitor-scan-step-cluster');
    expect(step).toHaveAttribute('data-status', 'failed'); // as the API reports it
    expect(step).toHaveAttribute('data-superseded', 'true');
    expect(step).toHaveTextContent(
      'Group purchase claims · Done by a recluster (clusters revision 2)'
    );
    expect(step).not.toHaveTextContent('Failed');
    expect(step.querySelector('svg')).toHaveClass('text-green-700');
    expect(screen.getByTestId('competitor-scan-step-tag')).not.toHaveAttribute(
      'data-superseded'
    );
    expect(
      screen.queryByTestId('competitor-scan-resume')
    ).not.toBeInTheDocument();
    expect(gateway.quoteCompetitorScanResume).not.toHaveBeenCalled();
  });

  it('still offers a resume the API allows on a superseded cluster step (#353)', async () => {
    query = { competitor_scan: 'scan_1' };
    gateway.listCompetitors.mockResolvedValue(ok({ items: [confirmed()] }));
    gateway.getCompetitorScan.mockResolvedValue(
      ok(reclusteredScan({ can_resume: true }))
    );
    // A flipped ad still to tag (C-32): no page and no clustering is owed.
    gateway.quoteCompetitorScanResume.mockResolvedValue(
      ok(
        scanQuote({
          kind: 'resume',
          scan_id: 'scan_1',
          market_data: {
            provider: 'scrapecreators',
            max_credits: 0,
            max_usd: '0',
            lines: [],
          },
          llm: {
            provider: 'openai',
            model: 'gpt-4.1-mini',
            calls: 1,
            max_usd: '0.0123',
            price_rule_version: 'v1',
            lines: [],
          },
          max_usd: '0.0123',
        })
      )
    );
    renderArea();

    const resume = await screen.findByTestId('competitor-scan-resume');
    expect(
      await within(resume).findByTestId('competitor-scan-quote')
    ).toHaveAttribute('data-kind', 'resume');
    expect(
      within(resume).getByRole('button', { name: 'Resume · up to $0.0123' })
    ).toBeEnabled();
    expect(screen.getByTestId('competitor-scan-step-cluster')).toHaveAttribute(
      'data-superseded',
      'true'
    );
  });

  it('keeps a cluster step failed when no recluster completed (#353)', async () => {
    query = { competitor_scan: 'scan_1' };
    gateway.listCompetitors.mockResolvedValue(ok({ items: [confirmed()] }));
    const failedOnly = reclusteredScan({ can_resume: true });
    gateway.getCompetitorScan.mockResolvedValue(
      ok({ ...failedOnly, reclusters: failedOnly.reclusters!.slice(0, 1) })
    );
    gateway.quoteCompetitorScanResume.mockResolvedValue(
      ok(scanQuote({ kind: 'resume', scan_id: 'scan_1' }))
    );
    renderArea();

    const step = await screen.findByTestId('competitor-scan-step-cluster');
    expect(step).toHaveAttribute('data-status', 'failed');
    expect(step).not.toHaveAttribute('data-superseded');
    expect(step).toHaveTextContent('Group purchase claims · Failed');
    expect(step.querySelector('svg')).toHaveClass('text-red-700');
    expect(
      await screen.findByTestId('competitor-scan-resume')
    ).toBeInTheDocument();
  });

  it('names the superseding clusters revision in zh-CN (#353)', () => {
    const i18n = getI18n().cloneInstance({
      lng: 'zh-CN',
      fallbackLng: false,
      forkResourceStore: true,
    });
    i18n.addResourceBundle('zh-CN', 'competitors', competitorsZh);
    i18n.addResourceBundle('zh-CN', 'common', commonZh);
    void i18n.changeLanguage('zh-CN');
    render(
      <I18nextProvider i18n={i18n}>
        <ScanProgress scan={reclusteredScan()} onChanged={jest.fn()} />
      </I18nextProvider>
    );
    expect(
      screen.getByTestId('competitor-scan-step-cluster')
    ).toHaveTextContent('聚类购买主张 · 已由重新聚类完成（第 2 版聚类）');
  });

  it('puts the board and summary under a scan with observations, and none under one without', async () => {
    query = { competitor_scan: 'scan_1' };
    gateway.listCompetitors.mockResolvedValue(ok({ items: [confirmed()] }));
    gateway.getCompetitorScan.mockResolvedValue(ok(scan()));
    const view = renderArea();
    expect(
      await screen.findByTestId('competitor-summary-empty')
    ).toHaveTextContent('No snapshot yet.');
    expect(screen.getByTestId('competitor-board')).toBeInTheDocument();
    expect(gateway.listCompetitorScanAds).toHaveBeenCalledWith(
      expect.objectContaining({ scanId: 'scan_1', role: 'self' })
    );
    view.unmount();

    gateway.getCompetitorScan.mockResolvedValue(
      ok(scan({ status: 'failed', observation_count: 0 }))
    );
    renderArea();
    await screen.findByTestId('competitor-scan-progress');
    expect(screen.queryByTestId('competitor-board-area')).toBeNull();
  });

  it('shows a refused scan start with its code copy', async () => {
    gateway.listCompetitors.mockResolvedValue(ok({ items: [confirmed()] }));
    gateway.createCompetitorScan.mockResolvedValue(
      problem(409, 'market_data_turn_unavailable')
    );
    renderArea();
    await screen.findByTestId('competitor-scan-quote');
    fireEvent.click(screen.getByTestId('competitor-scan-start'));
    expect(
      await screen.findByText(
        'Another resolve or scan kept the ScrapeCreators key busy for too long. Try again.'
      )
    ).toBeInTheDocument();
  });
});

describe('quote lines (#346)', () => {
  // A line's `credits` is its total (calls × the price of a call); at two
  // credits a call the line must not read as calls × credits.
  it('reads a scan line as its calls and its total credits when a call costs more than one credit', async () => {
    gateway.previewCompetitorScan.mockResolvedValue(
      ok(
        scanQuote({
          market_data: {
            provider: 'scrapecreators',
            max_credits: 7,
            max_usd: '0.01316',
            lines: [
              { operation: 'page', calls: 3, credits: 6, usd: '0.01128' },
              {
                operation: 'keyword_page',
                calls: 1,
                credits: 1,
                usd: '0.00188',
              },
            ],
          },
          max_usd: '0.85316',
        })
      )
    );
    gateway.listCompetitors.mockResolvedValue(ok({ items: [confirmed()] }));
    renderArea();
    const provider = await screen.findByTestId(
      'competitor-scan-quote-provider'
    );
    expect(
      within(provider).getByText('Competitor pages: 3 calls · 6 credits')
    ).toBeInTheDocument();
    expect(
      within(provider).getByText('Keyword pages: 1 call · 1 credit')
    ).toBeInTheDocument();
    // The total is the sum of the lines' credits, 6 + 1.
    expect(provider).toHaveTextContent('up to 7 credits · $0.0132');
    expect(provider).not.toHaveTextContent('×');
  });

  it('reads a resolve line as its calls and its total credits when a call costs more than one credit', async () => {
    gateway.quoteCompetitorResolve.mockResolvedValue(
      ok(
        resolveQuote({
          max_credits: 7,
          max_usd: '0.01316',
          lines: [
            { operation: 'page_search', calls: 1, credits: 1, usd: '0.00188' },
            { operation: 'probe', calls: 3, credits: 6, usd: '0.01128' },
          ],
        })
      )
    );
    gateway.listCompetitors.mockResolvedValue(ok({ items: [competitor()] }));
    renderArea();
    fireEvent.click(await screen.findByRole('button', { name: 'Resolve' }));
    const cost = await screen.findByTestId('competitor-resolve-cost');
    expect(
      within(cost).getByText('Candidate ad checks: up to 3 calls · 6 credits')
    ).toBeInTheDocument();
    expect(cost).toHaveTextContent('Up to 7 credits · $0.0132');
    expect(cost).not.toHaveTextContent('×');
  });
});

describe('in zh-CN', () => {
  function zhCN(node: ReactElement) {
    const i18n = getI18n().cloneInstance({
      lng: 'zh-CN',
      fallbackLng: false,
      forkResourceStore: true,
    });
    i18n.addResourceBundle('zh-CN', 'competitors', competitorsZh);
    i18n.addResourceBundle('zh-CN', 'common', commonZh);
    void i18n.changeLanguage('zh-CN');
    return <I18nextProvider i18n={i18n}>{node}</I18nextProvider>;
  }

  it('words the resolve quote lines as calls and credits', async () => {
    gateway.listCompetitors.mockResolvedValue(ok({ items: [competitor()] }));
    renderArea('US', zhCN);
    fireEvent.click(await screen.findByRole('button', { name: '解析' }));
    const cost = await screen.findByTestId('competitor-resolve-cost');
    expect(within(cost).getByText('名字搜索：1 次调用 · 1 额度')).toBeVisible();
    expect(
      within(cost).getByText('候选广告核对：最多 3 次调用 · 3 额度')
    ).toBeVisible();
    expect(cost).not.toHaveTextContent('×');
  });

  it('words the scan quote lines as calls and credits', async () => {
    gateway.listCompetitors.mockResolvedValue(ok({ items: [confirmed()] }));
    renderArea('US', zhCN);
    const provider = await screen.findByTestId(
      'competitor-scan-quote-provider'
    );
    expect(
      within(provider).getByText('竞品主页：3 次调用 · 3 额度')
    ).toBeVisible();
    expect(
      within(provider).getByText('过期图片补拉：最多 10 次调用 · 10 额度')
    ).toBeVisible();
    expect(provider).not.toHaveTextContent('×');
  });
});

describe('recluster (#355)', () => {
  const reclusterQuote = ok({
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
  });
  const queuedRecluster = {
    operation_id: 'op_recluster_1',
    job_id: 'job_recluster_1',
    status: 'queued',
    max_usd: '0',
  };

  /**
   * A finished scan with a snapshot whose recluster the backend accepts. Like
   * the backend, the quotes answer `competitor_scan_busy` once a recluster
   * is queued, and the scan stops offering a resume while it runs.
   */
  function reclusterable(overrides: Parameters<typeof scan>[0] = {}) {
    const finished = scan({ snapshot_id: 'snap_1', ...overrides });
    const queued = scan({
      snapshot_id: 'snap_1',
      ...overrides,
      can_resume: false,
      reclusters: [queuedRecluster],
    });
    query = { competitor_scan: 'scan_1' };
    gateway.listCompetitors.mockResolvedValue(ok({ items: [confirmed()] }));
    gateway.getCompetitorScanSummary.mockResolvedValue(ok(snapshotSummary()));
    let running = false;
    gateway.getCompetitorScan.mockImplementation(async () =>
      ok(running ? queued : finished)
    );
    gateway.quoteCompetitorScanRecluster.mockImplementation(async () =>
      running ? problem(409, 'competitor_scan_busy') : reclusterQuote
    );
    gateway.reclusterCompetitorScan.mockImplementation(async () => {
      running = true;
      return accepted(queued);
    });
    return { finished, queued };
  }

  async function confirmRecluster() {
    fireEvent.click(await screen.findByTestId('competitor-recluster'));
    const quote = await screen.findByTestId('competitor-recluster-quote');
    fireEvent.click(
      await within(quote).findByTestId('competitor-recluster-confirm')
    );
    expect(
      await screen.findByTestId('competitor-recluster-status')
    ).toHaveAttribute('data-status', 'queued');
    // Whatever the queued scan's render asked for has been asked by now.
    await waitFor(() =>
      expect(screen.queryByTestId('competitor-recluster-quote')).toBeNull()
    );
    await act(() => new Promise((resolve) => setTimeout(resolve, 50)));
  }

  it('quotes once: confirming does not re-quote the recluster it just queued (scenario 1)', async () => {
    reclusterable();
    renderArea();
    await confirmRecluster();
    expect(gateway.reclusterCompetitorScan).toHaveBeenCalledTimes(1);
    expect(gateway.quoteCompetitorScanRecluster).toHaveBeenCalledTimes(1);
    expect(gateway.quoteCompetitorScanResume).not.toHaveBeenCalled();
    expect(screen.getByTestId('competitor-recluster')).toBeDisabled();
  });

  it('asks neither quote again when the scan also offers a resume, like production S1 (scenario 2)', async () => {
    reclusterable({
      can_resume: true,
      steps: [
        { step: 'fetch', status: 'completed' },
        { step: 'media', status: 'completed' },
        { step: 'filter', status: 'completed' },
        { step: 'tag', status: 'completed' },
        { step: 'cluster', status: 'failed' },
        { step: 'freeze', status: 'completed' },
      ],
    });
    gateway.quoteCompetitorScanResume.mockResolvedValue(
      ok(scanQuote({ kind: 'resume', scan_id: 'scan_1' }))
    );
    renderArea();
    const resume = await screen.findByTestId('competitor-scan-resume');
    await within(resume).findByTestId('competitor-scan-quote');
    const resumeQuotes = gateway.quoteCompetitorScanResume.mock.calls.length;
    expect(resumeQuotes).toBeGreaterThan(0);

    await confirmRecluster();
    expect(gateway.quoteCompetitorScanRecluster).toHaveBeenCalledTimes(1);
    expect(gateway.quoteCompetitorScanResume).toHaveBeenCalledTimes(
      resumeQuotes
    );
    expect(screen.queryByTestId('competitor-scan-resume')).toBeNull();
  });

  it('does not quote a resume while a recluster runs, even if the scan still offers one', async () => {
    const { queued } = reclusterable({ can_resume: true });
    gateway.getCompetitorScan.mockResolvedValue(
      ok({ ...queued, can_resume: true })
    );
    renderArea();
    await screen.findByTestId('competitor-recluster-status');
    await act(() => new Promise((resolve) => setTimeout(resolve, 50)));
    expect(gateway.quoteCompetitorScanResume).not.toHaveBeenCalled();
    expect(gateway.quoteCompetitorScanRecluster).not.toHaveBeenCalled();
  });
});

describe('candidate card', () => {
  it('reports when the domain was not checked', async () => {
    gateway.listCompetitors.mockResolvedValue(
      ok({
        items: [
          competitor({
            website_domain: null,
            latest_resolution: {
              resolve_job_id: 'job_resolve_1',
              operation_id: 'op',
              status: 'completed',
              execution_mode: 'mock',
              auto_confirmed: false,
              reusable: true,
            },
          }),
        ],
      })
    );
    gateway.getCompetitorResolution.mockResolvedValue(
      ok(
        resolution({
          execution_mode: 'mock',
          website_domain: null,
          candidates: [candidate({ domain_hit: null, probe: null })],
        })
      )
    );
    renderArea();
    fireEvent.click(await screen.findByRole('button', { name: 'Resolve' }));
    const card = await screen.findByTestId('competitor-candidate-card');
    expect(within(card).getByText('No website given')).toBeInTheDocument();
    expect(within(card).getAllByText('Not checked').length).toBe(2);
    // A reusable resolve confirms without another quote.
    expect(
      screen.queryByTestId('competitor-resolve-cost')
    ).not.toBeInTheDocument();
  });
});

describe('a page that answered no ads (#355)', () => {
  // Not a page P0 recorded: the Mock answers it an empty page, like a wrong ID.
  const WRONG = '100000000000001';
  const NO_ADS =
    'No ads came back. The page may not be running ads right now, or the page ID may be wrong.';
  const AFTER_SCAN =
    'A link confirms the page without its name; it shows after the first scan.';
  const NO_ADS_ROW =
    'Scanned, but no ads from this page came back, so it has no name. If the page ID is wrong, change the page.';

  const linked = (overrides: Parameters<typeof confirmed>[0] = {}) =>
    confirmed({
      competitor_id: 'cmp_wrong',
      name: null,
      input_kind: 'ad_library',
      can_resolve: false,
      confirmation: {
        revision: 1,
        platform: 'meta',
        page_id: WRONG,
        page_name: null,
        source: 'link',
        link_kind: 'ad_library',
        confirmed_at: '2026-10-30T08:30:00Z',
      },
      observed_page_name: null,
      last_scanned_at: '2026-10-30T09:05:00Z',
      ...overrides,
    });
  const emptyTarget = (overrides: Record<string, unknown> = {}) => ({
    origin: 'page:cmp_wrong',
    kind: 'page',
    competitor_id: 'cmp_wrong',
    role: 'competitor',
    page_id: WRONG,
    page_name: null,
    observed_page_name: null,
    confirmation_revision: 1,
    status: 'completed',
    pages_planned: 1,
    pages_fetched: 1,
    ads_seen: 0,
    exhausted: true,
    ...overrides,
  });
  function scanned(targets: unknown[], items = [linked()]) {
    query = { competitor_scan: 'scan_1' };
    gateway.listCompetitors.mockResolvedValue(ok({ items }));
    gateway.getCompetitorScan.mockResolvedValue(
      ok(scan({ country: 'CA', targets, observation_count: 0 }))
    );
  }

  const scrollIntoView = jest.fn();
  beforeEach(() => {
    // jsdom lays nothing out, so it has no `scrollIntoView`.
    Element.prototype.scrollIntoView = scrollIntoView;
  });
  afterEach(() => {
    delete (Element.prototype as Partial<Element>).scrollIntoView;
  });

  it('says the ID may be wrong, links the page in the Ad Library and changes the page from the scan', async () => {
    scanned([emptyTarget()]);
    renderArea();
    const hint = await screen.findByTestId('competitor-scan-target-empty');
    expect(hint).toHaveTextContent(NO_ADS);
    const library = within(hint).getByRole('link', {
      name: 'View in the Meta Ad Library',
    });
    // The scan's market, the way the backend links discovered advertisers.
    expect(library).toHaveAttribute(
      'href',
      `https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=CA&view_all_page_id=${WRONG}`
    );
    expect(library).toHaveAttribute('target', '_blank');
    expect(library).toHaveAttribute('rel', 'noopener noreferrer');

    const row = screen.getByTestId('competitor-row');
    expect(within(row).queryByTestId('competitor-resolve-panel')).toBeNull();
    fireEvent.click(within(hint).getByRole('button', { name: 'Change page' }));
    expect(
      await within(row).findByTestId('competitor-resolve-panel')
    ).toBeInTheDocument();
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
    expect(scrollIntoView.mock.contexts[0]).toBe(row);
  });

  it('says it in zh-CN, on the scan and on the list row', async () => {
    const i18n = getI18n().cloneInstance({
      lng: 'zh-CN',
      fallbackLng: false,
      forkResourceStore: true,
    });
    i18n.addResourceBundle('zh-CN', 'competitors', competitorsZh);
    i18n.addResourceBundle('zh-CN', 'common', commonZh);
    void i18n.changeLanguage('zh-CN');
    scanned([emptyTarget()]);
    renderArea('US', (node) => (
      <I18nextProvider i18n={i18n}>{node}</I18nextProvider>
    ));
    const hint = await screen.findByTestId('competitor-scan-target-empty');
    expect(hint).toHaveTextContent(
      '没有返回广告。可能这个主页现在没有在投广告，也可能主页 ID 填错了。'
    );
    expect(
      within(hint).getByRole('link', { name: '在 Meta 广告库查看' })
    ).toBeInTheDocument();
    expect(
      within(hint).getByRole('button', { name: '更换主页' })
    ).toBeInTheDocument();
    expect(screen.getByTestId('competitor-name-after-scan')).toHaveTextContent(
      '已扫描，但没有看到这个主页的广告，所以没有名称；如果主页 ID 有误，请更换主页。'
    );
  });

  it.each([
    ['its observations name the page', { observed_page_name: 'Brightvolt' }],
    ['its confirmation names the page', { page_name: 'Brightvolt' }],
    ['the page answered ads', { ads_seen: 3 }],
    ['the page is still fetching', { status: 'fetching', pages_fetched: 0 }],
    [
      'the page failed',
      {
        status: 'failed',
        pages_fetched: 0,
        failure: { code: 'market_data_insufficient_credits' },
      },
    ],
    ['the page has not started', { status: 'pending', pages_fetched: 0 }],
    [
      'it is a keyword',
      {
        origin: 'keyword:NACS adapter',
        kind: 'keyword',
        keyword: 'NACS adapter',
        competitor_id: null,
        role: null,
        page_id: null,
        confirmation_revision: null,
      },
    ],
  ])('says nothing more when %s', async (_, overrides) => {
    scanned([emptyTarget(overrides)]);
    renderArea();
    await screen.findByTestId('competitor-scan-target');
    expect(screen.queryByTestId('competitor-scan-target-empty')).toBeNull();
  });

  it('offers no "Change page" for a competitor no longer on the list', async () => {
    scanned([emptyTarget()], []);
    renderArea();
    const hint = await screen.findByTestId('competitor-scan-target-empty');
    expect(hint).toHaveTextContent(NO_ADS);
    expect(
      within(hint).getByRole('link', { name: 'View in the Meta Ad Library' })
    ).toBeInTheDocument();
    expect(within(hint).queryByRole('button')).toBeNull();
  });

  it('words the list row by whether a scan ran since the page was confirmed', async () => {
    gateway.listCompetitors.mockResolvedValue(
      ok({
        items: [
          linked(),
          // Changed to another page after the last scan: not scanned yet.
          linked({
            competitor_id: 'cmp_changed',
            confirmation: {
              revision: 2,
              platform: 'meta',
              page_id: '100000000000002',
              page_name: null,
              source: 'link',
              link_kind: 'ad_library',
              confirmed_at: '2026-10-30T10:00:00Z',
            },
          }),
          linked({ competitor_id: 'cmp_new', last_scanned_at: null }),
        ],
      })
    );
    renderArea();
    await waitFor(() =>
      expect(screen.getAllByTestId('competitor-row')).toHaveLength(3)
    );
    const [scannedRow, changedRow, newRow] =
      screen.getAllByTestId('competitor-row');
    expect(
      within(scannedRow).getByTestId('competitor-name-after-scan')
    ).toHaveTextContent(NO_ADS_ROW);
    expect(
      within(changedRow).getByTestId('competitor-name-after-scan')
    ).toHaveTextContent(AFTER_SCAN);
    expect(
      within(newRow).getByTestId('competitor-name-after-scan')
    ).toHaveTextContent(AFTER_SCAN);
  });
});
