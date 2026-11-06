import type { SessionStrategy } from 'next-auth';

const isTruthy = (value: string | undefined, fallback: boolean) => {
  if (value === undefined || value === '') {
    return fallback;
  }

  return value === 'true';
};

const isProduction = () => process.env.NODE_ENV === 'production';

/**
 * A hardening switch that development may turn off but production may not.
 *
 * Refusing loudly rather than silently forcing the safe value: an operator who
 * set the variable believes it took effect, and a deployment that quietly
 * ignores its own configuration is its own kind of incident.
 */
const productionLockedToggle = (
  name: string,
  value: string | undefined,
  fallback: boolean
) => {
  const requested = isTruthy(value, fallback);

  if (isProduction() && !requested) {
    throw new Error(
      `${name}=false is refused in production. This switch exists for local ` +
        'development only; disabling it in production would ship the app ' +
        'without its security headers. Remove the variable or set it to true.'
    );
  }

  return requested;
};

/**
 * MarketAI control-plane configuration.
 *
 * Upstream capabilities that this fork removed (SSO/SAML, SCIM/directory sync,
 * customer webhooks, Retraced, Stripe, team API keys, Slack, Sentry, Mixpanel,
 * reCAPTCHA) intentionally have no configuration surface here. Re-adding a key
 * is not enough to re-enable them; the routes and handlers are not present.
 * See web/HARDENING.md.
 */
const env = {
  databaseUrl: `${process.env.DATABASE_URL}`,
  appUrl: `${process.env.APP_URL}`,
  redirectIfAuthenticated: '/dashboard',

  // Hardened default: security headers and CSP are on unless explicitly
  // disabled, and production refuses to start if anything tries to disable
  // them. #15 accepts the tracer on the basis that security headers are
  // asserted, so this must not be a plain environment toggle.
  securityHeadersEnabled: productionLockedToggle(
    'SECURITY_HEADERS_ENABLED',
    process.env.SECURITY_HEADERS_ENABLED,
    true
  ),

  // SMTP configuration for transactional email
  smtp: {
    host: process.env.SMTP_HOST,
    port: Number(process.env.SMTP_PORT),
    user: process.env.SMTP_USER,
    password: process.env.SMTP_PASSWORD,
    from: process.env.SMTP_FROM,
  },

  /**
   * NextAuth configuration.
   *
   * `jwt` is not a preference: next-auth 4.x refuses to start a credentials
   * sign-in when the session strategy is `database` and every configured
   * provider is `credentials` (see core/lib/assert.ts, "Signin in with
   * credentials only supported if JWT strategy is enabled"). Upstream never hit
   * that guard because it also shipped GitHub, Google and SAML providers; this
   * fork removed them, so `database` would break sign-in outright.
   *
   * Consequence: sessions are not revocable server-side yet. Revocation lands
   * with the delegated-identity seam, which needs a revocable `session_id`
   * claim anyway. `assertSupportedSessionStrategy` below fails fast if the
   * combination is ever misconfigured.
   */
  nextAuth: {
    secret: process.env.NEXTAUTH_SECRET,
    sessionStrategy: (process.env.NEXTAUTH_SESSION_STRATEGY ||
      'jwt') as SessionStrategy,
  },

  // Users will need to confirm their email before accessing the app feature
  confirmEmail: process.env.CONFIRM_EMAIL === 'true',

  disableNonBusinessEmailSignup:
    process.env.DISABLE_NON_BUSINESS_EMAIL_SIGNUP === 'true',

  /**
   * Invite-only Beta. Only `credentials` is supported: every other upstream
   * provider auto-provisions users on first sign-in, which is public
   * registration by another name.
   */
  authProviders: 'credentials',

  otel: {
    prefix: process.env.OTEL_PREFIX || 'marketai.control_plane',
  },

  darkModeEnabled: process.env.NEXT_PUBLIC_DARK_MODE !== 'false',

  /**
   * Beta entry policy. These are asserted server-side; they are not UI hints.
   *
   * Workspace deletion has no flag: the lifecycle
   * (provisioning -> ... -> deletion_pending -> deleted) is not implemented and
   * neither the route nor a `deleteTeam` helper exists to switch on.
   */
  registration: {
    // Public signup is closed. Accounts exist only by consuming an invitation.
    publicSignupEnabled: false,
    // Customers cannot create Workspaces. Provisioning is an operator action.
    selfServeWorkspaceCreationEnabled: false,
  },

  maxLoginAttempts: Number(process.env.MAX_LOGIN_ATTEMPTS) || 5,

  rateLimit: {
    enabled: isTruthy(process.env.RATE_LIMIT_ENABLED, true),
    windowMs: Number(process.env.RATE_LIMIT_WINDOW_MS) || 60_000,
    authMaxRequests: Number(process.env.RATE_LIMIT_AUTH_MAX) || 10,
    mutationMaxRequests: Number(process.env.RATE_LIMIT_MUTATION_MAX) || 60,
  },

  /**
   * The domain service (FastAPI) origin and `DomainProxy`'s `undici` pool
   * tuning (spec §4.8, §6.7). `baseUrl` was recorded earlier for the
   * readiness probe; the remaining fields configure the connection pool and
   * per-attempt timeouts the BFF catch-all route uses to forward requests.
   */
  domainApi: {
    baseUrl: process.env.DOMAIN_API_BASE_URL || null,
    maxConnections: Number(process.env.DOMAIN_API_MAX_CONNECTIONS ?? '20'),
    headersTimeoutMs: Number(
      process.env.DOMAIN_API_HEADERS_TIMEOUT_MS ?? '10000'
    ),
    bodyTimeoutMs: Number(process.env.DOMAIN_API_BODY_TIMEOUT_MS ?? '30000'),
    maxRetries: Number(process.env.DOMAIN_API_MAX_RETRIES ?? '1'),
  },

  /**
   * Absolute per-request deadline for the BFF domain proxy (spec §4.8),
   * created before any I/O in the catch-all handler and threaded through
   * every awaited dependency via `AbortSignal.timeout()`.
   */
  totalDeadlineMs: Number(process.env.TOTAL_DEADLINE_MS ?? '55000'),

  /**
   * Bearer token FastAPI's projection sync presents to
   * `/api/internal/domain/workspaces/{workspaceId}/authorization-snapshot`
   * (spec §3.10). Empty by default — the endpoint refuses all requests with
   * 500 until an operator sets this, rather than falling back to an
   * unauthenticated read of Team authorization state.
   */
  projectionSyncServiceToken: process.env.PROJECTION_SYNC_SERVICE_TOKEN || '',

  /** Bearer token used only for typed domain onboarding facts pushed into the
   * control-plane projection. It is intentionally distinct from snapshot
   * pull and entitlement-inbox credentials. */
  domainToControlPlaneSyncToken:
    process.env.DOMAIN_TO_CONTROL_PLANE_SYNC_TOKEN || '',

  /**
   * Push-direction bearer token (control plane -> domain) for the domain
   * inbox endpoint `/api/v1/entitlements/inbox` (spec §4.2). Complements the
   * pull-direction `projectionSyncServiceToken` (domain -> control plane).
   * Empty by default; the outbox dispatcher refuses to deliver when unset.
   */
  controlPlaneToDomainEntitlementToken:
    process.env.CONTROL_PLANE_TO_DOMAIN_ENTITLEMENT_TOKEN || '',

  /**
   * `DelegatedTokenIssuer` configuration (spec §1.7, §2.2). The raw base64
   * key is kept here undecoded — `getTokenIssuer()`
   * (`lib/delegated-identity/instances.ts`) decodes and length-checks it
   * lazily, the same fail-on-use pattern `nextAuth.secret` already follows,
   * so importing `env` never requires every deployment (including tests
   * that never mint a token) to set it.
   */
  delegatedIdentity: {
    signingKeyEncryptionKeyBase64:
      process.env.DELEGATED_SIGNING_KEY_ENCRYPTION_KEY || '',
    tokenMaxTtlSeconds: Number(
      process.env.DELEGATED_TOKEN_MAX_TTL_SECONDS ?? '60'
    ),
  },

  /**
   * HMAC-SHA256 signing key for `EntitlementSnapshot` envelopes (spec §1.9,
   * §1.14). Kept undecoded here — the same fail-on-use pattern as
   * `nextAuth.secret` and `delegatedIdentity.signingKeyEncryptionKeyBase64`,
   * so importing `env` never requires a key (including in tests that never
   * publish a snapshot). Empty by default; the publication path must refuse
   * to sign rather than fall back to an implicit or well-known key.
   */
  entitlementSnapshotSigningKey:
    process.env.ENTITLEMENT_SNAPSHOT_SIGNING_KEY || '',
};

export default env;
