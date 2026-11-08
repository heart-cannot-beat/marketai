import { runSuite } from './e2e-suite.mjs';

runSuite({
  label: 'Experiments',
  envFile: '.env.e2e.experiments',
  envFileVariable: 'E2E_EXPERIMENTS_ENV_FILE',
  required: [
    'E2E_EXPERIMENTS_ALLOW_MUTATIONS',
    'E2E_OWNER_EMAIL',
    'E2E_OWNER_PASSWORD',
    'E2E_MEMBER_EMAIL',
    'E2E_MEMBER_PASSWORD',
    'E2E_EXPERIMENTS_WORKSPACE_ID',
    'E2E_EXPERIMENTS_WORKSPACE_SLUG',
    'E2E_EXPERIMENTS_COMPOSE_PROJECT',
    'E2E_OWNER_USER_ID',
    'MARKETAI_DATABASE_URL',
    'MARKETAI_TENANT_CONTEXT_SECRET',
  ],
  mutationOptIn: 'E2E_EXPERIMENTS_ALLOW_MUTATIONS',
  playwrightArgs: [
    // Each seed retires the Workspace's earlier Meta connections, and Launch
    // recovery stops, starts and restarts the shared API and Worker, so the
    // files cannot run side by side.
    '--workers=1',
    'e2e/experiments/',
  ],
});
