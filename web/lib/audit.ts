import { logger } from './logger';

/**
 * Local, redacted audit emission.
 *
 * This replaces the upstream Retraced integration, which shipped an external
 * audit-log service and a customer-visible viewer. The Beta keeps an in-process
 * structured audit trail; the durable, dual-actor audit store belongs to the
 * domain service.
 */
export type AuditActor = {
  id: string;
  email?: string | null;
  name?: string | null;
};

export type AuditWorkspace = {
  id: string;
  slug: string;
};

export type AuditRequest = {
  action: string;
  crud: 'c' | 'r' | 'u' | 'd';
  user: AuditActor;
  team: AuditWorkspace;
};

export const sendAudit = (request: AuditRequest) => {
  logger.info('control_plane.audit', {
    action: request.action,
    crud: request.crud,
    actor_user_id: request.user?.id,
    workspace_id: request.team?.id,
    workspace_slug: request.team?.slug,
  });
};

export default sendAudit;
