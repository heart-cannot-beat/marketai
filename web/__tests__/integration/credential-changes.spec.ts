// This file has no static imports; `export {}` keeps it a module so its
// top-level names do not collide with the other integration suites.
export {};

/**
 * Password-security state changes are atomic, and session identity is never
 * stale. Runs against real PostgreSQL; skips when unconfigured.
 */
const controlUrl = process.env.CONTROL_PLANE_DATABASE_URL;
const configured = Boolean(controlUrl);

const describeIfConfigured = configured ? describe : describe.skip;

describeIfConfigured('Credential changes (PostgreSQL)', () => {
  jest.setTimeout(30_000);

  /* eslint-disable @typescript-eslint/no-require-imports */
  let prisma: any;
  let credentials: any;
  let sessions: any;
  let getAuthOptions: any;
  let randomUUID: () => string;

  const created: string[] = [];

  const newUser = async () => {
    const user = await prisma.user.create({
      data: {
        name: 'Credential',
        email: `credential-${randomUUID().slice(0, 8)}@example.test`,
        password: 'old-hash',
      },
    });
    created.push(user.id);
    return user;
  };

  const newSession = async (userId: string, expires?: Date) =>
    prisma.session.create({
      data: {
        sessionToken: randomUUID(),
        userId,
        expires: expires ?? new Date(Date.now() + 60 * 60 * 1000),
      },
    });

  beforeAll(async () => {
    process.env.DATABASE_URL = controlUrl as string;

    ({ randomUUID } = require('crypto'));
    ({ prisma } = require('../../lib/prisma'));
    credentials = require('../../models/credentials');
    sessions = require('../../models/session');
    ({ getAuthOptions } = require('../../lib/nextAuth'));
  });

  afterAll(async () => {
    if (!prisma) return;
    await prisma.passwordReset.deleteMany({
      where: { userId: { in: created } },
    });
    await prisma.session.deleteMany({ where: { userId: { in: created } } });
    await prisma.user.deleteMany({ where: { id: { in: created } } });
    await prisma.$disconnect();
  });

  describe('password change', () => {
    it('sets the password and revokes other sessions in one transaction', async () => {
      const user = await newUser();
      const keep = await newSession(user.id);
      await newSession(user.id);
      await newSession(user.id);

      const { revokedSessions } = await credentials.changePassword({
        userId: user.id,
        hashedPassword: 'new-hash',
        keepSessionId: keep.id,
        expectedCredentialVersion: user.credentialVersion,
      });

      expect(revokedSessions).toBe(2);

      const remaining = await prisma.session.findMany({
        where: { userId: user.id },
      });

      expect(remaining.map((s: any) => s.id)).toEqual([keep.id]);
      expect(
        (await prisma.user.findUnique({ where: { id: user.id } })).password
      ).toBe('new-hash');
    });

    it('leaves the old password in place when the transaction cannot finish', async () => {
      const user = await newUser();
      const keep = await newSession(user.id);

      await expect(
        credentials.changePassword({
          userId: 'no-such-user',
          hashedPassword: 'new-hash',
          keepSessionId: keep.id,
          expectedCredentialVersion: 0,
        })
      ).rejects.toThrow();

      // Nothing was half-applied for the real user.
      expect(
        (await prisma.user.findUnique({ where: { id: user.id } })).password
      ).toBe('old-hash');
      expect(await prisma.session.count({ where: { userId: user.id } })).toBe(
        1
      );
    });
  });

  describe('password reset', () => {
    const newReset = async (
      user: { id: string; email: string; credentialVersion: number },
      expiresAt?: Date
    ) =>
      prisma.passwordReset.create({
        data: {
          userId: user.id,
          email: user.email,
          token: randomUUID(),
          credentialVersionAtIssue: user.credentialVersion,
          expiresAt: expiresAt ?? new Date(Date.now() + 60 * 60 * 1000),
        },
      });

    it('consumes the token, sets the password and revokes every session', async () => {
      const user = await newUser();
      await newSession(user.id);
      await newSession(user.id);
      const reset = await newReset(user);

      const result = await credentials.redeemPasswordReset({
        token: reset.token,
        hashedPassword: 'reset-hash',
      });

      expect(result.userId).toBe(user.id);
      expect(result.revokedSessions).toBe(2);
      expect(await prisma.session.count({ where: { userId: user.id } })).toBe(
        0
      );
      expect(
        await prisma.passwordReset.count({ where: { token: reset.token } })
      ).toBe(0);

      const updated = await prisma.user.findUnique({ where: { id: user.id } });
      expect(updated.password).toBe('reset-hash');
      expect(updated.invalid_login_attempts).toBe(0);
      expect(updated.lockedAt).toBeNull();
    });

    it('admits exactly one of two concurrent redemptions', async () => {
      const user = await newUser();
      const reset = await newReset(user);

      const attempts = await Promise.allSettled(
        Array.from({ length: 4 }, () =>
          credentials.redeemPasswordReset({
            token: reset.token,
            hashedPassword: 'concurrent-hash',
          })
        )
      );

      expect(attempts.filter((a) => a.status === 'fulfilled')).toHaveLength(1);
    });

    it('invalidates every sibling token, so an older link cannot replay', async () => {
      const user = await newUser();

      // Two live tokens for one account — upstream allowed this, and both
      // could be redeemed, the older one overwriting the newer password.
      const first = await newReset(user);
      const second = await newReset(user);

      await credentials.redeemPasswordReset({
        token: second.token,
        hashedPassword: 'second-hash',
      });

      await expect(
        credentials.redeemPasswordReset({
          token: first.token,
          hashedPassword: 'first-hash',
        })
      ).rejects.toThrow(/no longer valid|Invalid password reset token/i);

      expect(
        (await prisma.user.findUnique({ where: { id: user.id } })).password
      ).toBe('second-hash');
      expect(
        await prisma.passwordReset.count({ where: { userId: user.id } })
      ).toBe(0);
    });

    it('is refused when the credential version moved on since issue', async () => {
      const user = await newUser();
      const stale = await newReset(user);
      const keep = await newSession(user.id);

      // A plain password change bumps the version and destroys outstanding
      // links, so the emailed one is dead on arrival.
      const { revokedResetTokens } = await credentials.changePassword({
        userId: user.id,
        hashedPassword: 'changed-hash',
        keepSessionId: keep.id,
        expectedCredentialVersion: user.credentialVersion,
      });

      expect(revokedResetTokens).toBe(1);

      await expect(
        credentials.redeemPasswordReset({
          token: stale.token,
          hashedPassword: 'stale-hash',
        })
      ).rejects.toThrow(/Invalid password reset token/i);

      expect(
        (await prisma.user.findUnique({ where: { id: user.id } })).password
      ).toBe('changed-hash');
    });

    it('does not consume an expired token', async () => {
      const user = await newUser();
      const reset = await newReset(user, new Date(Date.now() - 1000));

      await expect(
        credentials.redeemPasswordReset({
          token: reset.token,
          hashedPassword: 'nope',
        })
      ).rejects.toThrow(/expired/i);

      // Rolled back, and the password is untouched.
      expect(
        await prisma.passwordReset.count({ where: { token: reset.token } })
      ).toBe(1);
      expect(
        (await prisma.user.findUnique({ where: { id: user.id } })).password
      ).toBe('old-hash');

      await prisma.passwordReset.deleteMany({ where: { token: reset.token } });
    });
  });

  describe('email change', () => {
    const newReset = async (user: {
      id: string;
      email: string;
      credentialVersion: number;
    }) =>
      prisma.passwordReset.create({
        data: {
          userId: user.id,
          email: user.email,
          token: randomUUID(),
          credentialVersionAtIssue: user.credentialVersion,
          expiresAt: new Date(Date.now() + 60 * 60 * 1000),
        },
      });

    it('invalidates a reset link already delivered to the previous address', async () => {
      const user = await newUser();

      // A reset link is sent to the address the account currently uses...
      const oldAddressToken = await newReset(user);

      // ...and the account then moves to a new address. Tokens are bound to the
      // account, so without bumping the credential version the old link would
      // still take it over.
      await credentials.changeEmail({
        userId: user.id,
        email: `moved-${randomUUID().slice(0, 8)}@example.test`,
        expectedCredentialVersion: user.credentialVersion,
      });

      await expect(
        credentials.redeemPasswordReset({
          token: oldAddressToken.token,
          hashedPassword: 'taken-over',
        })
      ).rejects.toThrow(/Invalid password reset token/i);

      expect(
        (await prisma.user.findUnique({ where: { id: user.id } })).password
      ).toBe('old-hash');
    });

    it('bumps the credential version and destroys outstanding tokens', async () => {
      const user = await newUser();
      await newReset(user);
      await newReset(user);

      const { revokedResetTokens } = await credentials.changeEmail({
        userId: user.id,
        email: `moved-${randomUUID().slice(0, 8)}@example.test`,
        expectedCredentialVersion: user.credentialVersion,
      });

      expect(revokedResetTokens).toBe(2);

      const updated = await prisma.user.findUnique({ where: { id: user.id } });
      expect(updated.credentialVersion).toBe(user.credentialVersion + 1);
      expect(
        await prisma.passwordReset.count({ where: { userId: user.id } })
      ).toBe(0);
    });

    it('refuses a sign-in that verified credentials from before the change', async () => {
      const user = await newUser();
      const versionAtSignIn = user.credentialVersion;

      await credentials.changeEmail({
        userId: user.id,
        email: `moved-${randomUUID().slice(0, 8)}@example.test`,
        expectedCredentialVersion: versionAtSignIn,
      });

      await expect(
        sessions.createAuthSession(user.id, 3600, versionAtSignIn)
      ).rejects.toThrow(/credentials-changed/);
    });

    it('lets exactly one of two concurrent email changes win', async () => {
      const user = await newUser();

      const attempts = await Promise.allSettled([
        credentials.changeEmail({
          userId: user.id,
          email: `first-${randomUUID().slice(0, 8)}@example.test`,
          expectedCredentialVersion: user.credentialVersion,
        }),
        credentials.changeEmail({
          userId: user.id,
          email: `second-${randomUUID().slice(0, 8)}@example.test`,
          expectedCredentialVersion: user.credentialVersion,
        }),
      ]);

      expect(attempts.filter((a) => a.status === 'fulfilled')).toHaveLength(1);
    });
  });

  describe('lock ordering', () => {
    const newReset = async (user: {
      id: string;
      email: string;
      credentialVersion: number;
    }) =>
      prisma.passwordReset.create({
        data: {
          userId: user.id,
          email: user.email,
          token: randomUUID(),
          credentialVersionAtIssue: user.credentialVersion,
          expiresAt: new Date(Date.now() + 60 * 60 * 1000),
        },
      });

    /**
     * Consuming the token before locking the user gave two redemptions of
     * *different* tokens opposite lock orders, and PostgreSQL reported
     * 40P01 deadlock detected (Prisma P2010, a 500 to the caller).
     */
    it('does not deadlock when two different reset tokens are redeemed at once', async () => {
      const user = await newUser();
      const first = await newReset(user);
      const second = await newReset(user);

      const attempts = await Promise.allSettled([
        credentials.redeemPasswordReset({
          token: first.token,
          hashedPassword: 'first-hash',
        }),
        credentials.redeemPasswordReset({
          token: second.token,
          hashedPassword: 'second-hash',
        }),
      ]);

      const reasons = attempts
        .filter((a) => a.status === 'rejected')
        .map((a) => String((a as PromiseRejectedResult).reason?.message ?? ''));

      for (const reason of reasons) {
        expect(reason).not.toMatch(/deadlock/i);
        expect(reason).not.toMatch(/P2010/);
      }

      expect(attempts.filter((a) => a.status === 'fulfilled')).toHaveLength(1);
    });

    it('does not deadlock when a change and a reset collide', async () => {
      const user = await newUser();
      const keep = await newSession(user.id);
      const reset = await newReset(user);

      const attempts = await Promise.allSettled([
        credentials.changePassword({
          userId: user.id,
          hashedPassword: 'changed-hash',
          keepSessionId: keep.id,
          expectedCredentialVersion: user.credentialVersion,
        }),
        credentials.redeemPasswordReset({
          token: reset.token,
          hashedPassword: 'reset-hash',
        }),
      ]);

      const reasons = attempts
        .filter((a) => a.status === 'rejected')
        .map((a) => String((a as PromiseRejectedResult).reason?.message ?? ''));

      for (const reason of reasons) {
        expect(reason).not.toMatch(/deadlock/i);
        expect(reason).not.toMatch(/P2010/);
      }

      expect(attempts.filter((a) => a.status === 'fulfilled')).toHaveLength(1);
    });
  });

  describe('races between sign-in and a credential change', () => {
    const mintSession = (userId: string, version: number) =>
      sessions.createAuthSession(userId, 3600, version);

    const newReset = async (user: {
      id: string;
      email: string;
      credentialVersion: number;
    }) =>
      prisma.passwordReset.create({
        data: {
          userId: user.id,
          email: user.email,
          token: randomUUID(),
          credentialVersionAtIssue: user.credentialVersion,
          expiresAt: new Date(Date.now() + 60 * 60 * 1000),
        },
      });

    it('refuses to mint a session for credentials a reset already withdrew', async () => {
      const user = await newUser();

      // Interleaving: authorize() verified the old password and captured the
      // version it saw...
      const versionAtSignIn = user.credentialVersion;

      // ...a password reset lands, revoking every session...
      const reset = await newReset(user);
      await credentials.redeemPasswordReset({
        token: reset.token,
        hashedPassword: 'reset-hash',
      });

      // ...and only then does the sign-in try to mint its session.
      await expect(mintSession(user.id, versionAtSignIn)).rejects.toThrow(
        /credentials-changed/
      );

      expect(await prisma.session.count({ where: { userId: user.id } })).toBe(
        0
      );
    });

    it('mints a session when nothing changed underneath it', async () => {
      const user = await newUser();

      const row = await mintSession(user.id, user.credentialVersion);

      expect(row.userId).toBe(user.id);
      expect(await prisma.session.count({ where: { userId: user.id } })).toBe(
        1
      );
    });

    it('refuses a password change that a reset beat to the row', async () => {
      const user = await newUser();
      const keep = await newSession(user.id);

      // The change-password request verified the current password here.
      const versionAtVerification = user.credentialVersion;

      const reset = await newReset(user);
      await credentials.redeemPasswordReset({
        token: reset.token,
        hashedPassword: 'reset-hash',
      });

      await expect(
        credentials.changePassword({
          userId: user.id,
          hashedPassword: 'change-hash',
          keepSessionId: keep.id,
          expectedCredentialVersion: versionAtVerification,
        })
      ).rejects.toThrow(/changed elsewhere/i);

      // The reset's password survives; the stale change did not overwrite it.
      expect(
        (await prisma.user.findUnique({ where: { id: user.id } })).password
      ).toBe('reset-hash');
    });

    it('lets exactly one of two concurrent credential changes win', async () => {
      const user = await newUser();
      const keep = await newSession(user.id);

      const attempts = await Promise.allSettled([
        credentials.changePassword({
          userId: user.id,
          hashedPassword: 'first-hash',
          keepSessionId: keep.id,
          expectedCredentialVersion: user.credentialVersion,
        }),
        credentials.changePassword({
          userId: user.id,
          hashedPassword: 'second-hash',
          keepSessionId: keep.id,
          expectedCredentialVersion: user.credentialVersion,
        }),
      ]);

      expect(attempts.filter((a) => a.status === 'fulfilled')).toHaveLength(1);

      const updated = await prisma.user.findUnique({ where: { id: user.id } });

      expect(updated.credentialVersion).toBe(user.credentialVersion + 1);
      expect(['first-hash', 'second-hash']).toContain(updated.password);
    });

    it('bumps the credential version on every credential change', async () => {
      const user = await newUser();
      const keep = await newSession(user.id);

      await credentials.changePassword({
        userId: user.id,
        hashedPassword: 'v1',
        keepSessionId: keep.id,
        expectedCredentialVersion: user.credentialVersion,
      });

      const afterChange = await prisma.user.findUnique({
        where: { id: user.id },
      });
      expect(afterChange.credentialVersion).toBe(user.credentialVersion + 1);

      // Issued against the *current* credential state, as forgot-password does.
      const reset = await newReset(afterChange);
      await credentials.redeemPasswordReset({
        token: reset.token,
        hashedPassword: 'v2',
      });

      const afterReset = await prisma.user.findUnique({
        where: { id: user.id },
      });
      expect(afterReset.credentialVersion).toBe(user.credentialVersion + 2);
    });
  });

  describe('session identity', () => {
    const readSession = async (token: Record<string, unknown>) =>
      getAuthOptions().callbacks.session({
        session: {
          user: { name: 'stale name', email: 'stale@example.test' },
          expires: new Date().toISOString(),
        },
        token,
      });

    it('reports the current email, not the one the token was minted with', async () => {
      const user = await newUser();
      const row = await newSession(user.id);
      const token = { sub: user.id, sid: row.id };

      expect((await readSession(token)).user.email).toBe(user.email);

      const changed = `changed-${randomUUID().slice(0, 8)}@example.test`;
      await prisma.user.update({
        where: { id: user.id },
        data: { email: changed, name: 'Changed Name' },
      });

      // Same token, refreshed identity — invitation acceptance keys off this.
      const session = await readSession(token);
      expect(session.user.email).toBe(changed);
      expect(session.user.name).toBe('Changed Name');
    });

    it('deletes an expired row instead of leaving it to accumulate', async () => {
      const user = await newUser();
      const row = await newSession(user.id, new Date(Date.now() - 1000));

      expect(await sessions.findActiveAuthSession(row.id, user.id)).toBeNull();

      expect(await prisma.session.count({ where: { id: row.id } })).toBe(0);
    });

    it('prunes aged-out rows at sign-in', async () => {
      const user = await newUser();
      await newSession(user.id, new Date(Date.now() - 1000));
      await newSession(user.id, new Date(Date.now() - 2000));
      const live = await newSession(user.id);

      const token = await getAuthOptions().callbacks.jwt({
        token: {},
        user: { id: user.id, credentialVersion: user.credentialVersion },
        trigger: 'signIn',
      });

      const rows = await prisma.session.findMany({
        where: { userId: user.id },
      });

      expect(rows.map((r: any) => r.id).sort()).toEqual(
        [live.id, token.sid].sort()
      );
    });
  });
});

if (!configured) {
  describe('Credential changes (PostgreSQL)', () => {
    it('is skipped without CONTROL_PLANE_DATABASE_URL', () => {
      expect(configured).toBe(false);
    });
  });
}
