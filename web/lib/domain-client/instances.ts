/**
 * Process-wide singleton for `DomainProxy` (spec §6.7). One `undici` `Pool`
 * per process — recreating it per request would defeat the connection
 * pooling the proxy exists to provide.
 *
 * Importing this module also runs the generated allowlist binding's
 * startup validation (`./generated/allowlist-binding`, spec §6.2.4): that
 * module throws on any `DOMAIN_ALLOWLIST` / `OPERATION_REGISTRY` mismatch
 * as a side effect of being imported, so failing fast at server startup —
 * not on the first mismatched request — only works if something on the
 * request path actually imports it. `getDomainProxy()` is that path.
 */
import './generated/allowlist-binding';

import env from '@/lib/env';
import { DomainProxy } from './proxy';
import { OPERATION_REGISTRY } from './generated/operation-registry';
import { RESPONSE_VALIDATORS } from './generated/validators';
import { ApiError } from '../delegated-identity/token-issuer';

let domainProxy: DomainProxy | null = null;

export function getDomainProxy(): DomainProxy {
  if (!domainProxy) {
    const baseUrl = env.domainApi.baseUrl;
    if (!baseUrl) {
      throw new ApiError(
        500,
        'domain_api_not_configured',
        false,
        'DOMAIN_API_BASE_URL is not set'
      );
    }
    domainProxy = new DomainProxy(
      {
        baseUrl,
        maxConnections: env.domainApi.maxConnections,
        maxRetries: env.domainApi.maxRetries,
        headersTimeoutMs: env.domainApi.headersTimeoutMs,
        bodyTimeoutMs: env.domainApi.bodyTimeoutMs,
      },
      OPERATION_REGISTRY,
      RESPONSE_VALIDATORS
    );
  }
  return domainProxy;
}
