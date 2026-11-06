import fs from 'node:fs';
import path from 'node:path';
import { CJK, readNamespace } from './locale-files';

/**
 * The Scale evidence view words why an evaluation left health unclassified
 * (`health_reason`) and why a signal was not computed the normal way (its
 * `reason_code`) (#369), as `evidence.health-reason.<code>` and
 * `evidence.signal-reason.<code>` in `scale`. Both fields are free strings in
 * the contract, and the codes go into the evaluation's result hash, so they
 * are read from where `app/scale/evaluator.py` writes them and never renamed
 * for display.
 */
const HEALTH_PREFIX = 'evidence.health-reason.';
const SIGNAL_PREFIX = 'evidence.signal-reason.';
const WEB = path.resolve(__dirname, '../..');
const source = fs.readFileSync(
  path.resolve(WEB, '../app/scale/evaluator.py'),
  'utf8'
);

/** `_HealthResult(health, health_reason, ...)`, positional. */
const healthResults = Array.from(
  source.matchAll(/_HealthResult\(([^,]+),\s*"([a-z_]*)"/g),
  (m) => ({ health: m[1].trim(), reason: m[2] })
);
const healthKeywords = Array.from(
  source.matchAll(/\bhealth_reason=([^,\n)]+)/g),
  (m) => m[1].trim()
);
const healthReasons = Array.from(
  new Set([
    ...healthResults.map((result) => result.reason).filter(Boolean),
    ...healthKeywords.flatMap(
      (value) => value.match(/^"([a-z_]+)"$/)?.[1] ?? []
    ),
  ])
).sort();

/** `reason_code=` on a signal; a conditional names two codes. */
const signalWrites = Array.from(
  source.matchAll(/\breason_code=([^,\n)]+)/g),
  (m) => m[1].trim()
);
const signalReasons = Array.from(
  new Set(
    signalWrites.flatMap((value) =>
      Array.from(value.matchAll(/"([a-z_]+)"/g), (m) => m[1])
    )
  )
).sort();

/** A signal computed the normal way: its state and values say the rest. */
const UNWORDED = ['ok'];

test('reads every health reason the evaluator writes', () => {
  // A call this pattern cannot read fails here instead of dropping a reason.
  expect(healthResults).toHaveLength(source.match(/_HealthResult\(/g)!.length);
  expect(healthKeywords.filter((value) => !value.startsWith('"'))).toEqual([
    'health_result.health_reason',
  ]);
  expect(healthReasons).toEqual(
    expect.arrayContaining([
      'conflicting_signals',
      'partial_adverse_signals',
      'signal_unavailable',
      'quality_blocked',
    ])
  );
});

test('a health reason is given exactly when health is unclassified', () => {
  // What lets the badge read "not classified" instead of "no evidence".
  expect(
    healthResults.filter(
      (result) => (result.health === 'None') !== (result.reason !== '')
    )
  ).toEqual([]);
});

test('reads every signal reason the evaluator writes', () => {
  // `_signal` passes its argument on; the FDR rebuild copies a signal's own.
  expect(signalWrites.filter((value) => !value.includes('"'))).toEqual([
    'reason_code',
    'signal.reason_code',
  ]);
  expect(signalReasons).toEqual(
    expect.arrayContaining([
      'ok',
      'unavailable_baseline',
      'explained_by_pause',
      'data_quality_blocked',
    ])
  );
});

describe.each(['en', 'zh-CN'])('%s', (locale) => {
  const keys = readNamespace(locale, 'scale');
  const worded = signalReasons.filter((code) => !UNWORDED.includes(code));
  const missing = (prefix: string, codes: string[]) =>
    codes.filter((code) => !keys.has(prefix + code));
  const notChinese = (prefix: string, codes: string[]) =>
    codes.filter((code) => !CJK.test(String(keys.get(prefix + code))));
  const extra = (prefix: string, codes: string[]) =>
    Array.from(keys.keys())
      .filter((key) => key.startsWith(prefix))
      .map((key) => key.slice(prefix.length))
      .filter((code) => !codes.includes(code));

  it('words every health and signal reason', () => {
    expect(missing(HEALTH_PREFIX, healthReasons)).toEqual([]);
    expect(missing(SIGNAL_PREFIX, worded)).toEqual([]);
    expect(keys.has('health.unclassified')).toBe(true);
    if (locale === 'zh-CN') {
      expect(notChinese(HEALTH_PREFIX, healthReasons)).toEqual([]);
      expect(notChinese(SIGNAL_PREFIX, worded)).toEqual([]);
    }
  });

  it('has no copy for other reasons, ok included', () => {
    expect(extra(HEALTH_PREFIX, healthReasons)).toEqual([]);
    expect(extra(SIGNAL_PREFIX, worded)).toEqual([]);
  });
});

test('is rendered through the key families', () => {
  const read = (file: string) =>
    fs.readFileSync(path.join(WEB, 'components/scale', file), 'utf8');
  const timeline = read('detail/EvidenceTimeline.tsx');
  expect(timeline).toContain(`\`${HEALTH_PREFIX}\${`);
  expect(timeline).toContain(`\`${SIGNAL_PREFIX}\${`);
  expect(read('shared/HealthBadge.tsx')).toContain(`'health.unclassified'`);
});
