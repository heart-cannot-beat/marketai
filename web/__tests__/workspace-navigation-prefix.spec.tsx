/**
 * @jest-environment jsdom
 */
import { render, screen } from '@testing-library/react';
import { WorkspaceNavigation } from '@/components/workspace/WorkspaceNavigation';

// Mock useCanAccess to return false (member view)
jest.mock('hooks/useCanAccess', () => ({
  __esModule: true,
  default: () => ({ canAccess: () => false }),
}));

// Mock NavigationItem to expose active state
jest.mock('@/components/shared/shell/NavigationItems', () => ({
  __esModule: true,
  NavigationItem: ({ menu }: { menu: { name: string; active: boolean } }) => (
    <a data-active={menu.active}>{menu.name}</a>
  ),
}));

beforeEach(() => window.localStorage.clear());

describe('WorkspaceNavigation generate prefix matching', () => {
  it('highlights 新建生成 on /generate', () => {
    render(
      <WorkspaceNavigation slug="ws-a" activePathname="/w/ws-a/generate" />
    );
    const link = screen.getByText('New generation');
    expect(link).toHaveAttribute('data-active', 'true');
  });

  it('highlights 新建生成 on /generate/drafts/abc', () => {
    render(
      <WorkspaceNavigation
        slug="ws-a"
        activePathname="/w/ws-a/generate/drafts/abc"
      />
    );
    const link = screen.getByText('New generation');
    expect(link).toHaveAttribute('data-active', 'true');
  });

  it('highlights 新建生成 on /generate/runs/xyz', () => {
    render(
      <WorkspaceNavigation
        slug="ws-a"
        activePathname="/w/ws-a/generate/runs/xyz"
      />
    );
    const link = screen.getByText('New generation');
    expect(link).toHaveAttribute('data-active', 'true');
  });

  it('does not highlight 素材 on /generate sub-routes', () => {
    render(
      <WorkspaceNavigation
        slug="ws-a"
        activePathname="/w/ws-a/generate/drafts/abc"
      />
    );
    const link = screen.getByText('Assets');
    expect(link).toHaveAttribute('data-active', 'false');
  });

  it('highlights 营销测试 on the marketing test page', () => {
    render(
      <WorkspaceNavigation slug="ws-a" activePathname="/w/ws-a/experiments" />
    );
    expect(screen.getByText('Marketing tests')).toHaveAttribute(
      'data-active',
      'true'
    );
  });
});
