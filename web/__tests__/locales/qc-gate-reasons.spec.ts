import fs from 'node:fs';
import path from 'node:path';
import en from '../../locales/en/common.json';
import zhCN from '../../locales/zh-CN/common.json';

/**
 * A QC finding the server held back carries a `gate_reason` (#294 adds
 * rendered_text_mismatch / rendered_text_missing). The review renders it as
 * `marketing-ai-qc-gate-<reason>`, so every reason `normalize_report` can set
 * needs copy in both languages; a missing key shows the raw key instead.
 */
const source = fs.readFileSync(
  path.resolve(__dirname, '../../../app/marketing/ai_qc.py'),
  'utf8'
);
const reasons = Array.from(
  new Set(Array.from(source.matchAll(/gate_reason="([a-z_]+)"/g), (m) => m[1]))
).sort();

test('the gate reasons are read from the server source', () => {
  expect(reasons).toEqual(
    expect.arrayContaining([
      'low_confidence',
      'rendered_text_mismatch',
      'rendered_text_missing',
    ])
  );
});

test.each(reasons)('gate reason %s has copy in en and zh-CN', (reason) => {
  const key = `marketing-ai-qc-gate-${reason}`;
  expect(en).toHaveProperty([key]);
  expect(zhCN).toHaveProperty([key]);
});
