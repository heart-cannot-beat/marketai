import fs from 'fs';
import path from 'path';

const root = path.join(__dirname, '..', '..');
const packageJson = JSON.parse(
  fs.readFileSync(path.join(root, 'package.json'), 'utf8')
);

/**
 * The operator scripts are documented as scheduled jobs, so they have to run
 * from a production install (`npm ci --omit=dev`). Behaviour is exercised
 * against the compiled output by `npm run check-ops`, which runs after the
 * build; these assertions cover the packaging contract that makes that
 * possible.
 */
describe('Operator scripts', () => {
  it('run compiled JavaScript, not a TypeScript runtime', () => {
    for (const script of Object.keys(packageJson.scripts).filter((name) =>
      name.startsWith('ops:')
    )) {
      const command: string = packageJson.scripts[script];

      expect(command).toMatch(/^node \.ops\/scripts\/[a-z-]+\.js$/);
      // `ts-node` and `tsconfig-paths` are dev dependencies; a production
      // install would not have them.
      expect(command).not.toContain('ts-node');
      expect(command).not.toContain('tsconfig-paths');
    }
  });

  it('are emitted by the build', () => {
    expect(packageJson.scripts.build).toContain('npm run build:ops');
    expect(packageJson.scripts['build:ops']).toContain(
      'scripts/tsconfig.build.json'
    );
  });

  it('declare every package they import at runtime as a direct dependency', () => {
    // `@next/env` is imported by scripts/loadEnv.ts, so relying on it being
    // hoisted out of `next` would be luck.
    expect(packageJson.dependencies['@next/env']).toBeDefined();
    expect(packageJson.devDependencies['@next/env']).toBeUndefined();
  });

  it('are covered by a production-shaped smoke check in the gate', () => {
    expect(packageJson.scripts['check-ops']).toBe('node scripts/check-ops.mjs');
    expect(packageJson.scripts.verify).toContain('npm run check-ops');
  });

  it('load the runtime env file the way the server does', () => {
    // Comments explain what is deliberately not loaded; assert on code only.
    const loader = fs
      .readFileSync(path.join(root, 'scripts', 'loadEnv.ts'), 'utf8')
      .replace(/\/\*[\s\S]*?\*\//g, '')
      .replace(/^\s*\/\/.*$/gm, '');

    expect(loader).toContain("from '@next/env'");
    expect(loader).toContain('DATABASE_URL is not set');
    // The schema owner belongs to the migration step alone.
    expect(loader).not.toContain('.env.migrate');
  });

  it('keep compiled output out of version control', () => {
    const gitignore = fs.readFileSync(path.join(root, '.gitignore'), 'utf8');

    expect(gitignore).toContain('.ops/');
  });
});
