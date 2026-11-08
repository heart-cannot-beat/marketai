import fs from 'node:fs';
import path from 'node:path';
import ts from 'typescript';
import { CJK, namespacesOf, readNamespace } from './locale-files';

/**
 * The E2E suites find elements by, and assert on, the text pages render. A
 * literal there that no locale holds any more is a locator that times out or
 * an assertion that fails, and only someone running that suite against a
 * stack would see it (#19). This guard reads every text literal the suites
 * match on and requires it to be locale copy, or to be listed in `NOT_COPY`
 * with a reason. Copy read with `en()` (`e2e/support/copy.ts`) is not a
 * literal and needs no check.
 *
 * Case follows Playwright: `getBy*`, `hasText` and `text=` / `:has-text()`
 * selectors ignore case unless `exact: true`; `toHaveText`, `toContainText`
 * and `aria-label` do not. A regular expression ignores case with its `i`
 * flag; its literal runs are checked one by one.
 *
 * A literal is copy when it is part of a locale string, or fills a locale
 * template whose slots each hold one word or another locale string
 * (`Attempt 3` for `Attempt {{number}}`). Pages join strings with `·` and
 * insert numbers, so a literal is also copy when each `·`-separated part, or
 * each run of it between digits, is. Chinese literals are matched against
 * `zh-CN`, and only in a suite that switches to it (mentions `zh-CN`).
 */
const WEB = path.join(__dirname, '..', '..');
const E2E = path.join(WEB, 'e2e');
const COPY_HELPER = path.join('e2e', 'support', 'copy.ts');

/** Text the suites match that is not locale copy, with the reason. */
const NOT_COPY: Readonly<Record<string, string>> = {
  // Names the suites seed or provision.
  'GP E2E Product': 'golden-path seeded Product',
  'GP E2E Offer': 'golden-path seeded Offer',
  'GP E2E Template': 'golden-path seeded template',
  'GP E2E Policy': 'golden-path seeded prompt policy',
  'GP E2E Person': 'golden-path seeded Person',
  'GP E2E Data Source': 'golden-path seeded data source',
  'GP E2E Experiment': 'golden-path seeded Experiment',
  'Golden Path Experiment': 'golden-path area driver Experiment name',
  'Golden Path Workspace': 'golden-path provisioned Workspace name',
  'E2E Controlled Mock Product': 'onboarding seeded Product',
  冰川白: 'colour in the pasted product text the intake suite submits',
  曜石黑: 'colour in the pasted product text the intake suite submits',
  // Data the page shows as the fixtures or the backend gave it.
  'seeded-a': 'Assets fixture version id, shown in the internal view',
  openai: 'Assets fixture provider name next to its count',
  Brightvolt: 'competitor page name in the panel double',
  '@acmecharge': 'own page handle in the panel double',
  'acme-charge.example': 'own domain in the real-backend competitor seed',
  'Explore Full Self-Driving':
    'ad title in the ScrapeCreators fixture under app/competitors',
  'Expression mechanism':
    'expression catalog label from app/marketing, not a locale string',
  表达机制: 'expression catalog label from app/marketing, not a locale string',
  // Text a component writes itself next to locale copy.
  'teammate@example.com': 'InviteViaEmail input placeholder',
  'A Approve': 'review shortcut key A beside the Approve copy',
  'C Needs changes': 'review shortcut key C beside the Needs changes copy',
  'X Reject': 'review shortcut key X beside the Reject copy',
};

type Case = 'sensitive' | 'insensitive';

export interface TextLiteral {
  /** `file:line`. */
  where: string;
  /** The locator, assertion or selector that matches on it. */
  via: string;
  case: Case;
  /** Literal runs; data (a substitution, a regex class) sits between two. */
  pieces: string[];
}

const LOCATORS = new Set([
  'getByText',
  'getByLabel',
  'getByPlaceholder',
  'getByTitle',
  'getByAltText',
]);
const TEXT_ASSERTIONS = new Set(['toHaveText', 'toContainText']);
const SELECTOR_TEXT = /(?:has-text\(|text=)\s*(["']?)([^"')]+)\1/g;

const e2eFiles = (dir: string): string[] =>
  fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      return entry.name.startsWith('.') || entry.name === 'artifacts'
        ? []
        : e2eFiles(full);
    }
    return entry.name.endsWith('.ts') ? [full] : [];
  });

const option = (
  node: ts.Node | undefined,
  name: string
): ts.Expression | undefined => {
  if (!node || !ts.isObjectLiteralExpression(node)) return undefined;
  for (const property of node.properties) {
    if (ts.isPropertyAssignment(property) && property.name.getText() === name)
      return property.initializer;
  }
  return undefined;
};

/**
 * The literal runs of a regular expression's source. Anything that is not a
 * plain character (a class, a group, `.`, an anchor, an alternation) ends a
 * run; a character made optional by `?`, `*` or `{0,…}` is dropped.
 */
export const regexRuns = (source: string): string[] => {
  const runs: string[] = [];
  let run = '';
  const end = () => {
    if (run) runs.push(run);
    run = '';
  };
  for (let i = 0; i < source.length; i += 1) {
    const c = source[i];
    if (c === '\\') {
      i += 1;
      const escaped = source[i] ?? '';
      if (/[dDwWsSbBnrtfv]/.test(escaped)) end();
      else run += escaped;
    } else if (c === '[') {
      for (i += 1; i < source.length && source[i] !== ']'; i += 1) {
        if (source[i] === '\\') i += 1;
      }
      end();
    } else if (c === '?' || c === '*') {
      run = run.slice(0, -1);
      end();
    } else if (c === '{') {
      if (/^\{0[,}]/.test(source.slice(i))) run = run.slice(0, -1);
      end();
      const close = source.indexOf('}', i);
      if (close !== -1) i = close;
    } else if (c === '(') {
      end();
      const lookaround = /^\(\?(?:<[=!]|[:=!]|<[^>]*>)/.exec(source.slice(i));
      if (lookaround) i += lookaround[0].length - 1;
    } else if ('.+^$|)'.includes(c)) {
      end();
    } else {
      run += c;
    }
  }
  end();
  return runs;
};

/** Every text literal one E2E file matches page text with. */
export const textLiterals = (file: string, source: string): TextLiteral[] => {
  const sourceFile = ts.createSourceFile(
    file,
    source,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TS
  );
  const found: TextLiteral[] = [];
  const where = (node: ts.Node) =>
    `${file}:${sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1}`;
  const add = (
    call: ts.Node,
    via: string,
    value: ts.Node | undefined,
    textCase: Case
  ) => {
    if (!value) return;
    if (
      ts.isStringLiteral(value) ||
      ts.isNoSubstitutionTemplateLiteral(value)
    ) {
      found.push({
        where: where(call),
        via,
        case: textCase,
        pieces: [value.text],
      });
    } else if (ts.isTemplateExpression(value)) {
      found.push({
        where: where(call),
        via,
        case: textCase,
        pieces: [
          value.head.text,
          ...value.templateSpans.map((span) => span.literal.text),
        ],
      });
    } else if (ts.isRegularExpressionLiteral(value)) {
      const slash = value.text.lastIndexOf('/');
      found.push({
        where: where(call),
        via,
        case: value.text.slice(slash + 1).includes('i')
          ? 'insensitive'
          : 'sensitive',
        pieces: regexRuns(value.text.slice(1, slash)),
      });
    }
  };
  const caseOf = (options: ts.Node | undefined): Case =>
    option(options, 'exact')?.kind === ts.SyntaxKind.TrueKeyword
      ? 'sensitive'
      : 'insensitive';
  const visit = (node: ts.Node) => {
    if (
      ts.isCallExpression(node) &&
      ts.isPropertyAccessExpression(node.expression)
    ) {
      const method = node.expression.name.text;
      const [first, second] = node.arguments;
      if (LOCATORS.has(method)) {
        add(node, method, first, caseOf(second));
      } else if (method === 'getByRole') {
        add(node, method, option(second, 'name'), caseOf(second));
      } else if (TEXT_ASSERTIONS.has(method)) {
        const values =
          first && ts.isArrayLiteralExpression(first)
            ? first.elements
            : [first];
        values.forEach((value) => add(node, method, value, 'sensitive'));
      } else if (method === 'filter') {
        add(node, 'hasText', option(first, 'hasText'), 'insensitive');
      } else if (method === 'locator') {
        add(node, 'hasText', option(second, 'hasText'), 'insensitive');
        if (first && ts.isStringLiteralLike(first)) {
          for (const match of Array.from(first.text.matchAll(SELECTOR_TEXT))) {
            found.push({
              where: where(node),
              via: 'selector',
              case: 'insensitive',
              pieces: [match[2]],
            });
          }
        }
      } else if (
        method === 'toHaveAttribute' &&
        first &&
        ts.isStringLiteralLike(first) &&
        first.text === 'aria-label'
      ) {
        add(node, 'aria-label', second, 'sensitive');
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return found;
};

const squash = (text: string) => text.replace(/\s+/g, ' ').trim();
const LETTER = /[A-Za-zÀ-ɏ㐀-䶿一-鿿]/g;
const letters = (text: string) => (text.match(LETTER) ?? []).length;
const PLACEHOLDER = /{{\s*[^}]+?\s*}}/;
const escapeRegExp = (text: string) =>
  text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/** A literal's parts: its pieces split where pages join strings with `·`. */
export const partsOf = (literal: TextLiteral): string[] =>
  literal.pieces
    .flatMap((piece) => piece.split('·'))
    .map(squash)
    .filter((part) => letters(part) > 0);

/** A part's runs between numbers, which pages insert as data. */
export const wordsOf = (part: string): string[] =>
  part
    .split(/\d+/)
    .map(squash)
    .filter((word) => letters(word) > 0);

/** The strings of one locale, searchable the way Playwright matches. */
export class LocaleCopy {
  private readonly fixed: Record<Case, string>;
  private readonly templates: Record<Case, RegExp>[];

  constructor(strings: string[]) {
    const values = strings.map(squash);
    // Every run of fixed text between placeholders, kept apart by NUL so a
    // match never spans two strings or a slot.
    const fixed = values
      .flatMap((value) => value.split(PLACEHOLDER))
      .join('\u0000');
    this.fixed = { sensitive: fixed, insensitive: fixed.toLowerCase() };
    // A string with placeholders, and each `·` segment of one, as a pattern.
    // Fewer than three letters of fixed text would let it match anything.
    this.templates = values
      .flatMap((value) => [value, ...value.split('·').map(squash)])
      .filter((segment) => PLACEHOLDER.test(segment))
      .map((segment) => segment.split(PLACEHOLDER))
      .filter((runs) => letters(runs.join('')) >= 3)
      .map((runs) => {
        const source = `^${runs.map(escapeRegExp).join('(.+?)')}$`;
        return {
          sensitive: new RegExp(source),
          insensitive: new RegExp(source, 'i'),
        };
      });
  }

  /** Whether `text` is part of a string's fixed text. */
  holds(text: string, textCase: Case): boolean {
    return textCase === 'sensitive'
      ? this.fixed.sensitive.includes(text)
      : this.fixed.insensitive.includes(text.toLowerCase());
  }

  /**
   * Whether `text` fills a template. A slot holds one word or a string of
   * its own: any text at all would let `{{count}} source` accept every
   * sentence that ends in "source".
   */
  fills(text: string, textCase: Case): boolean {
    return this.templates.some((template) => {
      const match = template[textCase].exec(text);
      return (
        match !== null &&
        match
          .slice(1)
          .every(
            (slot) => /^\S+$/.test(slot) || this.holds(squash(slot), textCase)
          )
      );
    });
  }

  /** Whether `literal` is copy; `isData` text counts as copy. */
  matches(
    literal: TextLiteral,
    isData: (text: string) => boolean = () => false
  ): boolean {
    const textCase = literal.case;
    if (
      literal.pieces.length === 1 &&
      this.fills(squash(literal.pieces[0]), textCase)
    ) {
      return true;
    }
    return partsOf(literal).every(
      (part) =>
        isData(part) ||
        this.holds(part, textCase) ||
        this.fills(part, textCase) ||
        wordsOf(part).every(
          (word) => isData(word) || this.holds(word, textCase)
        )
    );
  }
}

const localeCopy = (locale: string) =>
  new LocaleCopy(
    namespacesOf(locale).flatMap((namespace) =>
      Array.from(readNamespace(locale, namespace).values()).filter(
        (value): value is string => typeof value === 'string'
      )
    )
  );

describe('E2E interface copy', () => {
  const sources = new Map(
    e2eFiles(E2E)
      .map((file) => path.relative(WEB, file))
      .filter((file) => file !== COPY_HELPER)
      .map((file) => [file, fs.readFileSync(path.join(WEB, file), 'utf8')])
  );
  const literals = Array.from(sources).flatMap(([file, source]) =>
    textLiterals(file, source)
  );
  const en = localeCopy('en');
  const zhCN = localeCopy('zh-CN');
  const isData = (text: string) => text in NOT_COPY;
  const isCopy = (literal: TextLiteral) => {
    if (!CJK.test(literal.pieces.join(''))) return en.matches(literal, isData);
    const file = literal.where.slice(0, literal.where.lastIndexOf(':'));
    return sources.get(file)!.includes('zh-CN')
      ? zhCN.matches(literal, isData)
      : partsOf(literal).every(isData);
  };

  it('has suites to read', () => {
    expect(sources.size).toBeGreaterThan(80);
    expect(literals.length).toBeGreaterThan(600);
  });

  it('matches pages only on locale copy or text in NOT_COPY', () => {
    const drifted = literals
      .filter((literal) => !isCopy(literal))
      .map(
        (literal) =>
          `${literal.where} ${literal.via}(${JSON.stringify(literal.pieces.join('…'))})`
      );
    // Read the copy with en(namespace, key) from e2e/support/copy.ts, or add
    // test data and text a component writes itself to NOT_COPY with a reason.
    expect(drifted).toEqual([]);
  });

  it('lists in NOT_COPY only text a suite still matches on', () => {
    const matched = new Set(
      literals.flatMap((literal) =>
        partsOf(literal).flatMap((part) => [part, ...wordsOf(part)])
      )
    );
    expect(Object.keys(NOT_COPY).filter((text) => !matched.has(text))).toEqual(
      []
    );
  });
});

describe('E2E copy guard', () => {
  it('reads locators, text assertions and selectors', () => {
    const source = [
      "page.getByLabel('Old label');",
      "page.getByRole('button', { name: 'Retry item', exact: true });",
      'page.getByText(/Attempt \\d+ ·/i);',
      "expect(row).toContainText(['Ready', `Total ${n} items`]);",
      "row.filter({ hasText: 'US ·' });",
      "page.locator('text=Empty library');",
      "page.locator('li', { hasText: 'Pending' });",
      "expect(button).toHaveAttribute('aria-label', 'Close');",
      "page.getByText(en('library', 'title'));",
      "expect(value).toBe('not matched on the page');",
    ].join('\n');
    expect(
      textLiterals('s.ts', source).map(({ where, via, case: c, pieces }) => [
        where,
        via,
        c,
        pieces,
      ])
    ).toEqual([
      ['s.ts:1', 'getByLabel', 'insensitive', ['Old label']],
      ['s.ts:2', 'getByRole', 'sensitive', ['Retry item']],
      ['s.ts:3', 'getByText', 'insensitive', ['Attempt ', ' ·']],
      ['s.ts:4', 'toContainText', 'sensitive', ['Ready']],
      ['s.ts:4', 'toContainText', 'sensitive', ['Total ', ' items']],
      ['s.ts:5', 'hasText', 'insensitive', ['US ·']],
      ['s.ts:6', 'selector', 'insensitive', ['Empty library']],
      ['s.ts:7', 'hasText', 'insensitive', ['Pending']],
      ['s.ts:8', 'aria-label', 'sensitive', ['Close']],
    ]);
  });

  it('splits a regular expression into its literal runs', () => {
    expect(regexRuns('Item \\d+: (Succeeded|Reused)')).toEqual([
      'Item ',
      ': ',
      'Succeeded',
      'Reused',
    ]);
    expect(regexRuns('^Start scan · up to \\$')).toEqual([
      'Start scan · up to $',
    ]);
    expect(regexRuns('colou?r (?:red|blue)[0-9]{0,2}x')).toEqual([
      'colo',
      'r ',
      'red',
      'blue',
      'x',
    ]);
  });

  it('accepts copy the way Playwright matches it and nothing else', () => {
    const copy = new LocaleCopy([
      'Qualified creative / offer / source',
      'Attempt {{number}} · {{provider}}/{{model}}',
      'Add {{type}}',
      'Settled as charged',
      'by the Owner',
      '{{count}} source',
      'Retry unavailable: {{reason}}',
      'all allowed attempts have been used',
      'Meta Sandbox · {{mode}}',
    ]);
    const literal = (text: string, textCase: Case = 'insensitive') => ({
      where: '',
      via: '',
      case: textCase,
      pieces: [text],
    });
    const cases: Array<[TextLiteral, boolean]> = [
      [literal('qualified creative'), true],
      [literal('Qualified Creative', 'sensitive'), false],
      [literal('Old creative / offer / source'), false],
      [literal('Attempt 3 ·', 'sensitive'), true],
      [literal('Add Offer'), true],
      [literal('3 source'), true],
      [literal('Settled as charged · by the Owner', 'sensitive'), true],
      [literal('Meta Sandbox · controlled_mock'), true],
      [
        literal(
          'Retry unavailable: all allowed attempts have been used',
          'sensitive'
        ),
        true,
      ],
      [literal('Retry unavailable: anything the page said'), false],
      [literal('attempts_exhausted', 'sensitive'), false],
      [literal('2/3', 'sensitive'), true],
    ];
    expect(
      cases.map(([value]) => [value.pieces[0], copy.matches(value)])
    ).toEqual(cases.map(([value, expected]) => [value.pieces[0], expected]));
    expect(
      copy.matches(
        literal('Brightvolt charger'),
        (text) => text === 'Brightvolt charger'
      )
    ).toBe(true);
  });
});
