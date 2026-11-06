import i18nConfig from '../../next-i18next.config';
import { DEFAULT_LOCALE, SUPPORTED_LOCALES } from '@/lib/i18n/locales';

describe('locale configuration', () => {
  it('serves English and Simplified Chinese with English as the default', () => {
    expect(i18nConfig.i18n.locales).toEqual([...SUPPORTED_LOCALES]);
    expect(i18nConfig.i18n.defaultLocale).toBe(DEFAULT_LOCALE);
    expect(SUPPORTED_LOCALES).toEqual(['en', 'zh-CN']);
  });

  it('keeps Next.js locale detection on', () => {
    // Next.js accepts only `false` here; leaving it out enables detection.
    expect(
      (i18nConfig.i18n as { localeDetection?: boolean }).localeDetection
    ).not.toBe(false);
  });
});
