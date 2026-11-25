/**
 * @jest-environment jsdom
 */
import type { ReactNode } from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { SWRConfig } from 'swr';
import { TestHistory } from '@/components/tests/TestHistory';
import type { WorkspaceProduct } from '@/lib/products/workspace-products';
import type {
  EarlierTest,
  HistoryRound,
  HistoryTest,
  TestHistory as History,
} from '@/lib/tests/test-history';

/**
 * "Test history" (design §5.10, §5.11, #258): the product's Tests view lists
 * its tests and its earlier tests, each round with its reports (when, by
 * whom); a test opens on its own page, an earlier one on the earlier tests
 * page. No internal id shows.
 */
jest.mock('next/router', () => ({
  useRouter: () => ({
    asPath: '/w/acme/p/prod_home/tests',
    query: {},
    push: jest.fn(),
    replace: jest.fn(),
  }),
}));
let mockRole: 'OWNER' | 'MEMBER' = 'MEMBER';
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: { slug: 'acme', workspaceId: 'ws_1', role: mockRole },
  }),
}));
jest.mock('next-auth/react', () => ({
  useSession: () => ({ data: { user: { id: 'user_self' } } }),
}));
const mockMembers = jest.fn();
jest.mock('@/lib/fetcher', () => ({
  __esModule: true,
  default: (url: string) => mockMembers(url),
}));
const mockGateway = {
  listMarketingTests: jest.fn(),
};
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));

function ok<T>(data: T) {
  return { kind: 'ok' as const, status: 200, data, meta: {} };
}

function problem(code: string, status = 503) {
  return {
    kind: 'problem' as const,
    status,
    problem: {
      type: 'about:blank',
      title: code,
      status,
      detail: '',
      instance: '',
      code,
      trace_id: 't',
      retryable: false,
      field_errors: [],
    },
    meta: {},
  };
}

const round = (extra: Partial<HistoryRound> = {}): HistoryRound => ({
  id: 'mr_1',
  ordinal: 1,
  state: 'awaiting_data',
  start_date: '2026-11-26',
  end_date: '2026-12-02',
  effective_start_date: '2026-11-26',
  effective_end_date: '2026-12-02',
  reports: [],
  ...extra,
});

const RUNNING: HistoryTest = {
  id: 'mt_running',
  source_project_id: 'mp_ideas',
  comparison: 'claims',
  plan_summary: {
    target_cost: '30.00',
    round_budget: '1200.00',
    duration_days: 7,
    currency: 'USD',
    landing_page: 'https://acmecharge.example/home',
  },
  rounds: [
    round({
      effective_start_date: '2026-11-27',
      reports: [
        {
          batch_id: 'mmb_1',
          kind: 'upload',
          replaces_batch_id: null,
          uploaded_at: '2026-11-28T10:00:00Z',
          uploaded_by: 'user_self',
          row_count: 14,
        },
        {
          batch_id: 'mmb_2',
          kind: 'upload',
          replaces_batch_id: null,
          uploaded_at: '2026-11-30T10:00:00Z',
          uploaded_by: 'user_other',
          row_count: 1,
        },
      ],
    }),
  ],
  current_round_id: 'mr_1',
  created_at: '2026-11-25T09:00:00Z',
};

const CANCELLED: HistoryTest = {
  ...RUNNING,
  id: 'mt_cancelled',
  comparison: 'single',
  rounds: [round({ id: 'mr_0', state: 'closed', reports: [] })],
  current_round_id: null,
  created_at: '2026-11-20T09:00:00Z',
};

const EARLIER: EarlierTest = {
  id: 'mp_earlier',
  state: 'plan_confirmed',
  product_name: 'Acme Charge Home',
  rounds: [
    round({
      id: 'mr_e1',
      state: 'closed',
      start_date: '2026-10-01',
      end_date: '2026-10-07',
      effective_start_date: '2026-10-01',
      effective_end_date: '2026-10-07',
      reports: [
        {
          batch_id: 'mmb_e1',
          kind: 'upload',
          replaces_batch_id: null,
          uploaded_at: '2026-10-08T10:00:00Z',
          uploaded_by: 'user_owner',
          row_count: 21,
        },
        {
          batch_id: 'mmb_e2',
          kind: 'correction',
          replaces_batch_id: 'mmb_e1',
          uploaded_at: '2026-10-09T10:00:00Z',
          uploaded_by: null,
          row_count: 21,
        },
      ],
    }),
  ],
  created_at: '2026-09-30T09:00:00Z',
};

const history = (extra: Partial<History> = {}): History => ({
  product_id: 'prod_home',
  tests: [RUNNING, CANCELLED],
  earlier_tests: [EARLIER],
  ...extra,
});

const PRODUCT: WorkspaceProduct = {
  productId: 'prod_home',
  name: 'Acme Charge Home',
  ideas: { projectId: 'mp_ideas', adCount: 6, keptCount: 2 },
  test: null,
  signals: {
    needsAttention: false,
    reportMissing: false,
    conclusionUnseen: false,
    testInProgress: false,
    keptUntested: 2,
    draftsUnhandled: 4,
    hasIdeas: true,
  },
  step: 'kept_untested',
  stepCount: 2,
};

function renderHistory(product: WorkspaceProduct = PRODUCT) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      {children}
    </SWRConfig>
  );
  return render(<TestHistory product={product} />, { wrapper });
}

beforeEach(() => {
  mockRole = 'MEMBER';
  mockGateway.listMarketingTests.mockReset();
  mockMembers.mockReset();
  mockMembers.mockResolvedValue({
    data: [
      { userId: 'user_owner', user: { name: 'Avery Example', email: '' } },
    ],
  });
});

describe('TestHistory (#258)', () => {
  it('lists the tests and the earlier tests, each round with its reports', async () => {
    mockGateway.listMarketingTests.mockResolvedValue(ok(history()));
    renderHistory();

    expect(
      await screen.findByRole('heading', { name: 'Test history' })
    ).toBeInTheDocument();
    expect(mockGateway.listMarketingTests).toHaveBeenCalledWith({
      workspaceSlug: 'acme',
      productId: 'prod_home',
    });

    const tests = screen.getAllByTestId('history-test');
    expect(tests).toHaveLength(2);
    const [running, cancelled] = tests;
    expect(running).toHaveAttribute('data-status', 'running');
    expect(running).toHaveTextContent('In progress');
    expect(running).toHaveTextContent('Claims against each other');
    expect(running).toHaveTextContent(
      'a success at $30 or less per order · $1,200 a round · 7 days'
    );
    // The start moved a day: both the days it runs and the planned ones show.
    expect(running).toHaveTextContent(
      'Round 1 · November 27, 2026 – December 2, 2026 · reports coming in'
    );
    expect(running).toHaveTextContent(
      'Planned November 26, 2026 – December 2, 2026'
    );
    const reports = within(running).getAllByTestId('history-report');
    expect(reports).toHaveLength(2);
    expect(reports[0]).toHaveTextContent('You · Report sent · 14 rows');
    expect(reports[1]).toHaveTextContent(
      'Another member · Report sent · 1 row'
    );
    expect(within(running).getByTestId('history-open-test')).toHaveAttribute(
      'href',
      '/w/acme/p/prod_home/tests/mt_running'
    );

    expect(cancelled).toHaveAttribute('data-status', 'ended');
    expect(cancelled).toHaveTextContent('One ad against the goal');
    expect(cancelled).toHaveTextContent('No report yet.');

    const earlier = screen.getByTestId('history-earlier-test');
    expect(earlier).toHaveTextContent('Acme Charge Home');
    expect(earlier).toHaveTextContent('Ended');
    const earlierReports = within(earlier).getAllByTestId('history-report');
    // A Member cannot read the member list: another person is "another member".
    expect(earlierReports[0]).toHaveTextContent(
      'Another member · Report sent · 21 rows'
    );
    // A record that names nobody leaves the person out.
    expect(earlierReports[1]).toHaveTextContent('Correction sent · 21 rows');
    expect(earlierReports[1]).not.toHaveTextContent('member');
    expect(within(earlier).getByTestId('history-open-earlier')).toHaveAttribute(
      'href',
      '/w/acme/experiments?test=mp_earlier'
    );
    expect(mockMembers).not.toHaveBeenCalled();

    // The one main action opens the test in progress.
    expect(screen.getByTestId('history-open-running')).toHaveAttribute(
      'href',
      '/w/acme/p/prod_home/tests/mt_running'
    );
    expect(screen.queryByTestId('history-take-to-test')).toBeNull();

    // No internal id reaches the page.
    expect(screen.queryByText(/mt_|mr_|mmb_|mp_|user_/)).toBeNull();
  });

  it('names the members an Owner can read', async () => {
    mockRole = 'OWNER';
    mockGateway.listMarketingTests.mockResolvedValue(ok(history()));
    renderHistory();
    const earlier = await screen.findByTestId('history-earlier-test');
    expect(mockMembers).toHaveBeenCalledWith('/api/teams/acme/members');
    expect(
      await within(earlier).findByText(/Avery Example · Report sent · 21 rows/)
    ).toBeInTheDocument();
  });

  it('opens a test still in its checks on the take-to-test screen', async () => {
    mockGateway.listMarketingTests.mockResolvedValue(
      ok(
        history({
          tests: [{ ...RUNNING, rounds: [round({ state: 'reviewing' })] }],
          earlier_tests: [],
        })
      )
    );
    renderHistory();
    const card = await screen.findByTestId('history-test');
    expect(card).toHaveAttribute('data-status', 'setting_up');
    expect(card).toHaveTextContent('Being set up');
    expect(card).toHaveTextContent('checking the ads');
    expect(within(card).getByTestId('history-open-test')).toHaveTextContent(
      'Carry on'
    );
    expect(within(card).getByTestId('history-open-test')).toHaveAttribute(
      'href',
      '/w/acme/p/prod_home/take-to-test'
    );
    expect(screen.getByTestId('history-open-running')).toHaveAttribute(
      'href',
      '/w/acme/p/prod_home/take-to-test'
    );
    expect(screen.queryByTestId('history-earlier')).toBeNull();
  });

  it('with no test in progress, offers taking the kept ads to a test', async () => {
    mockGateway.listMarketingTests.mockResolvedValue(
      ok(history({ tests: [CANCELLED] }))
    );
    renderHistory();
    expect(await screen.findByTestId('history-take-to-test')).toHaveAttribute(
      'href',
      '/w/acme/p/prod_home/take-to-test'
    );
    expect(screen.queryByTestId('history-open-running')).toBeNull();
  });

  it('with no test yet, points at the ad ideas', async () => {
    mockGateway.listMarketingTests.mockResolvedValue(
      ok(history({ tests: [], earlier_tests: [] }))
    );
    renderHistory({
      ...PRODUCT,
      ideas: { projectId: 'mp_ideas', adCount: 6, keptCount: 0 },
    });
    const empty = await screen.findByTestId('history-empty');
    expect(empty).toHaveTextContent('No tests yet.');
    expect(
      within(empty).getByRole('link', { name: 'Go to ad ideas' })
    ).toHaveAttribute('href', '/w/acme/p/prod_home/ideas');
    // Nothing kept: no "take to test" button.
    expect(screen.queryByTestId('history-take-to-test')).toBeNull();
  });

  it('a product with no card of its own has no tests', async () => {
    mockGateway.listMarketingTests.mockResolvedValue(
      problem('product_not_found', 404)
    );
    renderHistory();
    expect(await screen.findByTestId('history-empty')).toBeInTheDocument();
  });

  it('says when the list cannot be read and reads it again', async () => {
    mockGateway.listMarketingTests.mockResolvedValueOnce(
      problem('domain_unavailable')
    );
    renderHistory();
    expect(await screen.findByRole('alert')).toHaveTextContent(
      "The tests couldn't be read."
    );
    mockGateway.listMarketingTests.mockResolvedValue(ok(history()));
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await screen.findAllByTestId('history-test')).toHaveLength(2);
    expect(mockGateway.listMarketingTests).toHaveBeenCalledTimes(2);
  });
});
