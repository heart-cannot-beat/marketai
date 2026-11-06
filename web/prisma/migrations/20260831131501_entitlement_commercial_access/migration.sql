/*
  Warnings:

  - Added the required column `lifecycle` to the `Team` table without a default value. This is not possible if the table is not empty.

*/
-- CreateEnum
CREATE TYPE "PlanVersionState" AS ENUM ('EFFECTIVE', 'RETIRED');

-- CreateEnum
CREATE TYPE "CommercialAccessSourceKind" AS ENUM ('ACCESS_GRANT', 'SUBSCRIPTION');

-- CreateEnum
CREATE TYPE "AccessGrantKind" AS ENUM ('BASE', 'ADD_ON');

-- CreateEnum
CREATE TYPE "RestrictionKind" AS ENUM ('SECURITY', 'ABUSE', 'COMPLIANCE', 'BILLING');

-- CreateEnum
CREATE TYPE "RestrictionEffect" AS ENUM ('DISABLE', 'REDUCE_CAP');

-- CreateEnum
CREATE TYPE "SubscriptionState" AS ENUM ('PROVISIONING', 'ACTIVE', 'PAST_DUE', 'GRACE_PERIOD', 'CANCELLED', 'EXPIRED');

-- CreateEnum
CREATE TYPE "SnapshotDeliveryState" AS ENUM ('PENDING', 'DELIVERING', 'DELIVERED', 'FAILED', 'DEAD_LETTER');

-- AlterTable
-- Prisma generates "lifecycle" as required, which is not possible on a
-- non-empty table without a default. Add it nullable, backfill from the
-- existing "suspended" flag, then enforce NOT NULL + a value CHECK. See
-- spec §1.13.
ALTER TABLE "Team" ADD COLUMN     "lifecycle" TEXT;

-- Backfill from existing suspended flag.
UPDATE "Team" SET "lifecycle" = CASE WHEN "suspended" = true THEN 'SUSPENDED' ELSE 'ACTIVE' END;

-- Add CHECK and make non-null.
ALTER TABLE "Team" ADD CONSTRAINT team_lifecycle_valid
  CHECK ("lifecycle" IN ('PROVISIONING', 'ACTIVE', 'SUSPENDED', 'DELETION_PENDING', 'DELETED'));
ALTER TABLE "Team" ALTER COLUMN "lifecycle" SET NOT NULL;

-- CreateTable
CREATE TABLE "PlanVersion" (
    "id" TEXT NOT NULL,
    "planKey" TEXT NOT NULL,
    "versionNo" INTEGER NOT NULL,
    "displayName" TEXT NOT NULL,
    "state" "PlanVersionState" NOT NULL DEFAULT 'EFFECTIVE',
    "entitlements" JSONB NOT NULL,
    "entitlementsHash" TEXT NOT NULL,
    "publishedBy" TEXT NOT NULL,
    "publishedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "retiredAt" TIMESTAMP(3),
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "PlanVersion_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "AccessGrant" (
    "id" TEXT NOT NULL,
    "teamId" TEXT NOT NULL,
    "planVersionId" TEXT NOT NULL,
    "grantKind" "AccessGrantKind" NOT NULL,
    "source" TEXT NOT NULL,
    "reason" TEXT NOT NULL DEFAULT '',
    "periodAnchor" TIMESTAMP(3),
    "startsAt" TIMESTAMP(3) NOT NULL,
    "endsAt" TIMESTAMP(3),
    "grantedBy" TEXT NOT NULL,
    "revokedAt" TIMESTAMP(3),
    "revokedBy" TEXT,
    "revokeReason" TEXT,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "AccessGrant_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "Subscription" (
    "id" TEXT NOT NULL,
    "teamId" TEXT NOT NULL,
    "planVersionId" TEXT NOT NULL,
    "state" "SubscriptionState" NOT NULL DEFAULT 'PROVISIONING',
    "periodStart" TIMESTAMP(3) NOT NULL,
    "periodEnd" TIMESTAMP(3) NOT NULL,
    "periodAnchor" TIMESTAMP(3) NOT NULL,
    "graceEndsAt" TIMESTAMP(3),
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "Subscription_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "CommercialAccessBinding" (
    "id" TEXT NOT NULL,
    "teamId" TEXT NOT NULL,
    "sourceKind" "CommercialAccessSourceKind" NOT NULL,
    "accessGrantId" TEXT,
    "subscriptionId" TEXT,
    "planVersionId" TEXT NOT NULL,
    "effectiveAt" TIMESTAMP(3) NOT NULL,
    "deactivatedAt" TIMESTAMP(3),
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "CommercialAccessBinding_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "Restriction" (
    "id" TEXT NOT NULL,
    "teamId" TEXT NOT NULL,
    "kind" "RestrictionKind" NOT NULL,
    "effect" "RestrictionEffect" NOT NULL,
    "entitlementKey" TEXT NOT NULL,
    "restrictedValue" JSONB NOT NULL,
    "reason" TEXT NOT NULL,
    "imposedBy" TEXT NOT NULL,
    "imposedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "liftedAt" TIMESTAMP(3),
    "liftedBy" TEXT,
    "liftReason" TEXT,

    CONSTRAINT "Restriction_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "CommercialAccessProjection" (
    "id" TEXT NOT NULL,
    "teamId" TEXT NOT NULL,
    "currentSnapshotId" TEXT,
    "entitlementVersion" INTEGER NOT NULL DEFAULT 0,
    "leaseSequence" INTEGER NOT NULL DEFAULT 0,
    "semanticHash" TEXT NOT NULL DEFAULT '',
    "entitlementRegistryVersion" INTEGER NOT NULL DEFAULT 1,
    "resolutionPolicyVersion" INTEGER NOT NULL DEFAULT 1,
    "platformLimitsVersion" INTEGER NOT NULL DEFAULT 1,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "CommercialAccessProjection_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "EntitlementSnapshot" (
    "id" TEXT NOT NULL,
    "teamId" TEXT NOT NULL,
    "schemaVersion" INTEGER NOT NULL,
    "workspaceName" TEXT NOT NULL,
    "authorizationEpoch" INTEGER NOT NULL,
    "entitlementVersion" INTEGER NOT NULL,
    "leaseSequence" INTEGER NOT NULL,
    "projectionVersion" INTEGER NOT NULL,
    "lifecycle" TEXT NOT NULL,
    "suspended" BOOLEAN NOT NULL,
    "planVersionId" TEXT NOT NULL,
    "sourceRefs" JSONB NOT NULL,
    "periodStart" TIMESTAMP(3) NOT NULL,
    "periodEnd" TIMESTAMP(3) NOT NULL,
    "periodAnchor" TIMESTAMP(3) NOT NULL,
    "resolvedEntitlements" JSONB NOT NULL,
    "restrictionSummary" JSONB NOT NULL,
    "semanticHash" TEXT NOT NULL,
    "entitlementRegistryVersion" INTEGER NOT NULL,
    "resolutionPolicyVersion" INTEGER NOT NULL,
    "platformLimitsVersion" INTEGER NOT NULL,
    "issuedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "validUntil" TIMESTAMP(3) NOT NULL,
    "auditEventId" TEXT NOT NULL,
    "signatureHmac" TEXT NOT NULL,
    "signatureKeyId" TEXT NOT NULL,
    "signatureAlgorithm" TEXT NOT NULL DEFAULT 'hmac-sha256',
    "canonicalEnvelope" BYTEA NOT NULL,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "EntitlementSnapshot_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "EntitlementSnapshotOutbox" (
    "id" TEXT NOT NULL,
    "snapshotId" TEXT NOT NULL,
    "teamId" TEXT NOT NULL,
    "deliveryState" "SnapshotDeliveryState" NOT NULL DEFAULT 'PENDING',
    "attempts" INTEGER NOT NULL DEFAULT 0,
    "maxAttempts" INTEGER NOT NULL DEFAULT 5,
    "lastAttemptAt" TIMESTAMP(3),
    "nextAttemptAt" TIMESTAMP(3),
    "lastError" TEXT,
    "deliveryLeaseId" TEXT,
    "deliveryLeaseExpiresAt" TIMESTAMP(3),
    "deliveredAt" TIMESTAMP(3),
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "EntitlementSnapshotOutbox_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "ControlPlaneAuditEvent" (
    "id" TEXT NOT NULL,
    "scopeKind" TEXT NOT NULL,
    "teamId" TEXT,
    "actorKind" TEXT NOT NULL,
    "actorId" TEXT NOT NULL,
    "executor" TEXT NOT NULL,
    "eventType" TEXT NOT NULL,
    "resourceType" TEXT NOT NULL,
    "resourceId" TEXT NOT NULL,
    "requestId" TEXT,
    "traceId" TEXT,
    "details" JSONB NOT NULL,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "ControlPlaneAuditEvent_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE INDEX "PlanVersion_state_idx" ON "PlanVersion"("state");

-- CreateIndex
CREATE UNIQUE INDEX "PlanVersion_planKey_versionNo_key" ON "PlanVersion"("planKey", "versionNo");

-- CreateIndex
CREATE INDEX "AccessGrant_teamId_grantKind_idx" ON "AccessGrant"("teamId", "grantKind");

-- CreateIndex
CREATE INDEX "AccessGrant_teamId_revokedAt_idx" ON "AccessGrant"("teamId", "revokedAt");

-- CreateIndex
CREATE UNIQUE INDEX "AccessGrant_teamId_id_key" ON "AccessGrant"("teamId", "id");

-- CreateIndex
CREATE INDEX "Subscription_teamId_state_idx" ON "Subscription"("teamId", "state");

-- CreateIndex
CREATE UNIQUE INDEX "Subscription_teamId_id_key" ON "Subscription"("teamId", "id");

-- CreateIndex
CREATE UNIQUE INDEX "CommercialAccessBinding_accessGrantId_key" ON "CommercialAccessBinding"("accessGrantId");

-- CreateIndex
CREATE UNIQUE INDEX "CommercialAccessBinding_subscriptionId_key" ON "CommercialAccessBinding"("subscriptionId");

-- CreateIndex
CREATE INDEX "CommercialAccessBinding_teamId_deactivatedAt_idx" ON "CommercialAccessBinding"("teamId", "deactivatedAt");

-- CreateIndex
CREATE UNIQUE INDEX "CommercialAccessBinding_teamId_accessGrantId_key" ON "CommercialAccessBinding"("teamId", "accessGrantId");

-- CreateIndex
CREATE UNIQUE INDEX "CommercialAccessBinding_teamId_subscriptionId_key" ON "CommercialAccessBinding"("teamId", "subscriptionId");

-- CreateIndex
CREATE INDEX "Restriction_teamId_liftedAt_idx" ON "Restriction"("teamId", "liftedAt");

-- CreateIndex
CREATE UNIQUE INDEX "CommercialAccessProjection_teamId_key" ON "CommercialAccessProjection"("teamId");

-- CreateIndex
CREATE UNIQUE INDEX "CommercialAccessProjection_currentSnapshotId_key" ON "CommercialAccessProjection"("currentSnapshotId");

-- CreateIndex
CREATE UNIQUE INDEX "CommercialAccessProjection_teamId_currentSnapshotId_key" ON "CommercialAccessProjection"("teamId", "currentSnapshotId");

-- CreateIndex
CREATE INDEX "EntitlementSnapshot_teamId_entitlementVersion_idx" ON "EntitlementSnapshot"("teamId", "entitlementVersion");

-- CreateIndex
CREATE UNIQUE INDEX "EntitlementSnapshot_teamId_entitlementVersion_leaseSequence_key" ON "EntitlementSnapshot"("teamId", "entitlementVersion", "leaseSequence");

-- CreateIndex
CREATE UNIQUE INDEX "EntitlementSnapshot_teamId_id_key" ON "EntitlementSnapshot"("teamId", "id");

-- CreateIndex
CREATE UNIQUE INDEX "EntitlementSnapshotOutbox_snapshotId_key" ON "EntitlementSnapshotOutbox"("snapshotId");

-- CreateIndex
CREATE INDEX "EntitlementSnapshotOutbox_deliveryState_nextAttemptAt_idx" ON "EntitlementSnapshotOutbox"("deliveryState", "nextAttemptAt");

-- CreateIndex
CREATE INDEX "EntitlementSnapshotOutbox_teamId_idx" ON "EntitlementSnapshotOutbox"("teamId");

-- CreateIndex
CREATE INDEX "EntitlementSnapshotOutbox_deliveryLeaseExpiresAt_idx" ON "EntitlementSnapshotOutbox"("deliveryLeaseExpiresAt");

-- CreateIndex
CREATE UNIQUE INDEX "EntitlementSnapshotOutbox_teamId_snapshotId_key" ON "EntitlementSnapshotOutbox"("teamId", "snapshotId");

-- CreateIndex
CREATE INDEX "ControlPlaneAuditEvent_teamId_createdAt_idx" ON "ControlPlaneAuditEvent"("teamId", "createdAt");

-- CreateIndex
CREATE INDEX "ControlPlaneAuditEvent_eventType_createdAt_idx" ON "ControlPlaneAuditEvent"("eventType", "createdAt");

-- CreateIndex
CREATE UNIQUE INDEX "ControlPlaneAuditEvent_teamId_id_key" ON "ControlPlaneAuditEvent"("teamId", "id");

-- AddForeignKey
ALTER TABLE "AccessGrant" ADD CONSTRAINT "AccessGrant_teamId_fkey" FOREIGN KEY ("teamId") REFERENCES "Team"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "AccessGrant" ADD CONSTRAINT "AccessGrant_planVersionId_fkey" FOREIGN KEY ("planVersionId") REFERENCES "PlanVersion"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "Subscription" ADD CONSTRAINT "Subscription_teamId_fkey" FOREIGN KEY ("teamId") REFERENCES "Team"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "Subscription" ADD CONSTRAINT "Subscription_planVersionId_fkey" FOREIGN KEY ("planVersionId") REFERENCES "PlanVersion"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "CommercialAccessBinding" ADD CONSTRAINT "CommercialAccessBinding_teamId_fkey" FOREIGN KEY ("teamId") REFERENCES "Team"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "CommercialAccessBinding" ADD CONSTRAINT "CommercialAccessBinding_teamId_accessGrantId_fkey" FOREIGN KEY ("teamId", "accessGrantId") REFERENCES "AccessGrant"("teamId", "id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "CommercialAccessBinding" ADD CONSTRAINT "CommercialAccessBinding_teamId_subscriptionId_fkey" FOREIGN KEY ("teamId", "subscriptionId") REFERENCES "Subscription"("teamId", "id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "CommercialAccessBinding" ADD CONSTRAINT "CommercialAccessBinding_planVersionId_fkey" FOREIGN KEY ("planVersionId") REFERENCES "PlanVersion"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "Restriction" ADD CONSTRAINT "Restriction_teamId_fkey" FOREIGN KEY ("teamId") REFERENCES "Team"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "CommercialAccessProjection" ADD CONSTRAINT "CommercialAccessProjection_teamId_fkey" FOREIGN KEY ("teamId") REFERENCES "Team"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "CommercialAccessProjection" ADD CONSTRAINT "CommercialAccessProjection_teamId_currentSnapshotId_fkey" FOREIGN KEY ("teamId", "currentSnapshotId") REFERENCES "EntitlementSnapshot"("teamId", "id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "EntitlementSnapshot" ADD CONSTRAINT "EntitlementSnapshot_teamId_fkey" FOREIGN KEY ("teamId") REFERENCES "Team"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "EntitlementSnapshot" ADD CONSTRAINT "EntitlementSnapshot_teamId_auditEventId_fkey" FOREIGN KEY ("teamId", "auditEventId") REFERENCES "ControlPlaneAuditEvent"("teamId", "id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "EntitlementSnapshotOutbox" ADD CONSTRAINT "EntitlementSnapshotOutbox_teamId_snapshotId_fkey" FOREIGN KEY ("teamId", "snapshotId") REFERENCES "EntitlementSnapshot"("teamId", "id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ControlPlaneAuditEvent" ADD CONSTRAINT "ControlPlaneAuditEvent_teamId_fkey" FOREIGN KEY ("teamId") REFERENCES "Team"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- -----------------------------------------------------------------------
-- Database invariants Prisma cannot express natively: immutability
-- triggers, CHECK constraints and a partial unique index. Source of truth:
-- spec §1.2-§1.11 and
-- docs/architecture/subscription-entitlement-usage-cost-ownership.md.
-- -----------------------------------------------------------------------

-- PlanVersion content is immutable after publication (spec §1.2). Only
-- state/retiredAt (retirement) may change after creation.
CREATE FUNCTION plan_version_immutable_guard() RETURNS trigger AS $$
BEGIN
  IF OLD."planKey" IS DISTINCT FROM NEW."planKey"
     OR OLD."versionNo" IS DISTINCT FROM NEW."versionNo"
     OR OLD."entitlements"::text IS DISTINCT FROM NEW."entitlements"::text
     OR OLD."entitlementsHash" IS DISTINCT FROM NEW."entitlementsHash"
     OR OLD."publishedBy" IS DISTINCT FROM NEW."publishedBy"
     OR OLD."publishedAt" IS DISTINCT FROM NEW."publishedAt"
  THEN
    RAISE EXCEPTION 'PlanVersion content is immutable after publication';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_plan_version_immutable
  BEFORE UPDATE ON "PlanVersion"
  FOR EACH ROW EXECUTE FUNCTION plan_version_immutable_guard();

-- AccessGrant: BASE requires a periodAnchor, ADD_ON requires a non-empty
-- reason and forbids periodAnchor, endsAt (when set) must be after
-- startsAt. See spec §1.3.
ALTER TABLE "AccessGrant" ADD CONSTRAINT access_grant_base_requires_anchor
  CHECK ("grantKind" != 'BASE' OR "periodAnchor" IS NOT NULL);

ALTER TABLE "AccessGrant" ADD CONSTRAINT access_grant_addon_requires_reason
  CHECK ("grantKind" != 'ADD_ON' OR length("reason") > 0);

ALTER TABLE "AccessGrant" ADD CONSTRAINT access_grant_addon_no_anchor
  CHECK ("grantKind" != 'ADD_ON' OR "periodAnchor" IS NULL);

ALTER TABLE "AccessGrant" ADD CONSTRAINT access_grant_ends_after_starts
  CHECK ("endsAt" IS NULL OR "endsAt" > "startsAt");

-- CommercialAccessBinding: at most one active (non-deactivated) binding per
-- Workspace, and the source is exactly one of AccessGrant or Subscription
-- consistent with sourceKind. See spec §1.5.
CREATE UNIQUE INDEX commercial_access_binding_one_active
  ON "CommercialAccessBinding" ("teamId")
  WHERE "deactivatedAt" IS NULL;

ALTER TABLE "CommercialAccessBinding" ADD CONSTRAINT binding_source_exactly_one
  CHECK (
    ("sourceKind" = 'ACCESS_GRANT' AND "accessGrantId" IS NOT NULL AND "subscriptionId" IS NULL)
    OR
    ("sourceKind" = 'SUBSCRIPTION' AND "subscriptionId" IS NOT NULL AND "accessGrantId" IS NULL)
  );

-- Subscription: billing period must be well-formed. See spec §1.4.
ALTER TABLE "Subscription" ADD CONSTRAINT subscription_period_order
  CHECK ("periodEnd" > "periodStart");

-- Restriction: DISABLE always carries the literal value false; REDUCE_CAP
-- always carries a non-negative numeric cap. See spec §1.6.
ALTER TABLE "Restriction" ADD CONSTRAINT restriction_disable_value
  CHECK ("effect" != 'DISABLE' OR "restrictedValue"::text = 'false');

ALTER TABLE "Restriction" ADD CONSTRAINT restriction_reduce_cap_value
  CHECK ("effect" != 'REDUCE_CAP' OR (
    jsonb_typeof("restrictedValue"::jsonb) = 'number'
    AND ("restrictedValue"::jsonb)::numeric >= 0
  ));

-- ControlPlaneAuditEvent and EntitlementSnapshot are append-only durable
-- records. Reject UPDATE and DELETE outright. See spec §1.11.
CREATE FUNCTION control_plane_audit_event_immutable() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'ControlPlaneAuditEvent rows are immutable';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_audit_event_immutable
  BEFORE UPDATE OR DELETE ON "ControlPlaneAuditEvent"
  FOR EACH ROW EXECUTE FUNCTION control_plane_audit_event_immutable();

CREATE FUNCTION entitlement_snapshot_immutable() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'EntitlementSnapshot rows are immutable';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_entitlement_snapshot_immutable
  BEFORE UPDATE OR DELETE ON "EntitlementSnapshot"
  FOR EACH ROW EXECUTE FUNCTION entitlement_snapshot_immutable();
