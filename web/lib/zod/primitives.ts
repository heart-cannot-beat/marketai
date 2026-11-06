import { z } from 'zod';
import { isValidDomain, maxLengthPolicies, passwordPolicies } from '../common';
import { customerRoles } from '../roles';
import { SUPPORTED_LOCALES } from '../i18n/locales';

export const password = z
  .string({
    required_error: 'Password is required',
    invalid_type_error: 'Password must be a string',
  })
  .max(
    maxLengthPolicies.password,
    `Password should have at most ${maxLengthPolicies.password} characters`
  )
  .min(
    passwordPolicies.minLength,
    `Password must have at least ${passwordPolicies.minLength} characters`
  );

export const email = z
  .string({
    required_error: 'Email is required',
    invalid_type_error: 'Email must be a string',
  })
  .email('Enter a valid email address')
  .max(
    maxLengthPolicies.email,
    `Email should have at most ${maxLengthPolicies.email} characters`
  );

export const teamName = z
  .string({
    required_error: 'Workspace name is required',
    invalid_type_error: 'Workspace name must be a string',
  })
  .min(1, 'Workspace name is required')
  .max(
    maxLengthPolicies.team,
    `Workspace name should have at most ${maxLengthPolicies.team} characters`
  );

export const name = (length: number = maxLengthPolicies.name) =>
  z
    .string({
      required_error: 'Name is required',
      invalid_type_error: 'Name must be a string',
    })
    .min(1, 'Name is required')
    .max(length, `Name should have at most ${length} characters`);

export const slug = z
  .string({
    required_error: 'Slug is required',
    invalid_type_error: 'Slug must be a string',
  })
  .min(3, 'Slug must be at least 3 characters')
  .max(
    maxLengthPolicies.slug,
    `Slug should have at most ${maxLengthPolicies.slug} characters`
  );

export const image = z
  .string({
    required_error: 'Avatar is required',
    invalid_type_error: 'Avatar must be a string',
  })
  .url('Enter a valid URL')
  .refine(
    (imageUri) => imageUri.startsWith('data:image/'),
    'Avatar must be an image'
  )
  .refine((imageUri) => {
    const [, base64] = imageUri.split(',');
    if (!base64) {
      return false;
    }
    const size = base64.length * (3 / 4) - 2;
    return size < 2000000;
  }, 'Avatar must be less than 2MB');

export const domain = z
  .string({
    invalid_type_error: 'Domain must be a string',
  })
  .max(
    maxLengthPolicies.domain,
    `Domain should have at most ${maxLengthPolicies.domain} characters`
  )
  .optional()
  .refine(
    (domain) => {
      if (!domain) {
        return true;
      }

      return isValidDomain(domain);
    },
    {
      message: 'Enter a domain name in the format example.com',
    }
  )
  .transform((domain) => {
    if (!domain) {
      return null;
    }

    return domain.trim().toLowerCase();
  });

export const token = z
  .string({
    required_error: 'Token is required',
    invalid_type_error: 'Token must be a string',
  })
  .min(1, 'Token is required');

/**
 * Customer-visible role contract. Rejects any value outside Owner/Member,
 * including the upstream ADMIN literal.
 */
export const role = z.enum(customerRoles, {
  required_error: 'Role is required',
  invalid_type_error: 'Role must be one of OWNER or MEMBER',
});

export const expiredToken = z
  .string({
    required_error: 'Expired token is required',
    invalid_type_error: 'Expired token must be a string',
  })
  .min(1, 'Expired token is required')
  .max(
    maxLengthPolicies.expiredToken,
    `Expired token should have at most ${maxLengthPolicies.expiredToken} characters`
  );

export const sessionId = z
  .string({
    required_error: 'Session id is required',
    invalid_type_error: 'Session id must be a string',
  })
  .min(1, 'Session id is required');

export const invitationId = z
  .string({
    required_error: 'Invitation id is required',
    invalid_type_error: 'Invitation id must be a string',
  })
  .min(1, 'Invitation id is required')
  .max(
    maxLengthPolicies.invitationId,
    `Invitation id should be at most ${maxLengthPolicies.invitationId} characters`
  );

export const inviteToken = z
  .string({
    required_error: 'Invite token is required',
    invalid_type_error: 'Invite token must be a string',
  })
  .min(1, 'Invite token is required')
  .max(
    maxLengthPolicies.inviteToken,
    `Invite token should be at most ${maxLengthPolicies.inviteToken} characters`
  );

export const memberId = z
  .string({
    required_error: 'Member id is required',
    invalid_type_error: 'Member id must be a string',
  })
  .min(1)
  .max(
    maxLengthPolicies.memberId,
    `Member id should be at most ${maxLengthPolicies.memberId} characters`
  );

/** Interface language preference; `null` clears it (follow the browser). */
export const locale = z
  .enum(SUPPORTED_LOCALES, {
    errorMap: () => ({ message: 'Choose a supported language' }),
  })
  .nullable();
