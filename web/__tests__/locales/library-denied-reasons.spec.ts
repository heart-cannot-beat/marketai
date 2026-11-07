import fs from 'node:fs';
import path from 'node:path';
import { CJK, readNamespace } from './locale-files';

/**
 * Why the server refuses a Creative Library command
 * (`ActionEligibility.reason_code`, a free string in the contract) is worded
 * as `commands.denied.<code>` in `library` (#399). Entry and Version
 * eligibility come only from `_entry_allowed_actions` and
 * `_version_allowed_actions` in `app/library/v1_router.py`, so the codes are
 * read from there. A code added there without en and zh-CN copy fails here
 * instead of reaching the toolbar as a raw code, and so does copy for a code
 * the server never gives.
 */
const PREFIX = 'commands.denied.';
const LIBRARY = path.resolve(__dirname, '../../../app/library');
const WEB = path.resolve(__dirname, '../..');
const router = fs.readFileSync(path.join(LIBRARY, 'v1_router.py'), 'utf8');

const unique = (codes: string[]) => Array.from(new Set(codes)).sort();

/** The body of a top-level `def name(`, up to the next top-level statement. */
const functionBody = (source: string, name: string) => {
  const body = source.match(
    new RegExp(
      `^def ${name}\\([\\s\\S]*?(?=^def |^class |^# |^[A-Z_]+ = )`,
      'm'
    )
  )?.[0];
  if (!body) throw new Error(`${name} not found`);
  return body;
};

/** The top-level arguments of each `_check(...)` call. */
const checkCalls = (source: string) =>
  Array.from(source.matchAll(/\b_check\(/g), (m) => {
    const args: string[] = [];
    let depth = 1;
    let from = m.index! + m[0].length;
    for (let i = from; depth > 0 && i < source.length; i++) {
      const c = source[i];
      if (c === '(') depth++;
      else if (c === ')') depth--;
      if ((c === ',' && depth === 1) || depth === 0) {
        args.push(source.slice(from, i).trim());
        from = i + 1;
      }
    }
    // A trailing comma leaves an empty last argument.
    return args.filter(Boolean);
  });

/**
 * The codes of a reason written as `"<code>"` or
 * `"<code>" if <condition> else <reason>`, with no literal in a condition;
 * `null` for any other form (a variable, a call), which needs its own
 * reading.
 */
const reasonCodes = (expression: string): string[] | null => {
  const text = expression
    .trim()
    .replace(/^\(([\s\S]*)\)$/, '$1')
    .trim();
  const literal = text.match(/^"([a-z_]+)"$/);
  if (literal) return [literal[1]];
  const conditional = text.match(
    /^"([a-z_]+)"\s+if\s+[^"]+?\s+else\s+([\s\S]+)$/
  );
  if (!conditional) return null;
  const rest = reasonCodes(conditional[2]);
  return rest && [conditional[1], ...rest];
};

const eligibility = ['_entry_allowed_actions', '_version_allowed_actions'].map(
  (name) => {
    const body = functionBody(router, name);
    const checks = body.match(/^ {4}checks = \{\n([\s\S]*?)^ {4}\}$/m)?.[1];
    if (!checks) throw new Error(`${name} has no checks dict`);
    const calls = checkCalls(checks);
    return {
      name,
      body,
      actions: Array.from(checks.matchAll(/^\s*"([a-z_]+)":/gm), (m) => m[1]),
      calls,
      reasons: calls.map(([, reason]) => reasonCodes(reason ?? '')),
    };
  }
);
const codes = unique(
  eligibility.flatMap(({ reasons }) => reasons.flatMap((r) => r ?? []))
);

test('reads every reason the Library eligibility functions give', () => {
  for (const { name, body, actions, calls, reasons } of eligibility) {
    // Every listed action is evaluated, so none comes back `not_evaluated`.
    expect([
      name,
      body.includes('compute_actions(list(checks), checks'),
    ]).toEqual([name, true]);
    // One `_check(allowed, reason)` per action, nothing else in the dict.
    expect(calls).toHaveLength(actions.length);
    expect(body.match(/\b_check\(/g)).toHaveLength(actions.length);
    expect(calls.filter((args) => args.length !== 2)).toEqual([]);
    expect(
      calls.filter((_, i) => reasons[i] === null).map(([, reason]) => reason)
    ).toEqual([]);
  }
  expect(eligibility[0].actions).toEqual(
    expect.arrayContaining(['publish', 'archive', 'bind_reference'])
  );
  expect(eligibility[1].actions).toEqual(
    expect.arrayContaining(['withdraw', 'fork', 'clone_product'])
  );
  expect(codes).toEqual(
    expect.arrayContaining([
      'entry_archived',
      'no_draft',
      'owner_required',
      'invalid_state',
      'entry_type_mismatch',
    ])
  );
});

test('only the two eligibility functions set allowed_actions in app/library', () => {
  const pythonFiles = (dir: string): string[] =>
    fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) return pythonFiles(full);
      return entry.name.endsWith('.py') ? [full] : [];
    });
  // A keyword argument, a dict entry, or a subscript set to a value.
  const WRITE =
    /^.*(?:\ballowed_actions\s*=(?!=)|"allowed_actions"\s*(?::|\]\s*=(?!=))).*$/gm;
  const writes = pythonFiles(LIBRARY).flatMap((file) =>
    Array.from(
      fs.readFileSync(file, 'utf8').matchAll(WRITE),
      (m) => `${path.relative(LIBRARY, file)}: ${m[0].trim()}`
    )
  );
  expect(writes.sort()).toEqual([
    'v1_router.py: entry["allowed_actions"] = _entry_allowed_actions(entry, context)',
    'v1_router.py: version["allowed_actions"] = _version_allowed_actions(version, context)',
  ]);
});

describe.each(['en', 'zh-CN'])('%s', (locale) => {
  const keys = readNamespace(locale, 'library');

  it('words every reason the server gives', () => {
    expect(codes.filter((code) => !keys.has(PREFIX + code))).toEqual([]);
    if (locale === 'zh-CN')
      expect(
        codes.filter((code) => !CJK.test(String(keys.get(PREFIX + code))))
      ).toEqual([]);
  });

  it('has no copy for a reason the server never gives', () => {
    const extra = Array.from(keys.keys())
      .filter((key) => key.startsWith(PREFIX))
      .map((key) => key.slice(PREFIX.length))
      .filter((code) => !codes.includes(code));
    expect(extra).toEqual([]);
  });

  it('has a sentence for a reason without copy', () => {
    expect(keys.has('commands.denied-default')).toBe(true);
    if (locale === 'zh-CN')
      expect(CJK.test(String(keys.get('commands.denied-default')))).toBe(true);
  });
});

test('both Library action toolbars word reasons through deniedReason', () => {
  const read = (file: string) =>
    fs.readFileSync(
      path.join(WEB, 'components/library/commands', file),
      'utf8'
    );
  const entryActions = read('EntryActions.tsx');
  // `deniedReason` is the one place that reads `commands.denied.<code>`.
  expect(entryActions).toContain('export function deniedReason(');
  expect(entryActions.match(/\bt\(`commands\.denied\.[^`]*`/g)).toEqual([
    `t(\`${PREFIX}\${code}\``,
  ]);
  const versionActions = read('VersionActions.tsx');
  expect(versionActions).toContain('import { deniedReason');
  expect(versionActions).not.toContain('commands.denied.');
  for (const [file, source] of [
    ['EntryActions.tsx', entryActions],
    ['VersionActions.tsx', versionActions],
  ]) {
    expect([
      file,
      source.includes('deniedReason(t, action.reason_code)'),
    ]).toEqual([file, true]);
    expect([file, source.includes('defaultValue: action.reason_code')]).toEqual(
      [file, false]
    );
  }
});
