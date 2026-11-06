/**
 * @jest-environment jsdom
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import toast from 'react-hot-toast';
import { useSession } from 'next-auth/react';
import { LanguageSwitcher } from '@/components/shared/LanguageSwitcher';
import UpdateLanguage from '@/components/account/UpdateLanguage';
import { LocalePreference } from '@/components/shared/LocalePreference';
import { assignLocation, replaceLocation } from '@/lib/browser-navigation';

const router = {
  locale: 'en',
  asPath: '/w/acme/tests?plan=tp_1',
  isReady: true,
};
jest.mock('next/router', () => ({ useRouter: () => router }));
jest.mock('next-auth/react', () => ({ useSession: jest.fn() }));
jest.mock('@/lib/browser-navigation', () => ({
  assignLocation: jest.fn(),
  replaceLocation: jest.fn(),
}));
jest.mock('react-hot-toast', () => ({
  __esModule: true,
  default: { error: jest.fn() },
}));

const mockedSession = useSession as jest.Mock;
const fetchMock = jest.fn();

const clearCookie = () => {
  document.cookie = 'NEXT_LOCALE=; Path=/; Max-Age=0';
};

describe('language switching', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    clearCookie();
    router.locale = 'en';
    fetchMock.mockResolvedValue({ ok: true, json: async () => ({}) });
    global.fetch = fetchMock as unknown as typeof fetch;
  });

  it('names each language in its own language', () => {
    mockedSession.mockReturnValue({ status: 'unauthenticated', data: null });
    render(<LanguageSwitcher />);

    const select = screen.getByRole('combobox', { name: 'Language' });
    expect(select).toHaveValue('en');
    expect(
      screen.getAllByRole('option').map((option) => option.textContent)
    ).toEqual(['English', '简体中文']);
  });

  it('signed in: saves the account preference, writes the cookie, reloads in the new language', async () => {
    mockedSession.mockReturnValue({ status: 'authenticated', data: {} });
    render(<LanguageSwitcher />);

    fireEvent.change(screen.getByTestId('language-switcher'), {
      target: { value: 'zh-CN' },
    });

    await waitFor(() =>
      expect(assignLocation).toHaveBeenCalledWith(
        '/zh-CN/w/acme/tests?plan=tp_1'
      )
    );
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/users',
      expect.objectContaining({
        method: 'PUT',
        body: JSON.stringify({ locale: 'zh-CN' }),
      })
    );
    expect(document.cookie).toContain('NEXT_LOCALE=zh-CN');
  });

  it('signed out: only the cookie changes', async () => {
    mockedSession.mockReturnValue({ status: 'unauthenticated', data: null });
    router.locale = 'zh-CN';
    render(<LanguageSwitcher />);

    fireEvent.change(screen.getByTestId('language-switcher'), {
      target: { value: 'en' },
    });

    await waitFor(() =>
      expect(assignLocation).toHaveBeenCalledWith('/w/acme/tests?plan=tp_1')
    );
    expect(fetchMock).not.toHaveBeenCalled();
    expect(document.cookie).toContain('NEXT_LOCALE=en');
  });

  it('stays put and says so when the preference cannot be saved', async () => {
    mockedSession.mockReturnValue({ status: 'authenticated', data: {} });
    fetchMock.mockResolvedValue({
      ok: false,
      status: 503,
      json: async () => ({ error: { message: 'down' } }),
    });
    render(<LanguageSwitcher />);

    fireEvent.change(screen.getByTestId('language-switcher'), {
      target: { value: 'zh-CN' },
    });

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        'Your language could not be saved. Please try again.'
      )
    );
    expect(assignLocation).not.toHaveBeenCalled();
    expect(document.cookie).not.toContain('NEXT_LOCALE=zh-CN');
  });

  it('account settings: "Same as my browser" clears the preference and the cookie', async () => {
    mockedSession.mockReturnValue({ status: 'authenticated', data: {} });
    document.cookie = 'NEXT_LOCALE=zh-CN; Path=/';
    router.asPath = '/settings/account';
    render(<UpdateLanguage locale="zh-CN" />);

    const select = screen.getByTestId('account-language');
    expect(select).toHaveValue('zh-CN');
    fireEvent.change(select, { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }));

    await waitFor(() =>
      expect(assignLocation).toHaveBeenCalledWith('/settings/account')
    );
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/users',
      expect.objectContaining({ body: JSON.stringify({ locale: null }) })
    );
    expect(document.cookie).not.toContain('NEXT_LOCALE');
    router.asPath = '/w/acme/tests?plan=tp_1';
  });

  it('an open tab follows a preference saved elsewhere, once', () => {
    mockedSession.mockReturnValue({
      status: 'authenticated',
      data: { user: { locale: 'zh-CN' } },
    });
    const { rerender } = render(<LocalePreference />);
    rerender(<LocalePreference />);

    expect(replaceLocation).toHaveBeenCalledTimes(1);
    expect(replaceLocation).toHaveBeenCalledWith(
      '/zh-CN/w/acme/tests?plan=tp_1'
    );
    expect(document.cookie).toContain('NEXT_LOCALE=zh-CN');
  });

  it('an open tab already in the preferred language stays', () => {
    router.locale = 'zh-CN';
    mockedSession.mockReturnValue({
      status: 'authenticated',
      data: { user: { locale: 'zh-CN' } },
    });
    render(<LocalePreference />);
    expect(replaceLocation).not.toHaveBeenCalled();
  });
});
