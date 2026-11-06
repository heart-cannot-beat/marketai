// This file has no static imports; `export {}` keeps it a module so its
// top-level names do not collide with the other integration suites.
export {};

/**
 * `User.locale` (#252) is control-plane user data: an account changes its
 * own interface language and nobody else's.
 *
 * Runs against real PostgreSQL with the runtime role, after the reviewed
 * migrations; skips when no control-plane connection string is configured.
 */
const controlUrl = process.env.CONTROL_PLANE_DATABASE_URL;
const configured = Boolean(controlUrl);

const describeIfConfigured = configured ? describe : describe.skip;

jest.mock('../../lib/session', () => ({ getSession: jest.fn() }));

describeIfConfigured('User.locale (PostgreSQL)', () => {
  jest.setTimeout(30_000);

  /* eslint-disable @typescript-eslint/no-require-imports */
  let prisma: any;
  let getAuthOptions: any;
  let usersHandler: any;
  let getSession: jest.Mock;
  let createMocks: any;
  let randomUUID: () => string;
  let resetRateLimit: () => void;
  let alice: { id: string; email: string; credentialVersion: number };
  let bob: { id: string; email: string };

  const putAs = async (user: { id: string; email: string }, body: unknown) => {
    getSession.mockResolvedValue({
      sessionId: `sess_${user.id}`,
      user: { id: user.id, email: user.email },
    });
    const { req, res } = createMocks({ method: 'PUT', body });
    await usersHandler(req, res);
    return res;
  };

  const localeOf = async (id: string) =>
    (await prisma.user.findUnique({ where: { id }, select: { locale: true } }))
      .locale;

  beforeAll(async () => {
    process.env.DATABASE_URL = controlUrl as string;

    ({ randomUUID } = require('crypto'));
    ({ createMocks } = require('node-mocks-http'));
    ({ prisma } = require('../../lib/prisma'));
    ({ getAuthOptions } = require('../../lib/nextAuth'));
    ({ resetRateLimit } = require('../../lib/rateLimit'));
    ({ getSession } = require('../../lib/session'));
    usersHandler = require('../../pages/api/users').default;

    const suffix = randomUUID().slice(0, 8);
    alice = await prisma.user.create({
      data: { name: 'Alice', email: `locale-alice-${suffix}@example.test` },
    });
    bob = await prisma.user.create({
      data: { name: 'Bob', email: `locale-bob-${suffix}@example.test` },
    });
  });

  beforeEach(() => resetRateLimit());

  afterAll(async () => {
    if (!prisma) return;
    const ids = [alice?.id, bob?.id].filter(Boolean);
    await prisma.session.deleteMany({ where: { userId: { in: ids } } });
    await prisma.user.deleteMany({ where: { id: { in: ids } } });
    await prisma.$disconnect();
  });

  it('starts empty for existing accounts (follow the browser)', async () => {
    expect(await localeOf(alice.id)).toBeNull();
    expect(await localeOf(bob.id)).toBeNull();
  });

  it('changes only the signed-in account, whatever id the body names', async () => {
    const res = await putAs(alice, {
      locale: 'zh-CN',
      id: bob.id,
      userId: bob.id,
    });

    expect(res.statusCode).toBe(204);
    expect(await localeOf(alice.id)).toBe('zh-CN');
    expect(await localeOf(bob.id)).toBeNull();
  });

  it('clears back to null', async () => {
    await putAs(bob, { locale: 'en' });
    expect(await localeOf(bob.id)).toBe('en');

    await putAs(bob, { locale: null });
    expect(await localeOf(bob.id)).toBeNull();
    expect(await localeOf(alice.id)).toBe('zh-CN');
  });

  it('refuses a language the column does not allow, even from a direct write', async () => {
    await expect(
      prisma.user.update({ where: { id: bob.id }, data: { locale: 'fr' } })
    ).rejects.toThrow();
    expect(await localeOf(bob.id)).toBeNull();
  });

  it('hands the saved preference to the session the middleware reads', async () => {
    const callbacks = getAuthOptions().callbacks;
    const token = await callbacks.jwt({
      token: {},
      user: { id: alice.id, credentialVersion: alice.credentialVersion },
      trigger: 'signIn',
    });
    const session = await callbacks.session({
      session: {
        user: { name: 'Alice', email: alice.email },
        expires: new Date(Date.now() + 1000).toISOString(),
      },
      token,
    });

    expect(session.user.id).toBe(alice.id);
    expect(session.user.locale).toBe('zh-CN');
  });
});
