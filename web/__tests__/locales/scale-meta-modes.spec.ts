import fs from 'node:fs';
import path from 'node:path';
import { SCALE_META_MODES } from '@/lib/scale/meta-mode';
import { CJK, readNamespace } from './locale-files';

/**
 * Scale words a Meta mode as `meta-mode.<mode>` in `scale`: the mode
 * selector, and the Enrollment page's summary (#58), which reads any other
 * value as `meta-mode.unknown`. The contract types `meta_mode` as a free
 * string, so the modes are read from `app.scale.types.MetaMode`, which the
 * tables' CHECK constraint mirrors. A mode added there without en and zh-CN
 * copy, or missing from `SCALE_META_MODES`, fails here instead of reaching
 * the page as a raw code or the generic sentence.
 */
const WEB = path.resolve(__dirname, '../..');
const APP = path.resolve(WEB, '../app/scale');
const read = (file: string) => fs.readFileSync(path.join(APP, file), 'utf8');
const types = read('types.py');
const models = read('models.py');

const serverModes = Array.from(
  (
    types.match(/^class MetaMode\(str, Enum\):\n((?: {4}.*\n)+)/m)?.[1] ?? ''
  ).matchAll(/^ {4}[A-Z_]+ = "([a-z_]+)"$/gm),
  (m) => m[1]
);

const checkModes = Array.from(
  (models.match(/^META_MODE_VALUES = "([^\n]*)"$/m)?.[1] ?? '').matchAll(
    /'([a-z_]+)'/g
  ),
  (m) => m[1]
);

const PREFIX = 'meta-mode.';
/** Keys under `meta-mode.` that are not a mode. */
const OTHER_KEYS = ['label', 'unknown'];

test('reads the Meta modes from the server', () => {
  expect(serverModes).toEqual(
    expect.arrayContaining(['customer_sandbox', 'controlled_mock'])
  );
  // The tables' CHECK constraint allows exactly the Enum's values.
  expect(checkModes).toEqual(serverModes);
});

test('the client mirrors every server Meta mode', () => {
  expect([...SCALE_META_MODES]).toEqual(serverModes);
});

describe.each(['en', 'zh-CN'])('%s', (locale) => {
  const keys = readNamespace(locale, 'scale');
  const language = (value: unknown) =>
    locale === 'zh-CN' ? CJK.test(String(value)) : !CJK.test(String(value));

  it('words every Meta mode in its language', () => {
    expect(serverModes.filter((mode) => !keys.has(PREFIX + mode))).toEqual([]);
    expect(
      serverModes.filter((mode) => !language(keys.get(PREFIX + mode)))
    ).toEqual([]);
  });

  it('has the generic sentence for a mode it does not know', () => {
    expect(keys.has(`${PREFIX}unknown`)).toBe(true);
    expect(language(keys.get(`${PREFIX}unknown`))).toBe(true);
  });

  it('has no copy for other Meta modes', () => {
    const extra = Array.from(keys.keys())
      .filter((key) => key.startsWith(PREFIX))
      .map((key) => key.slice(PREFIX.length))
      .filter((key) => !OTHER_KEYS.includes(key) && !serverModes.includes(key));
    expect(extra).toEqual([]);
  });
});

/** Every `.tsx` under `components/scale`. */
const scaleComponents = (dir = path.join(WEB, 'components/scale')): string[] =>
  fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) return scaleComponents(full);
    return entry.name.endsWith('.tsx') ? [full] : [];
  });

test('no Scale component prints a meta_mode field', () => {
  const printed = scaleComponents().filter((file) =>
    /\{\s*[\w?.]+\.meta_mode\s*\}/.test(fs.readFileSync(file, 'utf8'))
  );
  expect(printed.map((file) => path.relative(WEB, file))).toEqual([]);
});

test('the Enrollment page words its Meta mode through the key family', () => {
  const detail = fs.readFileSync(
    path.join(WEB, 'components/scale/detail/EnrollmentDetail.tsx'),
    'utf8'
  );
  expect(detail).toContain('metaModeLabelKey(enrollment.meta_mode)');
});
