-- #76 review fixes: operator MFA recovery, invitation lifecycle, typed first value.
-- The rejected customer-owned reset table is guaranteed empty before it is
-- replaced. This avoids silently reinterpreting customer User ids as operator
-- identities if an unreleased build was ever deployed.

ALTER TABLE "Session" ADD COLUMN "recoveryOnly" BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE "User" ADD COLUMN "lastPasswordResetAt" TIMESTAMP(3);
ALTER TABLE "Invitation"
  ADD COLUMN "revokedAt" TIMESTAMP(3),
  ADD COLUMN "revokedReason" TEXT;

CREATE UNIQUE INDEX "Invitation_teamId_id_key" ON "Invitation"("teamId", "id");
CREATE UNIQUE INDEX "Invitation_one_first_owner_per_workspace_key"
  ON "Invitation"("teamId") WHERE "kind" = 'FIRST_OWNER';

ALTER TABLE "OnboardingProgress"
  ADD COLUMN "validPreviewAt" TIMESTAMP(3),
  ADD COLUMN "mockRecipeSnapshotAt" TIMESTAMP(3),
  ADD COLUMN "assetQualifiedAt" TIMESTAMP(3),
  ADD COLUMN "humanReviewAt" TIMESTAMP(3),
  DROP COLUMN "mfaEnrolledAt",
  DROP COLUMN "sourceConnectedAt",
  DROP COLUMN "catalogReadyAt",
  DROP COLUMN "firstGeneratedAt",
  DROP COLUMN "firstLocalizedAt";

CREATE TABLE "OnboardingDomainFact" (
  "id" TEXT NOT NULL,
  "teamId" TEXT NOT NULL,
  "sourceEventId" TEXT NOT NULL,
  "factType" TEXT NOT NULL,
  "sourceResourceId" TEXT NOT NULL,
  "observedAt" TIMESTAMP(3) NOT NULL,
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT "OnboardingDomainFact_pkey" PRIMARY KEY ("id")
);
CREATE UNIQUE INDEX "OnboardingDomainFact_sourceEventId_key"
  ON "OnboardingDomainFact"("sourceEventId");
CREATE UNIQUE INDEX "OnboardingDomainFact_teamId_factType_sourceResourceId_key"
  ON "OnboardingDomainFact"("teamId", "factType", "sourceResourceId");
CREATE INDEX "OnboardingDomainFact_teamId_observedAt_idx"
  ON "OnboardingDomainFact"("teamId", "observedAt");
ALTER TABLE "OnboardingDomainFact" ADD CONSTRAINT "OnboardingDomainFact_teamId_fkey"
  FOREIGN KEY ("teamId") REFERENCES "Team"("id") ON DELETE CASCADE ON UPDATE CASCADE;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM "MfaResetCase") THEN
    RAISE EXCEPTION 'legacy customer-owned MFA reset cases require manual cancellation before #76 migration';
  END IF;
END $$;

DROP TABLE "MfaResetCase";

CREATE TABLE "MfaResetCase" (
  "id" TEXT NOT NULL,
  "teamId" TEXT NOT NULL,
  "targetUserId" TEXT NOT NULL,
  "betaInvitationId" TEXT NOT NULL,
  "provisioningAuditEventId" TEXT NOT NULL,
  "requestedByOperatorId" TEXT NOT NULL,
  "reason" TEXT NOT NULL,
  "passwordSessionId" TEXT,
  "passwordVerifiedAt" TIMESTAMP(3),
  "passwordRevision" INTEGER,
  "emailVerifiedAt" TIMESTAMP(3),
  "status" TEXT NOT NULL DEFAULT 'AWAITING_EVIDENCE',
  "expiresAt" TIMESTAMP(3) NOT NULL,
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "completedAt" TIMESTAMP(3),
  CONSTRAINT "MfaResetCase_pkey" PRIMARY KEY ("id")
);

CREATE TABLE "MfaResetApproval" (
  "id" TEXT NOT NULL,
  "caseId" TEXT NOT NULL,
  "operatorId" TEXT NOT NULL,
  "approvedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT "MfaResetApproval_pkey" PRIMARY KEY ("id")
);

CREATE TABLE "MfaRecoveryEmailChallenge" (
  "id" TEXT NOT NULL,
  "caseId" TEXT NOT NULL,
  "tokenHash" TEXT NOT NULL,
  "expiresAt" TIMESTAMP(3) NOT NULL,
  "attempts" INTEGER NOT NULL DEFAULT 0,
  "verifiedAt" TIMESTAMP(3),
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT "MfaRecoveryEmailChallenge_pkey" PRIMARY KEY ("id")
);

CREATE UNIQUE INDEX "MfaResetCase_passwordSessionId_key" ON "MfaResetCase"("passwordSessionId");
CREATE INDEX "MfaResetCase_teamId_status_idx" ON "MfaResetCase"("teamId", "status");
CREATE INDEX "MfaResetCase_targetUserId_status_idx" ON "MfaResetCase"("targetUserId", "status");
CREATE UNIQUE INDEX "MfaResetApproval_caseId_operatorId_key" ON "MfaResetApproval"("caseId", "operatorId");
CREATE INDEX "MfaResetApproval_caseId_approvedAt_idx" ON "MfaResetApproval"("caseId", "approvedAt");
CREATE UNIQUE INDEX "MfaRecoveryEmailChallenge_caseId_key" ON "MfaRecoveryEmailChallenge"("caseId");

ALTER TABLE "MfaResetCase" ADD CONSTRAINT "MfaResetCase_teamId_fkey"
  FOREIGN KEY ("teamId") REFERENCES "Team"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
ALTER TABLE "MfaResetCase" ADD CONSTRAINT "MfaResetCase_targetUserId_fkey"
  FOREIGN KEY ("targetUserId") REFERENCES "User"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
ALTER TABLE "MfaResetCase" ADD CONSTRAINT "MfaResetCase_teamId_betaInvitationId_fkey"
  FOREIGN KEY ("teamId", "betaInvitationId") REFERENCES "Invitation"("teamId", "id") ON DELETE RESTRICT ON UPDATE CASCADE;
ALTER TABLE "MfaResetApproval" ADD CONSTRAINT "MfaResetApproval_caseId_fkey"
  FOREIGN KEY ("caseId") REFERENCES "MfaResetCase"("id") ON DELETE CASCADE ON UPDATE CASCADE;
ALTER TABLE "MfaRecoveryEmailChallenge" ADD CONSTRAINT "MfaRecoveryEmailChallenge_caseId_fkey"
  FOREIGN KEY ("caseId") REFERENCES "MfaResetCase"("id") ON DELETE CASCADE ON UPDATE CASCADE;
