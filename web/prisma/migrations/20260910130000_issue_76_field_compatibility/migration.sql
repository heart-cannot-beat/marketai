-- Converge databases that applied an early #76 review migration before the
-- control/domain boundary field names were finalized. Fresh installations
-- already have the target names, so each rename is conditional.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = 'OnboardingProgress'
      AND column_name = 'qualifiedAssetVersionAt'
  ) AND NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = 'OnboardingProgress'
      AND column_name = 'assetQualifiedAt'
  ) THEN
    ALTER TABLE "OnboardingProgress"
      RENAME COLUMN "qualifiedAssetVersionAt" TO "assetQualifiedAt";
  END IF;

  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = 'MfaResetCase'
      AND column_name = 'passwordCredentialVersion'
  ) AND NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = 'MfaResetCase'
      AND column_name = 'passwordRevision'
  ) THEN
    ALTER TABLE "MfaResetCase"
      RENAME COLUMN "passwordCredentialVersion" TO "passwordRevision";
  END IF;
END
$$;
