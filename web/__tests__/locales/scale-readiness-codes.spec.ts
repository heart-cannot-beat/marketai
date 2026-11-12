import fs from 'node:fs';
import path from 'node:path';
import { CJK, readNamespace } from './locale-files';

/**
 * The Scale evidence view words a source manifest's typed issues and the
 * readiness recovery action (#114), as `evidence.source-issue.<code>` and
 * `evidence.recovery.<action>` in `scale`. The source collector writes both
 * (`app/scale/collector.py`): every code it raises has a recovery action in
 * `RECOVERY_ACTIONS`, so the codes and actions are read from there.
 */
const WEB = path.resolve(__dirname, '../..');
const collector = fs.readFileSync(
  path.resolve(WEB, '../app/scale/collector.py'),
  'utf8'
);

/** `NAME = "value"` at module level. */
const constants = new Map(
  Array.from(
    collector.matchAll(/^([A-Z][A-Z0-9_]*) = "([a-z0-9_]+)"$/gm),
    (m) => [m[1], m[2]] as const
  )
);

const recoveryBlock =
  collector.match(
    /^RECOVERY_ACTIONS: dict\[str, str\] = \{\n([\s\S]*?)\n\}/m
  )?.[1] ?? '';
const recoveryEntries = Array.from(
  recoveryBlock.matchAll(/^ {4}([A-Z][A-Z0-9_]*): "([a-z_]+)",$/gm),
  (m) => [constants.get(m[1]), m[2]] as const
);
const issueCodes = recoveryEntries.map(([code]) => code);
const actions = Array.from(new Set(recoveryEntries.map(([, a]) => a)));

/** The constants `BLOCKED_CODES` lists: spec §1.7's floor. */
const blockedNames = Array.from(
  (
    collector.match(/^BLOCKED_CODES = frozenset\(\{([\s\S]*?)\}\)/m)?.[1] ?? ''
  ).matchAll(/[A-Z][A-Z0-9_]*/g),
  (m) => m[0]
);

/** The code constant every issue the collector raises starts with. */
const raisedNames = Array.from(
  new Set(
    Array.from(
      collector.matchAll(/\b_(?:Issue|blocked)\(\s*([A-Z][A-Z0-9_]*)\b/g),
      (m) => m[1]
    )
  )
);

const ROWS = [
  {
    name: 'source issue codes',
    values: issueCodes,
    prefix: 'evidence.source-issue.',
  },
  { name: 'recovery actions', values: actions, prefix: 'evidence.recovery.' },
];

test('reads every code and recovery action the collector writes', () => {
  expect(recoveryEntries.length).toBeGreaterThan(0);
  expect(issueCodes.every((code) => typeof code === 'string')).toBe(true);
  expect(issueCodes).toEqual(
    expect.arrayContaining([
      'transport_exhausted',
      'incomplete_pagination',
      'reconciliation_unresolved',
      'contract_instance_failed',
      'source_provenance_mismatch',
      'connection_ineligible',
    ])
  );
  expect(actions.sort()).toEqual([
    'reconcile_meta_sync',
    'repair_connection',
    'retry_meta_sync',
    'wait_for_next_sync',
  ]);
  // A blocking code or a raised issue without a recovery action would
  // reach the page with no words for it.
  const recoverable = new Set(issueCodes);
  expect(blockedNames).toHaveLength(6);
  expect(
    blockedNames.filter((name) => !recoverable.has(constants.get(name)))
  ).toEqual([]);
  expect(raisedNames.length).toBeGreaterThan(0);
  expect(
    raisedNames.filter((name) => !recoverable.has(constants.get(name)))
  ).toEqual([]);
});

describe.each(['en', 'zh-CN'])('%s', (locale) => {
  const keys = readNamespace(locale, 'scale');

  it.each(ROWS.map((row) => [row.name, row] as const))(
    'words every one of the %s',
    (_name, row) => {
      expect(
        row.values.filter((value) => !keys.has(`${row.prefix}${value}`))
      ).toEqual([]);
      if (locale === 'zh-CN')
        expect(
          row.values.filter(
            (value) => !CJK.test(String(keys.get(`${row.prefix}${value}`)))
          )
        ).toEqual([]);
    }
  );

  it.each(ROWS.map((row) => [row.name, row] as const))(
    'has no copy for other %s',
    (_name, row) => {
      const extra = Array.from(keys.keys())
        .filter((key) => key.startsWith(row.prefix))
        .map((key) => key.slice(row.prefix.length))
        .filter((value) => !row.values.includes(value as string));
      expect(extra).toEqual([]);
    }
  );
});

test.each(ROWS.map((row) => [row.name, row] as const))(
  'the %s are rendered through their key family',
  (_name, row) => {
    const timeline = fs.readFileSync(
      path.join(WEB, 'components/scale/detail/EvidenceTimeline.tsx'),
      'utf8'
    );
    expect(timeline).toContain(`\`${row.prefix}\${`);
  }
);
