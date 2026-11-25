/** @jest-environment jsdom */
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { I18nextProvider, getI18n } from 'react-i18next';
import { SWRConfig } from 'swr';
import type { ReactElement } from 'react';
import { ProductCompetitors } from '@/components/competitors/ProductCompetitors';
import type { UseWorkspaceProductsResult } from '@/hooks/products/useWorkspaceProducts';
import type { WorkspaceProduct } from '@/lib/products/workspace-products';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import competitorsZh from '../../../locales/zh-CN/competitors.json';
import commonZh from '../../../locales/zh-CN/common.json';
import {
  PRODUCT_ID,
  accepted,
  confirmed,
  created,
  ok,
  problem,
  scan,
  scanQuote,
  snapshotSummary,
} from './fixtures';

jest.mock('@/lib/workspace-bootstrap', () => ({
  getWorkspacePageProps: jest.fn(() => jest.fn()),
}));
jest.mock('@/components/layouts/WorkspaceLayout', () => ({
  __esModule: true,
  default: ({ children }: { children: unknown }) => children,
}));

let query: Record<string, string> = {};
const replace = jest.fn().mockImplementation(async ({ query: next }) => {
  query = next;
  return true;
});
jest.mock('next/router', () => ({
  useRouter: () => ({ query, replace, pathname: '/w/[slug]/competitors' }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: { workspaceId: 'ws_alpha', slug: 'alpha', role: 'OWNER' },
  }),
}));
const gateway = {
  getMarketingIdeas: jest.fn(),
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
  listCompetitorScanAds: jest.fn(),
  flipCompetitorAdRelevance: jest.fn(),
  listCompetitorAdAssessments: jest.fn(),
  confirmCompetitorAdTags: jest.fn(),
  listCompetitorAdTags: jest.fn(),
  refreezeCompetitorScan: jest.fn(),
  getCompetitorScanSummary: jest.fn(),
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

let mockProducts: UseWorkspaceProductsResult;
jest.mock('@/hooks/products/useWorkspaceProducts', () => ({
  useWorkspaceProducts: () => mockProducts,
}));

// Loaded after the mocks: the page module registers its server props once,
// before `beforeEach` clears the mocks.
import CompetitorsPage from '../../../pages/w/[slug]/competitors';
const registered = [...jest.mocked(getWorkspacePageProps).mock.calls];

const OTHER_ID = 'prod_go';

function workspaceProduct(
  productId: string,
  name: string | null,
  projectId: string | null
): WorkspaceProduct {
  return {
    productId,
    name,
    ideas: projectId ? { projectId, adCount: 6, keptCount: 0 } : null,
    test: null,
    signals: {
      needsAttention: false,
      reportMissing: false,
      conclusionUnseen: false,
      testInProgress: false,
      keptUntested: 0,
      draftsUnhandled: projectId ? 6 : 0,
      hasIdeas: projectId !== null,
    },
    step: projectId ? 'drafts_unhandled' : 'no_ideas',
    stepCount: projectId ? 6 : 0,
  };
}

function productsResult(
  products: WorkspaceProduct[],
  extra: Partial<UseWorkspaceProductsResult> = {}
): UseWorkspaceProductsResult {
  return {
    products,
    isLoading: false,
    testsLoading: false,
    problem: undefined,
    find: (id) => products.find((p) => p.productId === id),
    refresh: jest.fn().mockResolvedValue(undefined),
    ...extra,
  };
}

const HOME = workspaceProduct(PRODUCT_ID, 'Acme Charge Home', 'mp_home');
const GO = workspaceProduct(OTHER_ID, 'Acme Charge Go', null);

/** A scan that froze a snapshot, as Controlled Mock would leave it. */
const frozen = () => scan({ execution_mode: 'mock', snapshot_id: 'snap_1' });

function tree(ui?: (node: ReactElement) => ReactElement) {
  const node = (
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <ProductCompetitors />
    </SWRConfig>
  );
  return ui ? ui(node) : node;
}

beforeEach(() => {
  jest.clearAllMocks();
  query = { slug: 'alpha', product: PRODUCT_ID };
  mockProducts = productsResult([HOME, GO]);
  gateway.getMarketingIdeas.mockResolvedValue(
    ok({ id: 'mp_home', product_id: PRODUCT_ID, market: 'US' })
  );
  gateway.listCompetitors.mockResolvedValue(ok({ items: [] }));
  gateway.listCompetitorScans.mockResolvedValue(ok({ items: [] }));
  gateway.listCompetitorScanAds.mockResolvedValue(ok({ items: [], total: 0 }));
  gateway.listCompetitorAdTags.mockResolvedValue(ok({ items: [] }));
  gateway.listCompetitorAdAssessments.mockResolvedValue(ok({ items: [] }));
  gateway.getCompetitorScanSummary.mockResolvedValue(
    problem(404, 'competitor_snapshot_not_found')
  );
  gateway.getProductCompetitorSettings.mockResolvedValue(
    ok({ product_id: PRODUCT_ID, competitor_snapshot_in_drafts: true })
  );
  gateway.previewCompetitorScan.mockImplementation(async ({ body }) =>
    ok(scanQuote({ execution_mode: body.execution_mode ?? 'byok' }))
  );
});

describe('/w/{slug}/competitors (#203)', () => {
  it('exists only while the ads-first switch is on', () => {
    // `adsFirstOnly` answers 404 while the switch is off
    // (workspace-bootstrap.spec.ts).
    expect(registered).toEqual([
      [
        {
          namespaces: ['common', 'library', 'products', 'competitors'],
          adsFirstOnly: true,
        },
      ],
    ]);
  });

  it('renders the product picker and its competitor area', async () => {
    render(
      <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
        <CompetitorsPage
          bootstrap={{
            workspaceId: 'ws_alpha',
            slug: 'alpha',
            displayName: 'Acme Charge',
            membershipId: 'mem_1',
            role: 'OWNER',
          }}
        />
      </SWRConfig>
    );
    expect(
      screen.getByRole('heading', { level: 1, name: 'Competitors' })
    ).toBeInTheDocument();
    expect(screen.getByLabelText('Show competitors for')).toHaveValue(
      PRODUCT_ID
    );
    expect(await screen.findByText(/No competitors yet/)).toBeInTheDocument();
    expect(screen.queryByTestId('competitors-placeholder')).toBeNull();
  });
});

describe('ProductCompetitors (#203)', () => {
  it('adds a competitor, scans it and shows the summary without a marketing test', async () => {
    const added = confirmed();
    gateway.createCompetitor.mockResolvedValue(created(added));
    gateway.createCompetitorScan.mockResolvedValue(
      accepted(
        scan({
          execution_mode: 'mock',
          status: 'queued',
          stage: null,
          steps: [],
          cost: null,
        })
      )
    );
    gateway.getCompetitorScan.mockResolvedValue(ok(frozen()));
    gateway.getCompetitorScanSummary.mockResolvedValue(ok(snapshotSummary()));
    render(tree());

    // No fold on its own page: the list is there at once.
    const area = await screen.findByTestId('competitors-area');
    expect(within(area).queryByRole('button', { expanded: true })).toBeNull();
    await screen.findByText(/No competitors yet/);
    expect(gateway.listCompetitors).toHaveBeenCalledWith(
      expect.objectContaining({ productId: PRODUCT_ID })
    );

    const link =
      'https://www.facebook.com/ads/library/?view_all_page_id=100000000000010';
    fireEvent.change(
      screen.getByLabelText('Page link, Ad Library link or name'),
      { target: { value: link } }
    );
    gateway.listCompetitors.mockResolvedValue(ok({ items: [added] }));
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    await screen.findByTestId('competitor-row');
    expect(gateway.createCompetitor).toHaveBeenCalledWith(
      expect.objectContaining({
        body: expect.objectContaining({ product_id: PRODUCT_ID, input: link }),
      })
    );

    // The scan's market starts from the product's.
    await screen.findByTestId('competitor-scan-quote');
    expect(screen.getByLabelText('Market')).toHaveValue('US');
    expect(
      screen.getByText("Taken from this product's market “US”.")
    ).toBeInTheDocument();
    expect(gateway.getMarketingIdeas).toHaveBeenCalledWith({
      workspaceSlug: 'alpha',
      projectId: 'mp_home',
    });

    fireEvent.click(
      await screen.findByRole('button', { name: 'Start scan · up to $0.8644' })
    );
    await waitFor(() =>
      expect(gateway.createCompetitorScan).toHaveBeenCalledWith(
        expect.objectContaining({
          body: expect.objectContaining({
            product_id: PRODUCT_ID,
            country: 'US',
          }),
        })
      )
    );
    // The scan joins the product in the URL; neither drops the other.
    expect(replace).toHaveBeenLastCalledWith(
      expect.objectContaining({
        pathname: '/w/[slug]/competitors',
        query: expect.objectContaining({
          product: PRODUCT_ID,
          competitor_scan: 'scan_1',
        }),
      }),
      undefined,
      expect.objectContaining({ shallow: true })
    );

    const summary = await screen.findByTestId('competitor-summary');
    await screen.findByTestId('competitor-summary-cluster');
    expect(summary).toHaveAttribute('data-snapshot-id', 'snap_1');
    // No test on screen, so no plan whose snapshot could be stale.
    expect(screen.queryByTestId('competitor-snapshot-stale')).toBeNull();
    // Controlled Mock data stays marked as such on this page too.
    expect(
      screen.getAllByTestId('competitor-simulated-badge').length
    ).toBeGreaterThan(0);
  });

  it('follows the product picked: its own competitors, no scan of the other', async () => {
    query = { slug: 'alpha', product: PRODUCT_ID, competitor_scan: 'scan_1' };
    gateway.listCompetitors.mockImplementation(async ({ productId }) =>
      ok({ items: productId === PRODUCT_ID ? [confirmed()] : [] })
    );
    gateway.getCompetitorScan.mockResolvedValue(ok(frozen()));
    const view = render(tree());
    await screen.findByTestId('competitor-row');
    await screen.findByTestId('competitor-scan-progress');

    fireEvent.change(screen.getByLabelText('Show competitors for'), {
      target: { value: OTHER_ID },
    });
    expect(replace).toHaveBeenLastCalledWith(
      {
        pathname: '/w/[slug]/competitors',
        query: { slug: 'alpha', product: OTHER_ID },
      },
      undefined,
      expect.objectContaining({ shallow: true })
    );
    view.rerender(tree());

    expect(await screen.findByText(/No competitors yet/)).toBeInTheDocument();
    expect(screen.getByLabelText('Show competitors for')).toHaveValue(OTHER_ID);
    expect(gateway.listCompetitors).toHaveBeenLastCalledWith(
      expect.objectContaining({ productId: OTHER_ID })
    );
    expect(gateway.listCompetitorScans).toHaveBeenLastCalledWith(
      expect.objectContaining({ productId: OTHER_ID })
    );
    expect(screen.queryByTestId('competitor-row')).toBeNull();
    expect(screen.queryByTestId('competitor-scan-progress')).toBeNull();
    // A product made before the redesign has no ad ideas to read a market from.
    expect(
      gateway.getMarketingIdeas.mock.calls.map(([params]) => params.projectId)
    ).toEqual(['mp_home']);
  });

  it('names the first product in the URL when none, or an unknown one, is asked for', async () => {
    query = {
      slug: 'alpha',
      product: 'prod_elsewhere',
      competitor_scan: 'scan_9',
    };
    const view = render(tree());
    expect(screen.getByLabelText('Show competitors for')).toHaveValue(
      PRODUCT_ID
    );
    // Until the URL names the product shown, its area stays closed.
    expect(screen.queryByTestId('competitors-area')).toBeNull();
    await waitFor(() =>
      expect(replace).toHaveBeenCalledWith(
        {
          pathname: '/w/[slug]/competitors',
          query: { slug: 'alpha', product: PRODUCT_ID },
        },
        undefined,
        expect.objectContaining({ shallow: true })
      )
    );
    view.rerender(tree());
    await screen.findByText(/No competitors yet/);
    expect(gateway.getCompetitorScan).not.toHaveBeenCalled();

    replace.mockClear();
    query = { slug: 'alpha' };
    view.rerender(tree());
    await waitFor(() =>
      expect(replace).toHaveBeenCalledWith(
        expect.objectContaining({
          query: { slug: 'alpha', product: PRODUCT_ID },
        }),
        undefined,
        expect.objectContaining({ shallow: true })
      )
    );
  });

  it('lets the user choose the market of a product without one', async () => {
    query = { slug: 'alpha', product: OTHER_ID };
    gateway.listCompetitors.mockResolvedValue(
      ok({ items: [confirmed({ product_id: OTHER_ID })] })
    );
    render(tree());
    await screen.findByTestId('competitor-scan-quote');
    expect(screen.getByLabelText('Market')).toHaveValue('US');
    expect(
      screen.getByText(
        'One country per scan. Only ads active there are fetched.'
      )
    ).toBeInTheDocument();
    expect(gateway.getMarketingIdeas).not.toHaveBeenCalled();
  });

  it('asks for the country when the product market cannot be told', async () => {
    gateway.getMarketingIdeas.mockResolvedValue(
      ok({ id: 'mp_home', product_id: PRODUCT_ID, market: 'Southeast Asia' })
    );
    gateway.listCompetitors.mockResolvedValue(ok({ items: [confirmed()] }));
    render(tree());
    expect(
      await screen.findByText(
        /This product's market “Southeast Asia” is not a country/
      )
    ).toBeInTheDocument();
  });

  it('asks for a product first when the Workspace has none', () => {
    mockProducts = productsResult([]);
    render(tree());
    expect(screen.getByTestId('competitors-page-empty')).toHaveTextContent(
      'Competitors are kept per product.'
    );
    expect(screen.getByRole('link', { name: 'New product' })).toHaveAttribute(
      'href',
      '/w/alpha/products/new'
    );
    expect(screen.queryByTestId('competitors-area')).toBeNull();
    expect(gateway.listCompetitors).not.toHaveBeenCalled();
  });

  it('waits for the products, and offers a retry when they fail', () => {
    mockProducts = productsResult([], { isLoading: true });
    const view = render(tree());
    expect(screen.getByRole('status')).toHaveTextContent(
      'Loading your products…'
    );
    expect(replace).not.toHaveBeenCalled();

    const refresh = jest.fn().mockResolvedValue(undefined);
    mockProducts = productsResult([HOME], {
      refresh,
      problem: {
        type: 'about:blank',
        title: 'Unavailable',
        status: 503,
        detail: '',
        instance: '',
        code: 'domain_unavailable',
        trace_id: 'trace-1',
        retryable: true,
        field_errors: [],
      },
    });
    view.rerender(tree());
    expect(screen.getByRole('alert')).toHaveTextContent(
      "Couldn't load your products."
    );
    expect(screen.queryByTestId('competitors-area')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(refresh).toHaveBeenCalled();
  });

  it('reads in zh-CN', async () => {
    const i18n = getI18n().cloneInstance({
      lng: 'zh-CN',
      fallbackLng: false,
      forkResourceStore: true,
    });
    i18n.addResourceBundle('zh-CN', 'competitors', competitorsZh);
    i18n.addResourceBundle('zh-CN', 'common', commonZh);
    void i18n.changeLanguage('zh-CN');
    gateway.listCompetitors.mockResolvedValue(ok({ items: [confirmed()] }));
    render(
      tree((node) => <I18nextProvider i18n={i18n}>{node}</I18nextProvider>)
    );
    expect(
      screen.getByRole('heading', { level: 1, name: '竞品' })
    ).toBeInTheDocument();
    expect(screen.getByLabelText('看哪个产品的竞品')).toHaveValue(PRODUCT_ID);
    expect(
      await screen.findByText('取自这个产品投放的市场“US”。')
    ).toBeInTheDocument();
  });
});
