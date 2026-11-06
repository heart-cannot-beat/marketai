/** Audited operator-only MFA recovery case workflow. */
import { loadRuntimeEnv, projectRoot } from './loadEnv';

type Args = {
  action?: string;
  caseId?: string;
  reason?: string;
  workspaceSlug?: string;
  targetUserId?: string;
};

const parseArgs = (argv: string[]): Args => {
  const args: Args = {};
  for (let i = 0; i < argv.length; i++) {
    const [flag, inline] = argv[i].split('=', 2);
    const value = inline ?? argv[++i];
    if (flag === 'request' || flag === 'approve') args.action = flag;
    if (flag === '--case-id') args.caseId = value;
    if (flag === '--reason') args.reason = value;
    if (flag === '--workspace-slug') args.workspaceSlug = value;
    if (flag === '--target-user-id') args.targetUserId = value;
  }
  return args;
};

const usage = (): never => {
  process.stderr.write(
    'usage:\n' +
      '  OPERATOR_IDENTITY_TOKEN=<signed-token> npm run ops:mfa-reset -- request --case-id <case> --reason <reason> --workspace-slug <slug> --target-user-id <user>\n' +
      '  OPERATOR_IDENTITY_TOKEN=<signed-token> npm run ops:mfa-reset -- approve --case-id <case>\n'
  );
  process.exit(2);
};

const main = async () => {
  loadRuntimeEnv(projectRoot(__dirname));
  const { prisma } = await import('../lib/prisma');
  const { redactString } = await import('../lib/logger');
  const { approveMfaResetCase, createMfaResetCase } =
    await import('../lib/mfa-reset');
  const { requireOperatorIdentityFromEnv } =
    await import('../lib/operator-identity');
  const args = parseArgs(process.argv.slice(2));
  if (!args.action || !args.caseId) usage();
  const caseId = args.caseId as string;

  try {
    const operator = requireOperatorIdentityFromEnv();
    if (args.action === 'request') {
      if (!args.reason || !args.workspaceSlug || !args.targetUserId) usage();
      const reason = args.reason as string;
      const workspaceSlug = args.workspaceSlug as string;
      const targetUserId = args.targetUserId as string;
      const result = await createMfaResetCase(prisma, {
        caseId,
        reason,
        teamSlug: workspaceSlug,
        targetUserId,
        operator,
      });
      process.stdout.write(
        `MFA recovery case ${result.id}: ${result.status}\n`
      );
    } else if (args.action === 'approve') {
      const result = await approveMfaResetCase(prisma, {
        caseId,
        operator,
      });
      process.stdout.write(
        `MFA recovery case ${result.caseId}: ${result.status}\n`
      );
    } else {
      usage();
    }
  } catch (error) {
    process.stderr.write(`${redactString((error as Error).message)}\n`);
    process.exitCode = 1;
  } finally {
    await prisma.$disconnect();
  }
};

void main();
