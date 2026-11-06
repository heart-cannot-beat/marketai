import { randomUUID } from 'crypto';

import { ApiError } from '../lib/errors';
import { prisma } from '../lib/prisma';
import { lockUserCredentials } from './userLock';

/**
 * Server-side authorization sessions.
 *
 * The browser carries a JWT cookie, but the JWT is not the authority: every
 * token is bound to a `Session` row by its `sid` claim, and every request
 * re-reads that row. Deleting the row revokes the token immediately, which is
 * what ADR 0003 and the integration contract mean by a revocable session.
 *
 * The `sessionToken` column is kept unique and populated so the row shape stays
 * compatible with the NextAuth Prisma adapter, but it is never handed to the
 * browser.
 */
/**
 * Mints a session, but only if the credentials that authorized it are still
 * current.
 *
 * `expectedCredentialVersion` is what `authorize()` saw when it verified the
 * password. Re-checking it while the `User` row is locked closes the window
 * where a password reset revokes every session and the in-flight sign-in then
 * creates a new one carrying the authority the reset just withdrew.
 */
export const createAuthSession = async (
  userId: string,
  maxAgeSeconds: number,
  expectedCredentialVersion: number,
  mfaAuthenticatedAt?: Date | null
) => {
  return await prisma.$transaction(async (tx) => {
    const current = await lockUserCredentials(tx, userId);

    if (!current) {
      throw new ApiError(401, 'invalid-credentials');
    }

    if (current.credentialVersion !== expectedCredentialVersion) {
      throw new ApiError(401, 'credentials-changed');
    }

    return await tx.session.create({
      data: {
        sessionToken: randomUUID(),
        userId,
        expires: new Date(Date.now() + maxAgeSeconds * 1000),
        mfaAuthenticatedAt: mfaAuthenticatedAt ?? null,
      },
    });
  });
};

/**
 * Returns the session row and its owner when the session is still valid,
 * otherwise null. A missing row means the session was revoked; an expired row
 * means it aged out and is deleted on the way past.
 *
 * The user is loaded with it so that the identity in the session comes from the
 * database rather than from the token. A JWT minted before an email change
 * still carries the old address, and `session.user.email` decides which
 * invitation an account may accept.
 */
export const findActiveAuthSession = async (
  sessionId: string,
  userId: string
) => {
  const session = await prisma.session.findUnique({
    where: { id: sessionId },
    include: {
      user: {
        select: {
          id: true,
          email: true,
          name: true,
          image: true,
          locale: true,
        },
      },
    },
  });

  if (!session || session.userId !== userId || session.recoveryOnly) {
    return null;
  }

  if (session.expires.getTime() <= Date.now()) {
    // Lazy cleanup: an aged-out row is never usable again.
    await prisma.session
      .delete({ where: { id: session.id } })
      .catch(() => undefined);

    return null;
  }

  return session;
};

/** Best-effort removal of rows that can no longer authorize anything. */
export const deleteExpiredSessionsForUser = async (userId: string) => {
  return await prisma.session.deleteMany({
    where: { userId, expires: { lte: new Date() } },
  });
};

/**
 * Sweep aged-out rows across every account.
 *
 * Per-user pruning only runs when that account signs in again, so a dormant
 * account's rows would otherwise sit there forever. Run from
 * `npm run ops:prune-sessions` on a schedule; `Session.expires` is indexed for
 * it.
 */
export const deleteExpiredSessions = async () => {
  return await prisma.session.deleteMany({
    where: { expires: { lte: new Date() } },
  });
};

export const deleteManySessions = async ({ where }) => {
  return await prisma.session.deleteMany({
    where,
  });
};

/** Revoke every session a user holds (password reset, admin revocation). */
export const revokeAllSessionsForUser = async (userId: string) => {
  return await prisma.session.deleteMany({
    where: { userId },
  });
};

/** Revoke every session a user holds except the one making the request. */
export const revokeOtherSessionsForUser = async (
  userId: string,
  keepSessionId?: string
) => {
  return await prisma.session.deleteMany({
    where: {
      userId,
      ...(keepSessionId ? { NOT: { id: keepSessionId } } : {}),
    },
  });
};

export const findFirstSessionOrThrown = async ({ where }) => {
  return await prisma.session.findFirstOrThrow({
    where,
  });
};

export const findManySessions = async ({ where }) => {
  return await prisma.session.findMany({
    where,
  });
};

export const deleteSession = async ({ where }) => {
  return await prisma.session.delete({
    where,
  });
};

/**
 * Revoke a set of sessions atomically.
 *
 * A browser can hold both the plain and the `__Secure-` prefixed cookie. Either
 * all of the rows they name go, or none do — a partial revocation followed by a
 * cleared cookie is the "signed out but still authorized" state sign-out exists
 * to prevent.
 */
export const deleteSessions = async (ids: string[]) => {
  if (ids.length === 0) {
    return { count: 0 };
  }

  return await prisma.$transaction(async (tx) => {
    return await tx.session.deleteMany({ where: { id: { in: ids } } });
  });
};
