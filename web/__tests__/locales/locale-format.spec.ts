import fs from 'node:fs';
import path from 'node:path';
import ts from 'typescript';
import { formatDate, formatDateTime, formatNumber } from '@/lib/i18n/format';

/**
 * Dates and numbers follow the interface language. A `toLocaleString()`
 * without a locale follows the browser, and a written-out `'zh-CN'` or
 * `'en-US'` follows nobody, so neither may appear in components, pages, lib
 * or hooks: format through `lib/i18n/format.ts` with `i18n.language`.
 * `Intl.DateTimeFormat().resolvedOptions()` reads the browser's time zone and
 * formats nothing, so it stays allowed.
 */
const ROOT = path.join(__dirname, '..', '..');
const SCANNED = ['components', 'pages', 'lib', 'hooks'];
const EXTENSIONS = new Set(['.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs']);
const LOCALE_METHODS = new Set([
  'toLocaleString',
  'toLocaleDateString',
  'toLocaleTimeString',
]);

const sourceFiles = (dir: string): string[] =>
  fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      return entry.name === '__tests__' || entry.name === 'generated'
        ? []
        : sourceFiles(full);
    }
    return EXTENSIONS.has(path.extname(entry.name)) ? [full] : [];
  });

/** No locale argument, `undefined`, or a written-out language tag. */
const fixedOrBrowserLocale = (
  args: ts.NodeArray<ts.Expression> | undefined
) => {
  const first = args?.[0];
  if (!first) return true;
  if (ts.isIdentifier(first) && first.text === 'undefined') return true;
  return ts.isStringLiteral(first) || ts.isNoSubstitutionTemplateLiteral(first);
};

const isIntlFormat = (expression: ts.Expression) =>
  ts.isPropertyAccessExpression(expression) &&
  ts.isIdentifier(expression.expression) &&
  expression.expression.text === 'Intl' &&
  expression.name.text.endsWith('Format');

/** `file:line: text` for every formatting call that ignores the interface language. */
const unlocalizedFormats = (fileName: string, source: string): string[] => {
  const file = ts.createSourceFile(
    fileName,
    source,
    ts.ScriptTarget.Latest,
    true,
    fileName.endsWith('x') ? ts.ScriptKind.TSX : ts.ScriptKind.TS
  );
  const found: string[] = [];
  const report = (node: ts.Node) => {
    const { line } = file.getLineAndCharacterOfPosition(node.getStart(file));
    found.push(
      `${fileName}:${line + 1}: ${node.getText(file).replace(/\s+/g, ' ').slice(0, 80)}`
    );
  };
  const visit = (node: ts.Node) => {
    if (
      ts.isCallExpression(node) &&
      ts.isPropertyAccessExpression(node.expression) &&
      LOCALE_METHODS.has(node.expression.name.text) &&
      fixedOrBrowserLocale(node.arguments)
    ) {
      report(node);
    }
    if (
      (ts.isCallExpression(node) || ts.isNewExpression(node)) &&
      isIntlFormat(node.expression) &&
      fixedOrBrowserLocale(node.arguments)
    ) {
      const readsTimeZoneOnly =
        ts.isPropertyAccessExpression(node.parent) &&
        node.parent.name.text === 'resolvedOptions';
      if (!readsTimeZoneOnly) report(node);
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  return found;
};

describe('interface formatting', () => {
  const files = SCANNED.flatMap((dir) => sourceFiles(path.join(ROOT, dir)));

  it('has files to scan', () => {
    expect(files.length).toBeGreaterThan(200);
  });

  it('formats dates and numbers in the interface language only', () => {
    const found = files.flatMap((file) =>
      unlocalizedFormats(
        path.relative(ROOT, file),
        fs.readFileSync(file, 'utf8')
      )
    );
    expect(found).toEqual([]);
  });

  it('reports bare and fixed-locale formatting but not the time zone lookup', () => {
    const source = [
      'const a = new Date(x).toLocaleString();',
      "const b = d.toLocaleDateString('zh-CN', { month: 'numeric' });",
      'const c = n.toLocaleString(undefined);',
      "const e = new Intl.NumberFormat('en-US').format(1);",
      'const f = Intl.DateTimeFormat().format(d);',
      'const g = Intl.DateTimeFormat().resolvedOptions().timeZone;',
      'const h = d.toLocaleString(locale, { month: "short" });',
      'const i = new Intl.NumberFormat(i18n.language).format(1);',
      "const j = s.toLocaleUpperCase('tr');",
    ].join('\n');
    expect(unlocalizedFormats('sample.ts', source)).toEqual([
      'sample.ts:1: new Date(x).toLocaleString()',
      "sample.ts:2: d.toLocaleDateString('zh-CN', { month: 'numeric' })",
      'sample.ts:3: n.toLocaleString(undefined)',
      "sample.ts:4: new Intl.NumberFormat('en-US')",
      'sample.ts:5: Intl.DateTimeFormat()',
    ]);
  });
});

describe('lib/i18n/format', () => {
  const at = Date.UTC(2026, 9, 26, 15, 4, 5);

  it('formats in the language it is given', () => {
    expect(formatNumber('en', 12345.5)).toBe('12,345.5');
    expect(
      formatNumber('en', 0.34, { style: 'currency', currency: 'USD' })
    ).toBe('$0.34');
    expect(
      formatNumber('zh-CN', 0.34, { style: 'currency', currency: 'USD' })
    ).toBe('US$0.34');
    expect(formatDate('en', at, { timeZone: 'UTC' })).toBe('10/26/2026');
    expect(formatDate('zh-CN', at, { timeZone: 'UTC' })).toBe('2026/10/26');
    expect(
      formatDateTime('zh-CN', new Date(at).toISOString(), { timeZone: 'UTC' })
    ).toBe('2026/10/26 15:04:05');
  });

  it('falls back to the source language without a locale', () => {
    expect(formatDate(undefined, at, { timeZone: 'UTC' })).toBe('10/26/2026');
    expect(formatNumber('', 1000)).toBe('1,000');
  });
});
