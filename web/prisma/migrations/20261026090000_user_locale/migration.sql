-- Interface language preference (#252). NULL means "follow the browser", which
-- is also what every existing account gets. The check mirrors
-- `SUPPORTED_LOCALES` in `lib/i18n/locales.ts`; offering another language is a
-- reviewed migration, not a free-form value.
ALTER TABLE "User" ADD COLUMN "locale" TEXT;

ALTER TABLE "User"
  ADD CONSTRAINT "User_locale_check"
  CHECK ("locale" IS NULL OR "locale" IN ('en', 'zh-CN'));
