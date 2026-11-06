-- Keep consumed/revoked invitation rows as immutable history while allowing a
-- later invitation to the same address. Fail before changing constraints if a
-- manually-modified cluster already contains multiple active candidates.
DO $$
DECLARE
  duplicate_email RECORD;
  duplicate_owner RECORD;
BEGIN
  SELECT "teamId", lower("email") AS normalized_email, count(*) AS copies
    INTO duplicate_email
    FROM "Invitation"
   WHERE "consumedAt" IS NULL AND "revokedAt" IS NULL
   GROUP BY "teamId", lower("email")
  HAVING count(*) > 1
   LIMIT 1;
  IF FOUND THEN
    RAISE EXCEPTION
      'active invitation uniqueness violation for team %, email % (% rows); resolve before migration',
      duplicate_email."teamId", duplicate_email.normalized_email, duplicate_email.copies;
  END IF;

  SELECT "teamId", count(*) AS copies
    INTO duplicate_owner
    FROM "Invitation"
   WHERE "kind" = 'FIRST_OWNER'
     AND "consumedAt" IS NULL
     AND "revokedAt" IS NULL
   GROUP BY "teamId"
  HAVING count(*) > 1
   LIMIT 1;
  IF FOUND THEN
    RAISE EXCEPTION
      'active first-owner invitation uniqueness violation for team % (% rows); resolve before migration',
      duplicate_owner."teamId", duplicate_owner.copies;
  END IF;
END $$;

DROP INDEX "Invitation_teamId_email_key";
DROP INDEX "Invitation_one_first_owner_per_workspace_key";

CREATE INDEX "Invitation_teamId_email_idx"
  ON "Invitation"("teamId", "email");
CREATE UNIQUE INDEX "Invitation_active_team_email_key"
  ON "Invitation"("teamId", lower("email"))
  WHERE "consumedAt" IS NULL AND "revokedAt" IS NULL;
CREATE UNIQUE INDEX "Invitation_active_first_owner_team_key"
  ON "Invitation"("teamId")
  WHERE "kind" = 'FIRST_OWNER'
    AND "consumedAt" IS NULL
    AND "revokedAt" IS NULL;

ALTER TABLE "OperatorActionCase" ADD COLUMN "resultResourceId" TEXT;
