import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';

const script = path.join(__dirname, '..', '..', 'scripts', 'i18n-missing.mjs');

const run = (...args: string[]) =>
  spawnSync(process.execPath, [script, ...args], { encoding: 'utf8' });

describe('scripts/i18n-missing.mjs', () => {
  it('reports the real locale files as complete', () => {
    const result = run();
    expect(result.stdout).toContain('has exactly the keys of en');
    expect(result.status).toBe(0);
  });

  it('names every missing key, extra key, placeholder drift and missing namespace', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'i18n-missing-'));
    const write = (locale: string, ns: string, value: unknown) => {
      fs.mkdirSync(path.join(dir, locale), { recursive: true });
      fs.writeFileSync(
        path.join(dir, locale, `${ns}.json`),
        JSON.stringify(value)
      );
    };
    write('en', 'common', {
      kept: 'Kept',
      gone: 'Gone',
      count: '{{count}} images',
      nested: { deep: 'Deep' },
    });
    write('en', 'tests', { title: 'Tests' });
    write('zh-CN', 'common', {
      kept: '保留',
      count: '{{total}} 张',
      nested: { deep: '深', extra: '多余' },
    });

    try {
      const result = run('--locales-dir', dir);
      expect(result.status).toBe(1);
      expect(result.stdout).toContain('zh-CN/common.json: missing  gone');
      expect(result.stdout).toContain(
        'zh-CN/common.json: extra    nested.extra'
      );
      expect(result.stdout).toContain(
        'zh-CN/common.json: placeholders differ from en  count'
      );
      expect(result.stdout).toContain('zh-CN/tests.json: missing');

      const json = JSON.parse(run('--locales-dir', dir, '--json').stdout);
      expect(json).toEqual(
        expect.arrayContaining([
          expect.objectContaining({
            locale: 'zh-CN',
            namespace: 'common',
            missing: ['gone'],
            extra: ['nested.extra'],
            placeholderMismatch: ['count'],
          }),
          { locale: 'zh-CN', namespace: 'tests', problem: 'missing-namespace' },
        ])
      );
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
});
