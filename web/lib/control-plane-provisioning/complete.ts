import type { PrismaClient } from '@prisma/client';

import { activateWorkspaceAndPublish } from '../onboarding';
import { ApiError } from '../errors';
import type {
  CompleteProvisioningInput,
  CompleteProvisioningResult,
} from './types';

export async function completeProvisioning(
  prisma: PrismaClient,
  input: CompleteProvisioningInput
): Promise<CompleteProvisioningResult> {
  const team = await prisma.team.findUnique({
    where: { id: input.provisioningRef },
  });
  if (!team) {
    throw new ApiError(404, 'Provisioning workspace not found.');
  }
  if (team.lifecycle !== 'PROVISIONING') {
    throw new ApiError(
      409,
      `Workspace lifecycle is ${team.lifecycle}, expected PROVISIONING.`
    );
  }

  const invitation = await prisma.invitation.findFirst({
    where: { teamId: team.id, kind: 'FIRST_OWNER', consumedAt: { not: null } },
    orderBy: { createdAt: 'desc' },
  });
  if (!invitation) {
    throw new ApiError(
      409,
      'First-Owner invitation has not been consumed yet.'
    );
  }

  const ownerMember = await prisma.teamMember.findFirst({
    where: { teamId: team.id, role: 'OWNER' },
    include: { user: { include: { mfaCredential: true } } },
  });
  if (!ownerMember) {
    throw new ApiError(409, 'Owner membership not found.');
  }
  if (!ownerMember.user.mfaCredential?.verifiedAt) {
    throw new ApiError(409, 'Owner MFA is not verified.');
  }

  const projection = await prisma.commercialAccessProjection.findUnique({
    where: { teamId: team.id },
    select: { entitlementVersion: true },
  });
  const provisioningSnapshotVersion = projection?.entitlementVersion ?? 0;

  const result = await activateWorkspaceAndPublish(prisma, {
    teamId: team.id,
    signingKeyId: input.signingKeyId,
    signingKey: input.signingKey,
  });

  return {
    provisioningRef: team.id,
    workspaceRef: team.id,
    workspaceSlug: team.slug,
    ownerMembershipRef: ownerMember.id,
    provisioningSnapshotVersion,
    activeSnapshotVersion: result.activeSnapshotVersion,
  };
}
