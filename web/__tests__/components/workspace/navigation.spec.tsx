/**
 * @jest-environment jsdom
 */
import { fireEvent, render, screen, within } from '@testing-library/react';
import {
  ADVANCED_NAV_STORAGE_KEY,
  WorkspaceNavigation,
} from '@/components/workspace/WorkspaceNavigation';
import env from '@/lib/env';
import type { UseWorkspaceProductsResult } from '@/hooks/products/useWorkspaceProducts';
import type {
  NextStepKind,
  WorkspaceProduct,
} from '@/lib/products/workspace-products';

function workspaceProduct(
  productId: string,
  name: string | null,
  step: NextStepKind,
  stepCount: number
): WorkspaceProduct {
  return {
    productId,
    name,
    ideas: null,
    signals: {
      needsAttention: false,
      reportMissing: false,
      conclusionUnseen: false,
      keptUntested: 0,
      draftsUnhandled: 0,
      hasIdeas: false,
    },
    step,
    stepCount,
  };
}

function productsResult(
  products: WorkspaceProduct[]
): UseWorkspaceProductsResult {
  return {
    products,
    isLoading: false,
    problem: undefined,
    find: (id) => products.find((p) => p.productId === id),
    refresh: jest.fn().mockResolvedValue(undefined),
  };
}

let mockProducts: UseWorkspaceProductsResult = productsResult([]);
jest.mock('@/hooks/products/useWorkspaceProducts', () => ({
  useWorkspaceProducts: () => mockProducts,
}));

let mockIsOwner = true;
jest.mock('hooks/useCanAccess', () => ({
  __esModule: true,
  default: () => ({
    canAccess: (resource: string, actions: string[]) =>
      mockIsOwner && resource === 'team_member' && actions.includes('create'),
  }),
}));

jest.mock('next/router', () => ({
  useRouter: () => ({
    asPath: '/w/acme/assets',
    query: { slug: 'acme' },
  }),
}));

const ADVANCED = [
  'New generation',
  'Creative library',
  'Delivery experiments',
  'Localization',
  'Scale',
];

function linkNames() {
  return screen.getAllByRole('link').map((link) => link.textContent?.trim());
}

beforeEach(() => window.localStorage.clear());

describe('WorkspaceNavigation (#165)', () => {
  it('puts 产品 / 营销测试 / 素材 first and folds the v9 areas', () => {
    render(
      <WorkspaceNavigation slug="acme" activePathname="/w/acme/products" />
    );
    expect(linkNames().slice(0, 3)).toEqual([
      'Products',
      'Marketing tests',
      'Assets',
    ]);
    expect(
      screen.getByRole('link', { name: 'Marketing tests' })
    ).toHaveAttribute('href', '/w/acme/experiments');
    const toggle = screen.getByRole('button', { name: 'Advanced workbench' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    for (const name of ADVANCED) {
      expect(screen.queryByRole('link', { name })).not.toBeInTheDocument();
    }
  });

  it('expands the 高级工作台 group and remembers it per viewer', () => {
    const { unmount } = render(
      <WorkspaceNavigation slug="acme" activePathname="/w/acme/products" />
    );
    fireEvent.click(screen.getByRole('button', { name: 'Advanced workbench' }));
    expect(linkNames().slice(3, 8)).toEqual(ADVANCED);
    expect(
      screen.getByRole('link', { name: 'New generation' })
    ).toHaveAttribute('href', '/w/acme/generate');
    expect(window.localStorage.getItem(ADVANCED_NAV_STORAGE_KEY)).toBe('1');
    unmount();

    render(<WorkspaceNavigation slug="acme" activePathname="/w/acme/tests" />);
    expect(
      screen.getByRole('button', { name: 'Advanced workbench' })
    ).toHaveAttribute('aria-expanded', 'true');
    fireEvent.click(screen.getByRole('button', { name: 'Advanced workbench' }));
    expect(
      screen.queryByRole('link', { name: 'Scale' })
    ).not.toBeInTheDocument();
    expect(window.localStorage.getItem(ADVANCED_NAV_STORAGE_KEY)).toBeNull();
  });

  it('opens by itself on an advanced area and stays open when leaving it', () => {
    const { rerender } = render(
      <WorkspaceNavigation
        slug="acme"
        activePathname="/w/acme/legacy-experiments"
      />
    );
    const active = screen.getByRole('link', { name: 'Delivery experiments' });
    expect(active.className).toMatch(/\btext-white\b/);
    rerender(
      <WorkspaceNavigation slug="acme" activePathname="/w/acme/assets" />
    );
    expect(
      screen.getByRole('link', { name: 'Localization' })
    ).toBeInTheDocument();
    // Not persisted: only an explicit toggle is a preference.
    expect(window.localStorage.getItem(ADVANCED_NAV_STORAGE_KEY)).toBeNull();
  });

  it('shows Owner-only items when canAccess returns true', () => {
    render(<WorkspaceNavigation slug="acme" activePathname="/w/acme/assets" />);
    expect(screen.getByText('Settings')).toBeInTheDocument();
    expect(screen.getByText('Members')).toBeInTheDocument();
  });

  it('shows a Member Settings, where the spending limits are, but not Members', () => {
    mockIsOwner = false;
    try {
      render(
        <WorkspaceNavigation slug="acme" activePathname="/w/acme/assets" />
      );
      expect(screen.getByRole('link', { name: 'Settings' })).toHaveAttribute(
        'href',
        '/w/acme/settings'
      );
      expect(screen.queryByText('Members')).toBeNull();
    } finally {
      mockIsOwner = true;
    }
  });
});

describe('WorkspaceNavigation with the ads-first switch on (#194)', () => {
  beforeEach(() => {
    env.adsFirstEnabled = true;
    mockProducts = productsResult([
      workspaceProduct('prod_home', 'Acme Charge Home', 'drafts_unhandled', 6),
      workspaceProduct('prod_go', 'Acme Charge Go', 'kept_untested', 4),
      workspaceProduct('prod_pro', null, 'no_ideas', 0),
    ]);
  });
  afterEach(() => {
    env.adsFirstEnabled = false;
  });

  it('lists New product, Overview, the products, Asset library, Competitors, Settings', () => {
    render(
      <WorkspaceNavigation slug="acme" activePathname="/w/acme/overview" />
    );
    expect(linkNames()).toEqual([
      'New product',
      'Overview',
      'Acme Charge Home6 drafts',
      'Acme Charge Go4 kept',
      'Unnamed productNo ads yet',
      'Asset library',
      'Competitors',
      'Settings',
      'Members',
    ]);
    // The start page while the switch is on (#195).
    expect(screen.getByRole('link', { name: 'New product' })).toHaveAttribute(
      'href',
      '/w/acme/start'
    );
    expect(screen.getByRole('link', { name: 'Overview' })).toHaveAttribute(
      'href',
      '/w/acme/overview'
    );
    expect(screen.getByRole('link', { name: 'Overview' }).className).toMatch(
      /\btext-white\b/
    );
    expect(
      screen.getByRole('link', { name: /Acme Charge Home/ })
    ).toHaveAttribute('href', '/w/acme/p/prod_home/ideas');
    expect(screen.getByRole('link', { name: 'Competitors' })).toHaveAttribute(
      'href',
      '/w/acme/competitors'
    );
    // The golden path and the Owner/Member e2e still find these.
    expect(screen.getByTestId('workspace-settings-link')).toHaveAttribute(
      'href',
      '/w/acme/settings'
    );
    expect(screen.getByRole('link', { name: 'Members' })).toHaveAttribute(
      'href',
      '/w/acme/members'
    );
  });

  it('marks the product whose view is open', () => {
    render(
      <WorkspaceNavigation
        slug="acme"
        activePathname="/w/acme/p/prod_go/tests?x=1"
      />
    );
    expect(
      screen.getByRole('link', { name: /Acme Charge Go/ })
    ).toHaveAttribute('aria-current', 'true');
    expect(
      screen.getByRole('link', { name: /Acme Charge Home/ })
    ).not.toHaveAttribute('aria-current');
  });

  it('folds every older area into More, remembered per viewer', () => {
    const { unmount } = render(
      <WorkspaceNavigation slug="acme" activePathname="/w/acme/overview" />
    );
    const toggle = screen.getByTestId('workspace-nav-advanced-toggle');
    expect(toggle).toHaveAccessibleName('More');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(toggle);
    const more = within(document.getElementById('workspace-nav-advanced')!)
      .getAllByRole('link')
      .map((link) => [link.textContent, link.getAttribute('href')]);
    expect(more).toEqual([
      ['Localization', '/w/acme/localization'],
      ['Scale', '/w/acme/scale'],
      ['Creative library', '/w/acme/library'],
      ['New generation', '/w/acme/generate'],
      ['Delivery experiments (legacy)', '/w/acme/legacy-experiments'],
      ['Legacy tests (read-only)', '/w/acme/tests'],
      ['Tests from before the redesign', '/w/acme/experiments'],
    ]);
    expect(window.localStorage.getItem(ADVANCED_NAV_STORAGE_KEY)).toBe('1');
    unmount();

    render(<WorkspaceNavigation slug="acme" activePathname="/w/acme/assets" />);
    expect(screen.getByTestId('workspace-nav-advanced-toggle')).toHaveAttribute(
      'aria-expanded',
      'true'
    );
  });

  it.each([
    ['/w/acme/assets?overlay=v_1', 'workspace-nav-assets', '/w/acme/assets'],
    [
      '/w/acme/competitors?product=prod_go&competitor_scan=scan_1',
      'workspace-nav-competitors',
      '/w/acme/competitors',
    ],
    ['/w/acme/settings', 'workspace-settings-link', '/w/acme/settings'],
  ])(
    'opens %s from the sidebar, its entry marked and no product marked (#203)',
    (path, testId, href) => {
      render(<WorkspaceNavigation slug="acme" activePathname={path} />);
      const entry = screen.getByTestId(testId);
      expect(entry).toHaveAttribute('href', href);
      expect(entry.className).toMatch(/\btext-white\b/);
      const marked = screen
        .getAllByRole('link')
        .filter((link) => /\btext-white\b/.test(link.className));
      expect(marked).toEqual([entry]);
      expect(
        screen
          .getAllByTestId('workspace-nav-product')
          .filter((link) => link.hasAttribute('aria-current'))
      ).toEqual([]);
    }
  );

  it('opens More by itself on a test made before the redesign', () => {
    render(
      <WorkspaceNavigation
        slug="acme"
        activePathname="/w/acme/experiments?test=mp_1"
      />
    );
    expect(
      screen.getByRole('link', { name: 'Tests from before the redesign' })
        .className
    ).toMatch(/\btext-white\b/);
  });

  it('shows a Member Settings but not Members', () => {
    mockIsOwner = false;
    try {
      render(
        <WorkspaceNavigation slug="acme" activePathname="/w/acme/overview" />
      );
      expect(screen.getByTestId('workspace-settings-link')).toBeInTheDocument();
      expect(screen.queryByText('Members')).toBeNull();
    } finally {
      mockIsOwner = true;
    }
  });

  it('says when there are no products yet, or the list is loading or failed', () => {
    mockProducts = productsResult([]);
    const { rerender } = render(
      <WorkspaceNavigation slug="acme" activePathname="/w/acme/overview" />
    );
    expect(
      screen.getByText('No products yet. Add one to get started.')
    ).toBeInTheDocument();

    mockProducts = { ...productsResult([]), isLoading: true };
    rerender(
      <WorkspaceNavigation slug="acme" activePathname="/w/acme/overview" />
    );
    expect(screen.getByRole('status')).toHaveTextContent('Loading products…');

    const refresh = jest.fn().mockResolvedValue(undefined);
    mockProducts = {
      ...productsResult([]),
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
      refresh,
    };
    rerender(
      <WorkspaceNavigation slug="acme" activePathname="/w/acme/overview" />
    );
    expect(screen.getByRole('alert')).toHaveTextContent(
      "Couldn't load the products."
    );
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(refresh).toHaveBeenCalled();
  });
});

describe('WorkspaceNavigation with the ads-first switch off (#194)', () => {
  it('keeps the feature list: no New product, Overview or product list', () => {
    const read = jest.fn();
    mockProducts = new Proxy({} as UseWorkspaceProductsResult, {
      get: read,
    });
    render(<WorkspaceNavigation slug="acme" activePathname="/w/acme/assets" />);
    expect(screen.queryByRole('link', { name: 'New product' })).toBeNull();
    expect(screen.queryByRole('link', { name: 'Overview' })).toBeNull();
    expect(
      screen.getByTestId('workspace-nav-advanced-toggle')
    ).toHaveAccessibleName('Advanced workbench');
    expect(read).not.toHaveBeenCalled();
  });
});

describe('NavigationItem testId rendering', () => {
  it('Settings link has workspace-settings-link testid in real NavigationItems component', () => {
    const { container } = render(
      <WorkspaceNavigation
        slug="test-ws"
        activePathname="/w/test-ws/settings"
      />
    );
    const settingsLink = container.querySelector(
      '[data-testid="workspace-settings-link"]'
    );
    expect(settingsLink).toBeTruthy();
    expect(settingsLink?.textContent).toContain('Settings');
  });
});
