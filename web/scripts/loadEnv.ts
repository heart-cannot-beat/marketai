import { loadEnvConfig } from '@next/env';
import path from 'path';

/**
 * Load `web/.env` the same way `next dev`/`next start` does.
 *
 * Ops scripts run outside the Next.js runtime, so nothing loads the env files
 * for them. Without this a scheduler would have to know MarketAI's variable
 * names and inject them itself; with it, a CronJob only needs `DATABASE_URL` in
 * the container environment — real environment variables win over the file.
 *
 * `.env.migrate` is deliberately not loaded: the schema-owner credential
 * belongs to the migration release step alone.
 *
 * `projectDir` is passed in because the compiled script lives under `.ops/`,
 * one directory deeper than the source.
 */
export const loadRuntimeEnv = (projectDir: string) => {
  loadEnvConfig(projectDir, process.env.NODE_ENV !== 'production');

  if (!process.env.DATABASE_URL) {
    process.stderr.write(
      'DATABASE_URL is not set. Create web/.env from .env.example, or provide it in the environment.\n'
    );
    process.exit(1);
  }
};

/** Resolves `web/` from a script running either from source or from `.ops/`. */
export const projectRoot = (scriptDir: string) => {
  const fromSource = path.join(scriptDir, '..');

  return path.basename(path.join(fromSource, '..')) === 'web'
    ? path.join(fromSource, '..')
    : fromSource;
};
