import { Role } from '@prisma/client';
import { ApiError } from './errors';
import { getTeamMember } from 'models/team';
import { isCustomerRole } from './roles';

/**
 * Reject any role value outside the customer contract before it reaches the
 * database. Upstream allowed ADMIN; MarketAI does not.
 */
export function assertCustomerRole(role: unknown): asserts role is Role {
  if (!isCustomerRole(role)) {
    throw new ApiError(400, 'Role must be one of OWNER or MEMBER.');
  }
}

export async function validateMembershipOperation(
  memberId: string,
  teamMember,
  operationMeta?: {
    role?: Role;
  }
) {
  if (operationMeta?.role !== undefined) {
    assertCustomerRole(operationMeta.role);
  }

  const updatingMember = await getTeamMember(memberId, teamMember.team.slug);

  // A Member can never change the role of an Owner.
  if (teamMember.role === Role.MEMBER && updatingMember.role === Role.OWNER) {
    throw new ApiError(
      403,
      'You do not have permission to update the role of this member.'
    );
  }

  // A Member can never grant Owner.
  if (teamMember.role === Role.MEMBER && operationMeta?.role === Role.OWNER) {
    throw new ApiError(
      403,
      'You do not have permission to update the role of this member to Owner.'
    );
  }

  return updatingMember;
}

/** High-risk membership changes require a phrase carried in the request; a
 * generic modal click is not sufficient. MFA is checked only at login
 * (ADR 0015), so the phrase is the whole guard against a mistaken click. */
export function requireConfirmationPhrase(
  confirmation: unknown,
  expected: string
) {
  if (confirmation !== expected) {
    throw new ApiError(400, `Type ${expected} to confirm this action.`);
  }
}
