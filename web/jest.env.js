// Runs before the module registry is set up, so `lib/env.ts` sees these when it
// is first imported. The same-origin check and the session cookie names are
// both derived from APP_URL, so tests need a deterministic one.
process.env.APP_URL = process.env.APP_URL || 'http://localhost:4102';
process.env.NEXTAUTH_URL = process.env.NEXTAUTH_URL || process.env.APP_URL;
process.env.NEXTAUTH_SECRET =
  process.env.NEXTAUTH_SECRET || 'jest-only-secret-not-for-production';
