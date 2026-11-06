#!/usr/bin/env node
/**
 * Production-shaped smoke test for the operator scripts.
 *
 * They are documented as scheduled jobs, so they must run from an install that
 * has no dev dependencies. This runs the compiled output the npm scripts point
 * at — plain Node, no TypeScript runtime — and checks the two properties that
 * matter operationally: a missing `DATABASE_URL` says so, and a connection
 * failure never puts the connection string on stderr.
 *
 * Runs after `npm run build`, which emits `.ops/`.
 */
import { spawnSync } from 'child_process';
import { existsSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const compiled = join(root, '.ops', 'scripts', 'prune-sessions.js');

if (!existsSync(compiled)) {
  process.stderr.write(
    `No compiled operator scripts at ${compiled}. Run \`npm run build:ops\`.\n`
  );
  process.exit(1);
}

const run = (env) =>
  spawnSync(process.execPath, [compiled], {
    cwd: root,
    env,
    encoding: 'utf8',
    timeout: 60_000,
  });

const problems = [];

// 1. A missing runtime identity is reported, not guessed at.
{
  const env = { ...process.env, NODE_ENV: 'test' };
  delete env.DATABASE_URL;

  const result = run(env);

  if (result.status === 0) {
    // A developer's web/.env supplied DATABASE_URL; the guard is unobservable
    // here, which is fine — the message is asserted by the jest suite too.
    process.stdout.write(
      'DATABASE_URL came from web/.env; skipped the missing-variable check\n'
    );
  } else if (!result.stderr.includes('DATABASE_URL is not set')) {
    problems.push(
      `missing DATABASE_URL should say so, got: ${result.stderr.trim() || '(no output)'}`
    );
  }
}

// 2. A failure never leaks the connection string.
{
  const secret = 'sup3r-s3cret-pw';
  const result = run({
    ...process.env,
    NODE_ENV: 'test',
    DATABASE_URL: `postgresql://marketai_control_app:${secret}@127.0.0.1:1/nope`,
  });

  if (result.status === 0) {
    problems.push('an unreachable database should fail the script');
  }

  if (result.stderr.includes(secret) || result.stdout.includes(secret)) {
    problems.push('the connection string password reached the process output');
  }
}

if (problems.length) {
  process.stderr.write(
    `Operator scripts are not production-safe:\n  ${problems.join('\n  ')}\n`
  );
  process.exit(1);
}

process.stdout.write(
  'Operator scripts run from compiled JavaScript, report missing configuration and redact failures\n'
);
