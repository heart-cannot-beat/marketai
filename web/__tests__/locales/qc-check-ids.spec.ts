import fs from 'node:fs';
import path from 'node:path';
import { STANDARD_CHECKS } from '@/components/workspace/MarketingAIQC';
import { CJK, readNamespace } from './locale-files';

/**
 * The image review names a standard QC check by its id, in the interface
 * language (#308). The server keys every check by id: `CHECKS` in
 * `app/marketing/workflow.py`, plus the `file_valid` check `file_finding` adds
 * in `ai_qc.py`. `required_checks` carries the English criterion the AI judged
 * against; it is evidence and part of the input hash, so it is never reworded
 * for display. The plan's own `intent_<n>` checks show as the plan words them,
 * so they have no copy.
 *
 * The en copy is the criterion verbatim: a change to `CHECKS` (which makes
 * every existing check out of date) changes the copy with it.
 */
const PREFIX = 'marketing-ai-qc-check-';
const WORKFLOW = path.resolve(__dirname, '../../../app/marketing/workflow.py');
const WEB = path.resolve(__dirname, '../..');

const block =
  fs
    .readFileSync(WORKFLOW, 'utf8')
    .match(/^CHECKS = \{\n([\s\S]*?)^\}/m)?.[1]
    .split('\n')
    .filter((line) => line.trim()) ?? [];
const ENTRY = /^\s+"([a-z_]+)":\s*"([^"\\]*)",?$/;
const CHECKS: Record<string, string> = Object.fromEntries(
  block.flatMap((line) => {
    const m = line.match(ENTRY);
    return m ? [[m[1], m[2]]] : [];
  })
);
const ids = [...Object.keys(CHECKS), 'file_valid'].sort();

test('reads every check from the CHECKS literal', () => {
  // A line this pattern cannot read (a split string, an escape) fails here
  // instead of dropping a check.
  expect(block.filter((line) => !ENTRY.test(line))).toEqual([]);
  expect(Object.keys(CHECKS)).toEqual(
    expect.arrayContaining(['product_fidelity', 'expressions_distinct'])
  );
});

test('the review knows exactly the server standard checks', () => {
  expect(Array.from(STANDARD_CHECKS).sort()).toEqual(ids);
});

test.each(['en', 'zh-CN'])('%s words every standard check', (locale) => {
  const keys = readNamespace(locale, 'common');
  expect(ids.filter((id) => !keys.has(PREFIX + id))).toEqual([]);
  if (locale === 'zh-CN')
    expect(
      ids.filter((id) => !CJK.test(String(keys.get(PREFIX + id))))
    ).toEqual([]);
});

test('the en copy is the criterion the AI judged against, verbatim', () => {
  const en = readNamespace('en', 'common');
  expect(
    Object.fromEntries(
      Object.keys(CHECKS).map((id) => [id, en.get(PREFIX + id)])
    )
  ).toEqual(CHECKS);
});

test.each(['en', 'zh-CN'])(
  '%s has no copy for other check ids, intent_* included',
  (locale) => {
    const extra = Array.from(readNamespace(locale, 'common').keys())
      .filter((key) => key.startsWith(PREFIX))
      .map((key) => key.slice(PREFIX.length))
      .filter((id) => !ids.includes(id));
    expect(extra).toEqual([]);
  }
);

test('is rendered through the key family by MarketingAIQC.tsx', () => {
  const source = fs.readFileSync(
    path.join(WEB, 'components/workspace/MarketingAIQC.tsx'),
    'utf8'
  );
  expect(source).toContain(`\`${PREFIX}\${`);
});
