import { createMocks } from 'node-mocks-http';
import type { NextApiRequest, NextApiResponse } from 'next';

import { resetRateLimit } from '@/lib/rateLimit';

jest.mock('../../lib/session', () => ({ getSession: jest.fn() }));
jest.mock('../../models/user', () => ({
  updateUser: jest.fn().mockResolvedValue({}),
  getUser: jest.fn(),
  findFirstUserOrThrow: jest.fn(),
}));
jest.mock('../../models/credentials', () => ({ changeEmail: jest.fn() }));

import usersHandler from '../../pages/api/users';
import { getSession } from '../../lib/session';
import { updateUser } from '../../models/user';
import { changeEmail } from '../../models/credentials';

const signedInAs = (id: string) =>
  (getSession as jest.Mock).mockResolvedValue({
    sessionId: `sess_${id}`,
    user: { id, email: `${id}@example.test` },
  });

const put = async (body: unknown) => {
  const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
    method: 'PUT',
    body: body as Record<string, unknown>,
  });
  await usersHandler(req, res);
  return res;
};

/**
 * `User.locale` is control-plane user data (#252): only the account itself
 * can change it, through the session it is signed in with. The handler takes
 * no user id, so another account's id in the body changes nothing.
 */
describe('PUT /api/users — interface language', () => {
  beforeEach(() => {
    resetRateLimit();
    jest.clearAllMocks();
  });

  it('saves the signed-in account’s own preference', async () => {
    signedInAs('user_a');
    const res = await put({ locale: 'zh-CN' });

    expect(res.statusCode).toBe(204);
    expect(updateUser).toHaveBeenCalledTimes(1);
    expect(updateUser).toHaveBeenCalledWith({
      where: { id: 'user_a' },
      data: { locale: 'zh-CN' },
    });
  });

  it('clears the preference with null (follow the browser)', async () => {
    signedInAs('user_a');
    const res = await put({ locale: null });

    expect(res.statusCode).toBe(204);
    expect(updateUser).toHaveBeenCalledWith({
      where: { id: 'user_a' },
      data: { locale: null },
    });
  });

  it.each([
    [{ locale: 'zh-CN', id: 'user_b' }],
    [{ locale: 'zh-CN', userId: 'user_b' }],
    [{ locale: 'zh-CN', where: { id: 'user_b' } }],
  ])('cannot change another account’s preference (%j)', async (body) => {
    signedInAs('user_a');
    const res = await put(body);

    expect(res.statusCode).toBe(204);
    expect(updateUser).toHaveBeenCalledTimes(1);
    const [{ where, data }] = (updateUser as jest.Mock).mock.calls[0];
    expect(where).toEqual({ id: 'user_a' });
    // Nothing but the preference is written: no smuggled id or filter.
    expect(data).toEqual({ locale: 'zh-CN' });
  });

  it('answers 401 without a session and writes nothing', async () => {
    (getSession as jest.Mock).mockResolvedValue(null);
    const res = await put({ locale: 'zh-CN' });

    expect(res.statusCode).toBe(401);
    expect(updateUser).not.toHaveBeenCalled();
  });

  it.each([['fr'], ['zh'], ['ZH-CN'], [''], [42]])(
    'refuses the unsupported language %p',
    async (locale) => {
      signedInAs('user_a');
      const res = await put({ locale });

      expect(res.statusCode).toBe(422);
      expect(updateUser).not.toHaveBeenCalled();
    }
  );

  it('is not a way into the email-change path', async () => {
    signedInAs('user_a');
    await put({ locale: 'en' });
    expect(changeEmail).not.toHaveBeenCalled();
  });
});
