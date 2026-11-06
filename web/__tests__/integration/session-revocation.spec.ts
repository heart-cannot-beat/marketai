// This file has no static imports; `export {}` keeps it a module so its
// top-level names do not collide with the other integration suites.
export {};

/**
 * Sessions are revocable even though the cookie is a JWT.
 *
 * The token carries a `sid` claim bound to a `Session` row; the NextAuth
 * session callback re-reads that row on every session read. Deleting the row
 * must make the token unusable on the next request.
 *
 * Runs against real PostgreSQL; skips when no control-plane connection string
 * is configured.
 */
const controlUrl = process.env.CONTROL_PLANE_DATABASE_URL;
const configured = Boolean(controlUrl);

const describeIfConfigured = configured ? describe : describe.skip;

describeIfConfigured('Session revocation (PostgreSQL)', () => {
  jest.setTimeout(30_000);

  /* eslint-disable @typescript-eslint/no-require-imports */
  let prisma: any;
  let getAuthOptions: any;
  let randomUUID: () => string;
  let userId: string;
  let credentialVersion: number;

  const readSession = async (token: Record<string, unknown>) => {
    const callbacks = getAuthOptions().callbacks;

    return callbacks.session({
      session: {
        user: { name: 'Owner', email: 'owner@example.test' },
        expires: new Date(Date.now() + 1000).toISOString(),
      },
      token,
    });
  };

  const signInToken = async () => {
    const callbacks = getAuthOptions().callbacks;

    return callbacks.jwt({
      token: {},
      // `authorize()` supplies the credential version it verified against.
      user: { id: userId, credentialVersion },
      trigger: 'signIn',
    });
  };

  beforeAll(async () => {
    process.env.DATABASE_URL = controlUrl as string;

    ({ randomUUID } = require('crypto'));
    ({ prisma } = require('../../lib/prisma'));
    ({ getAuthOptions } = require('../../lib/nextAuth'));

    const user = await prisma.user.create({
      data: {
        name: 'Revocation',
        email: `revocation-${randomUUID().slice(0, 8)}@example.test`,
      },
    });

    userId = user.id;
    credentialVersion = user.credentialVersion;
  });

  afterAll(async () => {
    if (!prisma) return;
    await prisma.session.deleteMany({ where: { userId } });
    await prisma.user.deleteMany({ where: { id: userId } });
    await prisma.$disconnect();
  });

  it('mints a server-side session row and binds the token to it', async () => {
    const token = await signInToken();

    expect(token.sub).toBe(userId);
    expect(token.sid).toEqual(expect.any(String));

    const row = await prisma.session.findUnique({ where: { id: token.sid } });

    expect(row).not.toBeNull();
    expect(row.userId).toBe(userId);
    // The row's own token is never handed to the browser.
    expect(token.sid).not.toBe(row.sessionToken);
  });

  it('resolves a session while the row exists', async () => {
    const token = await signInToken();
    const session = await readSession(token);

    expect(session?.user?.id).toBe(userId);
    expect(session?.sessionId).toBe(token.sid);
  });

  it('reports "no session" as valid JSON the client can parse', async () => {
    // `null` would serialise to an empty body and break the NextAuth browser
    // client with "Unexpected end of JSON input".
    const empty = await readSession({});

    expect(empty).toEqual({});
    expect(JSON.parse(JSON.stringify(empty))).toEqual({});
  });

  it('stops resolving the moment the row is deleted', async () => {
    const token = await signInToken();

    expect((await readSession(token))?.user?.id).toBe(userId);

    await prisma.session.delete({ where: { id: token.sid } });

    // Same token, same signature, no session. `{}` is NextAuth's "no session"
    // shape: getServerSession resolves it to null.
    expect(await readSession(token)).toEqual({});
  });

  it('stops resolving once the row has expired', async () => {
    const token = await signInToken();

    await prisma.session.update({
      where: { id: token.sid },
      data: { expires: new Date(Date.now() - 1000) },
    });

    expect(await readSession(token)).toEqual({});
  });

  it('refuses a token that carries no session id', async () => {
    expect(await readSession({ sub: userId })).toEqual({});
    expect(await readSession({ sid: randomUUID() })).toEqual({});
  });

  it('refuses a session id that belongs to a different user', async () => {
    const token = await signInToken();
    const other = await prisma.user.create({
      data: {
        name: 'Other',
        email: `other-${randomUUID().slice(0, 8)}@example.test`,
      },
    });

    try {
      expect(await readSession({ ...token, sub: other.id })).toEqual({});
    } finally {
      await prisma.user.delete({ where: { id: other.id } });
    }
  });
});

if (!configured) {
  describe('Session revocation (PostgreSQL)', () => {
    it('is skipped without CONTROL_PLANE_DATABASE_URL', () => {
      expect(configured).toBe(false);
    });
  });
}
