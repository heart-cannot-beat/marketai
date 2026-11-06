import fs from 'node:fs';
import path from 'node:path';
import en from '../../locales/en/common.json';
import zhCN from '../../locales/zh-CN/common.json';
import {
  exportPackageStatus,
  originLabelKey,
  qcStatus,
  reviewStatus,
  runStatus,
  verdictStatus,
} from '@/lib/assets/status-labels';
import { CJK } from '../locales/locale-files';

/**
 * #255 item 4: the 42 `status-*` labels (moved to en + zh-CN by #252) are
 * produced only through `lib/assets/status-labels.ts` and always rendered
 * through the translator, so a runtime value never shows a raw enum or the
 * other language.
 */
const VALUES = {
  review: [
    'approved',
    'needs_changes',
    'rejected',
    'cleared',
    null,
    'bogus',
  ].map((v) => reviewStatus(v).labelKey),
  qc: [
    'passed',
    'failed_blocking',
    'failed_nonblocking',
    'failed',
    'uncertain',
    'skipped',
    'pending',
    'running',
    'bogus',
  ].map((v) => qcStatus(v).labelKey),
  run: [
    'pending',
    'queued',
    'running',
    'qc_pending',
    'succeeded',
    'reused',
    'partially_succeeded',
    'failed',
    'cancelled',
    'paused',
    'uncertain',
    'bogus',
  ].map((v) => runStatus(v).labelKey),
  origin: ['generation', 'upload', 'reuse', 'legacy', 'localized', 'bogus'].map(
    originLabelKey
  ),
  verdict: ['winner', 'learning', 'loser', 'bogus'].map(
    (v) => verdictStatus(v).labelKey
  ),
  export: ['pending', 'building', 'ready', 'failed', 'expired', 'bogus'].map(
    (v) => exportPackageStatus(v).labelKey
  ),
};
const produced = new Set(Object.values(VALUES).flat());
const statusKeys = Object.keys(en).filter((key) => key.startsWith('status-'));

describe('status-* labels', () => {
  it('are exactly the keys the status helpers produce', () => {
    expect(statusKeys).toHaveLength(42);
    expect(Array.from(produced).sort()).toEqual([...statusKeys].sort());
  });

  it('read in English under en and in Chinese under zh-CN', () => {
    const messages = [en, zhCN] as Record<string, string>[];
    const problems = statusKeys.filter(
      (key) =>
        !messages[0][key] ||
        CJK.test(messages[0][key]) ||
        !CJK.test(messages[1][key] ?? '')
    );
    expect(problems).toEqual([]);
  });

  it('are always rendered through the translator', () => {
    const root = path.resolve(__dirname, '../..');
    const files = ['components', 'lib', 'pages', 'hooks']
      .flatMap((dir) =>
        fs
          .readdirSync(path.join(root, dir), { recursive: true })
          .map((file) => path.join(root, dir, String(file)))
      )
      .filter(
        (file) =>
          /\.tsx?$/.test(file) &&
          !file.includes(`${path.sep}generated${path.sep}`) &&
          !file.endsWith(path.join('lib', 'assets', 'status-labels.ts')) &&
          /status-labels'/.test(fs.readFileSync(file, 'utf8'))
      );
    expect(files.length).toBeGreaterThan(5);
    // A label key is either passed straight to `t`/`tc`, or (FilterBar)
    // collected in a map whose result goes to `t(..., { ns: 'common' })`.
    const untranslated = files.flatMap((file) =>
      fs
        .readFileSync(file, 'utf8')
        .split('\n')
        .map((line, index) => ({ line, index }))
        .filter(
          ({ line }) =>
            /\.labelKey\b|LabelKey\(/.test(line) &&
            !/\bt[c]?\(/.test(line) &&
            !/^\s*(reviewValue|qcState): \(value\) =>/.test(line)
        )
        .map(
          ({ line, index }) =>
            `${path.relative(root, file)}:${index + 1} ${line.trim()}`
        )
    );
    expect(untranslated).toEqual([]);
  });
});
