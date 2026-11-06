const path = require('path');

/**
 * Interface languages. `en` is the source language of every key and is
 * served without a prefix; `zh-CN` pages live under `/zh-CN/...`. Keep
 * `locales` in step with `SUPPORTED_LOCALES` in `lib/i18n/locales.ts`
 * (`__tests__/locales/locale-config.spec.ts`).
 *
 * Locale detection stays on: Next.js only accepts `localeDetection: false`,
 * so leaving the option out is how it is enabled. Next.js detects on `/`;
 * `middleware.ts` applies the same choice (saved preference, `NEXT_LOCALE`
 * cookie, then `Accept-Language`) to every other page.
 *
 * `fallbackLng: false`: `zh-CN` has exactly the keys `en` has
 * (`__tests__/locales/key-parity.spec.ts`), so a fallback would only ship a
 * second copy of every namespace with each Chinese page.
 */
module.exports = {
  i18n: {
    defaultLocale: 'en',
    locales: ['en', 'zh-CN'],
  },
  fallbackLng: false,
  localePath: path.resolve('./locales'),
};
