/** Audited operator recovery for an expired/revoked first-Owner invitation. */
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
  const {
    approveOperatorActionCase,
    executeApprovedFirstOwnerReissue,
    requestFirstOwnerReissueCase,
  } = await import('../lib/onboarding');
  const { requireOperatorIdentityFromEnv } =
    await import('../lib/operator-identity');
  const action = process.argv
    .slice(2)
    .find((value) => ['request', 'approve'].includes(value));
  const workspaceSlug = arg('--workspace-slug');
  const email = arg('--email');
  const caseId = arg('--case-id');
  const reason = arg('--reason');
  if (
    !action ||
    !caseId ||
    (action === 'request' && (!workspaceSlug || !email || !reason))
  ) {
    process.stderr.write(
      'usage:\n' +
        '  OPERATOR_IDENTITY_TOKEN=<signed-token> npm run ops:reissue-first-owner -- request --workspace-slug <slug> --email <email> --case-id <case> --reason <reason>\n' +
        '  OPERATOR_IDENTITY_TOKEN=<signed-token> npm run ops:reissue-first-owner -- approve --case-id <case>\n'
    );
    process.exitCode = 2;
    return;
  }
  try {
    const operator = requireOperatorIdentityFromEnv();
    if (action === 'request') {
      const team = await prisma.team.findUnique({
        where: { slug: workspaceSlug as string },
      });
      if (!team) throw new Error('Workspace not found.');
      const actionCase = await requestFirstOwnerReissueCase(prisma, {
        teamId: team.id,
        email: email as string,
        operator,
        caseId,
        reason: reason as string,
      });
      process.stdout.write(
        `First-Owner reissue case ${actionCase.id}: ${actionCase.status}\n`
      );
    } else {
      const actionCase = await approveOperatorActionCase(prisma, {
        caseId,
        operator,
      });
      if (actionCase.status !== 'APPROVED') {
        process.stdout.write(
          `First-Owner reissue case ${actionCase.id}: ${actionCase.status}\n`
        );
      } else {
        const invitation = await executeApprovedFirstOwnerReissue(prisma, {
          caseId,
          operator,
        });
        process.stdout.write(
          `First-Owner invitation ${invitation.id} reissued.\n`
        );
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
