import fs from 'node:fs';
import path from 'node:path';
import { CJK, readNamespace } from './locale-files';

/**
 * A data source connection check concludes with one `reason_code` from a
 * closed set and the ConnectionHealth it means (#383). The settings panel
 * words each as `data-source-ops.outcome.<code>` and
 * `data-source-ops.health.<health>` in `library`, and the connector as
 * `data-source-ops.adapter-kind.<kind>`. The contract types `reason_code` as
 * a string, so the sets are read from where the server defines them.
 */
const APP = path.resolve(__dirname, '../../../app/library');
const read = (file: string) => fs.readFileSync(path.join(APP, file), 'utf8');

const block = (source: string, opening: RegExp): string => {
  const start = source.search(opening);
  expect(start).toBeGreaterThanOrEqual(0);
  const end = source.indexOf('\n}', start);
  return source.slice(start, end);
};

/** `REASON_HEALTH = {"code": HEALTH_X, ...}` and the `HEALTH_X = "x"` it names. */
const checks = read('connection_checks.py');
const HEALTH_NAMES = Object.fromEntries(
  Array.from(checks.matchAll(/^(HEALTH_[A-Z]+) = "([a-z]+)"$/gm), (m) => [
    m[1],
    m[2],
  ])
);
const REASONS = Object.fromEntries(
  Array.from(
    block(checks, /^REASON_HEALTH: dict\[str, str\] = \{/m).matchAll(
      /^\s+"([a-z_]+)": (HEALTH_[A-Z]+),$/gm
    ),
    (m) => [m[1], HEALTH_NAMES[m[2]]]
  )
);

/** The ConnectionHealth values the read response admits. */
const RESPONSE_HEALTH = Array.from(
  read('response_models.py')
    .match(/health: Literal\[([^\]]+)\]/)![1]
    .matchAll(/"([a-z]+)"/g),
  (m) => m[1]
);

/**
 * `ADAPTER_KINDS = frozenset(ADAPTER_EXECUTION_MODES)`: the source types are
 * the keys of `ADAPTER_EXECUTION_MODES: dict[str, str] = {"manual": ..., ...}`
 * (`data-source-adapters.spec.ts` guards that derivation).
 */
const ADAPTER_KINDS = Array.from(
  block(
    read('contracts.py'),
    /^ADAPTER_EXECUTION_MODES: dict\[str, str\] = \{/m
  ).matchAll(/^\s+"([a-z_]+)": /gm),
  (m) => m[1]
);

const keysUnder = (namespace: Map<string, unknown>, prefix: string) =>
  Array.from(namespace.keys())
    .filter((key) => key.startsWith(prefix))
    .map((key) => key.slice(prefix.length))
    .sort();

test('reads the closed sets the server defines', () => {
  expect(Object.keys(REASONS).sort()).toEqual([
    'adapter_not_enabled',
    'controlled_mock_source',
    'credential_not_active',
    'data_source_config_invalid',
    'data_source_not_published',
    'manual_source_no_connection',
  ]);
  expect(Object.values(REASONS).every(Boolean)).toBe(true);
  expect(RESPONSE_HEALTH).toEqual([
    'unknown',
    'healthy',
    'degraded',
    'failed',
    'revoked',
  ]);
  expect(ADAPTER_KINDS.sort()).toEqual(['manual', 'mock', 'shopify']);
});

test.each(['en', 'zh-CN'])(
  '%s words every reason, health and connector',
  (locale) => {
    const library = readNamespace(locale, 'library');
    expect(keysUnder(library, 'data-source-ops.outcome.')).toEqual(
      Object.keys(REASONS).sort()
    );
    expect(keysUnder(library, 'data-source-ops.health.')).toEqual(
      [...RESPONSE_HEALTH].sort()
    );
    expect(
      Object.values(REASONS).filter(
        (health) => !RESPONSE_HEALTH.includes(health)
      )
    ).toEqual([]);
    expect(keysUnder(library, 'data-source-ops.adapter-kind.')).toEqual(
      [...ADAPTER_KINDS].sort()
    );
  }
);

test('zh-CN reads as Chinese and en has none', () => {
  const zh = readNamespace('zh-CN', 'library');
  const en = readNamespace('en', 'library');
  for (const prefix of [
    'data-source-ops.outcome.',
    'data-source-ops.health.',
  ]) {
    for (const key of keysUnder(zh, prefix)) {
      expect([key, CJK.test(String(zh.get(prefix + key)))]).toEqual([
        key,
        true,
      ]);
      expect([key, CJK.test(String(en.get(prefix + key)))]).toEqual([
        key,
        false,
      ]);
    }
  }
});
