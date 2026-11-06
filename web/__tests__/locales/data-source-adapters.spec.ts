import fs from 'node:fs';
import path from 'node:path';
import { DATA_SOURCE_ADAPTERS } from '@/components/library/entry-types/data-source/DataSourceDraftEditor';
import { CJK, readNamespace } from './locale-files';

/**
 * The data source form offers a source type and writes the execution mode
 * the server pairs with it (#396). The server owns both sets:
 * `ADAPTER_EXECUTION_MODES` and `EXECUTION_MODE_*` in
 * `app/library/contracts.py` (publishing blocks any other pair with
 * `ds_execution_mode_mismatch`), and `IMPLEMENTED_ADAPTERS` in
 * `app/library/connection_checks.py`, the sources an adapter can serve. An
 * adapter going live fails here first, as the reminder to enable its option.
 */
const APP = path.resolve(__dirname, '../../../app/library');
const read = (file: string) => fs.readFileSync(path.join(APP, file), 'utf8');
const CONTRACTS = read('contracts.py');
const CONNECTION_CHECKS = read('connection_checks.py');

const blockLines = (source: string, header: RegExp) =>
  source
    .match(header)?.[1]
    .split('\n')
    .filter((line) => line.trim()) ?? [];

const MODE_CONSTANT = /^(EXECUTION_MODE_[A-Z]+) = "([a-z_]+)"$/gm;
const MODE_BY_CONSTANT: Record<string, string> = Object.fromEntries(
  Array.from(CONTRACTS.matchAll(MODE_CONSTANT), (m) => [m[1], m[2]])
);
const EXECUTION_MODES = (
  CONTRACTS.match(/^EXECUTION_MODES = frozenset\(\{([^}]*)\}\)$/m)?.[1] ?? ''
)
  .split(',')
  .map((name) => MODE_BY_CONSTANT[name.trim()]);

const PAIR_LINES = blockLines(
  CONTRACTS,
  /^ADAPTER_EXECUTION_MODES: dict\[str, str\] = \{\n([\s\S]*?)^\}/m
);
const PAIR = /^\s+"([a-z_]+)": (EXECUTION_MODE_[A-Z]+),$/;
const PAIRS: Record<string, string> = Object.fromEntries(
  PAIR_LINES.flatMap((line) => {
    const m = line.match(PAIR);
    return m ? [[m[1], MODE_BY_CONSTANT[m[2]]]] : [];
  })
);

const IMPLEMENTED_LINES = blockLines(
  CONNECTION_CHECKS,
  /^IMPLEMENTED_ADAPTERS: dict\[str, str\] = \{\n([\s\S]*?)^\}/m
);
const IMPLEMENTED_ENTRY = /^\s+"([a-z_]+)": [^,]+,$/;
const IMPLEMENTED = IMPLEMENTED_LINES.flatMap((line) => {
  const m = line.match(IMPLEMENTED_ENTRY);
  return m ? [m[1]] : [];
});

test('reads the server sets from their literals', () => {
  // A line these patterns cannot read fails here instead of dropping a value.
  expect(PAIR_LINES.filter((line) => !PAIR.test(line))).toEqual([]);
  expect(
    IMPLEMENTED_LINES.filter((line) => !IMPLEMENTED_ENTRY.test(line))
  ).toEqual([]);
  expect(EXECUTION_MODES.sort()).toEqual(['mock', 'real']);
  expect(Object.keys(PAIRS)).toEqual(
    expect.arrayContaining(['manual', 'mock'])
  );
  expect(IMPLEMENTED).toEqual(expect.arrayContaining(['manual', 'mock']));
});

test('the validator accepts exactly the paired source types', () => {
  expect(CONTRACTS).toMatch(
    /^ADAPTER_KINDS = frozenset\(ADAPTER_EXECUTION_MODES\)$/m
  );
});

test('the form offers every source type the server accepts, and no other', () => {
  expect(DATA_SOURCE_ADAPTERS.map((option) => option.kind).sort()).toEqual(
    Object.keys(PAIRS).sort()
  );
});

test('each option writes the execution mode the server pairs with it', () => {
  expect(
    Object.fromEntries(
      DATA_SOURCE_ADAPTERS.map((option) => [option.kind, option.executionMode])
    )
  ).toEqual(PAIRS);
  for (const option of DATA_SOURCE_ADAPTERS)
    expect(EXECUTION_MODES).toContain(option.executionMode);
});

test('only source types with an adapter can be chosen', () => {
  expect(
    DATA_SOURCE_ADAPTERS.filter((option) => option.available)
      .map((option) => option.kind)
      .sort()
  ).toEqual([...IMPLEMENTED].sort());
});

const FORM_KEYS = [
  'fields.data-source.adapter-kind',
  'fields.data-source.adapter-placeholder',
  'fields.data-source.adapter-unavailable',
  'fields.data-source.adapter-invalid',
  'fields.data-source.execution-mode',
  'fields.data-source.execution-mode-help',
  'fields.data-source.execution-mode-mismatch',
];

test.each(['en', 'zh-CN'])('%s names every source type', (locale) => {
  const keys = readNamespace(locale, 'library');
  const missing = DATA_SOURCE_ADAPTERS.map(
    (option) => `fields.data-source.adapter-${option.kind}`
  ).filter((key) => typeof keys.get(key) !== 'string');
  expect(missing).toEqual([]);
});

test.each(['en', 'zh-CN'])('%s words the form and its prompts', (locale) => {
  const keys = readNamespace(locale, 'library');
  expect(FORM_KEYS.filter((key) => typeof keys.get(key) !== 'string')).toEqual(
    []
  );
  if (locale === 'zh-CN')
    expect(FORM_KEYS.filter((key) => !CJK.test(String(keys.get(key))))).toEqual(
      []
    );
});

test.each(['en', 'zh-CN'])(
  '%s marks the controlled Mock option as an example',
  (locale) => {
    const label = String(
      readNamespace(locale, 'library').get('fields.data-source.adapter-mock')
    );
    expect(label).toMatch(locale === 'en' ? /Controlled Mock/ : /受控 Mock/);
    expect(label).toMatch(locale === 'en' ? /example/ : /示例/);
  }
);
