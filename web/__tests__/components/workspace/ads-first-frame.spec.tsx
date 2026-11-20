/**
 * @jest-environment jsdom
 */
import { fireEvent, render, screen, within } from '@testing-library/react';
import { WorkspaceOverview } from '@/components/workspace/WorkspaceOverview';
import { ProductFrame } from '@/components/workspace/ProductFrame';
import { WorkspaceMenu } from '@/components/workspace/WorkspaceMenu';
import env from '@/lib/env';
import type { UseWorkspaceProductsResult } from '@/hooks/products/useWorkspaceProducts';
import type {
  NextStepKind,
  WorkspaceProduct,
} from '@/lib/products/workspace-products';

const mockRouter = {
  asPath: '/w/acme/overview',
  query: { slug: 'acme' },
  replace: jest.fn().mockResolvedValue(true),
};
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme' } }),
}));
jest.mock('@/components/workspace/WorkspaceSelector', () => ({
  WorkspaceSelector: ({ id }: { id?: string }) => (
    <select id={id} aria-label="Switch Workspace" />
  ),
}));
jest.mock(
  '@/components/shared/shell/Brand',
  () =>
    function MockBrand() {
      return null;
    }
);
jest.mock('@/components/shared/LanguageSwitcher', () => ({
  LanguageSwitcher: () => (
    <select data-testid="language-switcher" aria-label="Language" />
  ),
}));
jest.mock('hooks/useCanAccess', () => ({
  __esModule: true,
  default: () => ({ canAccess: () => true }),
}));

function workspaceProduct(
  productId: string,
  name: string | null,
  step: NextStepKind,
  stepCount = 0,
  ideas: WorkspaceProduct['ideas'] = null
): WorkspaceProduct {
  return {
    productId,
    name,
    ideas,
    signals: {
      needsAttention: step === 'attention',
      reportMissing: step === 'report_missing',
      conclusionUnseen: step === 'conclusion_unseen',
      keptUntested: step === 'kept_untested' ? stepCount : 0,
      draftsUnhandled: step === 'drafts_unhandled' ? stepCount : 0,
      hasIdeas: ideas !== null,
    },
    step,
    stepCount,
  };
}

function productsResult(
  products: WorkspaceProduct[],
  extra: Partial<UseWorkspaceProductsResult> = {}
): UseWorkspaceProductsResult {
  return {
    products,
    isLoading: false,
    problem: undefined,
    find: (id) => products.find((p) => p.productId === id),
    refresh: jest.fn().mockResolvedValue(undefined),
    ...extra,
  };
}

let mockProducts = productsResult([]);
jest.mock('@/hooks/products/useWorkspaceProducts', () => ({
  useWorkspaceProducts: () => mockProducts,
}));
type Unfinished = {
  batches: {
    id: string;
    state: 'reading' | 'needs_attention';
    needs_attention: boolean;
    reason: 'several_models' | null;
    source_url: string | null;
    created_at: string;
    updated_at: string;
  }[];
  loaded: boolean;
  failed: boolean;
};
let mockUnfinished: Unfinished = { batches: [], loaded: true, failed: false };
jest.mock('@/hooks/ideas/useUnfinishedFirstBatches', () => ({
  useUnfinishedFirstBatches: () => mockUnfinished,
}));

const ALL_STEPS: [NextStepKind, string, string, string][] = [
  [
    'attention',
    'Something went wrong and is waiting for you.',
    'Sort it out',
    '/w/acme/p/prod_1/ideas',
  ],
  [
    'report_missing',
    'The test is running and its report is still missing.',
    'Upload the report',
    '/w/acme/p/prod_2/tests',
  ],
  [
    'conclusion_unseen',
    "There is a result you haven't looked at yet.",
    'See the result',
    '/w/acme/p/prod_3/results',
  ],
  [
    'kept_untested',
    "You kept 4 ads that haven't been tested yet.",
    'Review kept ads',
    '/w/acme/p/prod_4/ideas',
  ],
  [
    'drafts_unhandled',
    '6 ad drafts, none kept yet.',
    'See the ads',
    '/w/acme/p/prod_5/ideas',
  ],
  [
    'preparing',
    'The ads are still being prepared.',
    'See progress',
    '/w/acme/p/prod_6/ideas',
  ],
  [
    'no_ideas',
    'This product has no ad ideas yet.',
    'Open the product',
    '/w/acme/p/prod_7/ideas',
  ],
];

describe('WorkspaceOverview (#194)', () => {
  it('shows one card and exactly one main button per product', () => {
    mockProducts = productsResult(
      ALL_STEPS.map(([step], index) =>
        workspaceProduct(
          `prod_${index + 1}`,
          `Acme Charge ${index + 1}`,
          step,
          step === 'kept_untested' ? 4 : step === 'drafts_unhandled' ? 6 : 0
        )
      )
    );
    render(<WorkspaceOverview />);
    expect(
      screen.getByRole('heading', { level: 1, name: 'What to do next' })
    ).toBeInTheDocument();

    const cards = screen.getAllByTestId('overview-product');
    expect(cards).toHaveLength(ALL_STEPS.length);
    cards.forEach((card, index) => {
      const [step, line, action, href] = ALL_STEPS[index];
      const scoped = within(card);
      expect(card).toHaveAttribute('data-step', step);
      expect(scoped.getByText(line)).toBeInTheDocument();
      const controls = [
        ...scoped.queryAllByRole('link'),
        ...scoped.queryAllByRole('button'),
      ];
      expect(controls).toHaveLength(1);
      expect(controls[0]).toHaveTextContent(action);
      expect(controls[0]).toHaveAttribute('href', href);
    });

    expect(screen.getByTestId('overview-new-product')).toHaveAttribute(
      'href',
      '/w/acme/products/new'
    );
  });

  it('names an unnamed product as such, never by its id', () => {
    mockProducts = productsResult([
      workspaceProduct('prod_secret', null, 'no_ideas'),
    ]);
    render(<WorkspaceOverview />);
    expect(
      screen.getByRole('heading', { level: 2, name: 'Unnamed product' })
    ).toBeInTheDocument();
    expect(screen.queryByText(/prod_secret/)).toBeNull();
  });

  it('offers New product when the Workspace has none yet', () => {
    mockProducts = productsResult([]);
    render(<WorkspaceOverview />);
    expect(screen.getByTestId('overview-empty')).toHaveTextContent(
      'No products yet'
    );
    expect(screen.getByRole('link', { name: 'New product' })).toHaveAttribute(
      'href',
      '/w/acme/products/new'
    );
  });

  it('shows no cards while loading, and a retry when a list failed', () => {
    mockProducts = productsResult([], { isLoading: true });
    const { rerender } = render(<WorkspaceOverview />);
    expect(screen.getByRole('status')).toHaveTextContent(
      'Loading your products…'
    );

    const refresh = jest.fn().mockResolvedValue(undefined);
    mockProducts = productsResult(
      [workspaceProduct('prod_1', 'Acme Charge Home', 'no_ideas')],
      {
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
      }
    );
    rerender(<WorkspaceOverview />);
    expect(screen.queryAllByTestId('overview-product')).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(refresh).toHaveBeenCalled();
  });
});

describe('WorkspaceOverview: nothing yet, or a reading left behind (#204)', () => {
  beforeEach(() => {
    env.adsFirstEnabled = true;
    mockRouter.replace.mockClear();
    mockProducts = productsResult([]);
    mockUnfinished = { batches: [], loaded: true, failed: false };
  });
  afterEach(() => {
    env.adsFirstEnabled = false;
    mockUnfinished = { batches: [], loaded: true, failed: false };
  });

  it('an empty Workspace opens the start page (design §5.1)', () => {
    render(<WorkspaceOverview />);
    expect(mockRouter.replace).toHaveBeenCalledWith('/w/acme/start');
  });

  it('waits for both lists before deciding the Workspace is empty', () => {
    mockUnfinished = { batches: [], loaded: false, failed: false };
    const { rerender } = render(<WorkspaceOverview />);
    mockProducts = productsResult([], { isLoading: true });
    mockUnfinished = { batches: [], loaded: true, failed: false };
    rerender(<WorkspaceOverview />);
    expect(mockRouter.replace).not.toHaveBeenCalled();
  });

  it('a reading left before its product existed is listed instead, with the way back', () => {
    mockUnfinished = {
      loaded: true,
      failed: false,
      batches: [
        {
          id: 'mfb_9',
          state: 'reading',
          needs_attention: false,
          reason: null,
          source_url: 'https://acmecharge.example/products/home',
          created_at: '2026-11-17T08:00:00+00:00',
          updated_at: '2026-11-17T08:00:00+00:00',
        },
      ],
    };
    render(<WorkspaceOverview />);
    expect(mockRouter.replace).not.toHaveBeenCalled();
    const left = screen.getByTestId('unfinished-readings');
    expect(left).toHaveTextContent('acmecharge.example/products/home');
    expect(
      within(left).getByRole('link', { name: 'See progress' })
    ).toHaveAttribute('href', '/w/acme/start/mfb_9');
    expect(screen.getByTestId('overview-new-product')).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/mfb_/);
  });

  it('a Workspace with products stays on the overview', () => {
    mockProducts = productsResult([
      workspaceProduct('prod_1', 'Acme Charge Home', 'drafts_unhandled', 6),
    ]);
    render(<WorkspaceOverview />);
    expect(mockRouter.replace).not.toHaveBeenCalled();
    expect(screen.getAllByTestId('overview-product')).toHaveLength(1);
  });

  it('with the switch off nothing changes', () => {
    env.adsFirstEnabled = false;
    render(<WorkspaceOverview />);
    expect(mockRouter.replace).not.toHaveBeenCalled();
    expect(screen.getByTestId('overview-empty')).toBeInTheDocument();
  });
});

describe('ProductFrame (#194)', () => {
  const home = workspaceProduct(
    'prod_home',
    'Acme Charge Home',
    'drafts_unhandled',
    6,
    {
      projectId: 'mp_1',
      adCount: 6,
      keptCount: 0,
    }
  );

  it('shows the product name and its four views, the open one marked', () => {
    mockProducts = productsResult([home]);
    const body = jest.fn(() => <p data-testid="frame-body" />);
    render(
      <ProductFrame productId="prod_home" view="results">
        {body}
      </ProductFrame>
    );
    expect(
      screen.getByRole('heading', { level: 1, name: 'Acme Charge Home' })
    ).toBeInTheDocument();
    const views = within(
      screen.getByRole('navigation', { name: 'Product views' })
    ).getAllByRole('link');
    expect(
      views.map((link) => [link.textContent, link.getAttribute('href')])
    ).toEqual([
      ['Ad ideas', '/w/acme/p/prod_home/ideas'],
      ['Tests', '/w/acme/p/prod_home/tests'],
      ['Results', '/w/acme/p/prod_home/results'],
      ['Product profile', '/w/acme/p/prod_home/profile'],
    ]);
    expect(screen.getByRole('link', { name: 'Results' })).toHaveAttribute(
      'aria-current',
      'page'
    );
    expect(screen.getByRole('link', { name: 'Tests' })).not.toHaveAttribute(
      'aria-current'
    );
    expect(body).toHaveBeenCalledWith(home);
  });

  it('says so when the product is not in this Workspace', () => {
    mockProducts = productsResult([home]);
    render(
      <ProductFrame productId="prod_elsewhere" view="ideas">
        {() => <p data-testid="frame-body" />}
      </ProductFrame>
    );
    expect(
      screen.getByRole('heading', { name: "This product wasn't found" })
    ).toBeInTheDocument();
    expect(screen.queryByTestId('frame-body')).toBeNull();
    expect(
      screen.getByRole('link', { name: 'Back to the overview' })
    ).toHaveAttribute('href', '/w/acme/overview');
  });

  it('waits for the list before deciding the product is missing', () => {
    mockProducts = productsResult([], { isLoading: true });
    render(
      <ProductFrame productId="prod_home" view="ideas">
        {() => <p data-testid="frame-body" />}
      </ProductFrame>
    );
    expect(screen.getByRole('status')).toHaveTextContent(
      'Loading the product…'
    );
    expect(screen.queryByTestId('product-frame-not-found')).toBeNull();
  });
});

describe('WorkspaceMenu: the phone menu (#194)', () => {
  beforeEach(() => {
    env.adsFirstEnabled = true;
    mockRouter.asPath = '/w/acme/overview';
    mockProducts = productsResult([
      workspaceProduct('prod_home', 'Acme Charge Home', 'drafts_unhandled', 6),
    ]);
  });
  afterEach(() => {
    env.adsFirstEnabled = false;
    document.body.style.overflow = '';
  });

  it('opens the same navigation as the sidebar, and closes with Escape', () => {
    render(<WorkspaceMenu slug="acme" />);
    const opener = screen.getByRole('button', { name: 'Open menu' });
    expect(opener).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('dialog')).toBeNull();

    fireEvent.click(opener);
    const dialog = screen.getByRole('dialog', { name: 'Menu' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(opener).toHaveAttribute('aria-expanded', 'true');
    expect(
      within(dialog).getByRole('button', { name: 'Close menu' })
    ).toHaveFocus();
    expect(
      within(dialog).getByRole('link', { name: 'Overview' })
    ).toHaveAttribute('href', '/w/acme/overview');
    expect(
      within(dialog).getByRole('link', { name: /Acme Charge Home/ })
    ).toHaveAttribute('href', '/w/acme/p/prod_home/ideas');
    expect(within(dialog).getByTestId('workspace-settings-link')).toBeVisible();
    expect(document.body.style.overflow).toBe('hidden');

    fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(opener).toHaveFocus();
    expect(document.body.style.overflow).toBe('');
  });

  it('keeps Tab inside the open menu', () => {
    render(<WorkspaceMenu slug="acme" />);
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }));
    const dialog = screen.getByRole('dialog');
    const close = within(dialog).getByRole('button', { name: 'Close menu' });
    // The language choice ends the menu, as it ends the sidebar.
    const language = within(dialog).getByTestId('language-switcher');
    language.focus();
    fireEvent.keyDown(language, { key: 'Tab' });
    expect(close).toHaveFocus();
    fireEvent.keyDown(close, { key: 'Tab', shiftKey: true });
    expect(language).toHaveFocus();
  });

  it('closes after following a link, and when tapping outside', () => {
    const { rerender } = render(<WorkspaceMenu slug="acme" />);
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }));
    mockRouter.asPath = '/w/acme/p/prod_home/ideas';
    rerender(<WorkspaceMenu slug="acme" />);
    expect(screen.queryByRole('dialog')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }));
    fireEvent.click(screen.getByTestId('workspace-menu-backdrop'));
    expect(screen.queryByRole('dialog')).toBeNull();
  });
});
