/**
 * Golden-path E2E provisioning command adapter.
 *
 * JSON stdin, safe JSON stdout, invitation capability on fd 3,
 * bounded error codes on stderr.
 */
import { createWriteStream } from 'node:fs';
import { createHash } from 'node:crypto';
import { loadRuntimeEnv, projectRoot } from './loadEnv';

async function main() {
  loadRuntimeEnv(projectRoot(__dirname));
  const { prisma } = await import('../lib/prisma');
  const { beginProvisioning, completeProvisioning } =
    await import('../lib/control-plane-provisioning');

  const subcommand = process.argv[2];
  const input = await readJsonStdin();
  // The fixture sends the signing key base64-encoded. Both subcommands sign
  // with the decoded bytes: the API verifies begin's provisioning snapshot
  // as well as complete's activation snapshot (#126).
  const signingKey = Buffer.from(input.signingKey ?? '', 'base64');

  try {
    if (subcommand === 'begin') {
      const existingPlan = await prisma.planVersion.findUnique({
        where: { id: input.betaPlanVersionId },
      });
      if (!existingPlan) {
        const entitlements = { controlled_mock: true };
        await prisma.planVersion.create({
          data: {
            id: input.betaPlanVersionId,
            planKey: 'beta-golden-path',
            versionNo: 1,
            displayName: 'Golden Path Beta',
            state: 'EFFECTIVE',
            entitlements,
            entitlementsHash: createHash('sha256')
              .update(JSON.stringify(entitlements))
              .digest('hex'),
            publishedBy: 'golden-path-e2e',
          },
        });
      }
      const operatorEmailHash = createHash('sha256')
        .update(input.operatorIdentifier)
        .digest('hex')
        .slice(0, 16);
      await prisma.user.upsert({
        where: { id: input.operatorIdentifier },
        create: {
          id: input.operatorIdentifier,
          name: 'Golden Path Provisioning Operator',
          email: `golden-path-operator-${operatorEmailHash}@example.test`,
        },
        update: {},
      });
      const result = await beginProvisioning(prisma, { ...input, signingKey });
      process.stdout.write(
        JSON.stringify({
          provisioningRef: result.provisioningRef,
          workspaceRef: result.workspaceRef,
          workspaceSlug: result.workspaceSlug,
        })
      );
      const fd3 = createWriteStream('', { fd: 3 });
      fd3.write(JSON.stringify(result.ownerInvitation));
      fd3.end();
    } else if (subcommand === 'complete') {
      const result = await completeProvisioning(prisma, {
        provisioningRef: input.provisioningRef,
        signingKeyId: input.signingKeyId,
        signingKey,
      });
      await deliverSnapshots(prisma, result.workspaceRef);
      process.stdout.write(JSON.stringify(result));
    } else {
      process.stderr.write('error_code:unknown_subcommand\n');
      process.exitCode = 2;
    }
  } catch (error) {
    const status = (error as any).status;
    const code = status
      ? `http_${status}`
      : ((error as any).code ?? 'internal_error');
    process.stderr.write(`error_code:${code}\n`);
    process.exitCode = 1;
  } finally {
    await prisma.$disconnect();
  }
}

/** A refusal the fixture reads from stderr as `error_code:<code>`. */
class DeliveryError extends Error {
  constructor(readonly code: string) {
    super(code);
  }
}

/**
 * Delivers the Workspace's signed snapshots -- provisioning's and the
 * activation's -- to the API's entitlement inbox, as the production
 * dispatcher does (#126). The domain then applies them through
 * `marketai_apply_workspace_projection_v2`, which provisions the Workspace's
 * entitlement projection and its SpendGuardrail at the Beta defaults: no
 * seed writes either.
 */
async function deliverSnapshots(
  prisma: import('@prisma/client').PrismaClient,
  teamId: string
): Promise<void> {
  const pushToken = process.env.CONTROL_PLANE_TO_DOMAIN_ENTITLEMENT_TOKEN;
  const domainBaseUrl = process.env.API_BASE_URL;
  if (!pushToken || !domainBaseUrl) {
    throw new DeliveryError('snapshot_delivery_unconfigured');
  }
  const { claimAndDispatch } = await import('../lib/entitlements/dispatcher');
  await claimAndDispatch(prisma, `golden-path:${process.pid}`, {
    domainBaseUrl,
    pushToken,
    batchSize: 100,
  });
  const undelivered = await prisma.entitlementSnapshotOutbox.count({
    where: { teamId, deliveryState: { not: 'DELIVERED' } },
  });
  if (undelivered > 0) {
    throw new DeliveryError('snapshot_undelivered');
  }
}

async function readJsonStdin(): Promise<any> {
  const chunks: Buffer[] = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}

void main();
