import { runSuite } from './e2e-suite.mjs';

runSuite({
  label: 'Localization',
  envFile: '.env.e2e.localization',
  envFileVariable: 'E2E_LOCALIZATION_ENV_FILE',
  required: [
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
  ],
  mutationOptIn: 'E2E_LOCALIZATION_ALLOW_MUTATIONS',
  playwrightArgs: [
    // The Localization specs control one shared Docker Worker (including
    // stop/restart recovery assertions), so parallel files can invalidate one
    // another's process-boundary preconditions. Keep this dedicated suite on
    // one Playwright worker; callers may still pass a later --workers value
    // explicitly for diagnostics.
    '--workers=1',
    'e2e/localization/',
  ],
});
