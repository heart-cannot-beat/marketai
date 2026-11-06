/**
 * @jest-environment jsdom
 */
import { render, screen } from '@testing-library/react';
import TeamNavigation from '@/components/shared/shell/TeamNavigation';
import UserNavigation from '@/components/shared/shell/UserNavigation';

let canManageMembers = false;
jest.mock('hooks/useCanAccess', () => ({
  __esModule: true,
  default: () => ({ canAccess: () => canManageMembers }),
}));

// Render plain links so the assertions read hrefs without the Workspace
// navigation-blocker context GuardedLink belongs to.
jest.mock('@/components/shared/shell/NavigationItems', () => ({
  __esModule: true,
  default: ({ menus }: { menus: Array<{ name: string; href: string }> }) => (
    <nav>
      {menus.map((m) => (
        <a key={m.name} href={m.href}>
          {m.name}
        </a>
      ))}
    </nav>
  ),
}));

const linkNames = () => screen.getAllByRole('link').map((a) => a.textContent);

describe('Back to workspace from the account shell', () => {
  it('leads from team settings back to that Workspace first', () => {
    canManageMembers = true;
    render(
      <TeamNavigation
        slug="marketai-demo"
        activePathname="/teams/marketai-demo/settings"
      />
    );

    expect(
      screen.getByRole('link', { name: 'Back to workspace' })
    ).toHaveAttribute('href', '/w/marketai-demo/generate');
    expect(linkNames()).toEqual(['Back to workspace', 'Settings', 'Members']);
  });

  it('keeps the way back for members who cannot manage members', () => {
    canManageMembers = false;
    render(
      <TeamNavigation slug="ws-a" activePathname="/teams/ws-a/settings" />
    );
    expect(linkNames()).toEqual(['Back to workspace', 'Settings']);
  });

  it('leads from personal settings to the person’s Workspace via /dashboard', () => {
    render(<UserNavigation activePathname="/settings/account" />);

    expect(
      screen.getByRole('link', { name: 'Back to workspace' })
    ).toHaveAttribute('href', '/dashboard');
    expect(linkNames()[0]).toBe('Back to workspace');
  });
});
