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

import { CredentialBindingPanel } from '@/components/library/data-sources/CredentialBindingPanel';

// The manual fact import panel is covered by
// `manual-fact-import-panel.spec.tsx` (#14).

// ─── CredentialBindingPanel ───────────────────────────────────────────

describe('CredentialBindingPanel', () => {
  const creds = [
    {
      id: 'c1',
      provider: 'google_ads',
      createdAt: '2025-01-01',
      expiresAt: null,
      status: 'active',
    },
    {
      id: 'c2',
      provider: 'meta',
      createdAt: '2025-01-01',
      expiresAt: '2025-12-31',
      status: 'expiring',
    },
  ];

  it('shows no-credentials message when empty', () => {
    render(
      createElement(CredentialBindingPanel, {
        credentials: [],
        onBind: jest.fn(),
      })
    );
    expect(
      screen.getByText('library:data-source-ops.no-credentials')
    ).toBeTruthy();
  });

  it('renders credential list with providers', () => {
    render(
      createElement(CredentialBindingPanel, {
        credentials: creds,
        onBind: jest.fn(),
      })
    );
    expect(screen.getByText('google_ads')).toBeTruthy();
    expect(screen.getByText('meta')).toBeTruthy();
    expect(
      screen.getAllByText('library:data-source-ops.credential-status')
    ).toHaveLength(2);
  });

  it('binds on click without asking for an authenticator code (ADR 0015)', async () => {
    const onBind = jest.fn().mockResolvedValue(undefined);
    render(
      createElement(CredentialBindingPanel, {
        credentials: [creds[0]],
        onBind,
      })
    );

    await act(async () => {
      fireEvent.click(screen.getByText('library:data-source-ops.bind'));
    });

    expect(onBind).toHaveBeenCalledWith('c1');
    expect(screen.queryByTestId('mfa-challenge-code')).toBeNull();
  });

  it('shows error when onBind throws', async () => {
    const onBind = jest.fn().mockRejectedValue(new Error('Bind failed'));
    render(
      createElement(CredentialBindingPanel, {
        credentials: [creds[0]],
        onBind,
      })
    );

    await act(async () => {
      fireEvent.click(screen.getByText('library:data-source-ops.bind'));
    });

    expect(screen.getByText('Bind failed')).toBeTruthy();
  });
});
