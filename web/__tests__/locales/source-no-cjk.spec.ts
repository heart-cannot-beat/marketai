import fs from 'node:fs';
import path from 'node:path';
import ts from 'typescript';
import { CJK } from './locale-files';

/**
 * Interface copy comes from the locale files, so the code that renders it
 * holds no CJK text of its own. Only literals are checked — string and
 * template literals, JSX text and regular expressions; comments may quote the
 * Chinese contract wording they implement. Generated clients and tests are
 * out of scope.
 */
const ROOT = path.join(__dirname, '..', '..');
const SCANNED = ['components', 'pages', 'lib', 'hooks'];
const EXTENSIONS = new Set(['.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs']);

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

/** `file:line: text` for every literal in `source` that contains CJK. */
const cjkLiterals = (fileName: string, source: string): string[] => {
  const file = ts.createSourceFile(
    fileName,
    source,
    ts.ScriptTarget.Latest,
    true,
    fileName.endsWith('x') ? ts.ScriptKind.TSX : ts.ScriptKind.TS
  );
  const found: string[] = [];
  const visit = (node: ts.Node) => {
    if (
      ts.isStringLiteral(node) ||
      ts.isNoSubstitutionTemplateLiteral(node) ||
      ts.isTemplateHead(node) ||
      ts.isTemplateMiddle(node) ||
      ts.isTemplateTail(node) ||
      ts.isRegularExpressionLiteral(node) ||
      ts.isJsxText(node)
    ) {
      const text = node.getText(file);
      if (CJK.test(text)) {
        const { line } = file.getLineAndCharacterOfPosition(
          node.getStart(file)
        );
        found.push(`${fileName}:${line + 1}: ${text.trim().slice(0, 80)}`);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  return found;
};

describe('interface source', () => {
  const files = SCANNED.flatMap((dir) => sourceFiles(path.join(ROOT, dir)));

  it('has files to scan', () => {
    expect(files.length).toBeGreaterThan(200);
  });

  it('has no CJK literals in components, pages, lib or hooks', () => {
    const found = files.flatMap((file) =>
      cjkLiterals(path.relative(ROOT, file), fs.readFileSync(file, 'utf8'))
    );
    expect(found).toEqual([]);
  });

  it('reports literals and JSX text but not comments', () => {
    const source = [
      '// 注释可以引用合同原文',
      "const a = '中文';",
      'const b = `x${a}：`;',
      'const c = /[，。]/;',
      '/* 块注释 */ const d = <p>保存 {a}</p>;',
      'const e = <p>{/* 视觉风格 */}</p>;',
    ].join('\n');
    expect(cjkLiterals('sample.tsx', source)).toEqual([
      "sample.tsx:2: '中文'",
      'sample.tsx:3: }：`',
      'sample.tsx:4: /[，。]/',
      'sample.tsx:5: 保存',
    ]);
  });
});
