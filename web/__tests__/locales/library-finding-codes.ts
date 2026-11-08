import fs from 'node:fs';
import path from 'node:path';

/**
 * Reads the code of every Creative Library validation finding from the
 * server's source: each `Finding(...)` call under `app/library` (#20). A
 * finding reaches the draft page (`POST .../validate`) and the publish dialog
 * (`projection.blocking` / `warnings` of the publish impact) as
 * `Finding.as_dict()`, so these are all the codes the interface can be given.
 *
 * The code argument is read in three forms:
 * - a string literal, `"fact_currency"`: one code;
 * - an f-string whose only replacement field ends it,
 *   `f"reference_evidence_{field}"`: a template, read as its fixed prefix;
 * - `problem.code` from `fact_value_problems(...)` (`app/library/fact_types.py`):
 *   the codes of its `FactValueProblem(...)` calls.
 * Any other form is listed in `unreadable` instead of being skipped, so a
 * code this reader cannot see fails the guard rather than going without copy.
 */
export const LIBRARY = path.resolve(__dirname, '../../../app/library');

export interface FindingCodes {
  /** Codes written out in full, sorted. */
  codes: string[];
  /** Fixed prefixes of the f-string codes, sorted. */
  templates: string[];
  /** `file:line: code expression` for each call whose code is not readable. */
  unreadable: string[];
}

interface Call {
  line: number;
  args: string[];
}

const STRING_START = /^(?:[rRbBuUfF]{1,2})?('''|"""|'|")/;

/**
 * `source` with every string literal's body and every comment replaced by
 * spaces (line breaks kept), so brackets, commas and names inside them are
 * never read as code. Offsets and line numbers are unchanged.
 */
export function maskPython(source: string): string {
  const out = source.split('');
  const blank = (from: number, to: number) => {
    for (let k = from; k < to; k++) if (out[k] !== '\n') out[k] = ' ';
  };
  let i = 0;
  while (i < source.length) {
    const c = source[i];
    if (c === '#') {
      const end = source.indexOf('\n', i);
      const stop = end === -1 ? source.length : end;
      blank(i, stop);
      i = stop;
      continue;
    }
    const start = source.slice(i, i + 5).match(STRING_START);
    // `f"` starts a string; the `f"` ending a name such as `buf"` does not.
    const prefixed = start !== null && start[0].length > start[1].length;
    if (!start || (prefixed && /[A-Za-z0-9_]/.test(source[i - 1] ?? ''))) {
      i++;
      continue;
    }
    const quote = start[1];
    let j = i + start[0].length;
    while (j < source.length && !source.startsWith(quote, j)) {
      j += source[j] === '\\' ? 2 : 1;
    }
    blank(i + start[0].length, j);
    i = j + quote.length;
  }
  return out.join('');
}

/** The top-level arguments of every `name(...)` call in `source`. */
export function readCalls(source: string, name: string): Call[] {
  const masked = maskPython(source);
  const pattern = new RegExp(`(?<![A-Za-z0-9_])${name}\\(`, 'g');
  return Array.from(masked.matchAll(pattern), (match) => {
    const open = match.index! + match[0].length;
    const args: string[] = [];
    let depth = 1;
    let from = open;
    let i = open;
    for (; i < masked.length && depth > 0; i++) {
      const c = masked[i];
      if ('([{'.includes(c)) depth++;
      else if (')]}'.includes(c)) depth--;
      if ((c === ',' && depth === 1) || depth === 0) {
        args.push(source.slice(from, i).trim());
        from = i + 1;
      }
    }
    return {
      line: source.slice(0, match.index).split('\n').length,
      // A trailing comma leaves an empty last argument.
      args: args.filter(Boolean),
    };
  });
}

const KEYWORD = /^([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)\s*([\s\S]*)$/;

/** The argument passed as `keyword=` or, failing that, at `position`. */
export function argument(
  args: string[],
  keyword: string,
  position: number
): string | undefined {
  for (const arg of args) {
    const named = arg.match(KEYWORD);
    if (named && named[1] === keyword) return named[2].trim();
  }
  const positional = args.filter((arg) => !KEYWORD.test(arg));
  return positional[position];
}

/** The text of a plain one-part string literal, else `null`. */
const literal = (expression: string): string | null =>
  expression.match(/^"([^"\\{}]*)"$/)?.[1] ??
  expression.match(/^'([^'\\{}]*)'$/)?.[1] ??
  null;

/** The fixed prefix of `f"prefix{expr}"`, else `null`. */
const templatePrefix = (expression: string): string | null =>
  expression.match(/^[fF]"([^"\\{}]+)\{[^{}"]+\}"$/)?.[1] ??
  expression.match(/^[fF]'([^'\\{}]+)\{[^{}']+\}'$/)?.[1] ??
  null;

const pythonFiles = (dir: string): string[] =>
  fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) return pythonFiles(full);
    return entry.name.endsWith('.py') ? [full] : [];
  });

/**
 * The codes of `FactValueProblem(...)`: its dataclass fields are positional
 * arguments in declaration order, so the code is read by the position of
 * `code` in the class body.
 */
function factValueProblemCodes(factTypes: string, file: string) {
  const body = factTypes.match(
    /^class FactValueProblem:\n((?:(?:[ ]{4}.*)?\n)+)/m
  )?.[1];
  const fields = Array.from(
    (body ?? '').matchAll(/^ {4}([a-z_]+): /gm),
    (m) => m[1]
  );
  const position = fields.indexOf('code');
  const codes: string[] = [];
  const unreadable: string[] = [];
  if (position === -1) {
    unreadable.push(`${file}: class FactValueProblem has no code field`);
    return { codes, unreadable };
  }
  for (const { line, args } of readCalls(factTypes, 'FactValueProblem')) {
    const expression = argument(args, 'code', position) ?? '';
    const code = literal(expression);
    if (code === null) unreadable.push(`${file}:${line}: ${expression}`);
    else codes.push(code);
  }
  return { codes, unreadable };
}

/**
 * Codes passed as an expression that names where they come from. Each
 * entry reads the codes at that source; any expression not listed here is
 * unreadable.
 */
const INDIRECT = new Map<
  string,
  (
    source: string,
    root: string
  ) => { codes: string[]; unreadable: string[] } | null
>([
  [
    'problem.code',
    (source, root) => {
      if (!/\bfor problem in fact_value_problems\(/.test(source)) return null;
      const file = 'fact_types.py';
      return factValueProblemCodes(
        fs.readFileSync(path.join(root, file), 'utf8'),
        file
      );
    },
  ],
]);

/** The finding codes of `sources` (`{relative file name: Python source}`). */
export function findingCodesOf(
  sources: Record<string, string>,
  root = LIBRARY
): FindingCodes {
  const codes = new Set<string>();
  const templates = new Set<string>();
  const unreadable: string[] = [];
  for (const [file, source] of Object.entries(sources)) {
    for (const { line, args } of readCalls(source, 'Finding')) {
      const expression = (argument(args, 'code', 0) ?? '').replace(/\s+/g, ' ');
      const code = literal(expression);
      if (code !== null) {
        codes.add(code);
        continue;
      }
      const prefix = templatePrefix(expression);
      if (prefix !== null) {
        templates.add(prefix);
        continue;
      }
      const indirect = INDIRECT.get(expression)?.(source, root);
      if (indirect) {
        indirect.codes.forEach((c) => codes.add(c));
        unreadable.push(...indirect.unreadable);
        continue;
      }
      unreadable.push(`${file}:${line}: ${expression || '(no code)'}`);
    }
  }
  return {
    codes: Array.from(codes).sort(),
    templates: Array.from(templates).sort(),
    unreadable,
  };
}

/** The finding codes of every `.py` file under `app/library`. */
export function libraryFindingCodes(): FindingCodes {
  return findingCodesOf(
    Object.fromEntries(
      pythonFiles(LIBRARY).map((file) => [
        path.relative(LIBRARY, file),
        fs.readFileSync(file, 'utf8'),
      ])
    )
  );
}
