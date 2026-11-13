/**
 * @jest-environment jsdom
 */
import { fireEvent, render, screen } from '@testing-library/react';
import {
  ADVANCED_NAV_STORAGE_KEY,
  WorkspaceNavigation,
} from '@/components/workspace/WorkspaceNavigation';

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
