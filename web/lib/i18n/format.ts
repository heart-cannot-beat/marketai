import { DEFAULT_LOCALE } from './locales';

/**
 * Dates and numbers in the interface language. A bare `toLocaleString()`
 * follows the browser and a fixed `'zh-CN'` follows nobody; components pass
 * `i18n.language` (the page locale next-i18next resolved) instead.
 * `__tests__/locales/locale-format.spec.ts` keeps both patterns out of
 * components, pages, lib and hooks.
 *
 * Kept free of React imports so lib code (cost and count formatters) can take
 * the locale as a plain argument.
 */
type DateInput = string | number | Date;

const resolved = (locale: string | null | undefined) =>
  locale || DEFAULT_LOCALE;

/** Date and time, e.g. `10/26/2026, 3:04:05 PM` / `2026/10/26 15:04:05`. */
export const formatDateTime = (
  locale: string | null | undefined,
  value: DateInput,
  options?: Intl.DateTimeFormatOptions
): string => new Date(value).toLocaleString(resolved(locale), options);

/** Date only, e.g. `10/26/2026` / `2026/10/26`. */
export const formatDate = (
  locale: string | null | undefined,
  value: DateInput,
  options?: Intl.DateTimeFormatOptions
): string => new Date(value).toLocaleDateString(resolved(locale), options);

/** A number or an amount (`style: 'currency'`). Throws on an unknown currency code, like `Intl.NumberFormat`. */
export const formatNumber = (
  locale: string | null | undefined,
  value: number,
  options?: Intl.NumberFormatOptions
): string => new Intl.NumberFormat(resolved(locale), options).format(value);
