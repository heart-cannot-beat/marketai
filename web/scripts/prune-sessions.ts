/**
 * Scheduled removal of aged-out session rows across every account.
 *
 * Per-user pruning only runs when that account signs in or is read, so a
 * dormant account's rows never get cleaned up on their own. Run this on a
 * schedule (nightly is plenty):
 *
 *   npm run ops:prune-sessions
 *
 * The npm script runs the compiled JavaScript under `.ops/`, so a production
 * install (`npm ci --omit=dev`) can run it: nothing here needs a TypeScript
 * runtime. It loads `web/.env` the way the server does, so a CronJob only needs
 * `DATABASE_URL` — the runtime role, never the schema owner. `Session.expires`
 * is indexed for it, and it is safe to run alongside traffic: an expired row
 * cannot authorize anything.
 */
import { loadRuntimeEnv, projectRoot } from './loadEnv';

const main = async () => {
  // Loaded before anything reaches for `DATABASE_URL`.
  loadRuntimeEnv(projectRoot(__dirname));

  const { prisma } = await import('../lib/prisma');
  const { deleteExpiredSessions } = await import('../models/session');
  const { logger, redactString } = await import('../lib/logger');

  try {
    const { count } = await deleteExpiredSessions();

    logger.info('control_plane.sessions_pruned', { removed: count });
    process.stdout.write(`Removed ${count} expired session row(s)\n`);
  } catch (error) {
    logger.error('control_plane.session_prune_failed', { error });
    // The raw message can carry the connection string. The structured log above
    // is redacted; this line must be too.
    process.stderr.write(`${redactString((error as Error).message)}\n`);
    await prisma.$disconnect();
    process.exit(1);
  }

  await prisma.$disconnect();
};

void main();
