import fs from 'node:fs';
import path from 'node:path';
import { CJK, placeholdersOf, readNamespace } from './locale-files';

/**
 * #255: CSV preflight issues and results-import refusals reach the browser
 * as `code` (+ `params`) and read from the shared `error-<code>` table
 * (#253; `error-codes.spec.ts` checks every code has copy). This suite checks
 * what that one cannot: the copy's placeholders against the params each
 * Python table declares, and the labels the issue lines are built from.
 */
const APP = path.resolve(__dirname, '../../../app');
const source = (file: string) => fs.readFileSync(path.join(APP, file), 'utf8');

/** The `NAME = {...}` / `NAME = (...)` literal that starts at `name`. */
const block = (text: string, name: string) => {
  const start = text.search(new RegExp(`^${name}\\b[^=]*= [({]`, 'm'));
  if (start < 0) throw new Error(`${name} not found`);
  const end = text.indexOf('\n}\n', start);
  const tupleEnd = text.indexOf(')\n', start);
  return text.slice(
    start,
    text[text.indexOf('=', start) + 2] === '{' ? end : tupleEnd
  );
};

/** `"code": ("a", "b")` entries of a dict literal. */
const paramTable = (text: string, name: string) =>
  new Map(
    Array.from(
      block(text, name).matchAll(/^\s+"([a-z_]+)": \(([^)]*)\),$/gm),
      (m) => [m[1], Array.from(m[2].matchAll(/"([a-z_]+)"/g), (p) => p[1])]
    )
  );

/** The string keys of a `{"code": ..., ...}` dict literal. */
const dictKeys = (text: string, name: string) =>
  Array.from(block(text, name).matchAll(/^\s+"([a-z_]+)":/gm), (m) => m[1]);

const tupleItems = (text: string, name: string) =>
  Array.from(block(text, name).matchAll(/"([a-z_]+)"/g), (m) => m[1]);

const wording = (namespace: string) => ({
  en: readNamespace('en', namespace),
  zh: readNamespace('zh-CN', namespace),
});

/** Missing copy, English with CJK, or Chinese without it. */
const missing = (messages: ReturnType<typeof wording>, key: string) => {
  const en = messages.en.get(key);
  const zh = messages.zh.get(key);
  return [
    ...(typeof en !== 'string' || !en || CJK.test(en) ? [`${key}: en`] : []),
    ...(typeof zh !== 'string' || !CJK.test(zh) ? [`${key}: zh-CN`] : []),
  ];
};

/** Placeholders a value uses that `allowed` does not send, or (exact) the reverse. */
const drift = (
  messages: ReturnType<typeof wording>,
  key: string,
  allowed: string[],
  exact: boolean
) =>
  (['en', 'zh'] as const).flatMap((locale) => {
    const used = placeholdersOf(messages[locale].get(key));
    const ok = exact
      ? used.join(',') === [...allowed].sort().join(',')
      : used.every((name) => allowed.includes(name));
    return ok ? [] : [`${key}: ${locale} {{${used}}} vs params {{${allowed}}}`];
  });

const common = wording('common');

describe('CSV preflight issues (app/marketing/metrics.py)', () => {
  const metrics = source('marketing/metrics.py');
  const issues = paramTable(metrics, 'ISSUE_PARAMS');
  const fields = tupleItems(metrics, 'ISSUE_FIELDS');

  it('reads the server tables', () => {
    expect(issues.size).toBeGreaterThan(20);
    expect(issues.get('outside_round_dates')).toEqual(['start', 'end']);
    expect(fields).toEqual(expect.arrayContaining(['date', 'context']));
  });

  it('words every code with exactly the params each issue carries', () => {
    // An issue is worded once per row, with its own params.
    const problems = Array.from(issues).flatMap(([code, params]) => [
      ...missing(common, `error-${code}`),
      ...drift(common, `error-${code}`, params, true),
    ]);
    expect(problems).toEqual([]);
  });

  it('names every field an issue can point at', () => {
    expect(
      fields.flatMap((field) =>
        missing(common, `marketing-issue-field-${field}`)
      )
    ).toEqual([]);
  });
});

describe('Results-import refusals (app/learnings)', () => {
  const parsing = source('learnings/parsing.py');
  const attribution = source('learnings/attribution.py');
  const reasons = dictKeys(parsing, 'REASONS');
  const params = paramTable(parsing, 'REASON_PARAMS');
  const descriptions = dictKeys(attribution, 'NO_PLAN_LABELS');
  const learnings = wording('learnings');

  it('reads the server tables', () => {
    expect(reasons).toEqual(
      expect.arrayContaining(['unknown_code', 'currency_mismatch'])
    );
    expect(params.get('currency_mismatch')).toEqual(['expected', 'found']);
    expect(descriptions).toEqual(['external_asset', 'other_asset']);
  });

  it('words every reason using no value its rows do not carry', () => {
    // The rejected-rows summary words a code once for many rows, without
    // their params, so the copy may use fewer than the rows carry.
    const problems = reasons.flatMap((code) => [
      ...missing(common, `error-${code}`),
      ...drift(common, `error-${code}`, params.get(code) ?? [], false),
    ]);
    expect(problems).toEqual([]);
    expect(
      Array.from(params.keys()).filter((code) => !reasons.includes(code))
    ).toEqual([]);
  });

  it('words every description code', () => {
    expect(
      descriptions.flatMap((code) => missing(learnings, `description-${code}`))
    ).toEqual([]);
  });
});
