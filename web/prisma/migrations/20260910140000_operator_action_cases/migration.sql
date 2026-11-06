-- Persistent dual-control cases for invitation reissue and manual onboarding
-- reconciliation. Operator identities are verified at the process boundary;
-- their stable subjects are persisted here.

CREATE TABLE "OperatorActionCase" (
  "id" TEXT NOT NULL,
  "teamId" TEXT NOT NULL,
  "action" TEXT NOT NULL,
  "targetResourceId" TEXT NOT NULL,
  "requestedBySubject" TEXT NOT NULL,
  "reason" TEXT NOT NULL,
  "status" TEXT NOT NULL DEFAULT 'AWAITING_APPROVAL',
  "expiresAt" TIMESTAMP(3) NOT NULL,
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "completedAt" TIMESTAMP(3),
  CONSTRAINT "OperatorActionCase_pkey" PRIMARY KEY ("id")
);

CREATE TABLE "OperatorActionApproval" (
  "id" TEXT NOT NULL,
  "caseId" TEXT NOT NULL,
  "operatorSubject" TEXT NOT NULL,
  "operatorIssuer" TEXT NOT NULL,
  "approvedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT "OperatorActionApproval_pkey" PRIMARY KEY ("id")
);

CREATE INDEX "OperatorActionCase_teamId_action_status_idx"
  ON "OperatorActionCase"("teamId", "action", "status");
CREATE UNIQUE INDEX "OperatorActionApproval_caseId_operatorSubject_key"
  ON "OperatorActionApproval"("caseId", "operatorSubject");
CREATE INDEX "OperatorActionApproval_caseId_approvedAt_idx"
  ON "OperatorActionApproval"("caseId", "approvedAt");

ALTER TABLE "OperatorActionCase" ADD CONSTRAINT "OperatorActionCase_teamId_fkey"
  FOREIGN KEY ("teamId") REFERENCES "Team"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
ALTER TABLE "OperatorActionApproval" ADD CONSTRAINT "OperatorActionApproval_caseId_fkey"
  FOREIGN KEY ("caseId") REFERENCES "OperatorActionCase"("id") ON DELETE CASCADE ON UPDATE CASCADE;
