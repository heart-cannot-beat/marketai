import { defineConfig } from '@playwright/test';

export default defineConfig({
  globalTimeout: 40 * 60_000,
  timeout: 40 * 60_000,
  workers: 1,
  retries: 0,
  // `github` turns failures into check annotations: the job explains itself
  // even when the artifact upload is refused (storage quota).
  reporter: [['list'], ['github']],
  use: {
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
    trace: 'off',
    video: 'off',
    screenshot: 'off',
    storageState: undefined,
    serviceWorkers: 'block',
  },
  testDir: '.',
  testMatch: 'golden-path.spec.ts',
});
