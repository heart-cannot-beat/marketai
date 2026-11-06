import type { Prisma } from '@prisma/client';

/**
 * Serialization point for every credential-sensitive operation on an account.
 *
 * Sign-in (minting a session), changing a password and redeeming a reset token
 * all race each other. They serialize by taking the same row lock first, so the
 * `credentialVersion` each one read stays true for the rest of its transaction.
 * Keeping this in one place means the three call sites cannot drift apart.
 */
export const lockUserCredentials = async (
  tx: Prisma.TransactionClient,
  userId: string
): Promise<{ credentialVersion: number } | null> => {
  const rows = await tx.$queryRaw<{ credentialVersion: number }[]>`
    SELECT "credentialVersion" FROM "User" WHERE "id" = ${userId} FOR UPDATE
  `;

  return rows[0] ?? null;
};
