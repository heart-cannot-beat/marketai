-- Beta first-owner onboarding, MFA step-up, and recovery (#76).

CREATE TYPE "InvitationKind" AS ENUM ('MEMBER', 'FIRST_OWNER');

ALTER TABLE "Session" ADD COLUMN "mfaAuthenticatedAt" TIMESTAMP(3);

ALTER TABLE "Invitation"
  ADD COLUMN "kind" "InvitationKind" NOT NULL DEFAULT 'MEMBER',
  ADD COLUMN "consumedAt" TIMESTAMP(3),
  ADD COLUMN "consumedByUserId" TEXT;

CREATE TABLE "MfaCredential" (
  "id" TEXT NOT NULL,
  "userId" TEXT NOT NULL,
  "protectedSecret" JSONB NOT NULL,
  "verifiedAt" TIMESTAMP(3),
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "updatedAt" TIMESTAMP(3) NOT NULL,
  CONSTRAINT "MfaCredential_pkey" PRIMARY KEY ("id")
);

CREATE TABLE "MfaRecoveryCode" (
  "id" TEXT NOT NULL,
  "userId" TEXT NOT NULL,
  "codeHash" TEXT NOT NULL,
  "consumedAt" TIMESTAMP(3),
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT "MfaRecoveryCode_pkey" PRIMARY KEY ("id")
);

CREATE TABLE "StepUpGrant" (
  "id" TEXT NOT NULL,
  "sessionId" TEXT NOT NULL,
  "userId" TEXT NOT NULL,
  "teamId" TEXT NOT NULL,
  "action" TEXT NOT NULL,
  "targetId" TEXT NOT NULL,
  "targetRevision" TEXT NOT NULL,
  "contentHash" TEXT NOT NULL,
  "authTime" INTEGER NOT NULL,
  "expiresAt" TIMESTAMP(3) NOT NULL,
  "consumedAt" TIMESTAMP(3),
  "revokedAt" TIMESTAMP(3),
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT "StepUpGrant_pkey" PRIMARY KEY ("id")
);

CREATE TABLE "OnboardingProgress" (
  "id" TEXT NOT NULL,
  "teamId" TEXT NOT NULL,
  "workspacePreparedAt" TIMESTAMP(3),
  "ownerAcceptedAt" TIMESTAMP(3),
  "mfaEnrolledAt" TIMESTAMP(3),
  "sourceConnectedAt" TIMESTAMP(3),
  "catalogReadyAt" TIMESTAMP(3),
  "firstGeneratedAt" TIMESTAMP(3),
  "firstLocalizedAt" TIMESTAMP(3),
  "version" INTEGER NOT NULL DEFAULT 0,
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "updatedAt" TIMESTAMP(3) NOT NULL,
  CONSTRAINT "OnboardingProgress_pkey" PRIMARY KEY ("id")
);

CREATE TABLE "ControlPlaneOutboxEvent" (
  "id" TEXT NOT NULL,
  "teamId" TEXT,
  "eventType" TEXT NOT NULL,
  "payload" JSONB NOT NULL,
  "deliveredAt" TIMESTAMP(3),
  "attempts" INTEGER NOT NULL DEFAULT 0,
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT "ControlPlaneOutboxEvent_pkey" PRIMARY KEY ("id")
);

CREATE TABLE "MfaResetCase" (
  "id" TEXT NOT NULL,
  "teamId" TEXT NOT NULL,
  "targetUserId" TEXT NOT NULL,
  "requestedById" TEXT NOT NULL,
  "firstApprovedById" TEXT,
  "secondApprovedById" TEXT,
  "status" TEXT NOT NULL DEFAULT 'PENDING',
  "reason" TEXT NOT NULL,
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "completedAt" TIMESTAMP(3),
  CONSTRAINT "MfaResetCase_pkey" PRIMARY KEY ("id")
);

CREATE UNIQUE INDEX "MfaCredential_userId_key" ON "MfaCredential"("userId");
CREATE INDEX "MfaRecoveryCode_userId_consumedAt_idx" ON "MfaRecoveryCode"("userId", "consumedAt");
CREATE INDEX "StepUpGrant_sessionId_expiresAt_idx" ON "StepUpGrant"("sessionId", "expiresAt");
CREATE INDEX "StepUpGrant_userId_revokedAt_idx" ON "StepUpGrant"("userId", "revokedAt");
CREATE INDEX "StepUpGrant_teamId_action_idx" ON "StepUpGrant"("teamId", "action");
CREATE UNIQUE INDEX "OnboardingProgress_teamId_key" ON "OnboardingProgress"("teamId");
CREATE INDEX "ControlPlaneOutboxEvent_deliveredAt_createdAt_idx" ON "ControlPlaneOutboxEvent"("deliveredAt", "createdAt");
CREATE INDEX "ControlPlaneOutboxEvent_teamId_createdAt_idx" ON "ControlPlaneOutboxEvent"("teamId", "createdAt");
CREATE INDEX "MfaResetCase_teamId_status_idx" ON "MfaResetCase"("teamId", "status");
CREATE INDEX "MfaResetCase_targetUserId_status_idx" ON "MfaResetCase"("targetUserId", "status");

ALTER TABLE "MfaCredential" ADD CONSTRAINT "MfaCredential_userId_fkey"
  FOREIGN KEY ("userId") REFERENCES "User"("id") ON DELETE CASCADE ON UPDATE CASCADE;
ALTER TABLE "MfaRecoveryCode" ADD CONSTRAINT "MfaRecoveryCode_userId_fkey"
  FOREIGN KEY ("userId") REFERENCES "User"("id") ON DELETE CASCADE ON UPDATE CASCADE;
ALTER TABLE "StepUpGrant" ADD CONSTRAINT "StepUpGrant_sessionId_fkey"
  FOREIGN KEY ("sessionId") REFERENCES "Session"("id") ON DELETE CASCADE ON UPDATE CASCADE;
ALTER TABLE "StepUpGrant" ADD CONSTRAINT "StepUpGrant_userId_fkey"
  FOREIGN KEY ("userId") REFERENCES "User"("id") ON DELETE CASCADE ON UPDATE CASCADE;
ALTER TABLE "StepUpGrant" ADD CONSTRAINT "StepUpGrant_teamId_fkey"
  FOREIGN KEY ("teamId") REFERENCES "Team"("id") ON DELETE CASCADE ON UPDATE CASCADE;
ALTER TABLE "OnboardingProgress" ADD CONSTRAINT "OnboardingProgress_teamId_fkey"
  FOREIGN KEY ("teamId") REFERENCES "Team"("id") ON DELETE CASCADE ON UPDATE CASCADE;
ALTER TABLE "ControlPlaneOutboxEvent" ADD CONSTRAINT "ControlPlaneOutboxEvent_teamId_fkey"
  FOREIGN KEY ("teamId") REFERENCES "Team"("id") ON DELETE CASCADE ON UPDATE CASCADE;
ALTER TABLE "MfaResetCase" ADD CONSTRAINT "MfaResetCase_teamId_fkey"
  FOREIGN KEY ("teamId") REFERENCES "Team"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
ALTER TABLE "MfaResetCase" ADD CONSTRAINT "MfaResetCase_targetUserId_fkey"
  FOREIGN KEY ("targetUserId") REFERENCES "User"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
ALTER TABLE "MfaResetCase" ADD CONSTRAINT "MfaResetCase_requestedById_fkey"
  FOREIGN KEY ("requestedById") REFERENCES "User"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
ALTER TABLE "MfaResetCase" ADD CONSTRAINT "MfaResetCase_firstApprovedById_fkey"
  FOREIGN KEY ("firstApprovedById") REFERENCES "User"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
ALTER TABLE "MfaResetCase" ADD CONSTRAINT "MfaResetCase_secondApprovedById_fkey"
  FOREIGN KEY ("secondApprovedById") REFERENCES "User"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

ALTER TABLE "MfaResetCase" ADD CONSTRAINT "mfa_reset_distinct_approvers" CHECK (
  "firstApprovedById" IS NULL OR "secondApprovedById" IS NULL OR "firstApprovedById" <> "secondApprovedById"
);
