import { Role } from '@prisma/client';
import { customerRoles } from './roles';

type RoleType = (typeof Role)[keyof typeof Role];
export type Action = 'create' | 'update' | 'read' | 'delete' | 'leave';

/**
 * Resource list reduced to what the MarketAI control plane owns. Upstream
 * `team_sso`, `team_dsync`, `team_audit_log`, `team_webhook`, `team_payments`
 * and `team_api_key` were removed together with their routes and components.
 */
export type Resource = 'team' | 'team_member' | 'team_invitation';

type RolePermissions = {
  [role in RoleType]: Permission[];
};

export type Permission = {
  resource: Resource;
  actions: Action[] | '*';
};

export { availableRoles, customerRoles } from './roles';

export const permissions: RolePermissions = {
  OWNER: [
    {
      resource: 'team',
      actions: ['read', 'update'],
    },
    {
      resource: 'team_member',
      actions: '*',
    },
    {
      resource: 'team_invitation',
      actions: '*',
    },
  ],
  MEMBER: [
    {
      resource: 'team',
      actions: ['read', 'leave'],
    },
  ],
};

// Fail loudly if the Prisma enum ever drifts back to three roles.
const declaredRoles = Object.keys(permissions).sort();
const contractRoles = [...customerRoles].sort();

if (declaredRoles.join(',') !== contractRoles.join(',')) {
  throw new Error(
    `Role contract drift: permissions declares [${declaredRoles}] but the customer role contract is [${contractRoles}]`
  );
}
