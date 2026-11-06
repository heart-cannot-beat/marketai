import { requireSession } from '@/lib/requireSession';
import { changeEmail } from 'models/credentials';
import { logger } from '@/lib/logger';
import type { NextApiRequest, NextApiResponse } from 'next';
import { recordMetric } from '@/lib/metrics';
import { ApiError } from '@/lib/errors';
import env from '@/lib/env';
import { findFirstUserOrThrow, getUser, updateUser } from 'models/user';
import { isEmailAllowed } from '@/lib/email/utils';
import { updateAccountSchema, validateWithSchema } from '@/lib/zod';
import { withRateLimit } from '@/lib/rateLimit';

async function handler(req: NextApiRequest, res: NextApiResponse) {
  try {
    switch (req.method) {
      case 'PUT':
        await handlePUT(req, res);
        break;
      default:
        res.setHeader('Allow', 'PUT');
        res.status(405).json({
          error: { message: `Method ${req.method} Not Allowed` },
        });
    }
  } catch (error: any) {
    const message = error.message || 'Something went wrong';
    const status = error.status || 500;

    res.status(status).json({ error: { message } });
  }
}

const handlePUT = async (req: NextApiRequest, res: NextApiResponse) => {
  const data = validateWithSchema(updateAccountSchema, req.body);

  const session = await requireSession(req, res);

  if ('email' in data) {
    const allowEmailChange = env.confirmEmail === false;

    if (!allowEmailChange) {
      throw new ApiError(400, 'Email change is not allowed.');
    }

    if (!isEmailAllowed(data.email)) {
      throw new ApiError(400, 'Please use your work email.');
    }

    const existing = await getUser({ email: data.email });

    if (existing && existing.id !== session.user.id) {
      throw new ApiError(400, 'Email already in use.');
    }

    const current = await findFirstUserOrThrow({
      where: { id: session.user.id },
    });

    // Moving to a new address is a credential change: reset links are bound to
    // the account, so one already delivered to the old address would otherwise
    // still take it over.
    const { revokedResetTokens } = await changeEmail({
      userId: session.user.id,
      email: data.email,
      expectedCredentialVersion: current.credentialVersion,
    });

    logger.info('control_plane.email_changed', {
      actor_user_id: session.user.id,
      revoked_reset_tokens: revokedResetTokens,
    });

    recordMetric('user.updated');

    return res.status(204).end();
  }

  await updateUser({
    where: { id: session.user.id },
    data,
  });

  recordMetric('user.updated');

  res.status(204).end();
};

export default withRateLimit('mutation', handler);
