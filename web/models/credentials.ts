import { Prisma } from '@prisma/client';

import { ApiError } from '@/lib/errors';
import { prisma } from '@/lib/prisma';
import { lockUserCredentials } from './userLock';

/**
 * Credential-security state changes, each as one serialized transaction.
 *
 * Changing a credential and revoking what it protected are a single security
 * decision, and they race sign-in and each other. Four interleavings matter:
 *
 *  1. A sign-in verifies the old password, a reset then revokes every session,
 *     and the sign-in afterwards mints a session carrying the authority the
 *     reset just withdrew.
 *  2. A change-password request verifies the current password, a reset lands,
 *     and the change then overwrites the password the reset just set.
 *  3. Two reset tokens are outstanding; redeeming the newer and then the older
 *     leaves the account on the older token's password.
 *  4. The account moves to a new email address, and a reset link already
 *     delivered to the old one still takes it over.
 *
 * All four close the same way: `User.credentialVersion` is read when the
 * credentials are verified and re-checked while the `User` row is locked before
 * anything is written; every credential change bumps it and destroys all
 * outstanding reset tokens for the account; and every reset token records the
 * version it was issued against.
 *
 * **Lock order is always `User` first, then `PasswordReset`.** Taking them the
 * other way round — as consuming the token before locking the user did — lets
 * two concurrent redemptions of different tokens deadlock (PostgreSQL 40P01,
 * surfacing through Prisma as P2010 and a 500).
 */

export class CredentialConflictError extends ApiError {
  constructor(message: string) {
    super(409, message);
  }
}

const invalidToken = () =>
  new ApiError(422, 'Invalid password reset token. Please request a new one.');

export const changePassword = async (params: {
  userId: string;
  hashedPassword: string;
  keepSessionId: string;
  expectedCredentialVersion: number;
}): Promise<{ revokedSessions: number; revokedResetTokens: number }> => {
  return await prisma.$transaction(async (tx) => {
    const locked = await lockUserCredentials(tx, params.userId);

    if (!locked) {
      throw new ApiError(404, 'Account not found.');
    }

    if (locked.credentialVersion !== params.expectedCredentialVersion) {
      throw new CredentialConflictError(
        'Your password was changed elsewhere while this request was in flight. Sign in again and retry.'
      );
    }

    await tx.user.update({
      where: { id: params.userId },
      data: {
        password: params.hashedPassword,
        credentialVersion: { increment: 1 },
      },
    });

    const revoked = await tx.session.deleteMany({
      where: {
        userId: params.userId,
        NOT: { id: params.keepSessionId },
      },
    });

    // Someone who knows the new password should not still be reachable by a
    // reset link that was in flight beforehand.
    const resets = await tx.passwordReset.deleteMany({
      where: { userId: params.userId },
    });

    return { revokedSessions: revoked.count, revokedResetTokens: resets.count };
  });
};

/**
 * Moving an account to a new address is a credential change.
 *
 * Reset tokens are bound to `userId`, so a link already delivered to the old
 * address would otherwise keep working against the account under its new
 * address. Bumping the version also invalidates an in-flight sign-in that
 * verified the old credentials.
 */
export const changeEmail = async (params: {
  userId: string;
  email: string;
  expectedCredentialVersion: number;
}): Promise<{ revokedResetTokens: number }> => {
  return await prisma.$transaction(async (tx) => {
    const locked = await lockUserCredentials(tx, params.userId);

    if (!locked) {
      throw new ApiError(404, 'Account not found.');
    }

    if (locked.credentialVersion !== params.expectedCredentialVersion) {
      throw new CredentialConflictError(
        'Your account credentials changed while this request was in flight. Reload and retry.'
      );
    }

    try {
      await tx.user.update({
        where: { id: params.userId },
        data: {
          email: params.email,
          credentialVersion: { increment: 1 },
        },
      });
    } catch (error) {
      if (
        error instanceof Prisma.PrismaClientKnownRequestError &&
        error.code === 'P2002'
      ) {
        throw new ApiError(400, 'Email already in use.');
      }

      throw error;
    }

    const resets = await tx.passwordReset.deleteMany({
      where: { userId: params.userId },
    });

    return { revokedResetTokens: resets.count };
  });
};

export const redeemPasswordReset = async (params: {
  token: string;
  hashedPassword: string;
}): Promise<{
  userId: string;
  revokedSessions: number;
  revokedResetTokens: number;
}> => {
  // Resolve which account the token belongs to before opening the transaction,
  // so the locks inside are taken in the same order as everywhere else. Nothing
  // read here is trusted: expiry and credential version are re-checked under
  // the lock, and the token is only consumed there.
  const claimed = await prisma.passwordReset.findUnique({
    where: { token: params.token },
    select: { userId: true },
  });

  if (!claimed) {
    throw invalidToken();
  }

  return await prisma.$transaction(async (tx) => {
    const locked = await lockUserCredentials(tx, claimed.userId);

    if (!locked) {
      throw invalidToken();
    }

    // Read and consume the token under the user lock. Two requests carrying the
    // same token both reach here; only one deletes a row.
    const token = await tx.passwordReset.findUnique({
      where: { token: params.token },
    });

    if (!token || token.userId !== claimed.userId) {
      throw invalidToken();
    }

    const consumed = await tx.passwordReset.deleteMany({
      where: { token: params.token, userId: claimed.userId },
    });

    if (consumed.count !== 1) {
      throw invalidToken();
    }

    if (token.expiresAt < new Date()) {
      throw new ApiError(
        422,
        'Password reset token has expired. Please request a new one.'
      );
    }

    // A token issued before any later credential change is stale, even if its
    // row survived. This stops an older link from overwriting the password a
    // newer one just set, and stops a link sent to a previous address from
    // taking over the account.
    if (locked.credentialVersion !== token.credentialVersionAtIssue) {
      throw invalidToken();
    }

    const resetAt = new Date();
    await tx.user.update({
      where: { id: claimed.userId },
      data: {
        password: params.hashedPassword,
        credentialVersion: { increment: 1 },
        lastPasswordResetAt: resetAt,
        // Resetting the password also unlocks a locked-out account.
        invalid_login_attempts: 0,
        lockedAt: null,
      },
    });

    // A reset revokes every session on every device, including this one...
    const revoked = await tx.session.deleteMany({
      where: { userId: claimed.userId },
    });

    // ...and every sibling reset token, so none of them can be replayed.
    const resets = await tx.passwordReset.deleteMany({
      where: { userId: claimed.userId },
    });

    return {
      userId: claimed.userId,
      revokedSessions: revoked.count,
      revokedResetTokens: resets.count,
    };
  });
};
