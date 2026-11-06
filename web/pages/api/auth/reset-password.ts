import { hashPassword } from '@/lib/auth';
import type { NextApiRequest, NextApiResponse } from 'next';
import { recordMetric } from '@/lib/metrics';
import { redeemPasswordReset } from 'models/credentials';
import { logger } from '@/lib/logger';
import { resetPasswordSchema, validateWithSchema } from '@/lib/zod';
import { withRateLimit } from '@/lib/rateLimit';
import { ApiError } from '@/lib/errors';

async function handler(req: NextApiRequest, res: NextApiResponse) {
  const { method } = req;

  try {
    switch (method) {
      case 'POST':
        await handlePOST(req, res);
        break;
      default:
        res.setHeader('Allow', 'POST');
        res.status(405).json({
          error: { message: `Method ${method} Not Allowed` },
        });
    }
  } catch (error: any) {
    const message = error.message || 'Something went wrong';
    const status = error.status || 500;

    res.status(status).json({ error: { message } });
  }
}

const handlePOST = async (req: NextApiRequest, res: NextApiResponse) => {
  const { token, password } = validateWithSchema(resetPasswordSchema, req.body);

  if (!token) {
    throw new ApiError(422, 'Password reset token is required');
  }

  /**
   * One transaction consumes the reset token, sets the new password, unlocks
   * the account and revokes every session the account holds.
   *
   * Upstream ran these as four independent statements, which left two holes: a
   * failure between them could change the password while stolen sessions stayed
   * valid, and two concurrent requests could both redeem the same token.
   */
  const { userId, revokedSessions, revokedResetTokens } =
    await redeemPasswordReset({
      token,
      hashedPassword: await hashPassword(password),
    });

  logger.info('control_plane.sessions_revoked', {
    reason: 'password_reset',
    actor_user_id: userId,
    revoked_count: revokedSessions,
    revoked_reset_tokens: revokedResetTokens,
  });

  recordMetric('user.password.reset');

  res.status(200).json({ message: 'Password reset successfully' });
};

export default withRateLimit('auth', handler);
