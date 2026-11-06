import { CJK, namespacesOf, readNamespace } from './locale-files';

/**
 * `en` is the source language of every key and must read as English. The
 * Chinese copy lives in `zh-CN/`; a CJK character here is a value that was
 * never translated (or Chinese punctuation left in an English sentence).
 */
describe('English locale files', () => {
  const namespaces = namespacesOf('en');

  it('has namespaces to check', () => {
    expect(namespaces.length).toBeGreaterThanOrEqual(9);
  });

  it.each(namespaces)('en/%s.json has no CJK characters', (namespace) => {
    const offending = Array.from(readNamespace('en', namespace))
      .filter(([, value]) => typeof value === 'string' && CJK.test(value))
      .map(([key, value]) => `${key}: ${value}`);
    expect(offending).toEqual([]);
  });

  it('the CJK pattern catches ideographs and full-width punctuation only', () => {
    expect(CJK.test('视觉风格')).toBe(true);
    expect(CJK.test('Images；10')).toBe(true);
    expect(CJK.test('Images, 10 — “quoted” · ok…')).toBe(false);
  });
});
