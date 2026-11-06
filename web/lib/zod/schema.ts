import { z } from 'zod';
import { slugify } from '../server-common';
import {
  teamName,
  slug,
  domain,
  email,
  password,
  token,
  role,
  expiredToken,
  sessionId,
  memberId,
  inviteToken,
  invitationId,
  name,
  image,
  locale,
} from './primitives';

export const teamSlugSchema = z.object({
  slug,
});

export const updateTeamSchema = z.object({
  name: teamName,
  slug: slug.transform((slug) => slugify(slug)),
  domain,
});

export const updateAccountSchema = z.union([
  z.object({
    email,
  }),
  z.object({
    name: name(),
  }),
  z.object({
    image,
  }),
  z.object({
    locale,
  }),
]);

export const updatePasswordSchema = z.object({
  currentPassword: password,
  newPassword: password,
});

/**
 * Account creation is invitation-only, so the upstream `team`/`slug` branch
 * (which created a Workspace as a side effect of signup) is gone.
 */
export const userJoinSchema = z.object({
  name: name(),
  email,
  password,
  inviteToken,
});

export const resetPasswordSchema = z.object({
  password,
  token,
});

/**
 * An invitation names exactly one recipient. Upstream accepted a second shape
 * with no email and an `allowedDomains` list, which produced a shareable link
 * anyone in the domain could redeem.
 */
export const inviteViaEmailSchema = z.object({
  email,
  role,
});

export const resendLinkRequestSchema = z.object({
  email,
  expiredToken,
});

export const deleteSessionSchema = z.object({
  id: sessionId,
});

export const forgotPasswordSchema = z.object({
  email,
});

export const resendEmailToken = z.object({
  email,
});

export const updateMemberSchema = z.object({
  role,
  memberId,
});

export const acceptInvitationSchema = z.object({
  inviteToken,
});

export const getInvitationSchema = z.object({
  token: inviteToken,
});

export const deleteInvitationSchema = z.object({
  id: invitationId,
});

export const deleteMemberSchema = z.object({
  memberId,
});

// email or slug
