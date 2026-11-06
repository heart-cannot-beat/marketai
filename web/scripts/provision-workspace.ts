/** Audited, retry-safe operator provisioning for an invite-only Beta Workspace. */
import { loadRuntimeEnv, projectRoot } from './loadEnv';

type Args = {
  subcommand?: 'begin' | 'complete';
  name?: string;
  slug?: string;
  ownerEmail?: string;
  operatorUserId?: string;
  betaPlanVersionId?: string;
  provisioningRef?: string;
};

const parseArgs = (argv: string[]): Args => {
  const args: Args = {};
  const first = argv[0];
  if (first === 'begin' || first === 'complete') {
    args.subcommand = first;
    argv = argv.slice(1);
  }
  for (let i = 0; i < argv.length; i++) {
    const [flag, inline] = argv[i].split('=', 2);
    const value = inline ?? argv[++i];
    if (flag === '--name') args.name = value;
    if (flag === '--slug') args.slug = value;
    if (flag === '--owner-email') args.ownerEmail = value?.trim().toLowerCase();
    if (flag === '--operator-user-id') args.operatorUserId = value;
    if (flag === '--beta-plan-version-id') args.betaPlanVersionId = value;
    if (flag === '--provisioning-ref') args.provisioningRef = value;
  }
  return args;
};

const usage = () => {
  process.stderr.write(
    'usage:\n' +
      '  npm run ops:provision-workspace -- begin --name <name> --owner-email <email> --operator-user-id <id> --beta-plan-version-id <id> [--slug <slug>]\n' +
      '  npm run ops:provision-workspace -- complete --provisioning-ref <ref>\n' +
      '  npm run ops:provision-workspace -- --name <name> --owner-email <email> --operator-user-id <id> --beta-plan-version-id <id> [--slug <slug>]  (legacy one-shot)\n'
  );
  process.exit(2);
};

const main = async () => {
  loadRuntimeEnv(projectRoot(__dirname));
  const { prisma } = await import('../lib/prisma');
  const { redactString } = await import('../lib/logger');
  const env = (await import('../lib/env')).default;
  const args = parseArgs(process.argv.slice(2));

  const signingKey = env.entitlementSnapshotSigningKey;
  if (!signingKey)
    throw new Error('ENTITLEMENT_SNAPSHOT_SIGNING_KEY is required.');
  const signingKeyId =
    process.env.ENTITLEMENT_SNAPSHOT_SIGNING_KEY_ID || 'beta-v1';
  const signingKeyBuf = Buffer.from(signingKey, 'utf8');

  try {
    if (args.subcommand === 'complete') {
      if (!args.provisioningRef) {
        usage();
        return;
      }
      const { completeProvisioning } =
        await import('../lib/control-plane-provisioning');
      const result = await completeProvisioning(prisma, {
        provisioningRef: args.provisioningRef,
        signingKeyId,
        signingKey: signingKeyBuf,
      });
      process.stdout.write(
        `Workspace ${result.workspaceSlug} (${result.workspaceRef}) activated\n` +
          `  provisioning snapshot v${result.provisioningSnapshotVersion}\n` +
          `  active snapshot v${result.activeSnapshotVersion}\n`
      );
    } else if (args.subcommand === 'begin' || !args.subcommand) {
      if (
        !args.name ||
        !args.ownerEmail ||
        !args.operatorUserId ||
        !args.betaPlanVersionId
      ) {
        usage();
        return;
      }
      const { beginProvisioning } =
        await import('../lib/control-plane-provisioning');
      const result = await beginProvisioning(prisma, {
        idempotencyKey: `provision:${args.name}:${Date.now()}`,
        workspaceName: args.name,
        workspaceSlug: args.slug,
        ownerEmail: args.ownerEmail,
        operatorIdentifier: args.operatorUserId,
        betaPlanVersionId: args.betaPlanVersionId,
        namespace: 'default',
        signingKeyId,
        signingKey: signingKeyBuf,
      });
      process.stdout.write(
        `Workspace ${result.workspaceSlug} (${result.workspaceRef}) ${result.replay ? 'reused' : 'created'}\n`
      );
      process.stdout.write(
        `Owner invitation: ${env.appUrl}${result.ownerInvitation.invitationPath}\n`
      );
      if (args.subcommand === 'begin') {
        process.stdout.write(`Provisioning ref: ${result.provisioningRef}\n`);
      }
    }
  } catch (error) {
    process.stderr.write(`${redactString((error as Error).message)}\n`);
    process.exitCode = 1;
  } finally {
    await prisma.$disconnect();
  }
};

void main();
