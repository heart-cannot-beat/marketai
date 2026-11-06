/** Retry-safe recovery command for a Workspace stuck in PROVISIONING. */
import { loadRuntimeEnv, projectRoot } from './loadEnv';

const arg = (name: string) => {
  const position = process.argv.findIndex(
    (value) => value === name || value.startsWith(`${name}=`)
  );
  if (position < 0) return undefined;
  return process.argv[position].split('=', 2)[1] ?? process.argv[position + 1];
};

const main = async () => {
  loadRuntimeEnv(projectRoot(__dirname));
  const { prisma } = await import('../lib/prisma');
  const { redactString } = await import('../lib/logger');
  const { claimAndDispatch } = await import('../lib/entitlements/dispatcher');
  const {
    approveOperatorActionCase,
    executeApprovedProvisioningReconcile,
    requestProvisioningReconcileCase,
  } = await import('../lib/onboarding');
  const { requireOperatorIdentityFromEnv } =
    await import('../lib/operator-identity');
  const env = (await import('../lib/env')).default;
  const action = process.argv
    .slice(2)
    .find((value) => ['request', 'approve'].includes(value));
  const slug = arg('--slug');
  const caseId = arg('--case-id');
  const reason = arg('--reason');
  if (!action || !caseId || (action === 'request' && (!slug || !reason))) {
    process.stderr.write(
      'usage:\n' +
        '  OPERATOR_IDENTITY_TOKEN=<signed-token> npm run ops:reconcile-workspace-provisioning -- request --slug <slug> --case-id <case> --reason <reason>\n' +
        '  OPERATOR_IDENTITY_TOKEN=<signed-token> npm run ops:reconcile-workspace-provisioning -- approve --case-id <case>\n'
    );
    process.exitCode = 2;
    return;
  }
  try {
    const operator = requireOperatorIdentityFromEnv();
    if (action === 'request') {
      const team = await prisma.team.findUnique({
        where: { slug: slug as string },
      });
      if (!team) throw new Error(`Workspace ${slug} was not found.`);
      const actionCase = await requestProvisioningReconcileCase(prisma, {
        teamId: team.id,
        operator,
        caseId,
        reason: reason as string,
      });
      process.stdout.write(
        `Provisioning reconcile case ${actionCase.id}: ${actionCase.status}\n`
      );
      return;
    }
    const actionCase = await approveOperatorActionCase(prisma, {
      caseId,
      operator,
    });
    if (actionCase.status !== 'APPROVED') {
      process.stdout.write(
        `Provisioning reconcile case ${actionCase.id}: ${actionCase.status}\n`
      );
      return;
    }
    if (!env.entitlementSnapshotSigningKey) {
      throw new Error('ENTITLEMENT_SNAPSHOT_SIGNING_KEY is required.');
    }
    if (!env.domainApi.baseUrl) {
      throw new Error('DOMAIN_API_BASE_URL is required.');
    }
    if (!env.controlPlaneToDomainEntitlementToken) {
      throw new Error('CONTROL_PLANE_TO_DOMAIN_ENTITLEMENT_TOKEN is required.');
    }
    await claimAndDispatch(prisma, `reconcile:${process.pid}`, {
      domainBaseUrl: env.domainApi.baseUrl,
      pushToken: env.controlPlaneToDomainEntitlementToken,
      batchSize: 50,
    });
    const outcome = await executeApprovedProvisioningReconcile(prisma, {
      caseId,
      operator,
      signingKeyId:
        process.env.ENTITLEMENT_SNAPSHOT_SIGNING_KEY_ID || 'beta-v1',
      signingKey: Buffer.from(env.entitlementSnapshotSigningKey, 'utf8'),
    });
    process.stdout.write(`${actionCase.id}: ${outcome}\n`);
  } catch (error) {
    process.stderr.write(`${redactString((error as Error).message)}\n`);
    process.exitCode = 1;
  } finally {
    await prisma.$disconnect();
  }
};

void main();
