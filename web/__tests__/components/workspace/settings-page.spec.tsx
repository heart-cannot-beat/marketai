/** @jest-environment jsdom */
import { render, screen } from '@testing-library/react';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';

jest.mock('@/lib/workspace-bootstrap', () => ({
  getWorkspacePageProps: jest.fn(() => jest.fn()),
}));
jest.mock('@/components/layouts/WorkspaceLayout', () => ({
  __esModule: true,
  default: ({ children }: { children: unknown }) => children,
}));
jest.mock('hooks/useTeam', () => ({
  __esModule: true,
  default: () => ({
    isLoading: false,
    isError: false,
    team: { id: 'ws_test', name: 'Acme Charge', slug: 'acme' },
  }),
}));
jest.mock('@/components/team', () => ({
  TeamSettings: () => <div data-testid="team-settings" />,
}));
jest.mock('@/components/workspace/ProviderCredentialsSection', () => ({
  ProviderCredentialsSection: () => <div data-testid="provider-credentials" />,
}));
jest.mock('@/components/workspace/MetaConnectionSection', () => ({
  MetaConnectionSection: () => <div data-testid="meta-connection" />,
}));
jest.mock('@/components/workspace/SpendGuardrailSection', () => ({
  SpendGuardrailSection: () => <div data-testid="spend-guardrail" />,
}));

import SettingsPage from '../../../pages/w/[slug]/settings';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';

const bootstrap = (role: 'OWNER' | 'MEMBER'): WorkspaceBootstrap => ({
  workspaceId: 'ws_test',
  slug: 'acme',
  displayName: 'Acme Charge',
  membershipId: 'mem_1',
  role,
});

/**
 * Settings is every member's since #126: a Member reads the spending limits
 * there, and nothing else on the page (contract §12).
 */
describe('/w/{slug}/settings', () => {
  it('lets every member in, not only the Owner', () => {
    expect(jest.mocked(getWorkspacePageProps)).toHaveBeenCalledWith({
      namespaces: ['common', 'tests', 'settings'],
    });
  });

  it('shows a Member the spending limits and nothing of the Owner', () => {
    render(<SettingsPage bootstrap={bootstrap('MEMBER')} />);
    expect(screen.getByTestId('spend-guardrail')).toBeInTheDocument();
    expect(
      document
        .getElementById('spend-guardrail')
        ?.contains(screen.getByTestId('spend-guardrail'))
    ).toBe(true);
    for (const ownerOnly of [
      'team-settings',
      'provider-credentials',
      'meta-connection',
    ]) {
      expect(screen.queryByTestId(ownerOnly)).toBeNull();
    }
  });

  it('shows the Owner every section, the spending limits included', () => {
    render(<SettingsPage bootstrap={bootstrap('OWNER')} />);
    for (const section of [
      'team-settings',
      'provider-credentials',
      'meta-connection',
      'spend-guardrail',
    ]) {
      expect(screen.getByTestId(section)).toBeInTheDocument();
    }
  });

  /**
   * #181: only the frozen legacy test flow read the「生成默认值」, and a
   * marketing test picks its provider, model and format itself, so Settings
   * no longer offers them -- nor says tests generate with them.
   */
  it('no longer shows the generation defaults, not even to the Owner', () => {
    render(<SettingsPage bootstrap={bootstrap('OWNER')} />);
    expect(screen.queryByText(/generation defaults/i)).toBeNull();
    expect(
      screen.queryByText(/no longer ask for a model and aspect ratio/i)
    ).toBeNull();
    expect(
      document.querySelector('[data-testid="generation-defaults-section"]')
    ).toBeNull();
  });
});
