import {
  CJK,
  namespacesOf,
  placeholdersOf,
  readNamespace,
} from './locale-files';
import i18nConfig from '../../next-i18next.config';

/**
 * Every locale has exactly the keys `en` has, namespace by namespace. Add a
 * key to `en` and `zh-CN` in the same change; `node scripts/i18n-missing.mjs`
 * lists every gap at once.
 */
const targets = (i18nConfig.i18n.locales as string[]).filter(
  (locale) => locale !== 'en'
);

describe.each(targets)('%s locale files', (locale) => {
  it('has the same namespaces as en', () => {
    expect(namespacesOf(locale)).toEqual(namespacesOf('en'));
  });

  it.each(namespacesOf('en'))(
    `${locale}/%s.json has exactly the keys of en/%s.json`,
    (namespace) => {
      const source = readNamespace('en', namespace);
      const target = readNamespace(locale, namespace);
      expect({
        missing: Array.from(source.keys()).filter((key) => !target.has(key)),
        extra: Array.from(target.keys()).filter((key) => !source.has(key)),
      }).toEqual({ missing: [], extra: [] });
    }
  );

  it.each(namespacesOf('en'))(
    `${locale}/%s.json keeps every {{placeholder}} and leaves no value empty`,
    (namespace) => {
      const source = readNamespace('en', namespace);
      const target = readNamespace(locale, namespace);
      const problems = Array.from(source.keys())
        .filter((key) => target.has(key))
        .flatMap((key) => {
          const value = target.get(key);
          if (typeof value !== 'string' || value.trim() === '') {
            return [`${key}: empty`];
          }
          const want = placeholdersOf(source.get(key)).join(',');
          const got = placeholdersOf(value).join(',');
          return want === got ? [] : [`${key}: {{${got}}} vs en {{${want}}}`];
        });
      expect(problems).toEqual([]);
    }
  );
});

describe('zh-CN copy', () => {
  it('is mostly Chinese, not English copied across', () => {
    let total = 0;
    let chinese = 0;
    for (const namespace of namespacesOf('zh-CN')) {
      for (const value of Array.from(
        readNamespace('zh-CN', namespace).values()
      )) {
        total += 1;
        if (typeof value === 'string' && CJK.test(value)) chinese += 1;
      }
    }
    // Some values are legitimately the same in both languages (brand and
    // provider names, units, "{{count}}"); nearly all are not.
    expect(chinese / total).toBeGreaterThan(0.9);
  });
});
