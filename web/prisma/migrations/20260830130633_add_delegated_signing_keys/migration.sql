-- CreateEnum
CREATE TYPE "DelegatedSigningKeyState" AS ENUM ('PREPUBLISHED', 'ACTIVE', 'RETIRING', 'RETIRED', 'REVOKED');

-- CreateTable
CREATE TABLE "DelegatedSigningKey" (
    "id" TEXT NOT NULL,
    "kid" TEXT NOT NULL,
    "algorithm" TEXT NOT NULL DEFAULT 'ES256',
    "publicJwk" JSONB NOT NULL,
    "protectedPrivateKey" JSONB NOT NULL,
    "publicJwkThumbprint" TEXT NOT NULL,
    "state" "DelegatedSigningKeyState" NOT NULL,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "activatedAt" TIMESTAMP(3),
    "rotationCommittedAt" TIMESTAMP(3),
    "verifyUntil" TIMESTAMP(3),
    "revokedAt" TIMESTAMP(3),

    CONSTRAINT "DelegatedSigningKey_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE UNIQUE INDEX "DelegatedSigningKey_kid_key" ON "DelegatedSigningKey"("kid");

-- CreateIndex
CREATE INDEX "DelegatedSigningKey_state_idx" ON "DelegatedSigningKey"("state");

-- At most one ACTIVE key at any time. Prisma does not support partial unique
-- indexes natively (spec §1.6), so this is added as raw SQL.
CREATE UNIQUE INDEX delegated_signing_keys_active_unique
    ON "DelegatedSigningKey" (state)
    WHERE state = 'ACTIVE';

-- Prisma does not support arbitrary CHECK constraints natively, so these are
-- added as raw SQL per spec §1.6.
ALTER TABLE "DelegatedSigningKey" ADD CONSTRAINT "DelegatedSigningKey_algorithm_check" CHECK (algorithm = 'ES256');
ALTER TABLE "DelegatedSigningKey" ADD CONSTRAINT "DelegatedSigningKey_id_prefix_check" CHECK (id LIKE 'dsk_%');
