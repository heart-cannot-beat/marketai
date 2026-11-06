import type { PrismaClient } from '@prisma/client';

import { provisionFirstOwnerWorkspace } from '../onboarding';
import { slugify } from '../server-common';
import type { BeginProvisioningInput, BeginProvisioningResult } from './types';

export async function beginProvisioning(
  prisma: PrismaClient,
  input: BeginProvisioningInput
): Promise<BeginProvisioningResult> {
  const slug = input.workspaceSlug
    ? slugify(input.workspaceSlug)
    : slugify(input.workspaceName);

  const result = await provisionFirstOwnerWorkspace(prisma, {
    name: input.workspaceName,
    slug,
    ownerEmail: input.ownerEmail,
    operatorUserId: input.operatorIdentifier,
    betaPlanVersionId: input.betaPlanVersionId,
    signingKeyId: input.signingKeyId,
    signingKey: input.signingKey,
  });

  return {
    provisioningRef: result.team.id,
    workspaceRef: result.team.id,
    workspaceSlug: result.team.slug,
    ownerInvitation: {
      token: result.invitation.token,
      invitationPath: `/invitations/${result.invitation.token}`,
    },
    replay: result.replay,
  };
}
