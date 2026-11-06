import { createMocks } from 'node-mocks-http';
import type { NextApiRequest, NextApiResponse } from 'next';

import { resetRateLimit } from '@/lib/rateLimit';
import { hashPassword } from '@/lib/auth';

jest.mock('../../lib/session', () => ({ getSession: jest.fn() }));
jest.mock('../../models/user', () => ({ findFirstUserOrThrow: jest.fn() }));
jest.mock('../../models/credentials', () => ({ changePassword: jest.fn() }));

import passwordHandler from '../../pages/api/password';
import { getSession } from '../../lib/session';
import { findFirstUserOrThrow } from '../../models/user';
import { changePassword } from '../../models/credentials';

/**
 * The handler reads `error.status`. Next's internal `ApiError` exposes
 * `statusCode` instead, so throwing it turned a 400 "wrong current password"
 * into a 500.
 */
describe('Password change error contract', () => {
  beforeEach(async () => {
    resetRateLimit();
    jest.clearAllMocks();

    (getSession as jest.Mock).mockResolvedValue({
      sessionId: 'sess_1',
      user: { id: 'user_1', email: 'owner@example.test' },
    });
    (findFirstUserOrThrow as jest.Mock).mockResolvedValue({
      id: 'user_1',
      password: await hashPassword('the-real-password'),
      credentialVersion: 3,
    });
    (changePassword as jest.Mock).mockResolvedValue({ revokedSessions: 0 });
  });

  const request = (body: Record<string, string>) =>
    createMocks<NextApiRequest, NextApiResponse>({ method: 'PUT', body });

  it('answers 400 when the current password is wrong', async () => {
    const { req, res } = request({
      currentPassword: 'not-the-real-password',
      newPassword: 'a-brand-new-password',
    });

    await passwordHandler(req, res);

    expect(res.statusCode).toBe(400);
    expect(res._getJSONData().error.message).toMatch(/current password/i);
    expect(changePassword).not.toHaveBeenCalled();
  });

  it('passes the verified credential version through to the transaction', async () => {
    const { req, res } = request({
      currentPassword: 'the-real-password',
      newPassword: 'a-brand-new-password',
    });

    await passwordHandler(req, res);

    expect(res.statusCode).toBe(200);
    expect((changePassword as jest.Mock).mock.calls[0][0]).toMatchObject({
      userId: 'user_1',
      keepSessionId: 'sess_1',
      expectedCredentialVersion: 3,
    });
  });

  it('surfaces a credential conflict as 409, not 500', async () => {
    const { CredentialConflictError } = jest.requireActual(
      '../../models/credentials'
    );

    (changePassword as jest.Mock).mockRejectedValue(
      new CredentialConflictError('Your password was changed elsewhere.')
    );

    const { req, res } = request({
      currentPassword: 'the-real-password',
      newPassword: 'a-brand-new-password',
    });

    await passwordHandler(req, res);

    expect(res.statusCode).toBe(409);
  });

  it('answers 401 when the session lapsed', async () => {
    (getSession as jest.Mock).mockResolvedValue(null);

    const { req, res } = request({
      currentPassword: 'the-real-password',
      newPassword: 'a-brand-new-password',
    });

    await passwordHandler(req, res);

    expect(res.statusCode).toBe(401);
    expect(findFirstUserOrThrow).not.toHaveBeenCalled();
  });
});
