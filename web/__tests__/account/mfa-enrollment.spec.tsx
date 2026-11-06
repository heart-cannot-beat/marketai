/** @jest-environment jsdom */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

import MfaEnrollment from '@/components/account/MfaEnrollment';

jest.mock('next-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

jest.mock('next/router', () => ({
  useRouter: () => ({ query: { onboarding: 'beta-workspace' } }),
}));

function apiResponse(data: unknown, ok = true) {
  return Promise.resolve({
    ok,
    json: async () => (ok ? { data } : { error: data }),
  } as Response);
}

describe('MfaEnrollment', () => {
  it('completes enrollment, reveals recovery codes, and continues onboarding', async () => {
    const fetchMock = jest
      .fn()
      .mockImplementationOnce(() =>
        apiResponse({ enrolled: false, remainingRecoveryCodes: 0 })
      )
      .mockImplementationOnce(() =>
        apiResponse({
          secret: 'ABCDEFGHIJKLMNOP',
          otpauthUri: 'otpauth://totp/MarketAI:test',
        })
      )
      .mockImplementationOnce(() =>
        apiResponse({ recoveryCodes: ['AAAA-BBBB-CCCC-DDDD'] })
      );
    global.fetch = fetchMock;

    render(<MfaEnrollment />);

    fireEvent.click(
      await screen.findByRole('button', { name: 'onboarding-mfa-begin' })
    );
    expect(await screen.findByText('ABCDEFGHIJKLMNOP')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('onboarding-mfa-code'), {
      target: { value: '123456' },
    });
    fireEvent.click(
      screen.getByRole('button', { name: 'onboarding-mfa-confirm' })
    );

    expect(await screen.findByText('AAAA-BBBB-CCCC-DDDD')).toBeInTheDocument();
    expect(
      screen.getByRole('link', { name: 'onboarding-continue-workspace' })
    ).toHaveAttribute('href', '/w/beta-workspace/experiments');
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(fetchMock.mock.calls[2]?.[1]).toEqual(
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ action: 'confirm', code: '123456' }),
      })
    );

    delete (global as { fetch?: typeof fetch }).fetch;
  });
});
