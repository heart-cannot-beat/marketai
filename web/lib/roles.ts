import { Role } from '@prisma/client';

/**
 * The customer-facing role contract for the invite-only Beta.
 *
 * Upstream ships OWNER, ADMIN and MEMBER. MarketAI exposes Owner and Member
 * only; the reduction is enforced in three places so that no single omission
 * re-opens it:
 *
 *  1. the Prisma enum (see prisma/schema.prisma and the baseline migration),
 *  2. `customerRoles` below, used by every request schema and by the UI, and
 *  3. `assertCustomerRole`, called by the membership and invitation handlers.
 */
export const customerRoles = [Role.OWNER, Role.MEMBER] as const;

export type CustomerRole = (typeof customerRoles)[number];

export const availableRoles: { id: CustomerRole; name: string }[] = [
  {
    id: Role.MEMBER,
    name: 'Member',
  },
  {
    id: Role.OWNER,
    name: 'Owner',
  },
];

export const isCustomerRole = (value: unknown): value is CustomerRole =>
  typeof value === 'string' &&
  (customerRoles as readonly string[]).includes(value);
