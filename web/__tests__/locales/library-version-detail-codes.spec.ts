import fs from 'node:fs';
import path from 'node:path';
import { CLONE_OPTIONAL_FIELDS } from '@/components/library/commands/CloneProductDialog';
import { CJK, readNamespace } from './locale-files';

/**
 * Closed sets the version detail shows or sends (#25), read from where the
 * server fixes them in `app/library/contracts.py`: a version's usage
 * eligibility (`ELIGIBILITIES`, a free string in the OpenAPI contract) and
 * the fields a Clone Product copies only on request
 * (`CLONE_PRODUCT_OPTIONAL_KEYS`). A value added there without en and zh-CN
 * copy, or an option the server would refuse, fails here.
 */
const APP = path.resolve(__dirname, '../../../app');
const WEB = path.resolve(__dirname, '../..');
const contracts = fs.readFileSync(
  path.join(APP, 'library/contracts.py'),
  'utf8'
);

/** The string values of `NAME = frozenset({...})`, by literal or constant. */
const frozenset = (name: string): string[] => {
  const body = contracts.match(
    new RegExp(`^${name} = frozenset\\(\\{([^}]*)\\}\\)`, 'm')
  )?.[1];
  if (body === undefined) throw new Error(`${name} not found`);
  return body
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
    .map((item) => {
      const literal = item.match(/^"([a-z_]+)"$/);
      if (literal) return literal[1];
      const constant = contracts.match(
        new RegExp(`^${item} = "([a-z_]+)"$`, 'm')
      );
      if (!constant) throw new Error(`${name}: cannot read ${item}`);
      return constant[1];
    })
    .sort();
};

const ELIGIBILITIES = frozenset('ELIGIBILITIES');
const OPTIONAL = frozenset('CLONE_PRODUCT_OPTIONAL_KEYS');
const NEVER_COPIED = frozenset('CLONE_PRODUCT_NEVER_COPIED');

test('reads the server sets', () => {
  expect(ELIGIBILITIES).toEqual(['blocked', 'eligible', 'warning']);
  expect(OPTIONAL.length).toBeGreaterThan(0);
  expect(NEVER_COPIED).toEqual(
    expect.arrayContaining(['offers', 'price', 'landing_page_url'])
  );
});

test('Clone Product offers exactly the optional fields the server accepts', () => {
  expect([...CLONE_OPTIONAL_FIELDS].sort()).toEqual(OPTIONAL);
  expect(CLONE_OPTIONAL_FIELDS.filter((f) => NEVER_COPIED.includes(f))).toEqual(
    []
  );
});

test('the version detail words eligibility through its key family', () => {
  const source = fs.readFileSync(
    path.join(WEB, 'components/library/tabs/VersionDetail.tsx'),
    'utf8'
  );
  expect(source).toContain('t(`eligibility.${version.eligibility}`');
  expect(source).not.toMatch(/>\s*\{version\.eligibility\}\s*</);
});

describe.each(['en', 'zh-CN'])('%s', (locale) => {
  const keys = readNamespace(locale, 'library');
  const family = (prefix: string) =>
    Array.from(keys.keys())
      .filter((key) => key.startsWith(prefix))
      .map((key) => key.slice(prefix.length))
      .sort();
  const worded = (key: string) =>
    keys.has(key) && (locale !== 'zh-CN' || CJK.test(String(keys.get(key))));

  it('words every eligibility and nothing else', () => {
    expect(family('eligibility.')).toEqual(ELIGIBILITIES);
    expect(
      ELIGIBILITIES.filter((code) => !worded(`eligibility.${code}`))
    ).toEqual([]);
    expect(worded('eligibility-default')).toBe(true);
  });

  it('names every optional Clone Product field and nothing else', () => {
    expect(family('commands.clone-optional-field.')).toEqual(OPTIONAL);
    expect(
      OPTIONAL.filter(
        (field) =>
          !keys.has(`commands.clone-optional-field.${field}`) ||
          String(keys.get(`commands.clone-optional-field.${field}`)) === field
      )
    ).toEqual([]);
  });
});
