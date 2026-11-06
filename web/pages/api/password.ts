import { hashPassword, verifyPassword } from '@/lib/auth';
import { requireSession } from '@/lib/requireSession';
import type { NextApiRequest, NextApiResponse } from 'next';
import { recordMetric } from '@/lib/metrics';
import { findFirstUserOrThrow } from 'models/user';
import { changePassword } from 'models/credentials';
import { logger } from '@/lib/logger';
import { validateWithSchema, updatePasswordSchema } from '@/lib/zod';
import { withRateLimit } from '@/lib/rateLimit';
import { ApiError } from '@/lib/errors';

async function handler(req: NextApiRequest, res: NextApiResponse) {
  const { method } = req;

  try {
    switch (method) {
      case 'PUT':
        await handlePUT(req, res);
        break;
      default:
        res.setHeader('Allow', 'PUT');
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

const handlePUT = async (req: NextApiRequest, res: NextApiResponse) => {
  const session = await requireSession(req, res);

  const { currentPassword, newPassword } = validateWithSchema(
    updatePasswordSchema,
    req.body
  );

  const user = await findFirstUserOrThrow({
    where: { id: session.user.id },
  });

  if (!(await verifyPassword(currentPassword, user.password as string))) {
    throw new ApiError(400, 'Your current password is incorrect');
  }

  // One transaction: the new password and the revocation land together, so a
  // failure can never leave "password changed, stolen session still valid".
  const { revokedSessions, revokedResetTokens } = await changePassword({
    userId: session.user.id,
    hashedPassword: await hashPassword(newPassword),
    keepSessionId: session.sessionId,
    // The version read alongside the current password we just verified. If a
    // reset lands in between, this request loses rather than overwriting it.
    expectedCredentialVersion: user.credentialVersion,
  });

  logger.info('control_plane.sessions_revoked', {
    reason: 'password_changed',
    actor_user_id: session.user.id,
    revoked_count: revokedSessions,
    revoked_reset_tokens: revokedResetTokens,
  });

  recordMetric('user.password.updated');

  res.status(200).json({ data: {} });
};

export default withRateLimit('mutation', handler);
