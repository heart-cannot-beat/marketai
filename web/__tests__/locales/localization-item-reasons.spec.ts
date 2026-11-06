import fs from 'node:fs';
import path from 'node:path';
import { ITEM_REASONS } from '@/components/localization/LocalizationWorkspace';
import { familyOf } from '@/lib/i18n/domain-errors';
import { CJK, readNamespace } from './locale-files';

/**
 * A localization Item's `reason_code` says why it was skipped, paused, left
 * waiting for a retry, failed, cancelled or adopted (#369). The inspector
 * words the domain's own reasons as `item.reason.<code>` in `localization`;
 * any other reason is a dispatch outcome the call coordinator reported and
 * reads as its `error-<code>` copy. The field is a free string in the
 * contract, so the codes are read from where the server writes them:
 * `worker.py` and `batch.py` in `app/localization/`.
 */
const PREFIX = 'item.reason.';
const APP = path.resolve(__dirname, '../../../app/localization');
const WEB = path.resolve(__dirname, '../..');
const read = (file: string) => fs.readFileSync(path.join(APP, file), 'utf8');
const worker = read('worker.py');
const batch = read('batch.py');
const truth = read('truth.py');

/** A keyword or attribute, a dict entry, or a subscript set to a value. */
const WRITE =
  /\breason_code\s*=(?!=)\s*([^,\n)]+)|"reason_code":\s*([^,\n}]+)|\["reason_code"\]\s*=\s*([^\n]+)/g;
const LITERAL = /^"([a-z_]+)"$/;
const FALLBACK = /^(.+?) or "([a-z_]+)"$/;

const named: string[] = [];
const indirect: string[] = [];
for (const source of [worker, batch]) {
  for (const m of Array.from(source.matchAll(WRITE))) {
    const value = (m[1] ?? m[2] ?? m[3]).trim();
    const literal = value.match(LITERAL);
    const fallback = value.match(FALLBACK);
    if (literal) named.push(literal[1]);
    else if (fallback) {
      named.push(fallback[2]);
      indirect.push(fallback[1]);
    } else indirect.push(value);
  }
}

/**
 * The writes that pass a value on instead of naming a code, each traced to
 * where its codes come from. A new form fails here instead of slipping by.
 */
const INDIRECT = [
  // Clears the reason: a requeued, resumed or succeeded Item.
  'None',
  'None',
  'None',
  // `_ItemPlan` carries `_pair_initial_disposition`'s reason (read below).
  'combo.reason_code',
  'disposition[1]',
  // `_transition_item` stores what `_pause_item` and
  // `_handle_attempt_failure` pass on; every caller names its code.
  'reason_code',
  'reason_code',
  'reason_code',
  // A failed edit call's code from the coordinator: `error-<code>` copy,
  // which the `error-codes` guard collects.
  'result.error_code',
];

// A market Truth says is unavailable skips its Item; a failed one fails it
// with `truth_<reason>`.
const disposition =
  batch.match(
    /^def _pair_initial_disposition\([\s\S]*?(?=^def |^class |^# )/m
  )?.[0] ?? '';
const dispositionReasons = Array.from(
  disposition.matchAll(/return \(\s*"[a-z]+",\s*([^,\s]+),/g),
  (m) => m[1]
);
const truthFailures = [
  ...Array.from(
    truth.matchAll(/_FAILED, \{"reason": "([a-z_]+)"/g),
    (m) => m[1]
  ),
  ...Array.from(
    batch.matchAll(/detail\.get\("reason"\) or "([a-z_]+)"/g),
    (m) => m[1]
  ),
].map((reason) => `truth_${reason}`);

const codes = Array.from(
  new Set([
    ...named,
    ...dispositionReasons.flatMap((value) => value.match(LITERAL)?.[1] ?? []),
    ...truthFailures,
  ])
).sort();

/**
 * Reasons that are the coordinator's dispatch outcome rather than the
 * domain's: `dispatch_uncertain` has its own error copy, and the fallback for
 * a failed edit call without a code reads as the `dispatch_failed` family.
 */
const VIA_ERROR_COPY = ['dispatch_uncertain', 'edit_dispatch_failed'];

test('reads every reason the server writes', () => {
  // A write this reading cannot follow fails here instead of dropping a code.
  expect(indirect.sort()).toEqual(INDIRECT);
  expect(dispositionReasons.sort()).toEqual([
    '"skipped_market_unavailable"',
    '_truth_failure_code(tpm)',
  ]);
  expect(codes).toEqual(
    expect.arrayContaining([
      'skipped_market_unavailable',
      'truth_price_unresolved',
      'truth_market_failed',
      'ineligible_on_resume',
      'qc_exhausted',
      'adopted_isolated_output',
      'batch_cancelled',
      ...VIA_ERROR_COPY,
    ])
  );
});

test('the inspector words exactly the domain reasons', () => {
  expect(Array.from(ITEM_REASONS).sort()).toEqual(
    codes.filter((code) => !VIA_ERROR_COPY.includes(code))
  );
});

test.each(['en', 'zh-CN'])(
  '%s has error copy for the dispatch outcomes',
  (locale) => {
    const common = readNamespace(locale, 'common');
    const worded = (code: string) =>
      common.has(`error-${code}`) ||
      common.has(`error-${familyOf(code) ?? code}`);
    expect(VIA_ERROR_COPY.filter((code) => !codes.includes(code))).toEqual([]);
    expect(VIA_ERROR_COPY.filter((code) => !worded(code))).toEqual([]);
  }
);

test.each(['en', 'zh-CN'])('%s words every domain reason', (locale) => {
  const keys = readNamespace(locale, 'localization');
  const reasons = Array.from(ITEM_REASONS);
  expect(reasons.filter((code) => !keys.has(PREFIX + code))).toEqual([]);
  if (locale === 'zh-CN')
    expect(
      reasons.filter((code) => !CJK.test(String(keys.get(PREFIX + code))))
    ).toEqual([]);
});

test.each(['en', 'zh-CN'])('%s has no copy for other reasons', (locale) => {
  const extra = Array.from(readNamespace(locale, 'localization').keys())
    .filter((key) => key.startsWith(PREFIX))
    .map((key) => key.slice(PREFIX.length))
    .filter((code) => !ITEM_REASONS.has(code));
  expect(extra).toEqual([]);
});

test('is rendered through the key family by LocalizationWorkspace.tsx', () => {
  const source = fs.readFileSync(
    path.join(WEB, 'components/localization/LocalizationWorkspace.tsx'),
    'utf8'
  );
  expect(source).toContain(`\`${PREFIX}\${`);
});
