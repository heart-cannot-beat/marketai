import { NextAuthOptions } from 'next-auth';
import CredentialsProvider from 'next-auth/providers/credentials';
import { PrismaAdapter } from '@next-auth/prisma-adapter';
import type { Provider } from 'next-auth/providers';

import { getUser } from 'models/user';
import {
  createAuthSession,
  deleteExpiredSessionsForUser,
  findActiveAuthSession,
} from 'models/session';
import { verifyPassword } from '@/lib/auth';
import { isEmailAllowed } from '@/lib/email/utils';
import env from '@/lib/env';
import { prisma } from '@/lib/prisma';
import { authProviderEnabled, isAuthProviderEnabled } from '@/lib/auth';
import { assertSupportedSessionStrategy } from '@/lib/sessionStrategy';
import { logger } from '@/lib/logger';
import type { AUTH_PROVIDER } from 'types';
import {
  clearLoginAttempts,
  exceededLoginAttemptsThreshold,
  incrementLoginAttempts,
} from '@/lib/accountLock';
import { maxLengthPolicies } from '@/lib/common';
import { verifyMfaChallenge } from '@/lib/mfa';

// Fails at import time rather than as a 500 on the login page.
assertSupportedSessionStrategy(
  env.nextAuth.sessionStrategy,
  (Object.keys(authProviderEnabled()) as AUTH_PROVIDER[]).filter(
    (provider) => authProviderEnabled()[provider]
  )
);

const adapter = PrismaAdapter(prisma);
const providers: Provider[] = [];
export const useSecureCookie = env.appUrl.startsWith('https://');

/**
 * NextAuth prefixes the cookie with `__Secure-` on HTTPS. Anything that clears
 * or reads the session cookie must use this value rather than a hard-coded
 * name, or sign-out silently leaves a valid token in the browser on HTTPS.
 */
export const sessionTokenCookieName =
  (useSecureCookie ? '__Secure-' : '') + 'next-auth.session-token';

/** Every cookie name NextAuth could have used for the session, for clearing. */
export const sessionTokenCookieNames = [
  'next-auth.session-token',
  '__Secure-next-auth.session-token',
];

export const sessionMaxAgeSeconds = 14 * 24 * 60 * 60;

/**
 * Only the credentials provider is wired.
 *
 * Upstream additionally shipped GitHub, Google, magic-link email, BoxyHQ SAML
 * and IdP-initiated providers. Every one of them provisions a User row on first
 * successful sign-in, which is public registration. They are removed rather
 * than feature-flagged so that setting an environment variable cannot re-open
 * the entry path.
 */
if (isAuthProviderEnabled('credentials')) {
  providers.push(
    CredentialsProvider({
      id: 'credentials',
      credentials: {
        email: { type: 'email' },
        password: { type: 'password' },
        mfaCode: { type: 'text' },
      },
      async authorize(credentials) {
        if (!credentials) {
          throw new Error('no-credentials');
        }

        const { email, password, mfaCode } = credentials;

        if (!email || !password) {
          return null;
        }

        const user = await getUser({ email });

        // Unknown accounts never create anything. Invite-only Beta.
        if (!user) {
          throw new Error('invalid-credentials');
        }

        if (exceededLoginAttemptsThreshold(user)) {
          throw new Error('exceeded-login-attempts');
        }

        if (env.confirmEmail && !user.emailVerified) {
          throw new Error('confirm-your-email');
        }

        const hasValidPassword = await verifyPassword(
          password,
          user?.password as string
        );

        if (!hasValidPassword) {
          if (
            exceededLoginAttemptsThreshold(await incrementLoginAttempts(user))
          ) {
            throw new Error('exceeded-login-attempts');
          }

          throw new Error('invalid-credentials');
        }

        await clearLoginAttempts(user);

        const mfaCredential = await prisma.mfaCredential.findUnique({
          where: { userId: user.id },
          select: { verifiedAt: true },
        });
        let mfaAuthenticatedAt: string | null = null;
        if (mfaCredential?.verifiedAt) {
          if (!mfaCode) throw new Error('mfa-required');
          if (!(await verifyMfaChallenge(user.id, mfaCode))) {
            throw new Error('mfa-invalid');
          }
          mfaAuthenticatedAt = new Date().toISOString();
        }

        return {
          id: user.id,
          name: user.name,
          email: user.email,
          // Captured at the moment the password was verified; re-checked under
          // a row lock before the session is minted.
          credentialVersion: user.credentialVersion,
          mfaAuthenticatedAt,
        };
      },
    })
  );
}

/**
 * The options no longer depend on the request: the database-session shim that
 * needed `req`/`res` is gone, and session validity is decided by the session
 * callback against the server-side session row.
 */
export const getAuthOptions = () => {
  const authOptions: NextAuthOptions = {
    adapter,
    providers,
    pages: {
      signIn: '/auth/login',
      verifyRequest: '/auth/verify-request',
    },
    session: {
      strategy: env.nextAuth.sessionStrategy,
      maxAge: sessionMaxAgeSeconds,
    },
    secret: env.nextAuth.secret,
    callbacks: {
      async signIn({ user, account }) {
        if (!user || !user.email || !account) {
          return false;
        }

        if (!isEmailAllowed(user.email)) {
          return '/auth/login?error=allow-only-work-email';
        }

        // Defence in depth: a provider added by mistake must not be able to
        // introduce an account that does not already exist.
        if (account.provider !== 'credentials') {
          logger.warn('control_plane.signin_rejected_unsupported_provider', {
            provider: account.provider,
          });
          return false;
        }

        const existingUser = await getUser({ email: user.email });

        if (!existingUser) {
          logger.warn('control_plane.signin_rejected_unknown_account');
          return false;
        }

        return true;
      },

      /**
       * The JWT is a carrier, not the authority.
       *
       * Every session read re-reads the server-side session row named by the
       * `sid` claim. A revoked row (sign-out, password change, password reset,
       * an entry removed from Manage Sessions) makes the token unusable on the
       * very next request, which is what makes a stateless cookie revocable.
       *
       * Returning an empty object is NextAuth's "no session" shape:
       * `getServerSession` resolves it to `null`, and `/api/auth/session`
       * answers with valid JSON the browser client reads as signed out.
       * Returning `null` here instead produces an empty response body that the
       * client cannot parse.
       */
      async session({ session, token }) {
        const noSession = {} as unknown as typeof session;

        if (!token?.sub || !token.sid) {
          return noSession;
        }

        const active = await findActiveAuthSession(
          token.sid as string,
          token.sub
        );

        if (!active) {
          logger.warn('control_plane.session_rejected_revoked');
          return noSession;
        }

        // Identity comes from the row, not from the token: a JWT minted before
        // an email or name change still carries the old values, and
        // `session.user.email` decides which invitation an account may accept.
        session.user.id = active.user.id;
        session.user.email = active.user.email;
        session.user.image = active.user.image;
        // The middleware reads the saved interface language from here.
        session.user.locale = active.user.locale;
        session.user.name = active.user.name
          ? active.user.name.substring(0, maxLengthPolicies.name)
          : active.user.name;
        session.sessionId = active.id;
        session.mfaAuthenticatedAt =
          active.mfaAuthenticatedAt?.toISOString() ?? null;
        session.expires = active.expires.toISOString();

        return session;
      },

      async jwt({ token, user, trigger, session }) {
        // Mint the server-side session at sign-in and bind the token to it.
        if (trigger === 'signIn' && user?.id) {
          const authenticatedUser = user as typeof user & {
            credentialVersion?: number;
            mfaAuthenticatedAt?: string | null;
          };
          // Prune this account's aged-out rows while we are already writing.
          await deleteExpiredSessionsForUser(user.id).catch(() => undefined);

          // Throws when the credentials changed since `authorize()` verified
          // them, which fails the sign-in rather than minting a session whose
          // authority a password reset has already withdrawn.
          const record = await createAuthSession(
            user.id,
            sessionMaxAgeSeconds,
            authenticatedUser.credentialVersion ?? -1,
            authenticatedUser.mfaAuthenticatedAt
              ? new Date(authenticatedUser.mfaAuthenticatedAt)
              : null
          );

          return { ...token, sub: user.id, sid: record.id };
        }

        if (trigger === 'update' && 'name' in session && session.name) {
          return { ...token, name: session.name };
        }

        return token;
      },
    },
  };

  return authOptions;
};
