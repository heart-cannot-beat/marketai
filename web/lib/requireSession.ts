import type {
  GetServerSidePropsContext,
  NextApiRequest,
  NextApiResponse,
} from 'next';

import { ApiError } from './errors';
import { getSession } from './session';

/**
 * A session that has actually been authenticated.
 *
 * `getSession` returns `null` whenever the token is missing, malformed or bound
 * to a revoked session row — including when the row is revoked *between* the
 * middleware check and the handler. Reading `session?.user.id` off that `null`
 * yields `undefined`, and Prisma treats an `undefined` filter as "no filter at
 * all": `where: { userId: undefined }` silently becomes an unfiltered query and
 * returns every user's rows. Every user-scoped handler goes through this
 * function so the failure is a 401, never a wider result set.
 *
 * See https://www.prisma.io/docs/orm/prisma-client/special-fields-and-types/null-and-undefined
 */
export type AuthenticatedSession = {
  sessionId: string;
  mfaAuthenticatedAt?: string | null;
  user: {
    id: string;
    email: string;
    name?: string | null;
    image?: string | null;
    /** Saved interface language (#252); `null` follows the browser. */
    locale?: string | null;
  };
};

const isAuthenticated = (session: unknown): session is AuthenticatedSession => {
  const candidate = session as AuthenticatedSession | null;

  return Boolean(
    candidate &&
    typeof candidate.sessionId === 'string' &&
    candidate.sessionId.length > 0 &&
    candidate.user &&
    typeof candidate.user.id === 'string' &&
    candidate.user.id.length > 0 &&
    typeof candidate.user.email === 'string' &&
    candidate.user.email.length > 0
  );
};

/** Throws `401` unless the request carries a live session. */
export const requireSession = async (
  req: NextApiRequest | GetServerSidePropsContext['req'],
  res: NextApiResponse | GetServerSidePropsContext['res']
): Promise<AuthenticatedSession> => {
  const session = await getSession(req, res);

  if (!isAuthenticated(session)) {
    throw new ApiError(401, 'You must be signed in to perform this action.');
  }

  return session;
};

/** Returns the session, or `null` — for pages that redirect rather than throw. */
export const getAuthenticatedSession = async (
  req: NextApiRequest | GetServerSidePropsContext['req'],
  res: NextApiResponse | GetServerSidePropsContext['res']
): Promise<AuthenticatedSession | null> => {
  const session = await getSession(req, res);

  return isAuthenticated(session) ? session : null;
};
