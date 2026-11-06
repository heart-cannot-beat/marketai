import {
  DEFAULT_LOCALE,
  isSupportedLocale,
  localizedPath,
  matchAcceptLanguage,
  resolveLocale,
  strategyLanguageFor,
} from '@/lib/i18n/locales';

describe('interface locale resolution', () => {
  it('accepts only the offered locales', () => {
    expect(isSupportedLocale('en')).toBe(true);
    expect(isSupportedLocale('zh-CN')).toBe(true);
    for (const value of ['zh', 'zh-cn', 'fr', '', null, undefined, 1]) {
      expect(isSupportedLocale(value)).toBe(false);
    }
  });

  it.each([
    [null, 'en'],
    ['', 'en'],
    ['en-US,en;q=0.9', 'en'],
    ['zh-CN,zh;q=0.9,en;q=0.8', 'zh-CN'],
    ['zh', 'zh-CN'],
    ['zh-TW', 'zh-CN'],
    ['fr-FR,fr;q=0.9,zh;q=0.5,en;q=0.4', 'zh-CN'],
    ['en;q=0.4,zh;q=0.8', 'zh-CN'],
    ['fr-FR,de;q=0.8', 'en'],
    ['zh;q=0,en', 'en'],
    ['*', 'en'],
  ])('Accept-Language %p -> %s', (header, locale) => {
    expect(matchAcceptLanguage(header)).toBe(locale);
  });

  it('prefers the saved preference, then the cookie, then the browser', () => {
    expect(
      resolveLocale({
        userLocale: 'en',
        cookieLocale: 'zh-CN',
        acceptLanguage: 'zh-CN',
      })
    ).toBe('en');
    expect(
      resolveLocale({
        userLocale: null,
        cookieLocale: 'en',
        acceptLanguage: 'zh-CN',
      })
    ).toBe('en');
    expect(
      resolveLocale({ userLocale: null, acceptLanguage: 'zh-CN,zh' })
    ).toBe('zh-CN');
    // A tampered cookie is ignored, not trusted.
    expect(resolveLocale({ cookieLocale: 'xx', acceptLanguage: 'zh-CN' })).toBe(
      'zh-CN'
    );
    expect(resolveLocale({})).toBe(DEFAULT_LOCALE);
  });

  it('prefixes every locale but the default', () => {
    expect(localizedPath('en', '/w/acme/tests?x=1')).toBe('/w/acme/tests?x=1');
    expect(localizedPath('zh-CN', '/w/acme/tests?x=1')).toBe(
      '/zh-CN/w/acme/tests?x=1'
    );
    expect(localizedPath('zh-CN', '/')).toBe('/zh-CN');
    expect(localizedPath('zh-CN', '/?next=1')).toBe('/zh-CN?next=1');
    expect(localizedPath('zh-CN', 'settings/account')).toBe(
      '/zh-CN/settings/account'
    );
  });

  it('drafts new plans in the interface language (ADR 0013)', () => {
    expect(strategyLanguageFor('zh-CN')).toBe('zh-CN');
    expect(strategyLanguageFor('en')).toBe('en-US');
    expect(strategyLanguageFor(undefined)).toBe('en-US');
  });
});
