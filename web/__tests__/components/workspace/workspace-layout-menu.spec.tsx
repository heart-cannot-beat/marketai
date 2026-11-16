/** @jest-environment jsdom */
import type { ReactNode } from 'react';
import { render, screen } from '@testing-library/react';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import env from '@/lib/env';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';

function mockPassThrough({ children }: { children: ReactNode }) {
  return <>{children}</>;
}
jest.mock('next/router', () => ({
  useRouter: () => ({ query: { slug: 'alpha' }, asPath: '/w/alpha' }),
}));
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  DomainGatewayProvider: mockPassThrough,
}));
jest.mock('@/lib/domain-client/blob-transfer', () => ({
  DefaultBrowserBlobTransferClient: class {},
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  WorkspaceProvider: mockPassThrough,
}));
jest.mock('@/components/workspace/NavigationBlocker', () => ({
  NavigationBlockerProvider: mockPassThrough,
}));
jest.mock('@/components/workspace/WorkspaceRouteBoundary', () => ({
  WorkspaceRouteBoundary: mockPassThrough,
}));
jest.mock('@/components/workspace/WorkspaceSelector', () => ({
  WorkspaceSelector: () => null,
}));
jest.mock('@/components/workspace/WorkspaceNavigation', () => ({
  WorkspaceNavigation: () => null,
}));
jest.mock('@/components/workspace/WorkspaceMenu', () => ({
  WorkspaceMenu: ({ slug }: { slug: string }) => (
    <button type="button" data-testid="workspace-menu-mock" data-slug={slug} />
  ),
}));
jest.mock('@/components/workspace/WorkspaceAccountMenu', () => ({
  WorkspaceAccountMenu: () => null,
}));
jest.mock('@/components/shared/shell/Brand', () => () => null);
jest.mock('@/components/shared/LanguageSwitcher', () => ({
  LanguageSwitcher: () => <select data-testid="language-switcher" />,
}));

const renderLayout = () =>
  render(
    <WorkspaceLayout bootstrap={{} as WorkspaceBootstrap}>
      <h1 data-testid="page" />
    </WorkspaceLayout>
  );

describe('WorkspaceLayout phone menu (#194)', () => {
  afterEach(() => {
    env.adsFirstEnabled = false;
  });

  it('adds the menu button to the phone top bar with the switch on', () => {
    env.adsFirstEnabled = true;
    renderLayout();
    expect(screen.getByTestId('workspace-menu-mock')).toHaveAttribute(
      'data-slug',
      'alpha'
    );
    // The phone bar leaves the language choice to the menu; the sidebar keeps it.
    expect(screen.getAllByTestId('language-switcher')).toHaveLength(1);
  });

  it('leaves the phone top bar as it is with the switch off', () => {
    renderLayout();
    expect(screen.queryByTestId('workspace-menu-mock')).toBeNull();
    expect(screen.getAllByTestId('language-switcher')).toHaveLength(2);
  });
});
