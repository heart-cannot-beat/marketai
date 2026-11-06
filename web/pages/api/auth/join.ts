import { hashPassword } from '@/lib/auth';
import { sendVerificationEmail } from '@/lib/email/sendVerificationEmail';
import { isEmailAllowed } from '@/lib/email/utils';
import env from '@/lib/env';
import { ApiError } from '@/lib/errors';
import { getUser } from 'models/user';
import type { NextApiRequest, NextApiResponse } from 'next';
import { recordMetric } from '@/lib/metrics';
import {
  getInvitation,
  isInvitationExpired,
  normalizeEmail,
  acceptInvitationForNewUser,
} from 'models/invitation';
import { logger } from '@/lib/logger';
import { withRateLimit } from '@/lib/rateLimit';
import { createVerificationToken } from 'models/verificationToken';
import { userJoinSchema, validateWithSchema } from '@/lib/zod';

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

export default withRateLimit('auth', handler);

/**
 * Account creation, invitation-only.
 *
 * Upstream accepted a request with no invitation token and created both a User
 * and a Team owned by that user. Both halves of that path are closed here:
 *
 *  - a request without a consumable invitation is rejected with 403, and
 *  - no branch of this handler creates a Workspace under any circumstance.
 */
const handlePOST = async (req: NextApiRequest, res: NextApiResponse) => {
  const { name, password, inviteToken } = req.body ?? {};

  if (env.registration.publicSignupEnabled) {
    throw new ApiError(
      500,
      'Public signup is not supported by this deployment.'
    );
  }

  if (!inviteToken) {
    logger.warn('control_plane.signup_rejected_no_invitation');
    throw new ApiError(
      403,
      'Sign-up is invitation-only. Ask a Workspace Owner for an invitation.'
    );
  }

  const invitation = await getInvitation({ token: inviteToken }).catch(() => {
    throw new ApiError(403, 'This invitation is not valid.');
  });

  if (await isInvitationExpired(invitation.expires)) {
    throw new ApiError(400, 'Invitation expired. Please request a new one.');
  }

  // The address is the invitation's, never the request body's.
  const email = normalizeEmail(invitation.email);

  validateWithSchema(userJoinSchema, {
    name,
    email,
    password,
    inviteToken,
  });

  if (!isEmailAllowed(email)) {
    throw new ApiError(
      400,
      `We currently only accept work email addresses for sign-up. Please use your work email to create an account. If you don't have a work email, feel free to contact MarketAI support for assistance.`
    );
  }

  if (await getUser({ email })) {
    throw new ApiError(400, 'An user with this email already exists.');
  }

  const { user, membership } = await acceptInvitationForNewUser({
    token: inviteToken,
    name,
    password: await hashPassword(password),
    emailVerified: new Date(),
  });

  // Send account verification email
  if (env.confirmEmail && !user.emailVerified) {
    const verificationToken = await createVerificationToken({
      identifier: user.email,
      expires: new Date(Date.now() + 24 * 60 * 60 * 1000),
    });

    await sendVerificationEmail({ user, verificationToken });
  }

  recordMetric('user.signup');

  logger.info('control_plane.signup_accepted', {
    actor_user_id: user.id,
    workspace_id: membership.teamId,
    via: 'invitation',
  });

  res.status(201).json({
    data: {
      confirmEmail: env.confirmEmail && !user.emailVerified,
      workspaceSlug: membership.teamSlug,
    },
  });
};
