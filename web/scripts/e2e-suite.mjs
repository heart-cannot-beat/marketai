import { existsSync } from 'node:fs';
import { loadEnvFile } from 'node:process';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const webRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '..'
);

/**
 * Runs one real-backend E2E suite on the `chromium` project (e2e/README.md).
 *
 * Loads the suite's env file (`envFile`, or the path in `envFileVariable`),
 * then `e2e/.auth/identities.env` when the `identities` project has written
 * it. With that file the Owner and Member states already exist, so the
 * password-only `setup` project is skipped with `--no-deps`. Exits 2 before
 * Playwright starts when a required variable is missing or the mutation
 * opt-in is not exactly 1. Extra command-line arguments go to Playwright.
 */
export function runSuite({
  label,
  envFile,
  envFileVariable,
  required,
  mutationOptIn,
  playwrightArgs,
}) {
  const suiteEnvFile = path.resolve(
    webRoot,
    process.env[envFileVariable] ?? envFile
  );
  if (existsSync(suiteEnvFile)) {
    loadEnvFile(suiteEnvFile);
  }
  const identitiesFile = path.join(webRoot, 'e2e', '.auth', 'identities.env');
  const hasIdentities = existsSync(identitiesFile);
  if (hasIdentities) {
    loadEnvFile(identitiesFile);
  }

  const missing = required.filter((name) => !(process.env[name] ?? '').trim());
  if (missing.length > 0) {
    const hint = existsSync(suiteEnvFile)
      ? suiteEnvFile
      : `${suiteEnvFile} (copy ${path.basename(envFile)}.example)`;
    console.error(
      `${label} E2E environment is incomplete; missing: ${missing.join(
        ', '
      )}. Configuration: ${hint}`
    );
    process.exit(2);
  }
  if (process.env[mutationOptIn] !== '1') {
    console.error(
      `${label} E2E mutation preflight failed; ${mutationOptIn} must be exactly 1`
    );
    process.exit(2);
  }

  const playwrightCli = path.join(
    webRoot,
    'node_modules',
    '@playwright',
    'test',
    'cli.js'
  );
  const result = spawnSync(
    process.execPath,
    [
      playwrightCli,
      'test',
      '--config',
      'playwright.config.ts',
      '--project=chromium',
      ...(hasIdentities ? ['--no-deps'] : []),
      ...playwrightArgs,
      ...process.argv.slice(2),
    ],
    { cwd: webRoot, env: process.env, stdio: 'inherit' }
  );
  if (result.error) {
    throw result.error;
  }
  process.exit(result.status ?? 1);
}
