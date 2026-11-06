import fs from 'node:fs';
import path from 'node:path';

/**
 * Shared reading of `web/locales/<locale>/<namespace>.json` for the locale
 * guard tests. Nested namespaces flatten to dotted paths, the form
 * `t('a.b')` looks them up by.
 */
export const LOCALES_DIR = path.join(__dirname, '..', '..', 'locales');

/** Han ideographs, CJK symbols and punctuation, and full-width forms. */
export const CJK =
  /[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]/;

export const namespacesOf = (locale: string): string[] =>
  fs
    .readdirSync(path.join(LOCALES_DIR, locale))
    .filter((file) => file.endsWith('.json'))
    .map((file) => file.slice(0, -'.json'.length))
    .sort();

const flatten = (
  value: Record<string, unknown>,
  prefix: string,
  out: Map<string, unknown>
) => {
  for (const [key, child] of Object.entries(value)) {
    const keyPath = prefix ? `${prefix}.${key}` : key;
    if (child && typeof child === 'object' && !Array.isArray(child)) {
      flatten(child as Record<string, unknown>, keyPath, out);
    } else {
      out.set(keyPath, child);
    }
  }
  return out;
};

export const readNamespace = (
  locale: string,
  namespace: string
): Map<string, unknown> =>
  flatten(
    JSON.parse(
      fs.readFileSync(
        path.join(LOCALES_DIR, locale, `${namespace}.json`),
        'utf8'
      )
    ),
    '',
    new Map()
  );

/** The `{{name}}` placeholders of a value, sorted, for comparing languages. */
export const placeholdersOf = (value: unknown): string[] =>
  typeof value === 'string'
    ? Array.from(value.matchAll(/{{\s*([^}\s]+)\s*}}/g), (m) => m[1]).sort()
    : [];
