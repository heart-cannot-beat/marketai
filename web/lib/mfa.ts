import crypto from 'node:crypto';
import bcrypt from 'bcryptjs';
import type { Prisma } from '@prisma/client';

import { ApiError } from '@/lib/errors';
import { logger } from '@/lib/logger';
import { reconcileProvisioningWorkspaceFromRuntime } from '@/lib/onboarding';
import { prisma } from '@/lib/prisma';

const ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
const TOTP_PERIOD_SECONDS = 30;
const TOTP_DIGITS = 6;
const RECOVERY_CODE_COUNT = 10;

type ProtectedSecret = {
  ciphertext: string;
  iv: string;
  authTag: string;
};

function encryptionKey(): Buffer {
  const configured = process.env.MFA_ENCRYPTION_KEY;
  if (!configured) {
    throw new ApiError(500, 'MFA encryption is not configured.');
  }
  const key = Buffer.from(configured, 'base64');
  if (key.length !== 32) {
    throw new ApiError(500, 'MFA_ENCRYPTION_KEY must decode to 32 bytes.');
  }
  return key;
}

export function encodeBase32(input: Buffer): string {
  let bits = '';
  for (const byte of Array.from(input)) {
    bits += byte.toString(2).padStart(8, '0');
  }
  let encoded = '';
  for (let offset = 0; offset < bits.length; offset += 5) {
    encoded +=
      ALPHABET[
        Number.parseInt(bits.slice(offset, offset + 5).padEnd(5, '0'), 2)
      ];
  }
  return encoded;
}

export function decodeBase32(input: string): Buffer {
  const normalized = input.toUpperCase().replace(/=|\s|-/g, '');
  let bits = '';
  for (const character of normalized) {
    const value = ALPHABET.indexOf(character);
    if (value < 0) throw new ApiError(400, 'Invalid MFA secret.');
    bits += value.toString(2).padStart(5, '0');
  }
  const bytes: number[] = [];
  for (let offset = 0; offset + 8 <= bits.length; offset += 8) {
    bytes.push(Number.parseInt(bits.slice(offset, offset + 8), 2));
  }
  return Buffer.from(bytes);
}

export function totpCode(secret: string, at = Date.now()): string {
  const counter = Math.floor(at / 1000 / TOTP_PERIOD_SECONDS);
  const counterBuffer = Buffer.alloc(8);
  counterBuffer.writeBigUInt64BE(BigInt(counter));
  const digest = crypto
    .createHmac('sha1', decodeBase32(secret))
    .update(counterBuffer)
    .digest();
  const offset = digest[digest.length - 1] & 0x0f;
  const value = (digest.readUInt32BE(offset) & 0x7fffffff) % 10 ** TOTP_DIGITS;
  return value.toString().padStart(TOTP_DIGITS, '0');
}

export function verifyTotp(
  secret: string,
  code: string,
  at = Date.now()
): boolean {
  if (!/^\d{6}$/.test(code)) return false;
  return [-1, 0, 1].some((window) => {
    const candidate = totpCode(
      secret,
      at + window * TOTP_PERIOD_SECONDS * 1000
    );
    return crypto.timingSafeEqual(Buffer.from(candidate), Buffer.from(code));
  });
}

export function protectMfaSecret(secret: string): ProtectedSecret {
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv('aes-256-gcm', encryptionKey(), iv);
  const ciphertext = Buffer.concat([
    cipher.update(secret, 'utf8'),
    cipher.final(),
  ]);
  return {
    ciphertext: ciphertext.toString('base64'),
    iv: iv.toString('base64'),
    authTag: cipher.getAuthTag().toString('base64'),
  };
}

export function revealMfaSecret(value: unknown): string {
  const envelope = value as ProtectedSecret;
  if (!envelope?.ciphertext || !envelope.iv || !envelope.authTag) {
    throw new ApiError(500, 'Stored MFA credential is invalid.');
  }
  const decipher = crypto.createDecipheriv(
    'aes-256-gcm',
    encryptionKey(),
    Buffer.from(envelope.iv, 'base64')
  );
  decipher.setAuthTag(Buffer.from(envelope.authTag, 'base64'));
  return Buffer.concat([
    decipher.update(Buffer.from(envelope.ciphertext, 'base64')),
    decipher.final(),
  ]).toString('utf8');
}

const newRecoveryCode = () => {
  const raw = crypto.randomBytes(8).toString('hex').toUpperCase();
  return `${raw.slice(0, 4)}-${raw.slice(4, 8)}-${raw.slice(8, 12)}-${raw.slice(12)}`;
};

export async function beginMfaEnrollment(userId: string, email: string) {
  const existing = await prisma.mfaCredential.findUnique({ where: { userId } });
  if (existing?.verifiedAt) {
    throw new ApiError(
      409,
      'MFA is already enrolled. Reset it before enrolling again.'
    );
  }
  const secret = encodeBase32(crypto.randomBytes(20));
  await prisma.mfaCredential.upsert({
    where: { userId },
    create: {
      userId,
      protectedSecret: protectMfaSecret(
        secret
      ) as unknown as Prisma.InputJsonValue,
    },
    update: {
      protectedSecret: protectMfaSecret(
        secret
      ) as unknown as Prisma.InputJsonValue,
      verifiedAt: null,
    },
  });
  const issuer = 'MarketAI';
  const label = `${issuer}:${email}`;
  return {
    secret,
    otpauthUri: `otpauth://totp/${encodeURIComponent(label)}?secret=${secret}&issuer=${encodeURIComponent(issuer)}&period=${TOTP_PERIOD_SECONDS}&digits=${TOTP_DIGITS}`,
  };
}

export async function confirmMfaEnrollment(params: {
  userId: string;
  sessionId: string;
  code: string;
}): Promise<{ recoveryCodes: string[] }> {
  const credential = await prisma.mfaCredential.findUnique({
    where: { userId: params.userId },
  });
  if (!credential || credential.verifiedAt) {
    throw new ApiError(409, 'No pending MFA enrollment was found.');
  }
  if (!verifyTotp(revealMfaSecret(credential.protectedSecret), params.code)) {
    throw new ApiError(422, 'The authenticator code is invalid.');
  }

  const recoveryCodes = Array.from(
    { length: RECOVERY_CODE_COUNT },
    newRecoveryCode
  );
  const hashes = await Promise.all(
    recoveryCodes.map((code) => bcrypt.hash(code, 12))
  );
  const now = new Date();

  const provisioningOwnerTeams = await prisma.teamMember.findMany({
    where: {
      userId: params.userId,
      role: 'OWNER',
      team: { lifecycle: 'PROVISIONING' },
    },
    select: { teamId: true },
  });
  await prisma.$transaction(async (tx) => {
    const confirmed = await tx.mfaCredential.updateMany({
      where: { userId: params.userId, verifiedAt: null },
      data: { verifiedAt: now },
    });
    if (confirmed.count !== 1) {
      throw new ApiError(409, 'MFA enrollment was already completed.');
    }
    await tx.mfaRecoveryCode.deleteMany({ where: { userId: params.userId } });
    await tx.mfaRecoveryCode.createMany({
      data: hashes.map((codeHash) => ({ userId: params.userId, codeHash })),
    });
    await tx.session.updateMany({
      where: { id: params.sessionId, userId: params.userId },
      data: { mfaAuthenticatedAt: now },
    });

    const ownerTeams = await tx.teamMember.findMany({
      where: { userId: params.userId, role: 'OWNER' },
      select: { teamId: true },
    });
    for (const { teamId } of ownerTeams) {
      await tx.controlPlaneAuditEvent.create({
        data: {
          scopeKind: 'WORKSPACE',
          teamId,
          actorKind: 'USER',
          actorId: params.userId,
          executor: 'control-plane',
          eventType: 'mfa.enrolled',
          resourceType: 'User',
          resourceId: params.userId,
          details: { recoveryCodeCount: RECOVERY_CODE_COUNT },
        },
      });
      await tx.controlPlaneOutboxEvent.create({
        data: {
          teamId,
          eventType: 'onboarding.mfa_enrolled',
          payload: { userId: params.userId },
        },
      });
    }
  });

  for (const { teamId } of provisioningOwnerTeams) {
    try {
      await reconcileProvisioningWorkspaceFromRuntime(prisma, teamId);
    } catch (error) {
      // MFA is already committed. A transient reconciliation failure must not
      // make the client retry enrollment; the scheduled dispatcher will scan
      // this durable PROVISIONING state again.
      logger.warn('Provisioning reconcile deferred after MFA enrollment', {
        teamId,
        reason: error instanceof Error ? error.name : 'unknown',
      });
    }
  }

  return { recoveryCodes };
}

/** Verify TOTP or atomically consume one recovery code. */
export async function verifyMfaChallenge(
  userId: string,
  challenge: string
): Promise<boolean> {
  const credential = await prisma.mfaCredential.findUnique({
    where: { userId },
  });
  if (!credential?.verifiedAt) return false;
  if (verifyTotp(revealMfaSecret(credential.protectedSecret), challenge))
    return true;

  const candidates = await prisma.mfaRecoveryCode.findMany({
    where: { userId, consumedAt: null },
    select: { id: true, codeHash: true },
  });
  for (const candidate of candidates) {
    if (!(await bcrypt.compare(challenge.toUpperCase(), candidate.codeHash)))
      continue;
    const consumed = await prisma.mfaRecoveryCode.updateMany({
      where: { id: candidate.id, userId, consumedAt: null },
      data: { consumedAt: new Date() },
    });
    return consumed.count === 1;
  }
  return false;
}

export async function mfaStatus(userId: string) {
  const credential = await prisma.mfaCredential.findUnique({
    where: { userId },
    select: { verifiedAt: true },
  });
  const remainingRecoveryCodes = credential?.verifiedAt
    ? await prisma.mfaRecoveryCode.count({
        where: { userId, consumedAt: null },
      })
    : 0;
  return { enrolled: Boolean(credential?.verifiedAt), remainingRecoveryCodes };
}
