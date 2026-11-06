import { useState } from 'react';
import { useRouter } from 'next/router';
import { useSession } from 'next-auth/react';
import { useTranslation } from 'next-i18next';
import toast from 'react-hot-toast';
import { LanguageIcon } from '@heroicons/react/24/outline';
import {
  DEFAULT_LOCALE,
  SUPPORTED_LOCALES,
  isSupportedLocale,
} from '@/lib/i18n/locales';
import { VOCABULARY } from '@/lib/i18n/vocabulary';
import { reloadInLocale, saveLocalePreference } from '@/lib/i18n/switch-locale';

interface LanguageSwitcherProps {
  className?: string;
  /** The narrow mobile top bar: no icon, short language names. */
  compact?: boolean;
}

/**
 * The interface-language switch in the top bars. Signed in, the choice is
 * saved to the account first, so it follows the person to the next sign-in;
 * signed out (sign-in and invitation pages) it only sets the language cookie.
 * Either way the page reloads in the new language.
 */
export function LanguageSwitcher({
  className = '',
  compact = false,
}: LanguageSwitcherProps) {
  const router = useRouter();
  const { status } = useSession();
  const { t } = useTranslation('common');
  const [busy, setBusy] = useState(false);
  const current = isSupportedLocale(router.locale)
    ? router.locale
    : DEFAULT_LOCALE;

  const choose = async (value: string) => {
    if (!isSupportedLocale(value) || value === current) return;
    setBusy(true);
    try {
      if (status === 'authenticated') {
        await saveLocalePreference(value);
      }
      reloadInLocale(value, router.asPath);
    } catch {
      toast.error(t('language-save-failed'));
      setBusy(false);
    }
  };

  return (
    <label
      className={`inline-flex shrink-0 items-center gap-x-1 text-sm text-gray-700 dark:text-gray-200 ${className}`}
    >
      {!compact && (
        <LanguageIcon className="h-5 w-5 text-gray-500" aria-hidden="true" />
      )}
      <span className="sr-only">{t('language')}</span>
      <select
        data-testid="language-switcher"
        className="rounded-md border border-gray-300 bg-white py-1 pl-2 pr-7 text-sm focus:border-gray-600 focus:outline-none focus:ring-2 focus:ring-gray-200 disabled:opacity-60 dark:border-gray-600 dark:bg-gray-900"
        value={current}
        disabled={busy}
        onChange={(event) => void choose(event.target.value)}
      >
        {SUPPORTED_LOCALES.map((locale) => (
          <option key={locale} value={locale} lang={locale}>
            {
              VOCABULARY[locale][
                compact ? 'language-short-name' : 'language-self-name'
              ]
            }
          </option>
        ))}
      </select>
    </label>
  );
}

export default LanguageSwitcher;
