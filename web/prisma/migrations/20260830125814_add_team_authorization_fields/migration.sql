-- AlterTable
ALTER TABLE "Team" ADD COLUMN     "authorizationEpoch" INTEGER NOT NULL DEFAULT 0,
ADD COLUMN     "entitlementVersion" INTEGER NOT NULL DEFAULT 0,
ADD COLUMN     "projectionVersion" INTEGER NOT NULL DEFAULT 0,
ADD COLUMN     "suspended" BOOLEAN NOT NULL DEFAULT false;
