import fs from 'node:fs';
import path from 'node:path';
import { CJK, readNamespace } from './locale-files';

/**
 * The Scale evidence view words a signal's state and a data-quality
 * finding's code, impact and window (#384), as `evidence.signal-states.*`,
 * `evidence.quality-issue.*`, `evidence.quality-severity.*` and
 * `evidence.window.*` in `scale`. The contract types these fields as free
 * strings, but the values are the `str` Enums of `app/scale/types.py`, which
 * go into the sealed snapshot, so they are read from there.
 */
const WEB = path.resolve(__dirname, '../..');
const APP = path.resolve(WEB, '../app/scale');
const read = (file: string) => fs.readFileSync(path.join(APP, file), 'utf8');
const types = read('types.py');
const models = read('models.py');
const evidence = read('evidence.py');

/**
 * The values of `class <name>(str, Enum):`, up to the next class or the end
 * of the file; member names may carry digits (`CURRENT_7D`).
 */
const enumValues = (name: string) => {
  const body = types.match(
    new RegExp(
      `^class ${name}\\(str, Enum\\):\\n([\\s\\S]*?)(?=\\n\\n\\n|\\nclass |\\n*(?![\\s\\S]))`,
      'm'
    )
  )?.[1];
  if (!body) throw new Error(`${name} not found`);
  return Array.from(
    body.matchAll(/^ {4}[A-Z0-9_]+ = "([a-z0-9_]+)"$/gm),
    (m) => m[1]
  );
};

/** A `NAME = "('a', 'b')"` CHECK value list in `app/scale/models.py`. */
const checkValues = (name: string) =>
  Array.from(
    (
      models.match(
        new RegExp(`^${name} = (\\([\\s\\S]*?\\)|"[^\\n]*")$`, 'm')
      )?.[1] ?? ''
    ).matchAll(/'([a-z0-9_]+)'/g),
    (m) => m[1]
  );

const signalStates = enumValues('SignalState');
const qualityCodes = enumValues('DataQualityIssueCode');
const qualityImpacts = enumValues('DataQualityImpact');
const windowRoles = enumValues('WindowRole');

/** The one writer of a snapshot's quality findings. */
const qualityRows =
  evidence.match(
    /^def _quality_issue_rows\([\s\S]*?(?=^def |^class |^# )/m
  )?.[0] ?? '';

interface Row {
  name: string;
  codes: string[];
  /** Key prefix in `scale`; the code completes the key. */
  prefix: string;
}

const ROWS: Row[] = [
  {
    name: 'signal states',
    codes: signalStates,
    prefix: 'evidence.signal-states.',
  },
  {
    name: 'data-quality codes',
    codes: qualityCodes,
    prefix: 'evidence.quality-issue.',
  },
  {
    name: 'data-quality impacts',
    codes: qualityImpacts,
    prefix: 'evidence.quality-severity.',
  },
  { name: 'window roles', codes: windowRoles, prefix: 'evidence.window.' },
];

test('reads every Enum the evidence fields carry', () => {
  expect(signalStates).toEqual(['hit', 'miss', 'unavailable', 'blocked']);
  expect(qualityCodes).toEqual(
    expect.arrayContaining([
      'daily_slot_missing',
      'exact_window_value_unavailable',
      'breakdown_segment_unknown',
    ])
  );
  expect(qualityImpacts.sort()).toEqual(['blocking', 'warning']);
  expect(windowRoles).toEqual(['current_7d', 'baseline_7d', 'rolling_14d']);
  // The tables' CHECK constraints allow exactly the Enums' values.
  expect(checkValues('SIGNAL_STATE_VALUES')).toEqual(signalStates);
  expect(checkValues('DATA_QUALITY_IMPACT_VALUES').sort()).toEqual(
    qualityImpacts.sort()
  );
});

test('a quality finding is written from those Enums, without detail', () => {
  expect(qualityRows).toContain('code=item.code.value,');
  expect(qualityRows).toContain('severity=item.impact.value,');
  expect(qualityRows).toContain('window_role=loc.window_role.value');
  // `detail` and `recovery_action` stay technical information while nothing
  // writes them; a writer that starts to needs their wording decided first.
  expect(qualityRows).not.toMatch(/\b(detail|recovery_action)=/);
});

describe.each(['en', 'zh-CN'])('%s', (locale) => {
  const keys = readNamespace(locale, 'scale');

  it.each(ROWS.map((row) => [row.name, row] as const))(
    'words every one of the %s',
    (_name, row) => {
      expect(row.codes.filter((code) => !keys.has(row.prefix + code))).toEqual(
        []
      );
      if (locale === 'zh-CN')
        expect(
          row.codes.filter(
            (code) => !CJK.test(String(keys.get(row.prefix + code)))
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
        .filter((code) => !row.codes.includes(code));
      expect(extra).toEqual([]);
    }
  );

  it('has the technical details label', () => {
    expect(keys.has('evidence.technical-details')).toBe(true);
  });
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
