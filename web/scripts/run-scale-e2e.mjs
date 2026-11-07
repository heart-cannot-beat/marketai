import { runSuite } from './e2e-suite.mjs';

runSuite({
  label: 'Scale',
  envFile: '.env.e2e.scale',
  envFileVariable: 'E2E_SCALE_ENV_FILE',
  required: [
    'E2E_SCALE_ALLOW_MUTATIONS',
    'E2E_OWNER_EMAIL',
    'E2E_OWNER_PASSWORD',
    'E2E_MEMBER_EMAIL',
    'E2E_MEMBER_PASSWORD',
    'E2E_SCALE_WORKSPACE_ID',
    'E2E_SCALE_WORKSPACE_SLUG',
    'E2E_SCALE_COMPOSE_PROJECT',
    'E2E_OWNER_USER_ID',
    'MARKETAI_DATABASE_URL',
    'MARKETAI_TENANT_CONTEXT_SECRET',
  ],
  mutationOptIn: 'E2E_SCALE_ALLOW_MUTATIONS',
  playwrightArgs: [
    // The journey restarts the shared API and Worker containers and drains
    // Jobs with a one-shot Worker, so its files cannot run side by side.
    '--workers=1',
    'e2e/scale/',
  ],
});
