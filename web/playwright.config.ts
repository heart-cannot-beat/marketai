import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright configuration for the workspace shell E2E suite.
 *
 * Scope: these tests exercise the full stack described in
 * docs/architecture/saas-ui-fastapi-integration-contract.md — Next.js dev
 * server, PostgreSQL, FastAPI, and local blob transfer must all be running.
 * They are not part of `make verify` and are intended to be run manually
 * (or from a dedicated CI job) against a seeded environment. See
 * `e2e/workspace/fixtures.ts` for the seed-data assumptions (an `owner` and
 * a `member` credential, each belonging to at least one Workspace).
 *
 * The `setup` project authenticates once per run and persists browser
 * storage state to `e2e/.auth/*.json`, which the `chromium` project then
 * reuses instead of re-authenticating in every test file.
 */
export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  retries: process.env.CI ? 2 : 0,
  use: {
    // The control plane's dev/start scripts bind port 4002 (see
    // package.json "dev"/"start" and .env.example APP_URL/NEXTAUTH_URL),
    // not Next.js's default 3000.
    baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:4002',
    trace: 'on-first-retry',
  },
  projects: [
    {
      name: 'setup',
      testMatch: /workspace\/fixtures\.ts/,
    },
    {
      name: 'chromium',
      testIgnore: /onboarding\//,
      use: {
        ...devices['Desktop Chrome'],
        storageState: 'e2e/.auth/owner.json',
      },
      dependencies: ['setup'],
    },
    {
      name: 'tablet-chromium',
      testIgnore: /onboarding\//,
      use: {
        ...devices['iPad Pro 11 landscape'],
        storageState: 'e2e/.auth/owner.json',
      },
      dependencies: ['setup'],
    },
    {
      name: 'mobile-chromium',
      testIgnore: /onboarding\//,
      use: {
        ...devices['Pixel 7'],
        storageState: 'e2e/.auth/owner.json',
      },
      dependencies: ['setup'],
    },
    {
      // A fresh Workspace with an MFA Owner and a Member for the
      // Localization and Scale runners (e2e/support/identities.setup.ts).
      name: 'identities',
      testMatch: /support\/identities\.setup\.ts/,
      retries: 0,
      use: {
        ...devices['Desktop Chrome'],
        storageState: undefined,
      },
    },
    {
      name: 'onboarding',
      testMatch: /onboarding\/first-owner-controlled-mock\.spec\.ts/,
      use: {
        ...devices['Desktop Chrome'],
        storageState: undefined,
      },
    },
    {
      name: 'golden-path',
      // The v9 journey plus the front-flow product intake (#162), which
      // needs the same compose stack and a running Worker, and the interface
      // language preference (#252), which needs real sign-ins.
      testMatch: [
        /golden-path\/full-journey\.spec\.ts/,
        /products\/paste-text-intake\.spec\.ts/,
        /i18n\/locale-preference\.spec\.ts/,
      ],
      retries: 0,
      timeout: 900_000,
      // Recovery-code hashing and workspace provisioning can exceed the
      // default 5s assertion wait on the standard 2-core GitHub fallback.
      expect: {
        timeout:
          process.env.E2E_STANDARD_GITHUB_RUNNER === 'true' ? 30_000 : 5_000,
      },
      use: {
        ...devices['Desktop Chrome'],
        actionTimeout: 15_000,
        navigationTimeout: 30_000,
        storageState: undefined,
        // A passing run keeps nothing: per-commit videos and traces were
        // ~110 MB per run and exhausted the Actions storage quota.
        video: 'retain-on-failure',
        trace: 'retain-on-failure',
      },
    },
  ],
  webServer: process.env.CI
    ? undefined
    : {
        command: 'npm run dev',
        port: 4002,
        reuseExistingServer: true,
      },
});
