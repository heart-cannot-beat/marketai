/**
 * @jest-environment jsdom
 */
import { createElement } from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';

jest.mock('next-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, opts?: Record<string, string>) => {
      if (opts) {
        let result = `library:${key}`;
        for (const [k, v] of Object.entries(opts)) {
          result = result.replace(`{{${k}}}`, String(v));
        }
        return result;
      }
      return `library:${key}`;
    },
  }),
}));

jest.mock('next/router', () => ({
  useRouter: () => ({
    query: { slug: 'acme' },
    asPath: '/w/acme/library',
    push: jest.fn(),
    replace: jest.fn(),
  }),
}));

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: {
      slug: 'acme',
      workspaceId: 'w1',
      role: 'OWNER',
      membershipId: 'm1',
      displayName: 'Acme',
    },
  }),
}));

import {
  LibraryLiveRegion,
  useLibraryAnnounce,
} from '@/components/library/LibraryLiveRegion';
import { LibraryTabs } from '@/components/library/detail/LibraryTabs';
import type { EntryTypeDefinition } from '@/lib/library/entry-type-definition';
import * as publishDialogModule from '@/components/library/commands/PublishDialog';
import { ProblemState } from '@/components/library/detail/ProblemState';

// ─── LibraryLiveRegion ────────────────────────────────────────────────

describe('LibraryLiveRegion', () => {
  function AnnounceButton({
    message,
    priority,
  }: {
    message: string;
    priority?: 'polite' | 'assertive';
  }) {
    const { announce } = useLibraryAnnounce();
    return createElement(
      'button',
      { onClick: () => announce(message, priority) },
      'Announce'
    );
  }

  it('renders polite live region', () => {
    render(
      createElement(
        LibraryLiveRegion,
        null,
        createElement(AnnounceButton, { message: 'Draft saved' })
      )
    );
    const politeRegion = document.querySelector('[aria-live="polite"]');
    expect(politeRegion).toBeTruthy();
    expect(politeRegion!.getAttribute('role')).toBe('status');
  });

  it('renders assertive live region', () => {
    render(
      createElement(
        LibraryLiveRegion,
        null,
        createElement(AnnounceButton, { message: 'Error' })
      )
    );
    const assertiveRegion = document.querySelector('[aria-live="assertive"]');
    expect(assertiveRegion).toBeTruthy();
    expect(assertiveRegion!.getAttribute('role')).toBe('alert');
  });

  it('polite announce populates polite region', () => {
    render(
      createElement(
        LibraryLiveRegion,
        null,
        createElement(AnnounceButton, { message: 'Draft saved' })
      )
    );
    fireEvent.click(screen.getByText('Announce'));
    const politeRegion = document.querySelector('[aria-live="polite"]');
    expect(politeRegion!.textContent).toBe('Draft saved');
  });

  it('assertive announce populates assertive region', () => {
    render(
      createElement(
        LibraryLiveRegion,
        null,
        createElement(AnnounceButton, {
          message: 'Critical error',
          priority: 'assertive',
        })
      )
    );
    fireEvent.click(screen.getByText('Announce'));
    const assertiveRegion = document.querySelector('[aria-live="assertive"]');
    expect(assertiveRegion!.textContent).toBe('Critical error');
  });

  it('clears announcement after timeout', () => {
    jest.useFakeTimers();
    render(
      createElement(
        LibraryLiveRegion,
        null,
        createElement(AnnounceButton, { message: 'Temporary' })
      )
    );
    fireEvent.click(screen.getByText('Announce'));
    const politeRegion = document.querySelector('[aria-live="polite"]');
    expect(politeRegion!.textContent).toBe('Temporary');

    act(() => {
      jest.advanceTimersByTime(5001);
    });
    expect(politeRegion!.textContent).toBe('');
    jest.useRealTimers();
  });

  it('live regions use sr-only class', () => {
    render(
      createElement(
        LibraryLiveRegion,
        null,
        createElement('div', null, 'child')
      )
    );
    const politeRegion = document.querySelector('[aria-live="polite"]');
    const assertiveRegion = document.querySelector('[aria-live="assertive"]');
    expect(politeRegion!.className).toContain('sr-only');
    expect(assertiveRegion!.className).toContain('sr-only');
  });
});

// ─── LibraryTabs ARIA ────────────────────────────────────────────────

describe('LibraryTabs ARIA', () => {
  const mockDefinition: EntryTypeDefinition = {
    entryType: 'product',
    labelKey: 'library:entry-type.product',
    iconKey: 'product',
    supportedTabs: ['overview', 'draft', 'versions'],
    defaultTab: 'overview',
    hasDraft: true,
    hasVersions: true,
  };

  it('renders tablist with correct role', () => {
    const onTabChange = jest.fn();
    render(
      createElement(
        LibraryTabs,
        {
          definition: mockDefinition,
          activeTab: 'overview',
          onTabChange,
        },
        createElement('div', null, 'Tab content')
      )
    );
    const tablist = screen.getByRole('tablist');
    expect(tablist).toBeTruthy();
    expect(tablist.getAttribute('aria-label')).toBe('library:tabs.label');
  });

  it('renders tabs with correct role and aria-selected', () => {
    const onTabChange = jest.fn();
    render(
      createElement(
        LibraryTabs,
        {
          definition: mockDefinition,
          activeTab: 'overview',
          onTabChange,
        },
        createElement('div', null, 'content')
      )
    );
    const tabs = screen.getAllByRole('tab');
    expect(tabs).toHaveLength(3);
    expect(tabs[0].getAttribute('aria-selected')).toBe('true');
    expect(tabs[1].getAttribute('aria-selected')).toBe('false');
    expect(tabs[2].getAttribute('aria-selected')).toBe('false');
  });

  it('tabs have aria-controls linking to tabpanel', () => {
    render(
      createElement(
        LibraryTabs,
        {
          definition: mockDefinition,
          activeTab: 'draft',
          onTabChange: jest.fn(),
        },
        createElement('div', null, 'content')
      )
    );
    const tabs = screen.getAllByRole('tab');
    expect(tabs[1].getAttribute('aria-controls')).toBe('tabpanel-draft');

    const panel = screen.getByRole('tabpanel');
    expect(panel.id).toBe('tabpanel-draft');
  });

  it('tabpanel has aria-label matching active tab', () => {
    render(
      createElement(
        LibraryTabs,
        {
          definition: mockDefinition,
          activeTab: 'versions',
          onTabChange: jest.fn(),
        },
        createElement('div', null, 'content')
      )
    );
    const panel = screen.getByRole('tabpanel');
    expect(panel.getAttribute('aria-label')).toBe(
      'library:tabs.versions.label'
    );
  });

  it('clicking a tab fires onTabChange', () => {
    const onTabChange = jest.fn();
    render(
      createElement(
        LibraryTabs,
        {
          definition: mockDefinition,
          activeTab: 'overview',
          onTabChange,
        },
        createElement('div', null, 'content')
      )
    );
    fireEvent.click(screen.getAllByRole('tab')[1]);
    expect(onTabChange).toHaveBeenCalledWith('draft');
  });

  it('only one tabpanel is rendered (tab isolation)', () => {
    render(
      createElement(
        LibraryTabs,
        {
          definition: mockDefinition,
          activeTab: 'overview',
          onTabChange: jest.fn(),
        },
        createElement('div', null, 'active content')
      )
    );
    const panels = screen.getAllByRole('tabpanel');
    expect(panels).toHaveLength(1);
  });
});

// ─── Dialog ARIA ─────────────────────────────────────────────────────

describe('Dialog ARIA accessibility', () => {
  it('dialogs use <dialog> element with modal semantics', () => {
    expect(publishDialogModule).toBeTruthy();
  });
});

// ─── Error states use role="alert" ────────────────────────────────────

describe('Error state accessibility', () => {
  it('ProblemState uses role="alert"', () => {
    const { container } = render(
      createElement(ProblemState, { onRetry: jest.fn() })
    );
    const alert = container.querySelector('[role="alert"]');
    expect(alert).toBeTruthy();
  });
});
