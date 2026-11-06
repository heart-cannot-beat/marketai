/** @jest-environment jsdom */
import type { ReactNode } from 'react';
import { render } from '@testing-library/react';
import { useSWRConfig } from 'swr';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { retryTransientProblems } from '@/lib/domain-client/transient-problems';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';

function mockPassThrough({ children }: { children: ReactNode }) {
  return <>{children}</>;
}
jest.mock('next/router', () => ({
  useRouter: () => ({ query: { slug: 'alpha' }, asPath: '/w/alpha' }),
}));
jest.mock('next-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
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
jest.mock('@/components/workspace/WorkspaceAccountMenu', () => ({
  WorkspaceAccountMenu: () => null,
}));
jest.mock('@/components/workspace/ForbiddenState', () => ({
  ForbiddenState: () => null,
}));
jest.mock('@/components/shared/shell/Brand', () => () => null);
jest.mock('@/components/shared/LanguageSwitcher', () => ({
  LanguageSwitcher: () => null,
}));

test('every Workspace page retries transient access problems (#309)', () => {
  const seen: unknown[] = [];
  function Page() {
    const config = useSWRConfig();
    seen.push(config.onErrorRetry, config.revalidateOnFocus);
    return null;
  }
  render(
    <WorkspaceLayout bootstrap={{} as WorkspaceBootstrap}>
      <Page />
    </WorkspaceLayout>
  );
  expect(seen).toEqual([retryTransientProblems, false]);
});
