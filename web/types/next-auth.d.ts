import type { Role } from '@prisma/client';
import type { DefaultSession } from 'next-auth';

declare module 'next-auth' {
  /**
   * Returned by `useSession`, `getSession` and received as a prop on the `SessionProvider` React Context
   */
  interface Session {
    /** Id of the server-side session row this token is bound to. */
    sessionId: string;
    /** Time of the last server-verified MFA challenge for this session. */
    mfaAuthenticatedAt: string | null;
    user: DefaultSession['user'] & {
      id: string;
      roles: { teamId: string; role: Role }[];
      /** Saved interface language; null follows the browser. */
      locale: string | null;
    };
  }
}

declare module 'next-auth/jwt' {
  interface JWT {
    /** Server-side session row id. A token without it is not usable. */
    sid?: string;
    mfaAuthenticatedAt?: string | null;
  }
}
