import fs from 'fs';
import path from 'path';
import { spawnSync } from 'child_process';

const root = path.join(__dirname, '..', '..');
const packageJson = JSON.parse(
  fs.readFileSync(path.join(root, 'package.json'), 'utf8')
);
const envExample = fs.readFileSync(path.join(root, '.env.example'), 'utf8');
const envMigrateExample = fs.readFileSync(
  path.join(root, '.env.migrate.example'),
  'utf8'
);
const gitignore = fs.readFileSync(path.join(root, '.gitignore'), 'utf8');

const docs = [
  path.join(root, 'README.md'),
  path.join(root, '.env.migrate.example'),
  path.join(root, '..', 'README.md'),
  path.join(
    root,
    '..',
    'docs',
    'architecture',
    'control-plane-fork-boundary.md'
  ),
].map((file) => ({ file, body: fs.readFileSync(file, 'utf8') }));

/**
 * The runtime role holds DML only. Pointing `prisma migrate` at it fails with a
 * permission error, so the documented boot sequence has to name a separate
 * schema-owner identity.
 */
describe('Migration identity', () => {
  it('routes every migrate script through the owner-role wrapper', () => {
    for (const script of [
      'db:migrate:dev',
      'db:migrate:deploy',
      'db:migrate:status',
    ]) {
      expect(packageJson.scripts[script]).toContain(
        'node scripts/prisma-migrate.mjs'
      );
    }
  });

  it('documents the runtime role and the migration role in separate files', () => {
    expect(envExample).toMatch(
      /^DATABASE_URL=postgresql:\/\/marketai_control_app:/m
    );
    expect(envMigrateExample).toMatch(
      /^MIGRATION_DATABASE_URL=postgresql:\/\/marketai_control_owner:/m
    );
  });

  it('keeps the schema-owner credential out of the runtime environment', () => {
    // `web/.env` is loaded automatically by Next.js and becomes the running
    // process's environment, so the owner credential must not be templated
    // into it.
    expect(envExample).not.toContain('MIGRATION_DATABASE_URL=');
    expect(envExample).not.toContain('marketai_control_owner');
    expect(gitignore).toContain('.env.migrate');
  });

  it('refuses to run without MIGRATION_DATABASE_URL instead of falling back', () => {
    const env = { ...process.env };
    delete env.MIGRATION_DATABASE_URL;
    env.DATABASE_URL =
      'postgresql://marketai_control_app:pw@localhost:5432/marketai_control';

    const result = spawnSync(
      process.execPath,
      [path.join(root, 'scripts', 'prisma-migrate.mjs'), 'status'],
      { env, encoding: 'utf8' }
    );

    expect(result.status).toBe(1);
    expect(result.stderr).toContain('MIGRATION_DATABASE_URL is not set');
    // It must not have tried the runtime role.
    expect(result.stdout).not.toContain('migrations found');
  });

  it('still never pushes the schema', () => {
    for (const [name, command] of Object.entries(
      packageJson.scripts as Record<string, string>
    )) {
      expect(`${name}: ${command}`).not.toContain('db push');
    }
  });

  it('documents loading the migration credential in a subshell', () => {
    // `set +a` stops further exports; it does not unexport what was already
    // exported. Without the parentheses the schema-owner credential stays in
    // the shell that then starts the server.
    const offenders: string[] = [];

    for (const { file, body } of docs) {
      for (const match of Array.from(
        body.matchAll(/^.*\.\/\.env\.migrate.*$/gm)
      )) {
        const line = match[0].trim();

        if (line.startsWith('#')) continue;
        if (line.startsWith('(set -a;')) continue;

        offenders.push(`${path.basename(file)}: ${line}`);
      }
    }

    expect(offenders).toEqual([]);
  });

  it('shows the migration credential in no runtime start-up command', () => {
    for (const { body } of docs) {
      expect(body).not.toMatch(/set -a && \. \.\/\.env\.migrate/);
    }
  });
});
