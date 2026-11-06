/** Deliver durable control-plane entitlement snapshots to the domain inbox. */
import os from 'node:os';
import { loadRuntimeEnv, projectRoot } from './loadEnv';

const positiveInteger = (value: string | undefined, fallback: number) => {
  const parsed = Number(value ?? fallback);
  if (!Number.isSafeInteger(parsed) || parsed <= 0) {
    throw new Error(
      'ENTITLEMENT_DISPATCH_BATCH_SIZE must be a positive integer.'
    );
  }
  return parsed;
};

const main = async () => {
  loadRuntimeEnv(projectRoot(__dirname));
  const { prisma } = await import('../lib/prisma');
  const { redactString } = await import('../lib/logger');
  const { claimAndDispatch } = await import('../lib/entitlements/dispatcher');
  const { renewExpiringLeases, renewalIntervalSeconds } =
    await import('../lib/entitlements/renewal');
  const { reconcileReadyProvisioningWorkspaces } =
    await import('../lib/onboarding');
  const env = (await import('../lib/env')).default;

  try {
    if (!env.domainApi.baseUrl) {
      throw new Error('DOMAIN_API_BASE_URL is required.');
    }
    if (!env.controlPlaneToDomainEntitlementToken) {
      throw new Error('CONTROL_PLANE_TO_DOMAIN_ENTITLEMENT_TOKEN is required.');
    }
    if (!env.entitlementSnapshotSigningKey) {
      throw new Error('ENTITLEMENT_SNAPSHOT_SIGNING_KEY is required.');
    }
    const batchSize = positiveInteger(
      process.env.ENTITLEMENT_DISPATCH_BATCH_SIZE,
      50
    );
    const signingKeyId =
      process.env.ENTITLEMENT_SNAPSHOT_SIGNING_KEY_ID || 'beta-v1';
    const signingKey = Buffer.from(env.entitlementSnapshotSigningKey, 'utf8');
    // Renew before delivering so a renewed lease is pushed in this same run
    // (spec §1.12: periodic lease renewal, even if nothing changed).
    const renewal = await renewExpiringLeases(prisma, {
      signingKeyId,
      signingKey,
      intervalSeconds: renewalIntervalSeconds(
        process.env.ENTITLEMENT_RENEWAL_INTERVAL_SECONDS
      ),
      batchSize,
    });
    for (const failure of renewal.failures) {
      process.stderr.write(
        `lease renewal failed for workspace ${failure.teamId}: ${redactString(failure.error)}\n`
      );
    }
    const result = await claimAndDispatch(
      prisma,
      process.env.ENTITLEMENT_DISPATCHER_ID ||
        `ops:${os.hostname()}:${process.pid}`,
      {
        domainBaseUrl: env.domainApi.baseUrl,
        pushToken: env.controlPlaneToDomainEntitlementToken,
        batchSize,
      }
    );
    const reconciliation = await reconcileReadyProvisioningWorkspaces(prisma, {
      signingKeyId,
      signingKey,
      batchSize,
    });
    process.stdout.write(
      `renewal_examined=${renewal.examined} renewed=${renewal.renewed} renewal_failed=${renewal.failed} claimed=${result.claimed} delivered=${result.delivered} failed=${result.failed} dead_lettered=${result.deadLettered} provisioning_examined=${reconciliation.examined} provisioning_activated=${reconciliation.activated} provisioning_failed=${reconciliation.failed}\n`
    );
    if (renewal.failed > 0) {
      // Surface it on the cron's run status; delivery above still ran.
      process.exitCode = 1;
    }
  } catch (error) {
    process.stderr.write(`${redactString((error as Error).message)}\n`);
    process.exitCode = 1;
  } finally {
    await prisma.$disconnect();
  }
};

void main();
