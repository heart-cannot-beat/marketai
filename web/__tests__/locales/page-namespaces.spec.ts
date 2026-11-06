import fs from 'node:fs';
import path from 'node:path';
import ts from 'typescript';

/**
 * A page loads only the namespaces it lists for `serverSideTranslations`, and
 * `fallbackLng: false` leaves a key from any other namespace showing as the
 * raw key (#296: the products page showed `references.rights-scope` because
 * the reference upload zone reads `library`). Jest loads every namespace, so
 * component tests cannot see this; this spec walks each page's imports
 * instead and requires every namespace those files name.
 *
 * A file names a namespace with `useTranslation('ns')` /
 * `useTranslation(['a', 'b'])` (no argument means `common`), an `ns: 'x'`
 * option or `ns="x"` attribute, or an `x:key` literal. Imports through a
 * barrel (`index.ts` made only of re-exports) follow just the names imported,
 * so a page that takes `OperationStatus` from `@/components/workspace` does
 * not inherit the Settings sections' namespace.
 */
const WEB = path.resolve(__dirname, '../..');
const PAGES = path.join(WEB, 'pages');
const EXTENSIONS = ['.tsx', '.ts', '/index.ts', '/index.tsx'];
const ALIASES: [string, string][] = [
  ['@/lib/', 'lib/'],
  ['@/components/', 'components/'],
  ['@/hooks/', 'hooks/'],
];
const BASE_DIRS = ['components/', 'hooks/', 'lib/', 'types/'];

const parsed = new Map<string, ts.SourceFile>();
const parse = (file: string): ts.SourceFile => {
  let source = parsed.get(file);
  if (!source) {
    source = ts.createSourceFile(
      file,
      fs.readFileSync(file, 'utf8'),
      ts.ScriptTarget.Latest,
      true,
      file.endsWith('x') ? ts.ScriptKind.TSX : ts.ScriptKind.TS
    );
    parsed.set(file, source);
  }
  return source;
};

/** The local file a specifier names, or null for packages and generated code. */
const resolve = (specifier: string, from: string): string | null => {
  let base: string | null = null;
  if (specifier.startsWith('.')) {
    base = path.resolve(path.dirname(from), specifier);
  } else {
    const alias = ALIASES.find(([prefix]) => specifier.startsWith(prefix));
    if (alias)
      base = path.join(WEB, alias[1] + specifier.slice(alias[0].length));
    else if (BASE_DIRS.some((dir) => specifier.startsWith(dir)))
      base = path.join(WEB, specifier);
  }
  if (!base || base.includes(`${path.sep}generated`)) return null;
  const file = EXTENSIONS.map((ext) => base + ext).find((candidate) =>
    fs.existsSync(candidate)
  );
  return file ?? null;
};

const stringsOf = (node: ts.Node): string[] => {
  if (ts.isStringLiteralLike(node)) return [node.text];
  if (ts.isArrayLiteralExpression(node))
    return node.elements.flatMap((element) =>
      ts.isStringLiteralLike(element) ? [element.text] : []
    );
  return [];
};

const NAMESPACED_KEY = /^([a-z][a-z-]*):[a-z]/;

/** Namespaces one file reads its copy from. */
const namespacesOf = (file: string): Set<string> => {
  const found = new Set<string>();
  const visit = (node: ts.Node) => {
    if (ts.isCallExpression(node)) {
      const callee = node.expression;
      if (ts.isIdentifier(callee) && callee.text === 'useTranslation') {
        const [first] = node.arguments;
        if (!first) found.add('common');
        else stringsOf(first).forEach((ns) => found.add(ns));
      }
      const [key] = node.arguments;
      const match =
        key && ts.isStringLiteralLike(key)
          ? NAMESPACED_KEY.exec(key.text)
          : null;
      if (match && ts.isIdentifier(callee) && callee.text === 't')
        found.add(match[1]);
    }
    if (
      (ts.isPropertyAssignment(node) || ts.isJsxAttribute(node)) &&
      node.name.getText() === 'ns'
    ) {
      const value = ts.isPropertyAssignment(node)
        ? node.initializer
        : node.initializer;
      if (value && ts.isStringLiteralLike(value)) found.add(value.text);
    }
    ts.forEachChild(node, visit);
  };
  visit(parse(file));
  return found;
};

/** `export { A, B as C } from './x'` -> C -> ./x; `export * from` -> '*'. */
const barrelExports = (file: string): Map<string, string[]> | null => {
  const source = parse(file);
  const exports = new Map<string, string[]>();
  for (const statement of source.statements) {
    if (
      !ts.isExportDeclaration(statement) ||
      !statement.moduleSpecifier ||
      !ts.isStringLiteral(statement.moduleSpecifier)
    )
      return null;
    if (statement.isTypeOnly) continue;
    const target = resolve(statement.moduleSpecifier.text, file);
    if (!target) continue;
    const clause = statement.exportClause;
    const names =
      clause && ts.isNamedExports(clause)
        ? clause.elements
            .filter((element) => !element.isTypeOnly)
            .map((element) => element.name.text)
        : ['*'];
    for (const name of names)
      exports.set(name, [...(exports.get(name) ?? []), target]);
  }
  return exports;
};

/** Files a file loads at run time, with the names it takes from each. */
const importsOf = (file: string): [string, string[] | '*'][] => {
  const found: [string, string[] | '*'][] = [];
  const add = (specifier: string, names: string[] | '*') => {
    const target = resolve(specifier, file);
    if (target) found.push([target, names]);
  };
  const visit = (node: ts.Node) => {
    if (
      ts.isImportDeclaration(node) &&
      ts.isStringLiteral(node.moduleSpecifier) &&
      !node.importClause?.isTypeOnly
    ) {
      const bindings = node.importClause?.namedBindings;
      const names =
        bindings && ts.isNamedImports(bindings)
          ? bindings.elements
              .filter((element) => !element.isTypeOnly)
              .map((element) => (element.propertyName ?? element.name).text)
          : '*';
      if (names === '*' || names.length > 0 || node.importClause?.name)
        add(node.moduleSpecifier.text, names);
      else if (!node.importClause) add(node.moduleSpecifier.text, '*');
    } else if (
      ts.isExportDeclaration(node) &&
      node.moduleSpecifier &&
      ts.isStringLiteral(node.moduleSpecifier) &&
      !node.isTypeOnly
    ) {
      add(node.moduleSpecifier.text, '*');
    } else if (
      ts.isCallExpression(node) &&
      node.expression.kind === ts.SyntaxKind.ImportKeyword &&
      node.arguments[0] &&
      ts.isStringLiteralLike(node.arguments[0])
    ) {
      add(node.arguments[0].text, '*');
    }
    ts.forEachChild(node, visit);
  };
  visit(parse(file));
  return found;
};

/** Every file a page runs, mapped to the namespaces it reads. */
const pageClosure = (page: string): Map<string, Set<string>> => {
  const seen = new Map<string, Set<string>>();
  const stack = [page];
  while (stack.length > 0) {
    const file = stack.pop() as string;
    if (seen.has(file)) continue;
    seen.set(file, namespacesOf(file));
    for (const [target, names] of importsOf(file)) {
      const barrel = barrelExports(target);
      if (!barrel || names === '*') {
        if (barrel) barrel.forEach((files) => stack.push(...files));
        else stack.push(target);
        continue;
      }
      for (const name of names) stack.push(...(barrel.get(name) ?? []));
      stack.push(...(barrel.get('*') ?? []));
    }
  }
  return seen;
};

/** The namespace list a page hands `serverSideTranslations`, if any. */
const loadedNamespaces = (page: string): string[] | null => {
  let loaded: string[] | null = null;
  const visit = (node: ts.Node) => {
    if (
      ts.isPropertyAssignment(node) &&
      node.name.getText() === 'namespaces' &&
      ts.isArrayLiteralExpression(node.initializer)
    )
      loaded = stringsOf(node.initializer);
    if (
      ts.isCallExpression(node) &&
      ts.isIdentifier(node.expression) &&
      node.expression.text === 'serverSideTranslations' &&
      node.arguments[1] &&
      ts.isArrayLiteralExpression(node.arguments[1])
    )
      loaded = stringsOf(node.arguments[1]);
    ts.forEachChild(node, visit);
  };
  visit(parse(page));
  return loaded;
};

const pageFiles = (dir: string): string[] =>
  fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) return entry.name === 'api' ? [] : pageFiles(full);
    return /\.tsx?$/.test(entry.name) ? [full] : [];
  });

const pages = pageFiles(PAGES)
  .map((page) => [path.relative(WEB, page), loadedNamespaces(page)] as const)
  .filter((entry): entry is [string, string[]] => entry[1] !== null);

/** `{ namespace: [files that read it] }` for namespaces the page does not load. */
const missingNamespaces = (page: string, loaded: string[]) => {
  const missing: Record<string, string[]> = {};
  pageClosure(path.join(WEB, page)).forEach((namespaces, file) => {
    namespaces.forEach((ns) => {
      if (!loaded.includes(ns))
        (missing[ns] ??= []).push(path.relative(WEB, file));
    });
  });
  return missing;
};

describe('page namespaces', () => {
  it('finds the pages that load translations', () => {
    expect(pages.length).toBeGreaterThan(25);
    expect(pages.map(([page]) => page)).toContain(
      path.join('pages', 'w', '[slug]', 'products', 'index.tsx')
    );
  });

  it.each(pages)(
    '%s loads every namespace its components read',
    (page, loaded) => {
      expect(missingNamespaces(page, loaded)).toEqual({});
    }
  );

  it('follows only the imported names through a barrel', () => {
    const scale = path.join(WEB, 'pages', 'w', '[slug]', 'scale.tsx');
    const files = Array.from(pageClosure(scale).keys()).map((file) =>
      path.relative(WEB, file)
    );
    expect(files).not.toContain(
      path.join('components', 'workspace', 'ProviderCredentialsSection.tsx')
    );
  });

  it('reports the reference upload zone on a page without `library` (#296)', () => {
    const products = path.join('pages', 'w', '[slug]', 'products', 'index.tsx');
    expect(
      missingNamespaces(products, ['common', 'products', 'tests', 'learnings'])
    ).toEqual({
      library: expect.arrayContaining([
        path.join(
          'components',
          'library',
          'references',
          'ReferenceUploadZone.tsx'
        ),
      ]),
    });
  });
});
