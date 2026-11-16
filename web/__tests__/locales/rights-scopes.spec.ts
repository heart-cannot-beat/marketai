import {
  RIGHTS_SCOPES,
  DEFAULT_RIGHTS_SCOPE,
} from '@/lib/library/rights-scope';
import { CJK, readNamespace } from './locale-files';

/**
 * Every rights scope the reference upload offers (#182) has en and zh-CN
 * copy under `references.rights-scope-options`, and there is no copy for a
 * scope the list does not offer. The labels are words, not the stored code.
 */
const PREFIX = 'references.rights-scope-options.';

const scopesWithCopy = (locale: string) =>
  Array.from(readNamespace(locale, 'library').keys())
    .filter((key) => key.startsWith(PREFIX))
    .map((key) => key.slice(PREFIX.length))
    .sort();

describe('rights scope copy', () => {
  it('keeps owned_product_photo as the default scope', () => {
    expect(RIGHTS_SCOPES).toContain('owned_product_photo');
    expect(DEFAULT_RIGHTS_SCOPE).toBe('owned_product_photo');
  });

  it.each(['en', 'zh-CN'])('%s labels exactly the offered scopes', (locale) => {
    expect(scopesWithCopy(locale)).toEqual([...RIGHTS_SCOPES].sort());
  });

  it.each(RIGHTS_SCOPES)('%s reads as words in both locales', (scope) => {
    const english = readNamespace('en', 'library').get(PREFIX + scope);
    const chinese = readNamespace('zh-CN', 'library').get(PREFIX + scope);
    expect(typeof english).toBe('string');
    expect(english).not.toMatch(/_/);
    expect(english).not.toMatch(CJK);
    expect(chinese).toMatch(CJK);
    expect(chinese).not.toContain(scope);
  });
});
