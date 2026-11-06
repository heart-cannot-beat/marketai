import { existsSync } from 'node:fs';
import { loadEnvFile } from 'node:process';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const webRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '..'
);
const envFile = path.resolve(
  webRoot,
  process.env.E2E_LOCALIZATION_ENV_FILE ?? '.env.e2e.localization'
);

if (existsSync(envFile)) {
  loadEnvFile(envFile);
}

const requiredEnvironment = [
  'E2E_LOCALIZATION_ALLOW_MUTATIONS',
  'E2E_OWNER_EMAIL',
  'E2E_OWNER_PASSWORD',
  'E2E_MEMBER_EMAIL',
  'E2E_MEMBER_PASSWORD',
  'E2E_LOCALIZATION_WORKSPACE_ID',
  'E2E_LOCALIZATION_COMPOSE_PROJECT',
  'E2E_LOCALIZATION_CONTROL_OWNER_DATABASE_URL',
  'E2E_OWNER_USER_ID',
  'MARKETAI_DATABASE_URL',
  'MARKETAI_TENANT_CONTEXT_SECRET',
];
const missing = requiredEnvironment.filter(
  (name) => !(process.env[name] ?? '').trim()
);

if (missing.length > 0) {
  const envHint = existsSync(envFile)
    ? envFile
    : `${envFile} (copy .env.e2e.localization.example)`;
  console.error(
    `Localization E2E environment is incomplete; missing: ${missing.join(
      ', '
    )}. Configuration: ${envHint}`
  );
  process.exit(2);
}

if (process.env.E2E_LOCALIZATION_ALLOW_MUTATIONS !== '1') {
  console.error(
    'Localization E2E mutation preflight failed; ' +
      'E2E_LOCALIZATION_ALLOW_MUTATIONS must be exactly 1'
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
    // The Localization specs control one shared Docker Worker (including
    // stop/restart recovery assertions), so parallel files can invalidate one
    // another's process-boundary preconditions. Keep this dedicated suite on
    // one Playwright worker; callers may still pass a later --workers value
    // explicitly for diagnostics.
    '--workers=1',
    'localization',
    ...process.argv.slice(2),
  ],
  {
    cwd: webRoot,
    env: process.env,
    stdio: 'inherit',
  }
);

if (result.error) {
  throw result.error;
}
process.exit(result.status ?? 1);
