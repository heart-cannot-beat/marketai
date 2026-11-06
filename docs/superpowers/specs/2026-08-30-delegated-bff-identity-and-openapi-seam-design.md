# Delegated BFF Identity and OpenAPI Seam — Design Specification

**Issue:** #17 — Implement the delegated BFF identity and OpenAPI seam  
**Status:** Draft  
**Date:** 2026-08-30  
**Approach:** C→A hybrid — minimal vertical tracer first, then bottom-up expansion  
**Branch:** `Raybbish/implement-the-delegated-bff-identity-and-openapi`  
**Contracts:** `docs/architecture/saas-ui-fastapi-integration-contract.md` §4–§10, ADRs 0002–0004

---

## Overview

This specification defines the identity and communication seam between the Next.js/BoxyHQ control plane and the FastAPI domain API. The first milestone is a minimal vertical tracer: login → session → membership → ES256 JWT → BFF → FastAPI verify → TenantContext → RLS → identity probe. After the tracer passes, the remaining infrastructure (Operation polling, idempotency propagation, file intents, generated contract bindings) is delivered in the same PR.

The browser never holds a domain JWT, never calls FastAPI directly, and never supplies identity claims. The BFF is the sole issuer of short-lived delegated tokens, and FastAPI is the sole consumer.

---

## 1. ES256 Key Management & JWKS Publication

### 1.1 Encryption at rest

Private keys are never stored as plaintext PEM in the database. A database leak must not grant the ability to forge arbitrary delegated identity tokens.

All private keys are encrypted with **application-level AES-256-GCM** before persistence. The encryption key is externally supplied via the environment variable `DELEGATED_SIGNING_KEY_ENCRYPTION_KEY` (32 bytes, base64-encoded, sourced from a secret manager in production).

#### PrivateKeyProtector interface

All key management lives in the control plane (Node.js / TypeScript / Prisma). There is no Python key management code.

```typescript
type ProtectedKey = {
  readonly ciphertext: Buffer;
  readonly iv: Buffer;
  readonly authTag: Buffer;
};

type KeyBinding = {
  readonly kid: string;
  readonly algorithm: string;
  readonly publicJwkThumbprint: string;
};

interface PrivateKeyProtector {
  protect(privateKeyPem: Buffer, binding: KeyBinding): ProtectedKey;
  unprotect(protectedKey: ProtectedKey, binding: KeyBinding): Buffer;
}
```

`KeyBinding` is used as AES-256-GCM Additional Authenticated Data (AAD). This binds the ciphertext to the specific key row — transplanting an encrypted private key from one row to another changes the AAD, causing decryption to fail with an authentication error.

The default implementation (`AesGcmPrivateKeyProtector`) uses Node.js `crypto`:

- **Encrypt:** Generate a random 12-byte IV via `crypto.randomBytes(12)`. Encrypt `privateKeyPem` with `crypto.createCipheriv('aes-256-gcm', ...)` using `DELEGATED_SIGNING_KEY_ENCRYPTION_KEY` as the key, the IV, and `canonicalJson(binding)` as AAD. Store `ciphertext`, `iv`, and `authTag` (16 bytes) in the `ProtectedKey`.
- **Decrypt:** Reconstruct the AAD from `binding`. Decrypt using `crypto.createDecipheriv` with the same key, IV, and AAD. Set the auth tag before finalizing. If authentication fails (tampered ciphertext, wrong binding, wrong key), throw `KeyProtectionError`.

### 1.2 Key lifecycle

Each signing key follows a five-state lifecycle:

```
PREPUBLISHED → ACTIVE → RETIRING → RETIRED
                                  ↘ REVOKED (emergency, from any non-RETIRED state)
```

| State | Signing | Published in JWKS | Description |
|---|---|---|---|
| `PREPUBLISHED` | No | No | Key exists in the database but is not yet used or published. Allows pre-generation before rotation. |
| `ACTIVE` | Yes | Yes | Current signing key. At most one key is ACTIVE at any time, enforced by a partial unique index. |
| `RETIRING` | No | Yes | Replaced by a new ACTIVE key. Still published in JWKS so tokens signed by it remain verifiable until `verify_until`. |
| `RETIRED` | No | No | Removed from JWKS. `verify_until` has passed. The row is retained for audit. |
| `REVOKED` | No | No | Emergency invalidation. Immediately removed from JWKS. Requires out-of-band incident response. Can be reached from `ACTIVE`, `RETIRING`, or `PREPUBLISHED`. |

#### Rotation sequence

1. Generate a new P-256 key pair. Create a `PREPUBLISHED` row with a new `kid`.
2. Encrypt the private key via `PrivateKeyProtector.protect()`.
3. In a single transaction:
   - Transition the current `ACTIVE` key to `RETIRING`. Set `rotation_committed_at = now()`. Compute `verify_until`.
   - Transition the `PREPUBLISHED` key to `ACTIVE`. Set `activated_at = now()`.
4. The JWKS response now includes both the new ACTIVE key and the RETIRING key.
5. After `verify_until` passes, transition the `RETIRING` key to `RETIRED`. It disappears from JWKS.

#### `verify_until` calculation

```
verify_until = rotation_committed_at
             + max_signer_cache_age
             + max_token_ttl
             + allowed_clock_skew
```

This is the latest moment a token signed by the retiring key could still be valid and verified by a FastAPI instance whose JWKS cache has not yet refreshed. `max_signer_cache_age` accounts for the BFF signer cache, `max_token_ttl` (60 seconds) for the longest token lifetime, and `allowed_clock_skew` (default 5 seconds) for clock drift.

`verify_until` is NOT based on `last_signed_at`. With multiple BFF instances, a debounced `last_signed_at` is unreliable — one instance may still be signing with the old key while another has already rotated.

### 1.3 Deep-module interfaces

Three TypeScript interfaces separate signing, JWKS publication, and the high-level BFF entry point. All run in Node.js.

```typescript
type JwksHttpResponse = {
  readonly body: Buffer;
  readonly etag: string;
  readonly cacheControl: string;
  readonly status: 200 | 304;
};

interface DelegatedTokenSigner {
  sign(claims: Record<string, unknown>): Promise<string>;
}

interface DelegatedJwksPublisher {
  jwksResponse(requestEtag: string | null): Promise<JwksHttpResponse>;
}
```

`DelegatedTokenSigner.sign()` atomically selects the ACTIVE key, writes the `kid` into the JWT header, and returns the signed compact JWS. There is no separate `current_kid()` — the key selection and `kid` embedding happen in a single operation to prevent a race where the active key rotates between selecting the kid and signing.

`DelegatedJwksPublisher` constructs the JWKS JSON response containing only ACTIVE and RETIRING public keys. It computes the ETag from the SHA-256 hash of the canonical response bytes, not from internal key state. This avoids spurious cache invalidation: when the ACTIVE key transitions to RETIRING without a new key being added yet, the response bytes are identical and the ETag does not change.

The BFF-facing entry point is `DelegatedTokenIssuer`, defined in Section 2.

### 1.4 JWKS endpoint

**Path:** `GET /.well-known/jwks.json` on the control plane (Next.js).

**Next.js routing:** Pages Router API Routes live under `/api/*`. The `/.well-known/jwks.json` path requires a **rewrite rule** in `next.config.js`:

```javascript
async rewrites() {
  return [
    {
      source: "/.well-known/jwks.json",
      destination: "/api/.well-known/jwks",
    },
  ];
}
```

The actual API Route handler is at `pages/api/.well-known/jwks.ts`, backed by `DelegatedJwksPublisher`. The rewrite ensures the standard `/.well-known/` path works externally while the handler lives in the Pages Router convention.

**Response:**

```json
{
  "keys": [
    {
      "kty": "EC",
      "crv": "P-256",
      "kid": "dsk_01J...",
      "use": "sig",
      "alg": "ES256",
      "x": "...",
      "y": "..."
    }
  ]
}
```

**Headers:**

| Header | Value |
|---|---|
| `Content-Type` | `application/json` |
| `Cache-Control` | `public, max-age={SIGNER_CACHE_MAX_AGE}, must-revalidate` |
| `ETag` | `"sha256:{hex}"` computed from canonical response bytes |

**Conditional request:** If the request includes `If-None-Match` matching the current ETag, return 304 with no body.

**Security:** The JWKS endpoint publishes only public keys. It is included in `unAuthenticatedRoutes` — no session required. It must not leak key state, rotation timestamps, or any metadata beyond the standard JWK fields.

### 1.5 Signer cache

The BFF maintains an in-memory cache of the decrypted ACTIVE signing key. Cache behavior:

- **Startup jitter:** Multiple BFF instances must not all load the key simultaneously on startup. Each instance adds a random delay (0–`SIGNER_CACHE_JITTER_MAX_SECONDS`) before the first load.
- **Background periodic refresh:** The cache refreshes on a background timer (`SIGNER_CACHE_REFRESH_INTERVAL_SECONDS`, default 300). This ensures the BFF picks up newly rotated keys before the previous key's `verify_until` expires. The timer runs independently of request traffic — a BFF instance with no requests still refreshes.
- **Single-flighted refresh:** Concurrent refresh requests (timer-triggered or startup) coalesce into a single Prisma read via a shared `Promise`. The second caller awaits the first caller's `Promise`, not a new database round-trip.
- **No request-path-only recovery:** If the cache is empty (startup) and the initial load fails, the BFF returns 503 for all BFF domain requests until the background timer succeeds. There is no "refresh on first request" fallback that would make all concurrent requests wait on a single database call.

Unknown-kid handling is on the **FastAPI side** (§3.2), not the BFF signer. If FastAPI encounters an unknown `kid`, it refreshes its own JWKS cache. The BFF signer does not react to FastAPI's unknown-kid errors.

### 1.6 Database schema

Table `delegated_signing_keys` in the control-plane database:

```sql
CREATE TABLE delegated_signing_keys (
    id                    text        PRIMARY KEY,  -- "dsk_" + ULID
    kid                   text        NOT NULL UNIQUE,
    algorithm             text        NOT NULL CHECK (algorithm = 'ES256'),
    public_jwk            jsonb       NOT NULL,
    protected_private_key jsonb       NOT NULL,     -- { ciphertext, iv, auth_tag } base64-encoded
    public_jwk_thumbprint text        NOT NULL,
    state                 text        NOT NULL CHECK (state IN (
                                          'PREPUBLISHED', 'ACTIVE', 'RETIRING', 'RETIRED', 'REVOKED'
                                      )),
    created_at            timestamptz NOT NULL DEFAULT now(),
    activated_at          timestamptz,
    rotation_committed_at timestamptz,
    verify_until          timestamptz,
    revoked_at            timestamptz
);

-- At most one ACTIVE key at any time.
CREATE UNIQUE INDEX delegated_signing_keys_active_unique
    ON delegated_signing_keys (state)
    WHERE state = 'ACTIVE';
```

The `protected_private_key` column stores a JSON object with base64-encoded `ciphertext`, `iv`, and `auth_tag` fields. The raw key material never appears in plaintext in the database.

### 1.7 Configuration

| Variable | Type | Default | Description |
|---|---|---|---|
| `DELEGATED_SIGNING_KEY_ENCRYPTION_KEY` | string (base64) | required | 32-byte AES-256 key for encrypting private keys at rest |
| `SIGNER_CACHE_MAX_AGE` | integer (seconds) | 600 | `max-age` for JWKS `Cache-Control` header; also used in `verify_until` calculation |
| `SIGNER_CACHE_REFRESH_INTERVAL_SECONDS` | integer | 300 | How often the BFF refreshes the signing key from the database |
| `SIGNER_CACHE_JITTER_MAX_SECONDS` | integer | 30 | Maximum random delay before first key load on startup |
| `DELEGATED_TOKEN_MAX_TTL_SECONDS` | integer | 60 | Maximum `exp - iat` for delegated tokens |
| `ALLOWED_CLOCK_SKEW_SECONDS` | integer | 5 | Clock skew tolerance for `verify_until` and JWT verification |
| `TOTAL_DEADLINE_MS` | integer | 55000 | Absolute deadline in milliseconds for the entire BFF handler lifecycle (session, CSRF, body read, token mint, proxy, response validation). Caps all nested `undici` stall timeouts. |

---

## 2. Session Verification & Delegated JWT Signing

### 2.1 Revocable sessions — existing implementation

The control-plane codebase already implements revocable sessions. The relevant components:

- **`Session` model** (Prisma): `id`, `sessionToken`, `userId`, `expires`. Rows are deleted on sign-out or credential change.
- **`getSession(req, res)`** (`web/lib/session.ts`): Calls `getServerSession(req, res, getAuthOptions())`. The session callback re-reads the `Session` row by `sid` claim. If the row has been deleted, returns `null`.
- **`requireSession(req, res)`** (`web/lib/requireSession.ts`): Calls `getSession`, throws 401 if the session is not authenticated. Returns `AuthenticatedSession` with `sessionId`, `user.id`, `user.email`.

The `sid` claim in the NextAuth JWT cookie binds the cookie to a specific
`Session` row. Deleting the row (on sign-out or credential change) invalidates
the cookie without requiring a secret rotation. Workspace member removal does
not need to destroy a user's global session: the missing membership blocks new
token issuance and the incremented authorization epoch invalidates prior
workspace tokens. The signed cookie makes modification of the `sid` claim
detectable. The `sid` value itself is neither secret nor an authorization
credential — it is a session identifier whose corresponding row must exist for
the session to be valid.

No new session revocation implementation is needed for #17. The tracer must regression-verify that session deletion correctly blocks subsequent BFF requests.

### 2.2 DelegatedTokenIssuer — the deep module

`DelegatedTokenIssuer` is the only interface the BFF API route calls. It encapsulates the entire flow from session verification to signed JWT:

```typescript
import type { NextApiRequest, NextApiResponse } from 'next';

interface DelegatedTokenIssuer {
  issueForRequest(params: {
    req: NextApiRequest;
    res: NextApiResponse;
    workspaceSlug: string;
    signal: AbortSignal;
  }): Promise<string>;
}
```

Internal sequence:

1. `requireSession(req, res)` → `AuthenticatedSession` with `sessionId` and `user.id`, awaited through the caller's deadline signal.
2. Resolve `workspaceSlug` to `Team.id` (UUID).
3. Single-query consistent snapshot: `TeamMember` JOIN `Team` WHERE `teamId = team.id` AND `userId = session.user.id`. This query returns `membership_id`, `role`, `authorization_epoch`, and `entitlement_version` in a single read, avoiding a race between separate queries. If using Prisma, this is a `findFirst` with `include: { team: true }`. For stronger isolation, use `REPEATABLE READ` transaction isolation.
4. If no active membership → throw 403 `workspace_membership_required`.
5. Assemble claims (Section 2.3).
6. Sign via `DelegatedTokenSigner.sign(claims)` — key selection and `kid` header embedding happen atomically inside `sign()`.
7. Return the signed JWT string. The token is placed in the `Authorization: Bearer` header of the FastAPI request by the BFF proxy layer, never returned to the browser.

Every await in the Module is guarded by `params.signal`. Prisma cannot cancel
an already-issued query, so an abort may allow that query to finish in its
pool, but its result is discarded and the BFF response lifecycle remains
bounded. No signing attempt begins after the deadline has fired.

### 2.3 Fixed JWT contract

Every delegated token has exactly this structure:

**Header:**

```json
{
  "alg": "ES256",
  "typ": "delegated+jwt",
  "kid": "<kid from ACTIVE signing key>"
}
```

**Payload:**

```json
{
  "iss": "urn:marketai:control-plane",
  "aud": "urn:marketai:domain-api",
  "sub": "<user_id>",
  "workspace_id": "<team_id>",
  "membership_id": "<team_member_id>",
  "role": "owner",
  "session_id": "<session_id>",
  "authorization_epoch": 0,
  "entitlement_version": 0,
  "jti": "jti_<uuid>",
  "iat": 1724976000,
  "nbf": 1724976000,
  "exp": 1724976060
}
```

| Claim | Type | Constraint |
|---|---|---|
| `iss` | string | Fixed: `"urn:marketai:control-plane"` |
| `aud` | string | Fixed: `"urn:marketai:domain-api"` |
| `sub` | string | Immutable user ID from `User.id` |
| `workspace_id` | string | Immutable Workspace UUID from `Team.id`, resolved server-side from slug |
| `membership_id` | string | `TeamMember.id` for this user in this workspace |
| `role` | string | `"owner"` or `"member"` — lowercase |
| `session_id` | string | `Session.id` — the revocable session row |
| `authorization_epoch` | integer | `Team.authorizationEpoch` at time of signing |
| `entitlement_version` | integer | `Team.entitlementVersion` at time of signing |
| `jti` | string | `"jti_"` + UUID v4, unique per issuance |
| `iat` | integer | Unix timestamp of issuance |
| `nbf` | integer | Equal to `iat` |
| `exp` | integer | `iat + ttl` where `ttl <= DELEGATED_TOKEN_MAX_TTL_SECONDS` (60) |

No additional claims are permitted. FastAPI rejects tokens with extra claims.

### 2.4 authorization_epoch semantics

`authorization_epoch` is an integer on the `Team` model, starting at 0. It is incremented when:

- A member is **removed** from the workspace.
- A member's **role is changed** (OWNER → MEMBER or MEMBER → OWNER).

It is NOT incremented when:

- A session is revoked or a password is changed (handled by deleting the `Session` row, which blocks new token issuance directly).
- A new member is added (the new member has no prior authorization to revoke).

FastAPI middleware **fails closed on ANY mismatch** between the JWT's `authorization_epoch` claim and the workspace's current `authorization_epoch` in the domain database:

- Claim ahead of projection → reject (the domain database hasn't seen the epoch change yet; the projection is stale).
- Claim behind projection → reject (the token was signed before a membership change; the caller's authorization may have been revoked).

Both directions are blocked. There is no "reads continue" grace period for epoch mismatches.

### 2.5 entitlement_version semantics

`entitlement_version` is an integer on the `Team` model, starting at 0. It tracks changes to the workspace's subscription or entitlement state.

FastAPI middleware behavior:

- **Claim matches projection:** Normal operation.
- **Claim ahead of projection:** The control plane has updated entitlements but the domain database hasn't received the sync yet. **Reads (GET) continue** using the local projection. **Non-safe methods (POST/PUT/PATCH/DELETE) are blocked** centrally by middleware, returning `entitlement_sync_pending` with `Retry-After`. This prevents creating resources under entitlement limits the domain hasn't verified.
- **Claim behind projection:** The domain database has a newer entitlement state. **Use the local (newer) projection** for policy enforcement. The stale claim is not a security risk — the domain always enforces the strictest known entitlement.

### 2.6 Cross-workspace prohibition

One token carries exactly one `workspace_id`. There is no multi-workspace token. Each BFF request is scoped to a single workspace via the URL slug, and the token is issued for that workspace only. A request to a different workspace requires a new token with that workspace's membership, epoch, and entitlement.

### 2.7 Domain module isolation

Domain services never read `request.state` directly. The middleware constructs a `VerifiedDelegatedIdentity` from the verified JWT claims, then projects it into `TenantContext` (which sets PostgreSQL RLS parameters via `SET LOCAL`). Domain handlers receive an explicit `TenantContext` via `Depends(request_tenant_context)` — the existing pattern in the codebase.

If a domain service needs actor-level information (user ID, role), it receives explicit `Actor` parameters, not a reference to the request or its state. This keeps domain logic testable without HTTP fixtures.

### 2.8 Team schema additions

Four new columns on the `Team` model in `web/prisma/schema.prisma`:

```prisma
model Team {
  id                 String       @id @default(uuid())
  name               String
  slug               String       @unique
  domain             String?      @unique
  defaultRole        Role         @default(MEMBER)
  authorizationEpoch Int          @default(0)
  entitlementVersion Int          @default(0)
  suspended          Boolean      @default(false)
  projectionVersion  Int          @default(0)
  createdAt          DateTime     @default(now())
  updatedAt          DateTime     @default(now())
  members            TeamMember[]
  invitations        Invitation[]
}
```

A Prisma migration adds the counter columns with default values of `0` and
`suspended` with a default of `false`, which is safe for existing rows.

`projectionVersion` is an ordering field for authorization snapshots; it is
not a delegated JWT claim. It increments atomically whenever one of the
projected authorization fields changes. `authorizationEpoch` remains the
revocation and repair signal carried by the JWT, while `projectionVersion`
prevents out-of-order snapshots from overwriting newer domain state.

### 2.9 Epoch increment logic

The `authorizationEpoch` is incremented atomically in the same transaction that performs the membership change:

```typescript
await prisma.$transaction([
  prisma.teamMember.delete({ where: { id: memberId } }),
  prisma.team.update({
    where: { id: teamId },
    data: {
      authorizationEpoch: { increment: 1 },
      projectionVersion: { increment: 1 },
    },
  }),
]);
```

Similarly for role changes:

```typescript
await prisma.$transaction([
  prisma.teamMember.update({
    where: { id: memberId },
    data: { role: newRole },
  }),
  prisma.team.update({
    where: { id: teamId },
    data: {
      authorizationEpoch: { increment: 1 },
      projectionVersion: { increment: 1 },
    },
  }),
]);
```

Member addition does NOT increment the epoch:

```typescript
await prisma.teamMember.create({
  data: { teamId, userId, role },
});
```

Member addition also does not increment `projectionVersion`, because it does
not change any field in the Workspace authorization projection. The newly
added member receives a token containing the Team's current epoch.

Entitlement changes increment both counters in the same transaction:

```typescript
await prisma.team.update({
  where: { id: teamId },
  data: {
    entitlementVersion: { increment: 1 },
    projectionVersion: { increment: 1 },
  },
});
```

Suspension and unsuspension increment `authorizationEpoch` as well as
`projectionVersion`:

```typescript
await prisma.team.update({
  where: { id: teamId },
  data: {
    suspended: nextSuspended,
    authorizationEpoch: { increment: 1 },
    projectionVersion: { increment: 1 },
  },
});
```

The authorization-epoch increment is intentional: `projectionVersion` is not
in the JWT, so the new epoch is the request-path signal that causes FastAPI to
repair its projection before enforcing the new suspension state. The token
issuer signs the current epoch even for a suspended Team; it does not treat
that token as authorization to execute a domain handler. FastAPI repairs the
projection and then returns `workspace_suspended` before the handler runs.

### 2.10 Tracer test requirements

The tracer must verify:

- Token with correct claims → identity probe succeeds.
- Wrong `iss` → rejected.
- Wrong `aud` → rejected.
- Expired token → rejected.
- Unknown `kid` → rejected.
- `authorization_epoch` mismatch (0 vs 1, 1 vs 0) → rejected.
- Forged `workspace_id` (valid workspace, user not a member) → rejected at BFF (no membership found).
- Forged `role` (token says owner, membership says member) → rejected at BFF (claims assembled server-side, not from browser input).
- Session revoked between middleware check and BFF handler → 401.
- Member removed → epoch increments → subsequent token rejected.
- Epoch progression: epoch 0 → remove member → epoch 1 → add member back → epoch still 1 (addition doesn't increment) → token with epoch 0 rejected, token with epoch 1 accepted.
- Suspension progression: epoch 1 / projection 1 → suspend → epoch 2 / projection 2 → token with epoch 2 triggers repair → FastAPI returns `workspace_suspended` before the handler runs. Unsuspend increments both counters again and the next token repairs the projection before access resumes.

---

## 3. FastAPI JWT Verification Middleware

### 3.1 Pure ASGI middleware

The verification middleware is a pure ASGI middleware, not `starlette.middleware.base.BaseHTTPMiddleware`. `BaseHTTPMiddleware` has two problems:

1. It wraps the request/response in a way that breaks `contextvars` propagation in some configurations.
2. Its `dispatch` method converts async I/O into sync, blocking the event loop during JWT verification and JWKS refresh.

```python
from enum import StrEnum
from starlette.types import ASGIApp, Receive, Scope, Send


class SyncResult(StrEnum):
    SYNCED = "synced"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    NOT_FOUND = "not_found"


class DelegatedIdentityMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        jwks_store: JwksKeyStore,
        admission_reader: WorkspaceAdmissionReader,
        projection_syncer: WorkspaceProjectionSyncer,
        config: IdentityConfig,
    ) -> None:
        self.app = app
        self.jwks_store = jwks_store
        self.admission_reader = admission_reader
        self.projection_syncer = projection_syncer
        self._projection_repair_limiter = PerKeyRateLimiter(
            interval_seconds=config.projection_repair_interval_seconds,
        )
        self.config = config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        scope.setdefault("state", {})

        path = scope.get("path", "")
        if self._is_exempt(path):
            await self.app(scope, receive, send)
            return

        token = self._extract_bearer_token(scope)
        if token is None:
            await self._send_401(send, "delegated_identity_required", "Bearer token required")
            return

        try:
            identity = await self._verify_token(token)
        except (JwksRefreshError, JwksStaleError, JwksNotYetLoadedError):
            await self._send_503(send, "jwks_unavailable",
                                 "Signing keys are temporarily unavailable")
            return
        except TokenVerificationError as e:
            await self._send_401(send, e.code, e.description)
            return

        # Read workspace projection — on miss, attempt sync then retry
        try:
            admission = await self.admission_reader.read(identity.workspace_id)
        except WorkspaceNotFoundError:
            result = await self._try_projection_sync(identity.workspace_id)
            if result == SyncResult.NOT_FOUND:
                await self._send_401(send, "workspace_not_found", "Workspace does not exist")
                return
            if result != SyncResult.SYNCED:
                await self._send_503(send, "projection_sync_unavailable",
                                     "Cannot verify workspace existence — upstream unavailable")
                return
            try:
                admission = await self.admission_reader.read(identity.workspace_id)
            except WorkspaceNotFoundError:
                await self._send_503(send, "projection_sync_unavailable",
                                     "Projection store reported success but no row is readable")
                return

        # Epoch/entitlement gates — on claim-ahead, attempt repair then re-read
        epoch_error = self._check_authorization_epoch(identity, admission)
        entitlement_error = self._check_entitlement_version(identity, admission, scope)

        if epoch_error == "authorization_sync_pending" or entitlement_error == "entitlement_sync_pending":
            repair = await self._try_projection_sync(identity.workspace_id)
            if repair == SyncResult.SYNCED:
                try:
                    admission = await self.admission_reader.read(identity.workspace_id)
                except WorkspaceNotFoundError:
                    await self._send_503(send, "projection_sync_unavailable",
                                         "Projection store reported success but no row is readable")
                    return
                epoch_error = self._check_authorization_epoch(identity, admission)
                entitlement_error = self._check_entitlement_version(identity, admission, scope)
            elif repair == SyncResult.NOT_FOUND:
                await self._send_401(send, "workspace_not_found", "Workspace no longer exists")
                return
            elif repair == SyncResult.UPSTREAM_UNAVAILABLE:
                await self._send_503(send, "projection_sync_unavailable",
                                     "Authorization projection repair is unavailable")
                return
            # RATE_LIMITED falls through to the existing fail-closed epoch or
            # write-entitlement result. An entitlement-ahead read has no
            # entitlement_error and may continue with the local projection.

        if epoch_error is not None:
            await self._send_403(send, epoch_error)
            return

        if entitlement_error is not None:
            await self._send_403(send, entitlement_error)
            return

        if admission.suspended:
            await self._send_403(send, "workspace_suspended")
            return

        tenant_context = self._build_tenant_context(identity, admission)
        scope["state"]["tenant_context"] = tenant_context

        await self.app(scope, receive, send)

    async def _try_projection_sync(self, workspace_id: str) -> SyncResult:
        if not self._projection_repair_limiter.allow(workspace_id):
            return SyncResult.RATE_LIMITED
        try:
            await self.projection_syncer.sync(workspace_id)
            return SyncResult.SYNCED
        except WorkspaceNotFoundUpstreamError:
            return SyncResult.NOT_FOUND
        except Exception:
            return SyncResult.UPSTREAM_UNAVAILABLE

    async def _send_503(
        self, send: Send, code: str, description: str,
    ) -> None:
        body = self._problem_details_bytes(503, code, description, retryable=True)
        await send({"type": "http.response.start", "status": 503, "headers": [
            [b"content-type", b"application/problem+json"],
            [b"retry-after", b"5"],
        ]})
        await send({"type": "http.response.body", "body": body})
```

**Protection scope:** The middleware protects only `/api/v1/*` paths. Legacy `/api/*` routes (health, readiness, OpenAPI docs) are outside the protection scope and do not require a delegated JWT. This is enforced by path-prefix check in `_is_exempt`, not by enumerating individual exempt paths. Exempt paths within `/api/v1/*` (none currently) must be explicitly listed.

Exempt paths include health and readiness endpoints (`/api/health/live`, `/api/health/ready`) and OpenAPI documentation (`/api/docs`, `/api/openapi.json`). These are outside `/api/v1/` and therefore outside the protection scope by default.

### 3.2 JwksKeyStore

`JwksKeyStore` owns the JWKS cache, the HTTP client, and the ETag-based conditional refresh logic. It does NOT wrap `jwt.PyJWKClient`, which has its own blocking HTTP and incompatible cache behavior.

```python
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
import jwt


class JwksRefreshError(Exception):
    """The remote response cannot safely refresh the installed key set."""


@dataclass
class CachedKey:
    kid: str
    jwk: jwt.PyJWK
    algorithm: str


class JwksKeyStore:
    def __init__(
        self,
        *,
        jwks_url: str,
        max_stale_seconds: int,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._jwks_url = jwks_url
        self._max_stale_seconds = max_stale_seconds
        self._http_client = http_client
        self._keys: dict[str, CachedKey] = {}
        self._etag: str | None = None
        self._last_successful_refresh_at: datetime | None = None
        self._refresh_task: asyncio.Task[None] | None = None

    async def get_key(self, kid: str) -> jwt.PyJWK:
        self._check_hard_stale()

        cached = self._keys.get(kid)
        if cached is not None:
            return cached.jwk

        await self._single_flighted_refresh()

        cached = self._keys.get(kid)
        if cached is not None:
            return cached.jwk

        raise UnknownKidError(kid)

    def _check_hard_stale(self) -> None:
        if self._last_successful_refresh_at is None:
            raise JwksNotYetLoadedError()
        age = (datetime.now(timezone.utc) - self._last_successful_refresh_at).total_seconds()
        if age > self._max_stale_seconds:
            raise JwksStaleError(age, self._max_stale_seconds)

    async def _single_flighted_refresh(self) -> None:
        existing = self._refresh_task
        if existing is not None:
            await asyncio.shield(existing)
            return

        task = asyncio.get_running_loop().create_task(self._do_refresh())
        self._refresh_task = task
        task.add_done_callback(self._clear_refresh_task)
        # A cancelled request waiter must not cancel the process-wide refresh
        # or clear its shared reference while it is still running.
        await asyncio.shield(task)

    def _clear_refresh_task(self, task: asyncio.Task[None]) -> None:
        if self._refresh_task is task:
            self._refresh_task = None

    async def _do_refresh(self) -> None:
        headers: dict[str, str] = {}
        if self._etag is not None:
            headers["If-None-Match"] = self._etag

        response = await self._http_client.get(self._jwks_url, headers=headers)

        now = datetime.now(timezone.utc)

        if response.status_code == 304:
            # A 304 is meaningful only after a successful non-empty load for
            # the ETag sent in this request. A stray first-start 304 must not
            # turn an empty cache into a healthy cache.
            if self._etag is None or len(self._keys) == 0:
                raise JwksRefreshError("304 received without a populated ETag cache")
            self._last_successful_refresh_at = now
            return

        response.raise_for_status()

        data = response.json()
        new_keys: dict[str, CachedKey] = {}
        for key_data in data.get("keys", []):
            kid = key_data.get("kid")
            if (
                not isinstance(kid, str)
                or not kid
                or key_data.get("kty") != "EC"
                or key_data.get("crv") != "P-256"
                or key_data.get("alg") != "ES256"
                or key_data.get("use") != "sig"
            ):
                continue
            if kid in new_keys:
                raise JwksRefreshError("JWKS response contains duplicate kid")
            try:
                jwk = jwt.PyJWK.from_dict(key_data)
            except Exception:
                continue  # skip unparseable individual keys
            new_keys[kid] = CachedKey(kid=kid, jwk=jwk, algorithm="ES256")

        # SAFETY: an empty valid-key set is ALWAYS a refresh failure. Existing
        # keys remain installed because assignment happens only after this
        # check. Raising makes the background loop enter exponential backoff.
        if len(new_keys) == 0:
            raise JwksRefreshError("JWKS response contained no valid signing keys")

        self._keys = new_keys
        self._etag = response.headers.get("ETag")
        self._last_successful_refresh_at = now

    async def initial_load(self) -> None:
        try:
            await self._do_refresh()
        except Exception as exc:
            raise JwksInitialLoadError("JWKS initial load failed") from exc
        if self._last_successful_refresh_at is None or len(self._keys) == 0:
            raise JwksInitialLoadError("JWKS initial load produced no usable keys")
```

Key behaviors:

- **Hard-stale:** If `now() - last_successful_refresh_at > max_stale_seconds`, all token verification is rejected. The JWKS cache is not trusted indefinitely.
- **304 counts as success only for a populated ETag cache:** A 304 updates `last_successful_refresh_at` only when the request carried an installed ETag and the cache already contains a key. A first-start or otherwise empty cache rejects 304 as `JwksRefreshError`.
- **Single-flighted refresh:** Concurrent `get_key()` calls for an unknown kid coalesce into one HTTP request via a shared `asyncio.Task` reference (not `asyncio.Event`). Every waiter uses `asyncio.shield`, so cancelling one request cannot cancel the shared refresh. A task completion callback clears the reference only after that exact task finishes.
- **Background periodic refresh:** The `JwksKeyStore` starts a background `asyncio.Task` that refreshes on a timer (`DELEGATED_JWKS_REFRESH_INTERVAL_SECONDS`). This ensures the cache stays fresh even without request traffic. If the background refresh fails, it retries with exponential backoff. If `last_successful_refresh_at` exceeds `max_stale_seconds`, hard-stale kicks in — but the background task keeps trying to recover.
- **Empty JWKS is always a failure:** If `_do_refresh()` parses zero valid keys, it raises `JwksRefreshError` without changing the cache, ETag, or success time. Initial startup wraps this as `JwksInitialLoadError`; an existing cache is preserved and the background loop retries with exponential backoff until recovery or hard-stale.
- **Strict key shape:** Installed keys must be non-empty unique `kid` values with
  `kty=EC`, `crv=P-256`, `alg=ES256`, and `use=sig`, and must parse through
  `PyJWK`. A duplicate `kid` fails the whole refresh instead of silently using
  last-wins semantics.
- **Unknown-kid refresh:** When `get_key()` encounters an unknown `kid`, it triggers a single-flighted refresh. If the kid is still unknown after refresh, the token is rejected. This is the FastAPI-side recovery path — the BFF signer cache is not involved.
- **`jwt.PyJWK.from_dict()`:** Individual keys are parsed using PyJWT's `PyJWK` class. The `PyJWKClient` class is not used.
- **Dependency:** `PyJWT>=2.13,<3`.

### 3.3 WorkspaceAdmissionReader

The middleware must read workspace projection data (authorization epoch, entitlement version, suspended status) before `TenantContext` is established. This creates a chicken-and-egg problem: RLS prevents reading the `workspaces` table without an active tenant context.

The solution is a PostgreSQL `SECURITY DEFINER` function that bypasses RLS for this narrow admission check:

```sql
CREATE OR REPLACE FUNCTION marketai_workspace_admission(
    p_workspace_id text
)
RETURNS TABLE (
    workspace_id   text,
    authorization_epoch integer,
    entitlement_version integer,
    suspended      boolean
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ''
AS $$
    SELECT w.id, w.authorization_epoch, w.entitlement_version, w.suspended
    FROM (VALUES (1)) AS seed(n)
    LEFT JOIN public.workspaces w ON w.id = p_workspace_id;
$$;

REVOKE ALL ON FUNCTION marketai_workspace_admission(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION marketai_workspace_admission(text)
    TO marketai_domain_api, marketai_domain_worker;
```

Design choices:

- **`SECURITY DEFINER`:** The function runs as `marketai_domain_owner` (its creator), which has `BYPASSRLS`. This allows reading the `workspaces` table without a tenant context.
- **`SET search_path = ''`:** Prevents search-path hijacking.
- **`LEFT JOIN` from `(VALUES (1)) AS seed(n)`:** Guarantees exactly one result row. If the workspace does not exist, all columns are NULL. This distinguishes "workspace not found" (NULL row) from "query failed" (exception).
- **Table name:** `workspaces` (not `workspace_records`). Column `id` is `text` (not UUID).
- **Narrow scope:** The function returns only the four fields needed for admission. It cannot be used to read arbitrary workspace data.
- **Explicit privilege boundary:** PostgreSQL's default PUBLIC function
  execution privilege is revoked before granting only the API and Worker
  runtime roles.
- **Worker access:** The `marketai_domain_worker` role also has EXECUTE on this function. Worker verification (§7.8 step 1) re-checks workspace admission before committing an upload. The Worker runs under its own database role, not `marketai_domain_api`, so it needs a separate GRANT.

Python wrapper (uses the existing synchronous SQLAlchemy engine, not asyncpg):

```python
from dataclasses import dataclass
from typing import Callable

import anyio
from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class WorkspaceAdmission:
    workspace_id: str
    authorization_epoch: int
    entitlement_version: int
    suspended: bool


class WorkspaceAdmissionReader:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    async def read(self, workspace_id: str) -> WorkspaceAdmission:
        return await anyio.to_thread.run_sync(
            lambda: self._read_sync(workspace_id)
        )

    def _read_sync(self, workspace_id: str) -> WorkspaceAdmission:
        session = self._session_factory()
        try:
            row = session.execute(
                text("SELECT * FROM marketai_workspace_admission(:ws_id)"),
                {"ws_id": workspace_id},
            ).mappings().first()
            if row is None or row["workspace_id"] is None:
                raise WorkspaceNotFoundError(workspace_id)
            return WorkspaceAdmission(
                workspace_id=row["workspace_id"],
                authorization_epoch=row["authorization_epoch"],
                entitlement_version=row["entitlement_version"],
                suspended=row["suspended"],
            )
        finally:
            session.close()
```

`WorkspaceAdmissionReader` accepts a session factory (the same `sessionmaker` the domain uses) and hides session creation, thread offload, and cleanup. The middleware calls `await admission_reader.read(workspace_id)` — a single async interface with no session leaking into the caller. Internally, `anyio.to_thread.run_sync` offloads the synchronous SQLAlchemy call to a worker thread. No separate connection pool is needed.

### 3.4 Strict JWT validation

The middleware validates every aspect of the incoming token before trusting any claim:

| Check | Rule | Rejection |
|---|---|---|
| Raw token length | ≤ 4096 bytes | `token_too_large` |
| Algorithm | Must be `ES256` | `algorithm_not_allowed` |
| `typ` header | Must be `delegated+jwt` | `invalid_token_type` |
| Signature | Verified against JWKS public key for `kid` | `invalid_signature` |
| `iss` | Must equal `"urn:marketai:control-plane"` | `invalid_issuer` |
| `aud` | Must equal `"urn:marketai:domain-api"` | `invalid_audience` |
| `exp` | Must be in the future (with clock skew) | `token_expired` |
| `nbf` | Must be in the past (with clock skew) | `token_not_yet_valid` |
| `iat` | Must be present; `exp - iat <= DELEGATED_TOKEN_MAX_TTL_SECONDS` | `token_ttl_exceeded` |
| `jti` | Must match pattern `jti_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}` | `invalid_jti_format` |
| `sub` | Non-empty string, length ≤ 255 | `invalid_sub` |
| `workspace_id` | Non-empty string, length ≤ 255 | `invalid_workspace_id` |
| `membership_id` | Non-empty string, length ≤ 255 | `invalid_membership_id` |
| `role` | Must be `"owner"` or `"member"` | `invalid_role` |
| `session_id` | Non-empty string, length ≤ 255 | `invalid_session_id` |
| `authorization_epoch` | Non-negative integer | `invalid_authorization_epoch` |
| `entitlement_version` | Non-negative integer | `invalid_entitlement_version` |
| Extra claims | No claims beyond the 13 defined fields | `unexpected_claims` |
| Clock skew | Configurable via `DELEGATED_JWT_ALLOWED_CLOCK_SKEW_SECONDS` (default 5) | — |

### 3.5 authorization_epoch gate

After JWT verification and workspace admission read, the middleware checks epoch direction:

```python
def _check_authorization_epoch(
    self,
    identity: VerifiedDelegatedIdentity,
    admission: WorkspaceAdmission,
) -> str | None:
    claim = identity.authorization_epoch
    local = admission.authorization_epoch

    if claim == local:
        return None

    if claim < local:
        return "authorization_epoch_stale"

    # claim > local: domain projection behind control plane
    return "authorization_sync_pending"
```

Both directions reject before the handler runs. The two cases differ in semantics and recovery:

**claim < local → `authorization_epoch_stale`:** The token was issued before a membership or authorization change. The BFF re-issues a token with the current epoch on the next request.

```json
{
  "type": "urn:marketai:error:authorization-epoch-stale",
  "title": "Authorization epoch stale",
  "status": 403,
  "code": "authorization_epoch_stale",
  "trace_id": "tr_...",
  "retryable": true
}
```

**claim > local → `authorization_sync_pending`:** The domain's workspace projection is behind the control plane. This triggers a projection repair (rate-limited, §3.8). The client should retry after a short delay.

```json
{
  "type": "urn:marketai:error:authorization-sync-pending",
  "title": "Authorization sync pending",
  "status": 403,
  "code": "authorization_sync_pending",
  "trace_id": "tr_...",
  "retryable": true,
  "retry_after": 5
}
```

### 3.6 entitlement_version gate

```python
def _check_entitlement_version(
    self,
    identity: VerifiedDelegatedIdentity,
    admission: WorkspaceAdmission,
    scope: Scope,
) -> str | None:
    claim = identity.entitlement_version
    local = admission.entitlement_version

    if claim == local:
        return None

    if claim > local:
        method = scope.get("method", "GET").upper()
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            return "entitlement_sync_pending"
        return None

    # claim < local: use local (newer) projection. No error.
    return None
```

When claim is ahead and method is non-safe, the response is 403:

```json
{
  "type": "urn:marketai:error:entitlement-sync-pending",
  "title": "Entitlement sync pending",
  "status": 403,
  "code": "entitlement_sync_pending",
  "trace_id": "tr_...",
  "retryable": true,
  "retry_after": 5
}
```

CI must enumerate all registered routes and verify that none bypass this centralized gate. No per-handler entitlement flag checking — the middleware is the single enforcement point.

### 3.7 VerifiedDelegatedIdentity and TenantContext projection

After all checks pass, the middleware constructs `VerifiedDelegatedIdentity`:

```python
@dataclass(frozen=True)
class VerifiedDelegatedIdentity:
    sub: str
    workspace_id: str
    membership_id: str
    role: str
    session_id: str
    authorization_epoch: int
    entitlement_version: int
    jti: str
```

This is projected into `TenantContext` (the existing `app/platform/tenancy.py` dataclass):

```python
def _build_tenant_context(
    self,
    identity: VerifiedDelegatedIdentity,
    admission: WorkspaceAdmission,
) -> TenantContext:
    return TenantContext(
        workspace_id=identity.workspace_id,
        role=identity.role,
        initiator_id=identity.sub,
        authorization_epoch=admission.authorization_epoch,
        executor="user",
    )
```

Key choices:

- **`executor="user"`:** Matches the existing `USER_EXECUTOR = "user"` in `tenancy.py`. Using any other value (e.g. `"delegated_user"`) would cause `is_system` to return `True`, which is incorrect for user-initiated requests through the BFF.
- **`authorization_epoch` from admission (local), not identity (claim):** At this point both are equal (the §3.5 gate rejected any mismatch). Using the local value makes the source of truth explicit.
- **No `effective_entitlement` calculation:** The entitlement version is not written into `TenantContext`. The middleware has already enforced the entitlement gate (§3.6). Domain code does not read entitlement version from the context — it reads from the workspace projection directly when needed.

`TenantContext` is set on `scope["state"]["tenant_context"]`, which the existing `request_tenant_context` dependency reads via `request.state.tenant_context`.

### 3.8 Operational constraints

- **`MARKETAI_LOCAL_IDENTITY_ENABLED` on PostgreSQL:** If the SQLAlchemy engine's dialect name is `postgresql` (covers all driver variants: `postgresql://`, `postgres://`, `postgresql+psycopg://`, `postgresql+asyncpg://`, etc.), and `MARKETAI_LOCAL_IDENTITY_ENABLED` is true, the application must fail at startup with a clear error message. Local identity is only valid for SQLite development. The check uses `engine.dialect.name == "postgresql"`, not string prefix matching on the URL.
- **Readiness checks:** `GET /api/health/ready` verifies two conditions:
  1. JWKS is loaded and not hard-stale (`jwks_store.last_successful_refresh_at` is recent enough).
  2. The `marketai_workspace_admission` function is callable (execute a test query with a known-nonexistent workspace ID and verify it returns a NULL row).
- **Error responses:** All 401 responses include `WWW-Authenticate: Bearer` with `error` and `error_description` parameters per RFC 6750:
  ```
  WWW-Authenticate: Bearer error="invalid_token", error_description="Token has expired"
  ```
  The response body uses Problem Details (RFC 9457) with the `code`, `trace_id`, and `retryable` extension fields.
- **Projection repair:** When `authorization_epoch` or `entitlement_version` claim is ahead of the local workspace projection, the middleware triggers a projection repair (re-sync from control plane). This repair is rate-limited (at most once per `PROJECTION_REPAIR_INTERVAL_SECONDS`, default 60) to prevent a flood of sync requests from concurrent requests with stale projections. The repair calls `WorkspaceProjectionSyncer.sync(workspace_id)` (§3.10), which fetches the latest authorization snapshot from the control plane and applies it monotonically.

### 3.9 Configuration

| Variable | Type | Default | Description |
|---|---|---|---|
| `DELEGATED_JWKS_URL` | string | required | URL of the control plane JWKS endpoint |
| `DELEGATED_JWKS_MAX_STALE_SECONDS` | integer | 600 | Maximum age of JWKS cache before rejecting all tokens |
| `DELEGATED_JWKS_REFRESH_INTERVAL_SECONDS` | integer | 300 | Periodic refresh interval |
| `DELEGATED_JWT_ISSUER` | string | `"urn:marketai:control-plane"` | Expected `iss` claim |
| `DELEGATED_JWT_AUDIENCE` | string | `"urn:marketai:domain-api"` | Expected `aud` claim |
| `DELEGATED_JWT_ALLOWED_CLOCK_SKEW_SECONDS` | integer | 5 | Clock skew tolerance |
| `DELEGATED_JWT_MAX_TOKEN_BYTES` | integer | 4096 | Maximum raw token length in bytes |
| `PROJECTION_REPAIR_INTERVAL_SECONDS` | integer | 60 | Minimum interval between projection repair triggers |
| `MARKETAI_CONTROL_PLANE_INTERNAL_BASE_URL` | string | required (production) | Private control-plane origin used only by the snapshot HTTP Adapter |
| `MARKETAI_PROJECTION_SYNC_SERVICE_TOKEN` | string | required (production) | Service identity token for control-plane snapshot fetcher |
| `MARKETAI_PROJECTION_SYNC_TIMEOUT_MS` | integer | 3000 | Absolute timeout for one control-plane snapshot fetch |

### 3.10 Authorization Snapshot Propagation

The domain `workspaces` table is a **projection** of the control-plane `Team`. The control plane is the source of truth for workspace existence, `authorization_epoch`, `entitlement_version`, and `suspended` status. The domain never creates or modifies these values on its own — it only accepts monotonic updates from the control plane.

#### Propagation Interface

```python
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AuthorizationSnapshot:
    workspace_id: str
    workspace_name: str  # required when provisioning the domain Workspace row
    authorization_epoch: int
    entitlement_version: int
    suspended: bool
    projection_version: int


class AuthorizationSnapshotFetcher(Protocol):
    async def fetch(self, workspace_id: str) -> AuthorizationSnapshot: ...


class WorkspaceProjectionStore(Protocol):
    async def apply(self, snapshot: AuthorizationSnapshot) -> bool:
        """Apply a newer snapshot; return False for a replay/stale snapshot."""
        ...


class WorkspaceProjectionSyncer:
    def __init__(
        self,
        snapshot_fetcher: AuthorizationSnapshotFetcher,
        projection_store: WorkspaceProjectionStore,
    ) -> None:
        self._snapshot_fetcher = snapshot_fetcher
        self._projection_store = projection_store

    async def sync(self, workspace_id: str) -> bool:
        snapshot = await self._snapshot_fetcher.fetch(workspace_id)
        return await self._projection_store.apply(snapshot)
```

This is the external Interface of the sync Module. The fetch and persistence
seams are internal dependencies: production uses an HTTP fetch Adapter plus a
PostgreSQL `SECURITY DEFINER` store Adapter; tests use in-memory Adapters; SQLite
development uses a direct SQLAlchemy store Adapter because SQLite has no RLS.

#### PostgreSQL Projection Store Adapter

The PostgreSQL runtime roles are `NOBYPASSRLS`, and the first Workspace row
does not yet have a TenantContext. The Adapter therefore does **not** issue
direct `INSERT`/`UPDATE` statements. It calls one narrowly scoped function
owned by `marketai_domain_owner`:

```sql
CREATE OR REPLACE FUNCTION marketai_apply_workspace_projection(
    p_workspace_id text,
    p_workspace_name text,
    p_authorization_epoch integer,
    p_entitlement_version integer,
    p_suspended boolean,
    p_projection_version integer,
    p_default_currency text
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    affected_rows integer;
BEGIN
    IF p_workspace_id IS NULL OR p_workspace_name IS NULL
       OR p_authorization_epoch IS NULL OR p_entitlement_version IS NULL
       OR p_suspended IS NULL OR p_projection_version IS NULL
       OR p_default_currency IS NULL
       OR p_workspace_id = '' OR length(p_workspace_id) > 255
       OR p_workspace_name = '' OR length(p_workspace_name) > 200
       OR p_authorization_epoch < 0 OR p_entitlement_version < 0
       OR p_projection_version < 0
       OR p_default_currency !~ '^[A-Z]{3}$' THEN
        RAISE EXCEPTION 'invalid workspace projection snapshot';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.workspaces AS current
        WHERE current.id = p_workspace_id
          AND p_projection_version > current.projection_version
          AND (
              p_authorization_epoch < current.authorization_epoch
              OR p_entitlement_version < current.entitlement_version
          )
    ) THEN
        RAISE EXCEPTION 'newer projection regresses a semantic counter';
    END IF;

    INSERT INTO public.workspaces (
        id, name, default_currency, authorization_epoch,
        entitlement_version, suspended, projection_version, created_at
    ) VALUES (
        p_workspace_id, p_workspace_name, p_default_currency,
        p_authorization_epoch, p_entitlement_version,
        p_suspended, p_projection_version, now()
    )
    ON CONFLICT (id) DO UPDATE SET
        authorization_epoch = GREATEST(
            public.workspaces.authorization_epoch,
            EXCLUDED.authorization_epoch
        ),
        entitlement_version = GREATEST(
            public.workspaces.entitlement_version,
            EXCLUDED.entitlement_version
        ),
        suspended = EXCLUDED.suspended,
        projection_version = EXCLUDED.projection_version
    WHERE public.workspaces.projection_version < EXCLUDED.projection_version;

    GET DIAGNOSTICS affected_rows = ROW_COUNT;
    RETURN affected_rows > 0;
END;
$$;

REVOKE ALL ON FUNCTION marketai_apply_workspace_projection(
    text, text, integer, integer, boolean, integer, text
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION marketai_apply_workspace_projection(
    text, text, integer, integer, boolean, integer, text
) TO marketai_domain_api;
```

`p_default_currency` comes from the existing domain configuration
`MARKETAI_CURRENCY` (default `USD`); it is used only on first provisioning.
`workspace_name` comes from `Team.name` and is also used only for the initial
row. Workspace naming updates are not part of this authorization projection.
The runtime role receives no direct cross-tenant DML grant on `workspaces`.

#### Monotonic Guarantees

The `WHERE public.workspaces.projection_version < EXCLUDED.projection_version`
guard ensures that an older snapshot never overwrites a newer one.
`projection_version` is an independent monotonic counter maintained by the
control plane and incremented atomically on every change to
`authorization_epoch`, `entitlement_version`, or `suspended`. `GREATEST` on
the two semantic counters is defense in depth. A replayed snapshot with the
same or lower `projection_version` is a no-op. Tests also reject an
authoritative snapshot whose newer `projection_version` contains a lower
semantic counter; such a response is a control-plane contract violation and
must be surfaced rather than normalized silently.

#### Propagation Paths

| Path | Trigger | Description |
|---|---|---|
| **Initial provisioning** | Control plane creates a Team | The first BFF request for a new workspace encounters `WorkspaceNotFoundError`. The middleware fetches the authoritative snapshot over the internal HTTP Adapter and calls `marketai_apply_workspace_projection(...)`. The function inserts all required Workspace columns before `read()` is retried. |
| **Epoch update** | Control plane increments `authorization_epoch` (member removed, role changed, or suspension toggled) | The next BFF request carries the new epoch. The middleware detects `claim > local` and repairs before any domain handler runs. Member addition does not increment the epoch. |
| **Entitlement update** | Control plane increments `entitlement_version` (plan change, BYOK change) | Same mechanism as epoch update but through the entitlement gate. |
| **Suspension update** | Control plane toggles `suspended` and increments both `authorization_epoch` and `projection_version` | The epoch claim triggers repair; the repaired `suspended` field blocks the current request before the handler. |
| **Middleware repair** | Any `claim > local` mismatch (epoch or entitlement) | Rate-limited `sync()` call from middleware (§3.8). If sync succeeds, the middleware re-reads and re-evaluates the current request immediately; otherwise it returns the stable pending/unavailable error. |
| **Startup seeding** | Application startup, optional | A startup task may pre-seed known workspaces to avoid cold-start `authorization_sync_pending` on first request. This is optional — the repair path handles cold starts correctly, just with one extra round-trip. |

#### AuthorizationSnapshotFetcher

```python
class WorkspaceNotFoundUpstreamError(Exception):
    """Control plane authoritatively reports workspace does not exist."""


class AuthorizationSnapshotFetcher(Protocol):
    async def fetch(self, workspace_id: str) -> AuthorizationSnapshot:
        """Fetch the latest authorization snapshot from the control plane.

        Raises:
            WorkspaceNotFoundUpstreamError: The control plane authoritatively
                says this workspace does not exist (HTTP 404).
            ProjectionUpstreamUnavailableError: timeout, authentication
                failure, malformed response, or non-404 upstream error.
        """
        ...
```

The production Adapter is HTTP-only. `app/` and `worker/` never connect to or
read the control-plane database, even when both databases share one PostgreSQL
cluster. The in-memory Adapter exists for tests; there is no direct-database
production Adapter.

**Control-plane Interface:**

```http
GET /api/internal/domain/workspaces/{workspace_id}/authorization-snapshot
Authorization: Bearer <PROJECTION_SYNC_SERVICE_TOKEN>
Accept: application/json
```

Successful response:

```json
{
  "workspace_id": "<Team.id>",
  "workspace_name": "<Team.name>",
  "authorization_epoch": 2,
  "entitlement_version": 4,
  "suspended": false,
  "projection_version": 7
}
```

The Next.js handler performs a constant-time comparison against
`PROJECTION_SYNC_SERVICE_TOKEN`, validates `workspace_id`, reads exactly one
`Team` snapshot, and returns `Cache-Control: no-store`. It has no browser CORS,
session-cookie, or user-JWT mode and is registered under a distinct
`apiServiceAuthenticatedRoutes` middleware list. `404` is authoritative
not-found. `401`, `403`, `429`, malformed JSON, timeouts, and all `5xx` are
transient upstream failures from the domain's perspective.

`ControlPlaneAuthorizationSnapshotHttpAdapter` sends
`MARKETAI_PROJECTION_SYNC_SERVICE_TOKEN`, enforces
`MARKETAI_PROJECTION_SYNC_TIMEOUT_MS`, validates the exact response schema and
workspace-ID equality, and maps only 404 to
`WorkspaceNotFoundUpstreamError`. This distinction drives `SyncResult`:
`NOT_FOUND` produces 401; all other fetch failures produce retryable 503.

#### What the Control Plane Increments

| Control-plane event | Fields incremented | Domain-side effect |
|---|---|---|
| Member added to Team | none | The member receives a token with the current epoch; no previously authorized token must be revoked. |
| Member removed from Team | `authorization_epoch`, `projection_version` | Existing JWTs with old epoch are rejected as `authorization_epoch_stale` |
| Member role changed | `authorization_epoch`, `projection_version` | Domain re-reads role from next JWT (role is per-token, not cached) |
| Subscription/plan changed | `entitlement_version`, `projection_version` | Entitlement gate blocks writes until domain syncs |
| Workspace suspended/unsuspended | `suspended`, `authorization_epoch`, `projection_version` | The epoch claim triggers repair; `projection_version` orders the snapshot; repaired suspension blocks before the handler. |

---

## 4. BFF Route Allowlist & DomainProxy

### 4.1 Request Security Gate

Every request to the BFF domain proxy passes three checks before any
business logic runs. The checks execute in fixed order; failure at any
step returns an immediate error response.

All boundary failures use one typed error shape so the handler's central catch
cannot accidentally turn a validation failure into an unstructured 500:

```typescript
class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly retryable = false,
    message = code,
  ) {
    super(message);
  }
}
```

#### Host Validation

The `Host` header (including port when non-default) must exactly match
the host-and-port extracted from the configured `APP_URL`. This
prevents DNS-rebinding attacks.

```typescript
function validateHost(req: NextApiRequest, appUrl: URL): void {
  const hostHeader = req.headers.host;
  if (!hostHeader) {
    throw new ApiError(400, 'missing_host');
  }
  const expected = appUrl.port
    ? `${appUrl.hostname}:${appUrl.port}`
    : appUrl.hostname;
  if (hostHeader !== expected) {
    throw new ApiError(400, 'invalid_host');
  }
}
```

#### Origin and CSRF Validation — Non-Safe Methods Only

Origin and CSRF checks apply only to **non-safe methods** (POST, PUT, PATCH, DELETE). Same-origin GET/HEAD requests typically omit the `Origin` header (per the Fetch spec, browsers attach `Origin` only to CORS requests and non-safe same-origin requests). Requiring `Origin` on GET would break the identity tracer's first call.

```typescript
function validateOriginIfNonSafe(req: NextApiRequest, appUrl: URL): void {
  const method = (req.method ?? 'GET').toUpperCase();
  if (method === 'GET' || method === 'HEAD') {
    return;
  }
  const origin = req.headers.origin;
  if (!origin) {
    throw new ApiError(403, 'missing_origin');
  }
  if (origin !== appUrl.origin) {
    throw new ApiError(403, 'origin_not_allowed');
  }
}
```

#### CSRF Verification — Non-Safe Methods Only

CSRF verification runs **after** `requireSession` and only for non-safe methods. A valid session must exist before the CSRF token can be validated, because the token is bound to the session. GET/HEAD requests do not require CSRF tokens.

```typescript
interface CsrfVerifier {
  verify(req: NextApiRequest, session: AuthenticatedSession): Promise<void>;
}
```

The `CsrfVerifier` implementation:

1. Returns immediately for GET/HEAD (no CSRF check).
2. Reads the `X-CSRF-Token` header from the request.
3. Validates the token against the session (the token is an HMAC of
   the session ID, signed with a server-side secret).
4. Throws `ApiError(403, 'csrf_validation_failed')` on mismatch or missing
   token.

The CSRF token endpoint (`GET /api/csrf`) issues tokens bound to the
caller's session (identified via the HttpOnly cookie, not a request
parameter).

### 4.2 Route Classification in Middleware

BFF domain routes must NOT be added to `unAuthenticatedRoutes`. Adding
them there would signal to future maintainers that these routes are
publicly accessible. Instead, a separate list declares routes that
handle their own authentication internally:

```typescript
export const apiSelfAuthenticatedRoutes = [
  '/api/domain/**',
  '/api/csrf',
];
```

The Next.js middleware skips the session-fetch redirect for these routes
(they are not unauthenticated — they call `requireSession` inside the
handler). The middleware still applies security headers and CSP to all
responses.

```typescript
// In middleware.ts
if (micromatch.isMatch(pathname, unAuthenticatedRoutes)) {
  return applySecurityHeaders(withNonce(), csp);
}

if (micromatch.isMatch(pathname, apiSelfAuthenticatedRoutes)) {
  return applySecurityHeaders(withNonce(), csp);
}

// ... existing session check and redirect for all other routes
```

### 4.3 Allowlist Entry

Each permitted BFF-to-FastAPI operation is declared as an
`AllowlistEntry`. The allowlist is a static array — no dynamic
registration, no generic proxy.

The reusable server-only types live in
`web/lib/domain-client/allowlist-types.ts`; generated `allowlist.ts` imports
them from its parent directory.

```typescript
import { z } from 'zod';
import type { ValidateFunction } from 'ajv/dist/core';
import type { DomainOperationMap } from './generated/operation-map';

type OperationId = keyof DomainOperationMap;
type RequestBody<K extends OperationId> = DomainOperationMap[K]['requestBody'];
type BodyOperationId = {
  [K in OperationId]: [RequestBody<K>] extends [never] ? never : K
}[OperationId];
export type AnyRequestBody = DomainOperationMap[BodyOperationId]['requestBody'];

type AllowlistEntry<K extends OperationId> = {
  operationId: K;
  method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  bffPattern: string;
  fastapiPathTemplate: string;
  pathParams: readonly string[];
  querySchema: z.ZodType | null;
  requestBodyValidator:
    [RequestBody<K>] extends [never]
      ? null
      : ValidateFunction<RequestBody<K>>;
  requestMediaTypes: readonly string[];
  maxRequestBytes: number;
  maxResponseBytes: number;
  requiresIdempotency: boolean;
  forwardConditionalHeaders: boolean;
};

export type AnyAllowlistEntry = {
  [K in OperationId]: AllowlistEntry<K>
}[OperationId];
```

| Field | Purpose |
|---|---|
| `operationId` | Links to the generated `OPERATION_REGISTRY` entry. Validated at module load by the allowlist binding. |
| `method` | HTTP method. Must match the registry entry's method. |
| `bffPattern` | Browser-facing path pattern with named params (`:slug`). Used for incoming request matching and Location rewriting. |
| `fastapiPathTemplate` | FastAPI path template with `{param}` placeholders. |
| `pathParams` | Ordered list of path parameter names extracted from the pattern. |
| `querySchema` | Zod schema for query string validation. `null` means no query parameters are allowed; any query string present is rejected. |
| `requestBodyValidator` | Pre-compiled Ajv standalone validator for the request body JSON shape. `null` means no body expected (must match `requestMediaTypes` being empty). When `requestMediaTypes` is non-empty, `requestBodyValidator` **must** be non-null — the allowlist binding (§6.2.4) validates this invariant at startup. The handler returns 422 `request_validation_failed` if the body fails validation. |
| `requestMediaTypes` | Allowed `Content-Type` values for the request body. Empty array means no body allowed. |
| `maxRequestBytes` | Hard limit on request body size. Exceeding returns 413. |
| `maxResponseBytes` | Hard limit on response body size from FastAPI. Exceeding returns 502 `invalid_domain_response`. |
| `requiresIdempotency` | Whether the `X-Intent-Id` header is required and an `Idempotency-Key` must be derived and forwarded. |
| `forwardConditionalHeaders` | Whether `If-Match` and `If-None-Match` from the browser are forwarded to FastAPI (for Operation polling ETag, etc.). Default `false`. |

Example entries for the tracer and file intent operations:

```typescript
const DOMAIN_ALLOWLIST = [
  {
    operationId: 'get_identity_probe',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/identity',
    fastapiPathTemplate: '/api/v1/identity',
    pathParams: [],
    querySchema: null,
    requestBodyValidator: null,
    requestMediaTypes: [],
    maxRequestBytes: 0,
    maxResponseBytes: 65_536,
    requiresIdempotency: false,
    forwardConditionalHeaders: false,
  },
  {
    operationId: 'get_operation',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/operations/:operation_id',
    fastapiPathTemplate: '/api/v1/operations/{operation_id}',
    pathParams: ['operation_id'],
    querySchema: null,
    requestBodyValidator: null,
    requestMediaTypes: [],
    maxRequestBytes: 0,
    maxResponseBytes: 65_536,
    requiresIdempotency: false,
    forwardConditionalHeaders: true,
  },
  {
    operationId: 'create_upload_intent',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/upload-intents',
    fastapiPathTemplate: '/api/v1/upload-intents',
    pathParams: [],
    querySchema: null,
    requestBodyValidator: REQUEST_VALIDATORS.create_upload_intent,
    requestMediaTypes: ['application/json'],
    maxRequestBytes: 8_192,
    maxResponseBytes: 65_536,
    requiresIdempotency: true,
    forwardConditionalHeaders: false,
  },
  {
    operationId: 'finalize_upload',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/upload-intents/:intent_id/finalize',
    fastapiPathTemplate: '/api/v1/upload-intents/{intent_id}/finalize',
    pathParams: ['intent_id'],
    querySchema: null,
    requestBodyValidator: REQUEST_VALIDATORS.finalize_upload,
    requestMediaTypes: ['application/json'],
    maxRequestBytes: 8_192,
    maxResponseBytes: 65_536,
    requiresIdempotency: true,
    forwardConditionalHeaders: false,
  },
  {
    operationId: 'create_download_intent',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/download-intents',
    fastapiPathTemplate: '/api/v1/download-intents',
    pathParams: [],
    querySchema: null,
    requestBodyValidator: REQUEST_VALIDATORS.create_download_intent,
    requestMediaTypes: ['application/json'],
    maxRequestBytes: 8_192,
    maxResponseBytes: 65_536,
    requiresIdempotency: true,
    forwardConditionalHeaders: false,
  },
] as const satisfies readonly AnyAllowlistEntry[];
```

### 4.4 Header Construction

Headers are built from an empty set — never passed through from the
browser request or the FastAPI response.

#### Path Normalization

Before any other processing (including route matching and session access), the handler validates and normalizes path segments:

```typescript
function validatePathSegments(slug: string, segments: string[]): boolean {
  const all = [slug, ...segments];
  for (const seg of all) {
    if (!seg) return false;
    let decoded: string;
    try {
      decoded = decodeURIComponent(seg);
    } catch {
      return false;
    }
    if (decoded === '.' || decoded === '..') return false;
    if (decoded.includes('\0') || decoded.includes('\\') || decoded.includes('/')) return false;
    // Reject residual percent-encoding (including double encoding) rather than
    // relying on another layer's decode count.
    if (decoded.includes('%')) return false;
  }
  return true;
}
```

Rejects malformed or residual/double percent-encoding, encoded slashes or
backslashes (`%2F`, `%5C`), NUL bytes, `.` and `..` segments, and any decoded
segment containing a path separator. This runs before route matching to
prevent path-confusion attacks.

#### Query Parameter Extraction

Query parameters are parsed from the raw URL, not `req.query`. Next.js populates `req.query` with both URL search params and dynamic route parameters (`slug`, `path[]`), making it impossible to distinguish user-supplied query params from routing params.

```typescript
function extractSearchParams(rawUrl: string | undefined): URLSearchParams {
  if (!rawUrl) return new URLSearchParams();
  const qIdx = rawUrl.indexOf('?');
  if (qIdx === -1) return new URLSearchParams();
  return new URLSearchParams(rawUrl.slice(qIdx + 1));
}
```

**Request headers sent to FastAPI:**

| Header | Source |
|---|---|
| `Authorization` | `Bearer <delegated_jwt>` — issued per attempt by `DelegatedTokenIssuer` |
| `Content-Type` | From allowlist `requestMediaTypes` — validated against browser's declared type |
| `Content-Length` | Recomputed from the serialized body bytes |
| `Idempotency-Key` | `SHA-256(allowlist_entry.operationId + intent_id)` — only when `requiresIdempotency` is true |
| `X-Request-Id` | Generated per handler invocation (UUID v4) |
| `X-Trace-Id` | From browser `X-Trace-Id` if present, otherwise same as `X-Request-Id` |
| `Accept` | `application/json` (fixed) |
| `If-Match` | From browser, only when `forwardConditionalHeaders` is true |
| `If-None-Match` | From browser, only when `forwardConditionalHeaders` is true |

No other headers are forwarded. Browser `Cookie`, `Authorization`,
`Referer`, `User-Agent`, and all other headers are stripped.

**Response headers returned to the browser:**

| Header | Source |
|---|---|
| `Content-Type` | From FastAPI response, validated against operation registry |
| `Content-Length` | Recomputed from response body bytes |
| `ETag` | From FastAPI response, passed through verbatim |
| `Retry-After` | From FastAPI response, passed through verbatim |
| `Location` | Rewritten from FastAPI private path to BFF public path (see §4.7) |
| `X-Trace-Id` | Same value sent in the request |
| `Cache-Control` | `no-store` forced for all responses carrying authenticated data |

All responses carrying authenticated domain data use `Cache-Control: no-store` — not just non-GET. Browser and proxy caches must not store responses that depend on delegated identity.

No other response headers are forwarded. FastAPI's `Server`,
`X-Powered-By`, `Set-Cookie`, and internal headers are stripped.

### 4.5 Request and Response Size Limits

The proxy enforces hard size limits at two points:

**Request body:** The Next.js body parser is disabled for domain proxy
routes. The proxy reads the raw body stream, counting bytes. If the
byte count exceeds `maxRequestBytes`, the stream is aborted and a 413
response is returned immediately.

```typescript
// pages/api/domain/v1/w/[slug]/[...path].ts
export const config = {
  api: {
    bodyParser: false,
  },
};
```

```typescript
async function readLimitedBody(
  req: NextApiRequest,
  maxBytes: number,
  signal: AbortSignal,
): Promise<Buffer> {
  const read = (async () => {
    const chunks: Buffer[] = [];
    let totalBytes = 0;
    for await (const chunk of req) {
      totalBytes += chunk.length;
      if (totalBytes > maxBytes) {
        req.pause();
        throw new ApiError(413, 'request_body_too_large', false);
      }
      chunks.push(chunk);
    }
    return Buffer.concat(chunks);
  })();
  return await awaitWithinDeadline(read, signal, () => req.pause());
}

async function assertNoRequestBody(
  req: NextApiRequest,
  signal: AbortSignal,
): Promise<void> {
  const declaredLength = Number(req.headers['content-length'] ?? '0');
  if (Number.isFinite(declaredLength) && declaredLength > 0) {
    throw new ApiError(400, 'unexpected_body', false);
  }
  const read = (async () => {
    for await (const chunk of req) {
      if (chunk.length > 0) {
        req.pause();
        throw new ApiError(400, 'unexpected_body', false);
      }
    }
  })();
  await awaitWithinDeadline(read, signal, () => req.pause());
}
```

`awaitWithinDeadline` registers an abort listener while awaiting the request
stream, removes it in `finally`, and pauses the stream before rejecting. This
covers slow/chunked bodies that do not declare
`Content-Length`. An operation whose `requestMediaTypes` is empty always calls
`assertNoRequestBody`; bodies are rejected, never silently discarded.

**Response body:** The proxy reads the FastAPI response body with the
same byte-counting approach. If `maxResponseBytes` is exceeded, the
proxy returns 502 `invalid_domain_response` to the browser (the
FastAPI response is discarded, not partially forwarded).

### 4.6 Intent-Based Idempotency

The browser manages user-action identity through `IntentTracker` (see
§6). For operations where `requiresIdempotency` is true:

1. The browser sends `X-Intent-Id` in the request header.
2. The BFF reads `X-Intent-Id` and derives the idempotency key:

```typescript
import { createHash } from 'node:crypto';

function deriveIdempotencyKey(
  operationId: string,
  intentId: string
): string {
  return createHash('sha256')
    .update(`${operationId}:${intentId}`)
    .digest('hex');
}
```

3. The derived key is sent to FastAPI as the `Idempotency-Key` header.
4. FastAPI binds the key to workspace and actor server-side — the BFF
   does not include workspace or session in the derivation.

If `requiresIdempotency` is true and `X-Intent-Id` is missing, the BFF
returns 400 `missing_intent_id`.

### 4.7 Location Rewriting

When FastAPI returns a `Location` header (typically with 201 or 202
responses), the BFF rewrites it from the private FastAPI path to the
public BFF path:

1. Match the `Location` value against `LocationShape` patterns in the
   operation's registry entry (`responses[status].locationShapes`).
2. Extract the `targetOperationId` from the matching shape.
3. Look up the `targetOperationId` in the allowlist to find the
   corresponding `bffPattern`.
4. Construct the browser-facing URL by applying extracted path params
   and the current request's `slug`.

```typescript
function rewriteLocation(
  fastapiLocation: string,
  operationEntry: OperationEntry,
  status: number,
  slug: string,
  allowlist: readonly AllowlistEntry[]
): string | null {
  const responseShape = operationEntry.responses[status];
  if (!responseShape) {
    return null;
  }

  for (const shape of responseShape.locationShapes) {
    const match = shape.pattern.exec(fastapiLocation);
    if (!match) continue;

    const targetEntry = allowlist.find(
      (e) => e.operationId === shape.targetOperationId
    );
    if (!targetEntry) {
      return null;
    }

    let bffPath = targetEntry.bffPattern.replace(
      ':slug',
      encodeURIComponent(slug),
    );
    for (const paramName of shape.extractParams) {
      const paramValue = match.groups?.[paramName];
      if (paramValue) {
        bffPath = bffPath.replace(`:${paramName}`, encodeURIComponent(paramValue));
      }
    }

    return bffPath;
  }

  return null;
}
```

If no matching shape is found, or the `targetOperationId` has no
allowlist entry, the Location header is **not forwarded** — the proxy
logs a warning and returns 502 `invalid_domain_response`. Invalid
Locations are never passed through to the browser.

### 4.8 Timeout and Deadline Management

`undici` is the HTTP adapter for server-side requests to FastAPI.
`undici`'s `bodyTimeout` is a **stall timeout** (time without data),
not a total timeout. The proxy uses `AbortSignal.timeout()` for an
absolute deadline.

The **deadline** is created at **handler step 1**, before host/path/route checks
and before any I/O. Every await crosses one of two deadline-aware
Interfaces: a dependency that accepts `AbortSignal`, or `awaitWithinDeadline`
for libraries such as Prisma that cannot cancel an already-issued operation.
The latter discards a late result even though the underlying pool operation may
finish. This bounds the BFF response lifecycle without pretending every
dependency supports physical cancellation.

```typescript
async function awaitWithinDeadline<T>(
  work: Promise<T>,
  signal: AbortSignal,
  onAbort: () => void = () => {},
): Promise<T> {
  if (signal.aborted) throw new GatewayTimeoutError();

  return new Promise<T>((resolve, reject) => {
    const cleanup = () => signal.removeEventListener('abort', abort);
    const abort = () => {
      cleanup();
      try {
        onAbort();
      } catch {
        // Cleanup failure cannot replace the stable deadline result.
      }
      reject(new GatewayTimeoutError());
    };
    signal.addEventListener('abort', abort, { once: true });
    work.then(
      (value) => {
        cleanup();
        resolve(value);
      },
      (error) => {
        cleanup();
        reject(error);
      },
    );
  });
}
```

`requireSession`, CSRF verification, request-body reading, per-attempt token
minting, proxy dispatch, response-body reading, validation, and response
writing are all guarded by the same signal. The route/host/path checks before
the first await are synchronous but occur after the deadline is created;
response writing performs a final abort check immediately before serialization.

Token signing happens **per attempt** inside `dispatchWithRetry()` — on retry, the previous token may be close to expiry. Headers are built per attempt because they include the fresh token.

The following retry logic is the internal implementation of `DomainProxy.forward()` (§6.7). The class is the public interface; this section details its Phase 1 / Phase 2 split and the body-read vs validation error separation:

```typescript
// Internal to DomainProxy — called from forward() after path construction.
// Token is signed PER ATTEMPT (a retry may need a fresh token).
// Headers are built per attempt because they include the token.
private async dispatchWithRetry(
  entry: OperationEntry,
  params: {
    tokenIssuer: DelegatedTokenIssuer;
    req: NextApiRequest;
    res: NextApiResponse;
    slug: string;
    path: string;
    serializedBody: string | undefined;
    signal: AbortSignal;
    idempotencyKey: string | undefined;
    requestId: string;
    traceId: string;
    conditionalHeaders: Record<string, string>;
  },
): Promise<ValidatedResponse> {
  const MAX_RETRIES = this.config.maxRetries; // DOMAIN_API_MAX_RETRIES, default 1
  const { signal } = params;
  const canRetry = this.isRetryableOperation(entry, params);

  let lastError: Error | null = null;

  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    if (signal.aborted) {
      throw new GatewayTimeoutError();
    }

    // Sign token per attempt — retry may need a fresh token
    const token = await params.tokenIssuer.issueForRequest({
      req: params.req,
      res: params.res,
      workspaceSlug: params.slug,
      signal,
    });

    // Build headers per attempt (includes fresh token)
    const headers: Record<string, string> = {
      Authorization: `Bearer ${token}`,
      Accept: "application/json",
      "X-Request-Id": params.requestId,
      "X-Trace-Id": params.traceId,
      ...params.conditionalHeaders,
    };

    if (params.serializedBody !== undefined) {
      headers["Content-Type"] = "application/json";
      headers["Content-Length"] = String(Buffer.byteLength(params.serializedBody, "utf-8"));
    }

    if (params.idempotencyKey !== undefined) {
      headers["Idempotency-Key"] = params.idempotencyKey;
    }

    // Phase 1: Send request, receive response headers.
    // undici pool.request() resolves when headers arrive — the body
    // stream has NOT been consumed yet.
    let response: Dispatcher.ResponseData;
    try {
      response = await this.pool.request({
        method: entry.method,
        path: params.path,
        headers,
        body: params.serializedBody,
        signal,
        headersTimeout: this.config.headersTimeoutMs, // default 10_000
        bodyTimeout: this.config.bodyTimeoutMs,       // default 30_000 (stall timeout, not total)
      });
    } catch (error) {
      // Only network-level failures (no headers received) reach here.
      // These are the ONLY errors eligible for retry.
      if (signal.aborted) {
        throw new GatewayTimeoutError();
      }
      if (!canRetry || !this.isRetryableError(error) || attempt >= MAX_RETRIES) {
        throw error;
      }
      lastError = error as Error;
      await this.backoffWithJitter(attempt, signal);
      continue;
    }

    // Phase 2: Response headers received — NEVER retry from this point.
    // Read body, then validate. Errors are separated:
    // - Body read I/O failure → domain_response_incomplete (502)
    // - Schema/status/Location validation failure → invalid_domain_response (502)
    // Both are unrecoverable, but the error codes differ for observability.
    let raw: string;
    try {
      raw = await awaitWithinDeadline(
        readBoundedBody(response.body, entry.maxResponseBytes),
        signal,
        () => response.body.destroy(new GatewayTimeoutError()),
      );
    } catch (bodyReadError) {
      await response.body.dump().catch(() => {});
      if (signal.aborted) {
        throw new GatewayTimeoutError();
      }
      if (bodyReadError instanceof DomainProxyError) {
        // The bounded reader uses invalid_domain_response for a size-limit
        // violation; preserve that typed boundary failure.
        throw bodyReadError;
      }
      throw new DomainProxyError(502, "domain_response_incomplete", {
        detail: `Response body read failed for ${entry.operationId}`,
      });
    }

    // Validation errors (schema, status, Location) propagate as-is —
    // they are already DomainProxyError with code "invalid_domain_response"
    return this.validateParsedResponse(entry.operationId, entry, response, raw, params.slug);
  }

  throw lastError ?? new GatewayTimeoutError();
}

private isRetryableOperation(
  entry: OperationEntry,
  params: DispatchParams,
): boolean {
  if (entry.method === "GET" || entry.method === "HEAD") {
    return true;
  }
  return entry.requiresIdempotency && params.idempotencyKey !== undefined;
}
```

Key design decisions:

- The deadline is created before handler I/O and passed through every
  asynchronous Interface. A late Prisma result is discarded; abortable HTTP
  and stream operations receive the signal directly.
- **Phase 1 / Phase 2 split:** `undici.request()` resolves when response
  headers arrive — the body is a readable stream, not yet consumed.
  Only Phase 1 failures (no headers received) may trigger retry.
  Once Phase 2 begins (headers received), body-read failures produce
  a `domain_response_incomplete` error, never a retry.
- Token issuance happens **inside** the attempt loop — each attempt
  gets a fresh ≤60-second token, since a prior token may have expired
  during backoff.
- `deadline.aborted` is checked before each retry.
- Timeout surfaces as Problem Details with code `gateway_timeout`.

### 4.9 Retry Policy

Automatic retry is **only** allowed for:

1. **Safe methods** (GET/HEAD) — inherently idempotent.
2. **Non-safe methods that carry an `Idempotency-Key`** — the server
   guarantees at-most-once execution for the same key.

All other non-safe requests are **never retried**. A POST without an
idempotency key could create duplicate resources.

| Condition | Action |
|---|---|
| Network error before any response received (ECONNREFUSED, ECONNRESET, socket timeout) | Retry if retryable operation (see above) |
| Complete HTTP response received (any status, including 5xx) | **Never retry** — return to browser |
| Deadline exceeded | Return 504 `gateway_timeout` |

The key difference from a naive retry: once `undici` has received a
complete HTTP response (headers + body), the proxy does not retry.
A 502/503/504 with a response body is returned to the browser as-is.
The browser or user can decide whether to retry. This prevents silent
replay of commands that may have already taken effect.

Additionally:
- Invalid `Location` header → 502 `invalid_domain_response` (not silently stripped)
- Missing response validator → 502 `invalid_domain_response` (fail closed)
- Response body exceeding `maxResponseBytes` → 502 `invalid_domain_response` (not truncated)

```typescript
function isRetryableError(error: unknown): boolean {
  if (error instanceof Error) {
    return (
      error.message.includes('ECONNREFUSED') ||
      error.message.includes('ECONNRESET') ||
      error.message.includes('UND_ERR_CONNECT_TIMEOUT') ||
      error.message.includes('UND_ERR_SOCKET')
    );
  }
  return false;
}

async function backoffWithJitter(
  attempt: number,
  deadline: AbortSignal
): Promise<void> {
  const baseMs = 100 * Math.pow(2, attempt);
  const jitterMs = Math.random() * baseMs;
  const delayMs = Math.min(baseMs + jitterMs, 2_000);
  await new Promise<void>((resolve, reject) => {
    const timer = setTimeout(resolve, delayMs);
    deadline.addEventListener('abort', () => {
      clearTimeout(timer);
      reject(new GatewayTimeoutError());
    }, { once: true });
  });
}
```

Maximum 1 retry (2 total attempts). Backoff capped at 2 seconds,
controlled by the same deadline `AbortSignal`.

### 4.10 Domain Proxy Handler

The catch-all API route assembles the full pipeline:

```typescript
// pages/api/domain/v1/w/[slug]/[...path].ts

export const config = {
  api: {
    bodyParser: false,
  },
};

export default async function handler(
  req: NextApiRequest,
  res: NextApiResponse
): Promise<void> {
  // 1. Correlation and one absolute deadline — before any I/O
  const deadline = AbortSignal.timeout(env.totalDeadlineMs); // TOTAL_DEADLINE_MS, default 55_000
  const requestId = generateRequestId();
  const traceId = normalizeBrowserTraceId(req.headers['x-trace-id']) ?? requestId;

  try {
  const appUrl = new URL(env.appUrl);

  // 2. Host validation
  validateHost(req, appUrl);

  // 3. Path normalization — before route matching or session access
  const rawSlug = req.query.slug;
  const rawPath = req.query.path;
  if (
    typeof rawSlug !== 'string'
    || !Array.isArray(rawPath)
    || !rawPath.every((segment): segment is string => typeof segment === 'string')
    || !validatePathSegments(rawSlug, rawPath)
  ) {
    res.status(400).json(problemDetails({
      type: 'urn:marketai:error:invalid-path',
      title: 'Invalid path',
      status: 400,
      code: 'invalid_path',
      traceId,
    }));
    return;
  }
  const slug = rawSlug;
  const pathSegments = rawPath;

  // 4. Route matching — before session (avoid DB hit for unknown routes)
  const matchedEntry = matchAllowlistEntry(req.method, pathSegments);
  if (!matchedEntry) {
    res.status(404).json(problemDetails({
      type: 'urn:marketai:error:route-not-found',
      title: 'Route not found',
      status: 404,
      code: 'route_not_found',
      detail: 'No matching domain operation.',
      traceId,
    }));
    return;
  }

  // 5. Origin validation — non-safe methods only (GET/HEAD omit Origin per Fetch spec)
  validateOriginIfNonSafe(req, appUrl);

  // 6. Session verification
  const session = await awaitWithinDeadline(requireSession(req, res), deadline);

  // 7. CSRF verification — non-safe methods only
  await awaitWithinDeadline(csrfVerifier.verify(req, session), deadline);

  // 8. Query validation — parse from raw URL, not req.query (req.query includes slug, path[], etc.)
  // Extract actual search params from the raw URL independently.
  // Object.fromEntries collapses duplicate keys to last-wins — the validated
  // (single-valued) record is what we forward, so "validated content" and
  // "sent content" are always identical.
  const rawSearchParams = extractSearchParams(req.url);
  let validatedQuery = new URLSearchParams();
  if (matchedEntry.entry.querySchema) {
    const collapsed = Object.fromEntries(rawSearchParams);
    const parsed = matchedEntry.entry.querySchema.safeParse(collapsed);
    if (!parsed.success) {
      res.status(400).json(problemDetails({
        type: 'urn:marketai:error:invalid-query',
        title: 'Invalid query parameters',
        status: 400,
        code: 'invalid_query',
        detail: 'Query parameters failed validation.',
        traceId,
        fieldErrors: zodToFieldErrors(parsed.error),
      }));
      return;
    }
    validatedQuery = serializeValidatedQuery(parsed.data);
  } else if (rawSearchParams.size > 0) {
    res.status(400).json(problemDetails({
      type: 'urn:marketai:error:unexpected-query',
      title: 'Unexpected query parameters',
      status: 400,
      code: 'unexpected_query',
      detail: 'This operation does not accept query parameters.',
      traceId,
    }));
    return;
  }

  // 9. Idempotency
  let idempotencyKey: string | undefined;
  if (matchedEntry.entry.requiresIdempotency) {
    const intentId = req.headers['x-intent-id'];
    if (typeof intentId !== 'string' || intentId.length === 0) {
      res.status(400).json(problemDetails({
        type: 'urn:marketai:error:missing-intent-id',
        title: 'Missing intent ID',
        status: 400,
        code: 'missing_intent_id',
        detail: 'X-Intent-Id header is required for this operation.',
        traceId,
      }));
      return;
    }
    idempotencyKey = deriveIdempotencyKey(
      matchedEntry.entry.operationId,
      intentId
    );
  }

  // 10. Body reading, Content-Type validation, and request body validation
  let parsedBody: AnyRequestBody | undefined;
  if (matchedEntry.entry.requestMediaTypes.length > 0) {
    const contentType = (req.headers["content-type"] ?? "").split(";")[0].trim().toLowerCase();
    if (!matchedEntry.entry.requestMediaTypes.includes(contentType)) {
      res.status(415).json(problemDetails({
        type: "urn:marketai:error:unsupported-media-type",
        title: "Unsupported media type",
        status: 415,
        code: "unsupported_media_type",
        detail: `Expected one of: ${matchedEntry.entry.requestMediaTypes.join(", ")}`,
        traceId,
      }));
      return;
    }

    const rawBody = await readLimitedBody(
      req,
      matchedEntry.entry.maxRequestBytes,
      deadline,
    );

    let candidateBody: unknown;
    try {
      candidateBody = JSON.parse(rawBody.toString("utf-8"));
    } catch {
      res.status(400).json(problemDetails({
        type: "urn:marketai:error:invalid-json",
        title: "Invalid JSON",
        status: 400,
        code: "invalid_json",
        traceId,
      }));
      return;
    }

    const requestValidator = matchedEntry.entry.requestBodyValidator;
    if (requestValidator === null) {
      // Generated binding/typecheck should make this unreachable. Keep the
      // runtime boundary fail-closed if generated artifacts are corrupted.
      throw new ApiError(500, "bff_contract_misconfigured");
    }
    if (!requestValidator(candidateBody)) {
      res.status(422).json(problemDetails({
        type: "urn:marketai:error:request-validation-failed",
        title: "Request body validation failed",
        status: 422,
        code: "request_validation_failed",
        detail: "Request body does not match the expected schema.",
        traceId,
        fieldErrors: ajvToFieldErrors(requestValidator.errors),
      }));
      return;
    }
    // Ajv's generated type predicate narrows candidateBody to the request-body
    // union associated with the matched body-bearing operation.
    parsedBody = candidateBody;
  } else {
    await assertNoRequestBody(req, deadline);
  }

  // 11. Extract conditional headers (If-Match, If-None-Match) when allowlist permits
  const conditionalHeaders: Record<string, string> = {};
  if (matchedEntry.entry.forwardConditionalHeaders) {
    const ifMatch = req.headers["if-match"];
    if (typeof ifMatch === "string") conditionalHeaders["If-Match"] = ifMatch;
    const ifNoneMatch = req.headers["if-none-match"];
    if (typeof ifNoneMatch === "string") conditionalHeaders["If-None-Match"] = ifNoneMatch;
  }

  // 12. Proxy to FastAPI via DomainProxy (§6.7) with deadline, retries, token issuance
  const proxyParams = {
    tokenIssuer,
    req,
    res,
    slug,
    pathParams: matchedEntry.extractedParams,
    query: validatedQuery,
    signal: deadline,
    idempotencyKey,
    requestId,
    traceId,
    conditionalHeaders,
  };
  let proxyResponse: ValidatedResponse<keyof DomainOperationMap>;
  if (matchedEntry.entry.requestBodyValidator === null) {
    proxyResponse = await domainProxy.forward(
      matchedEntry.entry.operationId,
      proxyParams,
    );
  } else {
    if (parsedBody === undefined) {
      throw new ApiError(500, "bff_contract_misconfigured");
    }
    proxyResponse = await domainProxy.forward(matchedEntry.entry.operationId, {
      ...proxyParams,
      body: parsedBody,
    });
  }

  // 13. Send validated response to browser
  if (deadline.aborted) throw new GatewayTimeoutError();
  writeProxyResponse(res, proxyResponse, traceId);
  } catch (error) {
    if (res.headersSent) return;
    if (error instanceof GatewayTimeoutError || deadline.aborted) {
      writeProblemResponse(res, 504, 'gateway_timeout', traceId, true);
      return;
    }
    if (error instanceof DomainProxyError || error instanceof ApiError) {
      writeProblemResponse(res, error.status, error.code, traceId, error.retryable);
      return;
    }
    writeProblemResponse(res, 502, 'domain_unavailable', traceId, true);
  }
}
```

`normalizeBrowserTraceId` accepts only the documented trace-ID format and
length; malformed browser values are ignored. `serializeValidatedQuery` uses
Zod's parsed output, not the pre-validation record, so defaults, coercions,
stripping, and normalization cannot diverge from what is forwarded.

The generated `AllowlistEntry` is a discriminated union keyed by
`operationId`, and each request validator is emitted as
`ValidateFunction<RequestBody<K>>`. `matchAllowlistEntry` preserves that
generic key. After Ajv succeeds, its type predicate narrows `parsedBody` to
`RequestBody<K>` before the call to `DomainProxy.forward`; the implementation
does not cast an arbitrary `unknown` body into `BodyParam<K>`. No-body entries
take the `assertNoRequestBody` branch and omit the `body` property entirely.

`writeProxyResponse` serializes the validated body exactly once, recomputes
`Content-Length`, installs the BFF `X-Trace-Id`, copies only the already
filtered response headers, and writes zero bytes for 204/304. It never uses the
upstream `Content-Length` or `X-Trace-Id`.

---

## 5. Operation Model & Idempotency

### 5.1 Operation as a Persistent Deep Module

An Operation is the user-facing representation of an asynchronous
domain command. It is a persistent database record with an append-only
audit trail — not a transient in-memory Pydantic model.

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal


class OperationState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PARTIALLY_SUCCEEDED = "partially_succeeded"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain"
```

#### OperationRecord

```python
@dataclass
class OperationRecord:
    id: str                                    # "op_" + ULID
    workspace_id: str
    initiator_id: str
    command_type: str                          # static per endpoint
    state: OperationState
    idempotency_key_hash: str | None
    result_ref: OperationResultRef | None
    created_at: datetime
    updated_at: datetime
```

#### OperationStateEvent

Every state transition appends an immutable event record. The history
is never rewritten.

```python
@dataclass
class OperationStateEvent:
    id: str                                    # "ose_" + ULID
    operation_id: str
    from_state: OperationState | None          # None for initial creation
    to_state: OperationState
    reason: str | None
    actor: str                                 # "system" or user ID
    created_at: datetime
```

#### OperationResultRef

The result reference uses resource type and ID — not a URL. The BFF
does not rewrite response bodies, so URLs pointing to private FastAPI
paths would be unusable by the browser. The browser constructs its own
navigation path from the resource type and ID.

```python
@dataclass(frozen=True)
class OperationResultRef:
    resource_type: str                         # "asset_version", "experiment", etc.
    resource_id: str                           # "av_01J...", "exp_01J...", etc.
```

### 5.2 State Machine

The Operation state machine defines valid transitions. Any transition
not in this table is rejected.

```
                    ┌──────────────────────────┐
                    │          queued           │
                    └────────────┬─────────────┘
                                 │
                                 ▼
                    ┌──────────────────────────┐
               ┌───►│         running          │◄──────────┐
               │    └──┬───┬───┬───┬───────────┘           │
               │       │   │   │   │                       │
               │       │   │   │   └──► uncertain ─────────┘
               │       │   │   │        (reconciler only)
               │       │   │   │
               │       ▼   │   ▼
               │  succeeded │  failed
               │            │
               │            ▼
               │   partially_succeeded
               │
               └── cancelled (from queued or running)
```

| From | To | Trigger |
|---|---|---|
| `queued` | `running` | Worker picks up Job |
| `queued` | `cancelled` | User cancellation before work starts |
| `running` | `succeeded` | All work completed successfully |
| `running` | `failed` | Work completed with unrecoverable error |
| `running` | `partially_succeeded` | Some items succeeded, some failed |
| `running` | `cancelled` | User cancellation during work |
| `running` | `uncertain` | External call returned ambiguous result (timeout, partial response) |
| `uncertain` | `succeeded` | Reconciler confirms success |
| `uncertain` | `failed` | Reconciler confirms failure |
| `uncertain` | `cancelled` | Reconciler confirms cancellation |
| `uncertain` | `running` | Reconciler determines work can continue |

**`uncertain` is non-terminal.** It represents an isolation state where
the outcome of an external side-effect (Provider call, payment, Meta
API) is unknown. Only the reconciler process can exit this state —
neither the user nor a retry mechanism can transition out of
`uncertain`. The reconciler inspects external evidence (transaction
logs, Provider callbacks, stored receipts) to determine the actual
outcome.

Backward transitions are prohibited except `uncertain → running`
(reconciler determines work can safely resume). No silent state
rollbacks.

### 5.3 Idempotency Records

Two idempotency patterns coexist in #17:

1. **Async commands** (e.g. `finalize_upload` → 202 + Operation): the idempotency record points to an `OperationRecord`.
2. **Sync resource creation** (e.g. `create_upload_intent` → 201): the idempotency record points to the created resource directly (no Operation).

The `operation_id` is therefore **nullable**. When null, `response_resource_type` and `response_resource_id` identify the created resource for replay.

```python
@dataclass
class IdempotencyRecord:
    key_hash: str                              # SHA-256 hex of raw Idempotency-Key
    workspace_id: str
    actor_id: str
    command_type: str
    request_fingerprint_hash: str              # SHA-256 of canonical fingerprint (see below)
    state: str                                 # "pending" or "committed"
    operation_id: str | None                   # FK to operation_records (nullable for sync 201)
    response_resource_type: str | None         # e.g. "upload_intent", "download_intent"
    response_resource_id: str | None           # e.g. "ui_01J..."
    response_status: int | None                # HTTP status; None while state="pending"
    created_at: datetime
    expires_at: datetime
```

**Request fingerprint** includes more than just the body — it is the SHA-256 of a canonical representation of the full request identity:

```python
def compute_fingerprint(
    method: str,
    operation_id: str,
    path: str,
    query: dict,
    body: dict | None,
    precondition_headers: dict[str, str],
) -> str:
    canonical = json.dumps(
        {
            "method": method,
            "operation_id": operation_id,
            "path": path,
            "query": dict(sorted(query.items())),
            "body": body,
            "precondition": dict(sorted(precondition_headers.items())),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
```

This ensures that the same idempotency key used with different methods, paths, query params, or precondition headers (`If-Match`) is detected as a reuse conflict.

**Database schema constraints:**

```sql
CREATE TABLE idempotency_records (
    key_hash                 text        NOT NULL,
    workspace_id             text        NOT NULL,
    actor_id                 text        NOT NULL,
    command_type             text        NOT NULL,
    request_fingerprint_hash text        NOT NULL,
    state                    text        NOT NULL DEFAULT 'pending'
                             CHECK (state IN ('pending', 'committed')),
    operation_id             text        REFERENCES operation_records(id),
    response_resource_type   text,
    response_resource_id     text,
    response_status          integer,
    created_at               timestamptz NOT NULL DEFAULT now(),
    expires_at               timestamptz NOT NULL,

    PRIMARY KEY (key_hash, workspace_id),
    CONSTRAINT idempotency_actor_binding
        CHECK (actor_id != '' AND command_type != ''),
    CONSTRAINT idempotency_pending_has_no_outcome
        CHECK (
            state != 'pending'
            OR (response_status IS NULL
                AND operation_id IS NULL
                AND response_resource_type IS NULL
                AND response_resource_id IS NULL)
        ),
    CONSTRAINT idempotency_committed_has_outcome
        CHECK (
            state != 'committed'
            OR (response_status IS NOT NULL
                AND (
                    (operation_id IS NOT NULL
                     AND response_resource_type IS NULL
                     AND response_resource_id IS NULL)
                    OR
                    (operation_id IS NULL
                     AND response_resource_type IS NOT NULL
                     AND response_resource_id IS NOT NULL)
                ))
        )
);

CREATE INDEX idx_idempotency_expires ON idempotency_records (expires_at);
```

The unique key is `(key_hash, workspace_id)`. Actor and command type binding is enforced by comparing `actor_id` and `command_type` on replay — a mismatch returns 409 `idempotency_key_reused`, not a silent success. The CHECK constraint ensures these fields are never empty.

The `state` column distinguishes a **pending reservation** (slot claimed, outcome not yet produced) from a **committed record** (outcome bound). Two CHECK constraints enforce the state invariants:

- `idempotency_pending_has_no_outcome`: a pending record must have `response_status`, `operation_id`, `response_resource_type`, and `response_resource_id` all NULL. This prevents partial writes from leaving outcome data on an uncommitted reservation.

- `idempotency_committed_has_outcome`: a committed record always has `response_status` and binds **exactly one** outcome — either `operation_id` (async 202 path, with resource fields NULL) **or** `response_resource_type + response_resource_id` (sync 201 path, with `operation_id` NULL). The XOR prevents both being set simultaneously.

### 5.4 Idempotency Concurrency Protocol

The protocol uses a **pending reservation** pattern to ensure exactly one outcome per idempotency key, even under concurrent requests.

**Why not ON CONFLICT DO NOTHING?** With `DO NOTHING`, two concurrent transactions can both create their own Operation/resource — the loser's `INSERT` into `idempotency_records` silently fails, but its Operation/resource is already committed. The system ends up with two Operations for the same idempotency key.

**Reservation protocol:** The first step is always to claim a pending reservation (nullable outcome). If the INSERT succeeds, this transaction owns the key — it creates the outcome and binds it. If the INSERT conflicts, a concurrent transaction already claimed the key — this transaction waits for the winner to commit, then reads the winner's result.

#### Async command path (202 + Operation)

```python
def create_or_replay_operation(
    *,
    idempotency_key: str | None,
    workspace_id: str,
    actor_id: str,
    command_type: str,
    fingerprint_hash: str,
    job_params: dict,
    session: Session,
) -> OperationRecord:
    if idempotency_key is None:
        return _create_fresh_operation(
            workspace_id=workspace_id,
            actor_id=actor_id,
            command_type=command_type,
            job_params=job_params,
            session=session,
        )

    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()

    # 1. Check for existing committed record (fast path — no contention)
    existing = session.execute(
        text("""
            SELECT actor_id, command_type, request_fingerprint_hash,
                   state, operation_id
            FROM idempotency_records
            WHERE key_hash = :key_hash AND workspace_id = :workspace_id
        """),
        {"key_hash": key_hash, "workspace_id": workspace_id},
    ).mappings().first()

    if existing is not None:
        if existing["actor_id"] != actor_id or existing["command_type"] != command_type:
            raise IdempotencyKeyReusedError(key_hash=key_hash)
        if existing["request_fingerprint_hash"] != fingerprint_hash:
            raise IdempotencyKeyReusedError(key_hash=key_hash)
        if existing["state"] == "committed":
            return _get_operation(existing["operation_id"], session)
        # pending — another transaction is creating the outcome.
        # Wait for it to commit (see below).

    # 2. Claim pending reservation — ON CONFLICT means another transaction won
    result = session.execute(
        text("""
            INSERT INTO idempotency_records (
                key_hash, workspace_id, actor_id, command_type,
                request_fingerprint_hash, state, expires_at
            )
            VALUES (
                :key_hash, :workspace_id, :actor_id, :command_type,
                :fingerprint_hash, 'pending', :expires_at
            )
            ON CONFLICT (key_hash, workspace_id) DO NOTHING
            RETURNING key_hash
        """),
        {
            "key_hash": key_hash,
            "workspace_id": workspace_id,
            "actor_id": actor_id,
            "command_type": command_type,
            "fingerprint_hash": fingerprint_hash,
            "expires_at": datetime.utcnow() + timedelta(hours=24),
        },
    ).first()

    if result is not None:
        # We won the reservation — create Operation and bind it
        operation = _create_fresh_operation(
            workspace_id=workspace_id,
            actor_id=actor_id,
            command_type=command_type,
            job_params=job_params,
            session=session,
            idempotency_key_hash=key_hash,
        )
        session.execute(
            text("""
                UPDATE idempotency_records
                SET state = 'committed',
                    operation_id = :operation_id,
                    response_status = 202
                WHERE key_hash = :key_hash AND workspace_id = :workspace_id
            """),
            {
                "key_hash": key_hash,
                "operation_id": operation.id,
                "workspace_id": workspace_id,
            },
        )
        return operation

    # Lost the reservation — wait for winner to commit, then read result
    return _wait_and_read_winner_operation(
        key_hash, workspace_id, actor_id, command_type, fingerprint_hash, session,
    )


def _wait_and_read_winner_operation(
    key_hash: str,
    workspace_id: str,
    actor_id: str,
    command_type: str,
    fingerprint_hash: str,
    session: Session,
    max_wait_seconds: float = 5.0,
    poll_interval: float = 0.1,
) -> OperationRecord:
    deadline = time.monotonic() + max_wait_seconds
    while time.monotonic() < deadline:
        row = session.execute(
            text("""
                SELECT actor_id, command_type, request_fingerprint_hash,
                       state, operation_id
                FROM idempotency_records
                WHERE key_hash = :key_hash AND workspace_id = :workspace_id
            """),
            {"key_hash": key_hash, "workspace_id": workspace_id},
        ).mappings().first()

        if row is None:
            raise IdempotencyReservationLostError(key_hash)
        if row["actor_id"] != actor_id or row["command_type"] != command_type:
            raise IdempotencyKeyReusedError(key_hash=key_hash)
        if row["request_fingerprint_hash"] != fingerprint_hash:
            raise IdempotencyKeyReusedError(key_hash=key_hash)
        if row["state"] == "committed":
            return _get_operation(row["operation_id"], session)

        time.sleep(poll_interval)

    raise IdempotencyTimeoutError(key_hash)
```

#### Sync resource path (201 — upload/download intents)

Same reservation protocol with `operation_id = NULL`, binding `resource_type + resource_id` instead:

```python
def create_or_replay_intent(
    *,
    idempotency_key: str,
    workspace_id: str,
    actor_id: str,
    command_type: str,
    fingerprint_hash: str,
    create_fn: Callable[[Session], tuple[str, str, Any]],
    session: Session,
) -> Any:
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()

    existing = session.execute(
        text("""
            SELECT actor_id, command_type, request_fingerprint_hash,
                   state, response_resource_type, response_resource_id
            FROM idempotency_records
            WHERE key_hash = :key_hash AND workspace_id = :workspace_id
        """),
        {"key_hash": key_hash, "workspace_id": workspace_id},
    ).mappings().first()

    if existing is not None:
        if existing["actor_id"] != actor_id or existing["command_type"] != command_type:
            raise IdempotencyKeyReusedError(key_hash=key_hash)
        if existing["request_fingerprint_hash"] != fingerprint_hash:
            raise IdempotencyKeyReusedError(key_hash=key_hash)
        if existing["state"] == "committed":
            return _replay_intent(
                existing["response_resource_type"],
                existing["response_resource_id"],
                session,
            )
        # pending — fall through to wait

    # Claim pending reservation
    result = session.execute(
        text("""
            INSERT INTO idempotency_records (
                key_hash, workspace_id, actor_id, command_type,
                request_fingerprint_hash, state, expires_at
            )
            VALUES (
                :key_hash, :workspace_id, :actor_id, :command_type,
                :fingerprint_hash, 'pending', :expires_at
            )
            ON CONFLICT (key_hash, workspace_id) DO NOTHING
            RETURNING key_hash
        """),
        {
            "key_hash": key_hash,
            "workspace_id": workspace_id,
            "actor_id": actor_id,
            "command_type": command_type,
            "fingerprint_hash": fingerprint_hash,
            "expires_at": datetime.utcnow() + timedelta(hours=24),
        },
    ).first()

    if result is not None:
        resource_type, resource_id, intent_result = create_fn(session)
        session.execute(
            text("""
                UPDATE idempotency_records
                SET state = 'committed',
                    response_resource_type = :resource_type,
                    response_resource_id = :resource_id,
                    response_status = 201
                WHERE key_hash = :key_hash AND workspace_id = :workspace_id
            """),
            {
                "key_hash": key_hash,
                "workspace_id": workspace_id,
                "resource_type": resource_type,
                "resource_id": resource_id,
            },
        )
        return intent_result

    # Lost reservation — wait for winner
    return _wait_and_read_winner_intent(
        key_hash, workspace_id, actor_id, command_type, fingerprint_hash, session,
    )
```

### 5.5 Transactional Creation

`OperationRecord`, `IdempotencyRecord` (if idempotent), `Job`, and the
initial `OperationStateEvent` are all created in a single database
transaction. If any step fails, the entire transaction rolls back —
there are no orphaned Operations without Jobs or vice versa.

```python
def _create_fresh_operation(
    *,
    workspace_id: str,
    actor_id: str,
    command_type: str,
    job_params: dict,
    session: Session,
    operation_id: str | None = None,
    idempotency_key_hash: str | None = None,
) -> OperationRecord:
    op_id = operation_id or generate_operation_id()
    now = datetime.utcnow()

    operation = OperationRecord(
        id=op_id,
        workspace_id=workspace_id,
        initiator_id=actor_id,
        command_type=command_type,
        state=OperationState.QUEUED,
        idempotency_key_hash=idempotency_key_hash,
        result_ref=None,
        created_at=now,
        updated_at=now,
    )

    initial_event = OperationStateEvent(
        id=generate_event_id(),
        operation_id=op_id,
        from_state=None,
        to_state=OperationState.QUEUED,
        reason="Operation created",
        actor=actor_id,
        created_at=now,
    )

    job = Job(
        id=generate_job_id(),
        operation_id=op_id,
        workspace_id=workspace_id,
        command_type=command_type,
        params=job_params,
        state=JobState.PENDING,
        created_at=now,
    )

    session.add(operation)
    session.add(initial_event)
    session.add(job)

    return operation
```

### 5.6 Operation Polling

`GET /api/v1/operations/{operation_id}` returns the current Operation
state with `ETag` for conditional requests and `Retry-After` for
non-terminal states.

```python
@router.get("/operations/{operation_id}")
async def get_operation(
    operation_id: str,
    request: Request,
    tenant: TenantContext = Depends(request_tenant_context),
) -> Response:
    operation = await operation_repository.get(
        operation_id=operation_id,
        workspace_id=tenant.workspace_id,
    )
    if operation is None:
        raise HTTPException(
            status_code=404,
            detail=problem_details(
                type="urn:marketai:error:not-found",
                title="Operation not found",
                status=404,
                code="operation_not_found",
                trace_id=request.state.trace_id,
            ),
        )

    body = operation_to_response(operation)
    etag = compute_etag(body)

    if_none_match = request.headers.get("if-none-match")
    if if_none_match and if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag})

    headers: dict[str, str] = {"ETag": etag}

    if operation.state in (
        OperationState.QUEUED,
        OperationState.RUNNING,
        OperationState.UNCERTAIN,
    ):
        headers["Retry-After"] = str(
            compute_retry_after_seconds(operation)
        )

    return JSONResponse(
        content=body,
        status_code=200,
        headers=headers,
    )
```

Each endpoint that creates Operations declares its `command_type`
statically — there is no dynamic command type registration.

### 5.7 Coexistence with Existing Routes

**#17 does NOT delete existing routes.** The new versioned router at
`/api/v1/` is added alongside the existing unversioned `/api/*`
routes. Both routers coexist in the same FastAPI application. Gradual
migration of existing endpoints to the v1 router happens in subsequent
tickets.

```python
# app/main.py — after #17
app.include_router(v1_router, prefix="/api/v1")
# Existing routes remain at /api/* unchanged
```

### 5.8 Response Extensibility

**Adding values to response enums is a breaking change** unless the
field is explicitly marked as extensible. `OperationState` is NOT
marked extensible in v1 — adding a new state value would break
TypeScript consumers that use exhaustive `switch` statements.

If a new state is required in the future, it must be introduced as a
new API version or the field must be retroactively marked extensible
(with an `_unknown` fallback variant added to the TypeScript union)
in a coordinated release.

### 5.9 Problem Details (RFC 9457)

All 4xx and 5xx responses use `application/problem+json` with
MarketAI-specific stable extension fields:

```python
@dataclass
class FieldError:
    field: str
    code: str
    detail: str


@dataclass
class ProblemDetails:
    type: str                     # URN: "urn:marketai:error:<kebab-code>"
    title: str                    # human-readable, stable per type
    status: int                   # HTTP status code
    detail: str                   # per-instance explanation
    instance: str                 # request path
    code: str                     # stable machine-readable error code
    trace_id: str                 # correlates BFF → FastAPI → Worker
    retryable: bool               # whether the client should retry
    field_errors: list[FieldError]  # per-field validation errors (empty if N/A)
```

Required error codes for #17:

| Code | Status | When |
|---|---|---|
| `delegated_identity_required` | 401 | No valid delegated JWT |
| `token_expired` | 401 | JWT `exp` has passed |
| `token_not_yet_valid` | 401 | JWT `nbf` is in the future |
| `invalid_issuer` | 401 | JWT `iss` doesn't match expected |
| `invalid_audience` | 401 | JWT `aud` doesn't match expected |
| `unknown_signing_key` | 401 | JWT `kid` not in JWKS |
| `algorithm_not_allowed` | 401 | JWT `alg` is not `ES256` |
| `invalid_token_type` | 401 | JWT `typ` is not `delegated+jwt` |
| `malformed_token` | 401 | JWT fails structural validation |
| `token_too_large` | 401 | Raw JWT exceeds the configured byte limit |
| `invalid_signature` | 401 | JWT signature verification fails |
| `token_ttl_exceeded` | 401 | `exp - iat` exceeds the configured maximum |
| `invalid_jti_format` | 401 | `jti` does not match `jti_<uuid>` |
| `invalid_sub` | 401 | `sub` is empty, wrong type, or too long |
| `invalid_workspace_id` | 401 | `workspace_id` is empty, wrong type, or too long |
| `invalid_membership_id` | 401 | `membership_id` is empty, wrong type, or too long |
| `invalid_role` | 401 | `role` is not `owner` or `member` |
| `invalid_session_id` | 401 | `session_id` is empty, wrong type, or too long |
| `invalid_authorization_epoch` | 401 | Authorization epoch claim is not a non-negative integer |
| `invalid_entitlement_version` | 401 | Entitlement version claim is not a non-negative integer |
| `unexpected_claims` | 401 | JWT contains claims outside the fixed 13-claim contract |
| `jwks_unavailable` | 503 | JWKS is not loaded/hard-stale, or a required unknown-key refresh fails |
| `authorization_epoch_stale` | 403 | JWT epoch behind workspace projection |
| `authorization_sync_pending` | 403 | JWT epoch ahead of workspace projection (domain needs sync) |
| `entitlement_sync_pending` | 403 | Entitlement version ahead on non-safe method |
| `workspace_not_found` | 401 | Workspace ID from JWT has no record (401, not 404, to avoid leaking workspace existence) |
| `workspace_membership_required` | 403 | Session user has no active membership for the requested workspace |
| `workspace_suspended` | 403 | Workspace is suspended |
| `operation_not_found` | 404 | Operation ID does not exist or wrong workspace |
| `idempotency_key_reused` | 409 | Same key, different request fingerprint |
| `revision_conflict` | 409 | ETag/revision mismatch |
| `upload_intent_expired` | 410 | Upload intent past expiry |
| `download_intent_expired` | 410 | Download intent past expiry |
| `missing_host` | 400 | BFF request omits the Host header |
| `invalid_host` | 400 | BFF Host does not exactly match configured `APP_URL` |
| `missing_origin` | 403 | Non-safe BFF request omits Origin |
| `origin_not_allowed` | 403 | Non-safe BFF Origin does not exactly match `APP_URL` |
| `missing_intent_id` | 400 | `X-Intent-Id` required but not provided |
| `invalid_path` | 400 | BFF slug/path segments fail canonical validation |
| `route_not_found` | 404 | No matching allowlist entry |
| `invalid_query` | 400 | Query parameters fail the generated schema |
| `unexpected_query` | 400 | Operation declares no query but parameters were supplied |
| `unexpected_body` | 400 | Operation declares no request body but bytes were supplied |
| `request_body_too_large` | 413 | Request exceeds the operation's generated byte limit |
| `unsupported_media_type` | 415 | Request Content-Type is not declared for the operation |
| `invalid_json` | 400 | Declared JSON request body cannot be parsed |
| `request_validation_failed` | 422 | Parsed request body fails the generated validator |
| `bff_contract_misconfigured` | 500 | Generated allowlist/validator invariant is broken at runtime (startup checks should prevent this) |
| `invalid_domain_response` | 502 | FastAPI response failed validation |
| `domain_response_incomplete` | 502 | FastAPI sent headers but its response body could not be read completely |
| `domain_unavailable` | 502 | Unexpected BFF/domain transport failure |
| `projection_sync_unavailable` | 503 | Authorization snapshot repair failed transiently |
| `gateway_timeout` | 504 | Absolute deadline exceeded |
| `csrf_validation_failed` | 403 | CSRF token invalid or missing |
| `mime_type_not_allowed` | 400 | File MIME type not in purpose allowlist |
| `object_replaced_during_verification` | 409 | Storage object changed between inspect and commit |

---

## 6. Generated Contract Bindings & DomainGateway

### 6.1 Layer Separation

The project uses **Pages Router** (not App Router). API Routes are server-only Node.js handlers. Browser components cannot access `req`/`res`, JWT issuers, or `DOMAIN_API_BASE_URL`.

| Layer | Runtime | Module | Responsibility |
|---|---|---|---|
| Browser component | Client | `DomainGateway` | Same-origin fetch to BFF, CSRF token, intent tracking, trace correlation |
| `pages/api/domain/v1/w/[slug]/[...path].ts` | Server | `DomainProxy` | Host/Origin/CSRF gate, session verification, JWT issuance, FastAPI dispatch, response validation, Location rewrite |

`DomainGateway` and `DomainProxy` share nothing at runtime. The only shared artifact is the HTTP contract defined by the BFF allowlist and the generated type/registry bindings.

### 6.2 Generated Contract Bindings

Seven committed artifacts, all CI-checked for drift against `contracts/openapi/marketai-domain-v1.json`. Generation pipeline: `scripts/generate_all.sh` invokes each generator in sequence. CI job runs `scripts/generate_all.sh` and `git diff --exit-code` on the output directory.

#### 6.2.1 Type Definitions

**File:** `web/lib/domain-client/generated/domain-v1.d.ts`
**Generator:** `openapi-typescript` (version-pinned in `package.json`)
**Runtime cost:** Zero — type-only `.d.ts`, erased at build.

Contains all request/response interfaces extracted from the OpenAPI 3.1 spec. Downstream artifacts (`operation-map.d.ts`, `validators.ts`) reference these types.

#### 6.2.2 Operation Map

**File:** `web/lib/domain-client/generated/operation-map.d.ts`
**Generator:** `scripts/generate_operation_map.ts`
**Runtime cost:** Zero — type-only.

Provides per-operation input/output type safety with **per-status success response mapping**:

```typescript
export type DomainOperationMap = {
  get_identity_probe: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: {
      200: IdentityProbeResponse;
    };
  };
  create_upload_intent: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: CreateUploadIntentRequest;
    successResponses: {
      201: UploadIntentResponse;
    };
  };
  finalize_upload: {
    pathParams: { intent_id: string };
    queryParams: Record<string, never>;
    requestBody: FinalizeUploadRequest;
    successResponses: {
      202: OperationResponse;
    };
  };
  get_operation: {
    pathParams: { operation_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: {
      200: OperationDetailResponse;
      304: null;
    };
  };
  create_download_intent: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: CreateDownloadIntentRequest;
    successResponses: {
      201: DownloadIntentResponse;
    };
  };
};
```

`successResponses` is a per-status mapping. Different statuses may have different body types. `requestBody: never` means the operation declares no request body; the consuming interfaces use a conditional type (§6.4) so that `never`-body operations omit the parameter entirely.

#### 6.2.3 Operation Registry

**File:** `web/lib/domain-client/generated/operation-registry.ts`
**Generator:** `scripts/generate_operation_registry.ts`
**Runtime cost:** Loaded at server startup. Used by `DomainProxy` for dispatch, validation, and Location rewriting.

**Single source of truth:** All route tables are derived from two inputs:

1. **OpenAPI spec** (`contracts/openapi/marketai-domain-v1.json`): the authoritative source for operation IDs, methods, paths, path params, query params, request/response schemas, and `requiresIdempotency` (via `x-marketai-idempotent: true` extension).

2. **Policy manifest** (`contracts/openapi/marketai-domain-v1-policy.json`): a hand-written JSON file that declares non-derivable fields per operation — `maxRequestBytes`, `maxResponseBytes`, `forwardConditionalHeaders`, BFF path patterns (`bffPattern`), and query schema overrides. The generator merges policy onto OpenAPI; a missing policy entry is a generation-time error (fail-closed).

The generator script (`scripts/generate_all.sh`) produces **seven** artifacts from these two inputs:

| Generated file | Generator script | Content |
|---|---|---|
| `domain-v1.d.ts` | `npx openapi-typescript` | TypeScript types from OpenAPI schemas |
| `operation-map.d.ts` | `generate_operation_map.ts` | `DomainOperationMap` — typed path/query/body/response per operation |
| `operation-registry.ts` | `generate_operation_registry.ts` | `OPERATION_REGISTRY` — full dispatch table with size limits from policy |
| `validators.ts` | `generate_response_validators.ts` | Pre-compiled Ajv validators for request and response bodies |
| `allowlist-binding.ts` | `generate_allowlist_binding.ts` | Startup validation that `DOMAIN_ALLOWLIST` matches `OPERATION_REGISTRY` |
| `paths.ts` | `generate_paths.ts` | `BFF_ROUTES` — browser-safe projection (no validators, no size limits) with `buildBffPath()` |
| `allowlist.ts` | `generate_allowlist.ts` | `DOMAIN_ALLOWLIST` — server-only fields (Ajv validators, query schemas, size limits, forwarding flags) |

The CI drift check (§6.8) ensures all seven generated files stay in sync with the OpenAPI spec + policy manifest.

```typescript
export type ResponseShape = {
  mediaTypes: readonly string[];
  body: { validatorId: string } | "none";
  locationShapes: readonly LocationShape[];
};

export type OperationEntry = {
  operationId: string;
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  pathTemplate: string;
  pathParams: readonly string[];
  queryParams: readonly string[];
  requestMediaTypes: readonly string[];
  requiresIdempotency: boolean;
  maxRequestBytes: number;
  maxResponseBytes: number;
  responses: Readonly<Record<number, ResponseShape>>;
};

export type LocationShape = {
  pattern: RegExp;
  targetOperationId: string;
  extractParams: readonly string[];
};

// COMMON_ERROR_RESPONSES — the generator merges these into every
// OperationEntry.responses. Shown here for reference; the generated
// output below already includes them in each entry's responses map.
const COMMON_ERROR_RESPONSES: Readonly<Record<number, ResponseShape>> = {
  401: { mediaTypes: ["application/problem+json"], body: { validatorId: "problem_details" }, locationShapes: [] },
  403: { mediaTypes: ["application/problem+json"], body: { validatorId: "problem_details" }, locationShapes: [] },
  409: { mediaTypes: ["application/problem+json"], body: { validatorId: "problem_details" }, locationShapes: [] },
  422: { mediaTypes: ["application/problem+json"], body: { validatorId: "problem_details" }, locationShapes: [] },
  500: { mediaTypes: ["application/problem+json"], body: { validatorId: "problem_details" }, locationShapes: [] },
};

export const OPERATION_REGISTRY: ReadonlyMap<string, OperationEntry> = new Map([
  [
    "get_identity_probe",
    {
      operationId: "get_identity_probe",
      method: "GET",
      pathTemplate: "/api/v1/identity",
      pathParams: [],
      queryParams: [],
      requestMediaTypes: [],
      requiresIdempotency: false,
      maxRequestBytes: 0,
      maxResponseBytes: 65_536,
      responses: {
        // Shared errors are spread first so an operation-specific declaration
        // for the same status wins.
        ...COMMON_ERROR_RESPONSES,
        200: {
          mediaTypes: ["application/json"],
          body: { validatorId: "get_identity_probe_200" },
          locationShapes: [],
        },
      },
    },
  ],
  [
    "create_upload_intent",
    {
      operationId: "create_upload_intent",
      method: "POST",
      pathTemplate: "/api/v1/upload-intents",
      pathParams: [],
      queryParams: [],
      requestMediaTypes: ["application/json"],
      requiresIdempotency: true,
      maxRequestBytes: 8_192,
      maxResponseBytes: 65_536,
      responses: {
        ...COMMON_ERROR_RESPONSES,
        201: {
          mediaTypes: ["application/json"],
          body: { validatorId: "create_upload_intent_201" },
          locationShapes: [],
        },
        410: {
          mediaTypes: ["application/problem+json"],
          body: { validatorId: "problem_details" },
          locationShapes: [],
        },
      },
    },
  ],
  [
    "finalize_upload",
    {
      operationId: "finalize_upload",
      method: "POST",
      pathTemplate: "/api/v1/upload-intents/{intent_id}/finalize",
      pathParams: ["intent_id"],
      queryParams: [],
      requestMediaTypes: ["application/json"],
      requiresIdempotency: true,
      maxRequestBytes: 8_192,
      maxResponseBytes: 65_536,
      responses: {
        ...COMMON_ERROR_RESPONSES,
        202: {
          mediaTypes: ["application/json"],
          body: { validatorId: "finalize_upload_202" },
          locationShapes: [
            {
              pattern: /^\/api\/v1\/operations\/(?<operation_id>op_[A-Za-z0-9]+)$/,
              targetOperationId: "get_operation",
              extractParams: ["operation_id"],
            },
          ],
        },
      },
    },
  ],
  [
    "get_operation",
    {
      operationId: "get_operation",
      method: "GET",
      pathTemplate: "/api/v1/operations/{operation_id}",
      pathParams: ["operation_id"],
      queryParams: [],
      requestMediaTypes: [],
      requiresIdempotency: false,
      maxRequestBytes: 0,
      maxResponseBytes: 65_536,
      responses: {
        ...COMMON_ERROR_RESPONSES,
        200: {
          mediaTypes: ["application/json"],
          body: { validatorId: "get_operation_200" },
          locationShapes: [],
        },
        304: {
          mediaTypes: [],
          body: "none",
          locationShapes: [],
        },
      },
    },
  ],
  [
    "create_download_intent",
    {
      operationId: "create_download_intent",
      method: "POST",
      pathTemplate: "/api/v1/download-intents",
      pathParams: [],
      queryParams: [],
      requestMediaTypes: ["application/json"],
      requiresIdempotency: true,
      maxRequestBytes: 8_192,
      maxResponseBytes: 65_536,
      responses: {
        ...COMMON_ERROR_RESPONSES,
        201: {
          mediaTypes: ["application/json"],
          body: { validatorId: "create_download_intent_201" },
          locationShapes: [],
        },
        410: {
          mediaTypes: ["application/problem+json"],
          body: { validatorId: "problem_details" },
          locationShapes: [],
        },
      },
    },
  ],
]);
```

`responses` is keyed by HTTP status code. Each status declares its own media types, body validator (or `"none"` for 204/304), and Location shapes. The generator merges `COMMON_ERROR_RESPONSES` (401, 403, 409, 422, 500 — all Problem Details) into every entry's `responses` map at generation time. `...COMMON_ERROR_RESPONSES` is shown explicitly in each entry above to prove the merge. An operation that overrides a shared status (e.g., a custom 409 with additional fields) declares it explicitly — the operation-specific entry wins.

The registry does **not** contain BFF paths — only FastAPI-side paths and `targetOperationId`. The BFF allowlist owns browser-facing paths; the binding layer (§6.2.4) connects them.

#### 6.2.4 Allowlist Binding

**File:** `web/lib/domain-client/generated/allowlist-binding.ts`
**Generator:** `scripts/generate_allowlist_binding.ts`
**Runtime cost:** Executed at module load (server startup). Throws on any mismatch — startup error, not runtime surprise.

Validation checks performed at load time:

| Check | Fails when |
|---|---|
| `operationId` resolution | Any `AllowlistEntry.operationId` not in `OPERATION_REGISTRY` |
| Method match | Allowlist method differs from registry method for the same `operationId` |
| Path parameter match | Allowlist path params differ from registry `pathParams` |
| Query parameter coverage | Allowlist `querySchema` does not cover all registry-declared `queryParams` |
| Request media type match | Allowlist `requestMediaTypes` is not a subset of registry's declared request media types |
| Request validator coverage | A body-bearing operation has no generated, strongly typed request validator, or a no-body operation declares one |
| Idempotency consistency | Allowlist `requiresIdempotency: true` but operation does not declare `Idempotency-Key` header, or vice versa |
| Response validation coverage | Every status in registry `responses` has a corresponding validation strategy (validator or `"none"`) |
| Error response contract | Every declared status ≥400 uses `application/problem+json` and a generated Problem Details schema (base or operation-specific subtype) |
| Location reverse mapping | Every `targetOperationId` in any `LocationShape` maps to an existing allowlist entry |

#### 6.2.5 Runtime Response Validators

**File:** `web/lib/domain-client/generated/validators.ts`
**Generator:** `scripts/generate_response_validators.ts`
**Runtime cost:** Loaded at server startup. Each validator is a pre-compiled Ajv standalone function — no runtime schema compilation.

```typescript
import type { ValidateFunction } from "ajv/dist/core";
import type { DomainOperationMap } from "./operation-map";

type OperationId = keyof DomainOperationMap;
type BodyOperationId = {
  [K in OperationId]: [DomainOperationMap[K]["requestBody"]] extends [never]
    ? never
    : K
}[OperationId];

type RequestValidatorMap = {
  [K in BodyOperationId]: ValidateFunction<DomainOperationMap[K]["requestBody"]>;
};

export const RESPONSE_VALIDATORS: ReadonlyMap<string, ValidateFunction> = new Map([
  ["get_identity_probe_200", validateGetIdentityProbe200],
  ["create_upload_intent_201", validateCreateUploadIntent201],
  ["finalize_upload_202", validateFinalizeUpload202],
  ["get_operation_200", validateGetOperation200],
  ["create_download_intent_201", validateCreateDownloadIntent201],
  ["problem_details", validateProblemDetails],
]);

export const REQUEST_VALIDATORS = {
  create_upload_intent: validateCreateUploadIntentRequest,
  finalize_upload: validateFinalizeUploadRequest,
  create_download_intent: validateCreateDownloadIntentRequest,
} satisfies RequestValidatorMap;
```

Generation uses `ajv2020` with `ajv-formats`, producing standalone ESM validator functions via `ajv/dist/standalone`. Ajv version pinned in `package.json` (`"ajv": "8.17.1"`, `"ajv-formats": "3.0.1"`). Input schemas come from `contracts/openapi/marketai-domain-v1.json` response definitions.

When `ResponseShape.body` is `"none"` (204, 304), the validation pipeline checks that the response has zero body bytes. Content-Type is not checked for bodiless responses.

CI drift check: `scripts/generate_all.sh` regenerates all seven artifacts (`domain-v1.d.ts`, `operation-map.d.ts`, `operation-registry.ts`, `validators.ts`, `allowlist-binding.ts`, `paths.ts`, `allowlist.ts`); `git diff --exit-code web/lib/domain-client/generated/` fails the build if any generated file has uncommitted drift.

### 6.3 BFF Path Single Source of Truth

The DomainGateway must not contain hardcoded `/api/domain/...` path strings. The static allowlist manifest exports a client-safe `buildBffPath()` function. Both the server-side route matcher and the browser-side DomainGateway derive paths from the same `bffPattern` definitions.

The allowlist is split into two modules to prevent browser code from importing server-only dependencies (Zod schemas, Ajv validators):

**Client-safe path builder** (`web/lib/domain-client/generated/paths.ts` — imported by browser):
**Generator:** `scripts/generate_paths.ts` (from OpenAPI spec + policy manifest `bffPattern` entries)

```typescript
// web/lib/domain-client/generated/paths.ts — browser-safe, no Zod or Ajv

type BffRoute = {
  readonly operationId: string;
  readonly method: string;
  readonly bffPattern: string;
  readonly pathParams: readonly string[];
  readonly requiresIdempotency: boolean;
};

export const BFF_ROUTES: readonly BffRoute[] = [
  { operationId: "get_identity_probe", method: "GET", bffPattern: "/api/domain/v1/w/:slug/identity", pathParams: [], requiresIdempotency: false },
  { operationId: "create_upload_intent", method: "POST", bffPattern: "/api/domain/v1/w/:slug/upload-intents", pathParams: [], requiresIdempotency: true },
  { operationId: "finalize_upload", method: "POST", bffPattern: "/api/domain/v1/w/:slug/upload-intents/:intent_id/finalize", pathParams: ["intent_id"], requiresIdempotency: true },
  { operationId: "get_operation", method: "GET", bffPattern: "/api/domain/v1/w/:slug/operations/:operation_id", pathParams: ["operation_id"], requiresIdempotency: false },
  { operationId: "create_download_intent", method: "POST", bffPattern: "/api/domain/v1/w/:slug/download-intents", pathParams: [], requiresIdempotency: true },
] as const;

export function buildBffPath(
  operationId: string,
  params: { slug: string } & Record<string, string>
): string {
  const route = BFF_ROUTES.find((r) => r.operationId === operationId);
  if (!route) {
    throw new Error(`Unknown operationId: ${operationId}`);
  }
  let path = route.bffPattern.replace(":slug", encodeURIComponent(params.slug));
  for (const param of route.pathParams) {
    const value = params[param];
    if (value === undefined) {
      throw new Error(`Missing path param "${param}" for ${operationId}`);
    }
    path = path.replace(`:${param}`, encodeURIComponent(value));
  }
  return path;
}
```

**Server-only allowlist** (`web/lib/domain-client/generated/allowlist.ts` — server only, has Zod + Ajv):
**Generator:** `scripts/generate_allowlist.ts` (from OpenAPI spec + policy manifest; merges size limits, query schemas, validator references, forwarding flags)

The full `AllowlistEntry` discriminated union (§4.3) lives in the parent
server-only `allowlist-types.ts` module and is never imported by browser code.
The generated allowlist imports that type plus the sibling generated
validators. The binding (§6.2.4) validates that every `BFF_ROUTES` entry has a
corresponding full allowlist entry at startup.

```typescript
// web/lib/domain-client/generated/allowlist.ts (server-only — has Zod, Ajv validators)
import type { AnyAllowlistEntry } from '../allowlist-types';
import { REQUEST_VALIDATORS } from './validators';

export const DOMAIN_ALLOWLIST = [
  {
    operationId: "get_identity_probe",
    method: "GET",
    bffPattern: "/api/domain/v1/w/:slug/identity",
    fastapiPathTemplate: "/api/v1/identity",
    pathParams: [],
    querySchema: null,
    requestBodyValidator: null,
    requestMediaTypes: [],
    maxRequestBytes: 0,
    maxResponseBytes: 65_536,
    requiresIdempotency: false,
    forwardConditionalHeaders: false,
  },
  {
    operationId: "create_upload_intent",
    method: "POST",
    bffPattern: "/api/domain/v1/w/:slug/upload-intents",
    fastapiPathTemplate: "/api/v1/upload-intents",
    pathParams: [],
    querySchema: null,
    requestBodyValidator: REQUEST_VALIDATORS.create_upload_intent,
    requestMediaTypes: ["application/json"],
    maxRequestBytes: 8_192,
    maxResponseBytes: 65_536,
    requiresIdempotency: true,
    forwardConditionalHeaders: false,
  },
  {
    operationId: "finalize_upload",
    method: "POST",
    bffPattern: "/api/domain/v1/w/:slug/upload-intents/:intent_id/finalize",
    fastapiPathTemplate: "/api/v1/upload-intents/{intent_id}/finalize",
    pathParams: ["intent_id"],
    querySchema: null,
    requestBodyValidator: REQUEST_VALIDATORS.finalize_upload,
    requestMediaTypes: ["application/json"],
    maxRequestBytes: 8_192,
    maxResponseBytes: 65_536,
    requiresIdempotency: true,
    forwardConditionalHeaders: false,
  },
  {
    operationId: "get_operation",
    method: "GET",
    bffPattern: "/api/domain/v1/w/:slug/operations/:operation_id",
    fastapiPathTemplate: "/api/v1/operations/{operation_id}",
    pathParams: ["operation_id"],
    querySchema: null,
    requestBodyValidator: null,
    requestMediaTypes: [],
    maxRequestBytes: 0,
    maxResponseBytes: 65_536,
    requiresIdempotency: false,
    forwardConditionalHeaders: true,
  },
  {
    operationId: "create_download_intent",
    method: "POST",
    bffPattern: "/api/domain/v1/w/:slug/download-intents",
    fastapiPathTemplate: "/api/v1/download-intents",
    pathParams: [],
    querySchema: null,
    requestBodyValidator: REQUEST_VALIDATORS.create_download_intent,
    requestMediaTypes: ["application/json"],
    maxRequestBytes: 8_192,
    maxResponseBytes: 65_536,
    requiresIdempotency: true,
    forwardConditionalHeaders: false,
  },
] as const satisfies readonly AnyAllowlistEntry[];
```

### 6.4 Conditional Body Type

Operations with `requestBody: never` must not require a `body` parameter. The conditional type uses `[T] extends [never]` (wrapped in a tuple) to avoid the distributive conditional trap:

```typescript
type RequestBody<K extends keyof DomainOperationMap> =
  DomainOperationMap[K]["requestBody"];

type BodyParam<K extends keyof DomainOperationMap> =
  [RequestBody<K>] extends [never]
    ? { body?: never }
    : { body: RequestBody<K> };
```

Usage in `DomainProxy.forward`:

```typescript
class DomainProxy {
  async forward<K extends keyof DomainOperationMap>(
    operationId: K,
    params: {
      token: string;
      pathParams: DomainOperationMap[K]["pathParams"];
      query: DomainOperationMap[K]["queryParams"];
      signal: AbortSignal;
      idempotencyKey: string | undefined;
      requestId: string;
      traceId: string;
      slug: string;
    } & BodyParam<K>,
  ): Promise<ValidatedResponse<K>> {
    // Implementation in §4
  }
}
```

Calling `forward("get_identity_probe", { ..., body: someValue })` is a compile-time error. Calling `forward("create_upload_intent", { ... })` without `body` is also a compile-time error.

### 6.5 Per-Operation Result Types

Each operation declares its possible success statuses via `successResponses`. `DomainResultFor<K>` narrows the result union so the compiler knows that `get_identity_probe` returns only `200`, never `201`/`202`. 204 and 304 are **not** unconditionally added — only operations whose `successResponses` declares them can return them:

```typescript
type SuccessResponses<K extends keyof DomainOperationMap> =
  DomainOperationMap[K]["successResponses"];

type StatusKindMap = {
  200: "ok";
  201: "created";
  202: "accepted";
  204: "no_content";
  304: "not_modified";
};

type SuccessVariants<K extends keyof DomainOperationMap> = {
  [S in keyof SuccessResponses<K> & number]: {
    kind: S extends keyof StatusKindMap ? StatusKindMap[S] : `status_${S}`;
    status: S;
    data: SuccessResponses<K>[S];
    meta: ResponseMeta;
  };
}[keyof SuccessResponses<K> & number];

type DomainResultFor<K extends keyof DomainOperationMap> =
  | SuccessVariants<K>
  | { kind: "problem"; status: number; problem: ProblemDetails; meta: ResponseMeta };

type ResponseMeta = {
  etag: string | null;
  retryAt: number | null;
  location: string | null;
};

type ProblemDetails = {
  type: string;
  title: string;
  status: number;
  detail: string;
  instance: string;
  code: string;
  trace_id: string;
  retryable: boolean;
  field_errors: FieldError[];
};

type FieldError = {
  field: string;
  code: string;
  message: string;
};
```

Operations that can return 304 (e.g. `get_operation` with ETag) declare `304: null` in their `successResponses`. Operations that cannot return 304 (e.g. `create_upload_intent`) do not — and the compiler prevents the caller from matching on a `304` variant.

The `"problem"` variant covers **all** 4xx/5xx responses. The shared Problem response types (401 `delegated_identity_required`, 403 `authorization_epoch_stale`, 403 `workspace_suspended`, 409 `idempotency_key_reused`, 422 validation error, 500 internal error) are declared in the OpenAPI spec as shared response definitions and validated by the `problem_details` validator. An HTTP status not declared in the operation's `responses` (in the operation registry) and not matching the shared Problem validator → 502 `invalid_domain_response`. Validator missing for a declared status → 502 `invalid_domain_response` (fail closed, not silent pass-through).

Example: `DomainResultFor<"get_identity_probe">` resolves to:

```typescript
| { kind: "ok"; status: 200; data: IdentityProbeResponse; meta: ResponseMeta }
| { kind: "problem"; status: number; problem: ProblemDetails; meta: ResponseMeta }
```

No `201`, `202`, `204`, or `304` variants — `get_identity_probe.successResponses` contains only `200`.

### 6.6 DomainGateway — Browser-Only

```typescript
// web/lib/domain-client/gateway.ts — runs in the browser only

import { buildBffPath } from "./generated/paths";
import type { DomainResultFor } from "./result-types";
import type {
  CreateUploadIntentRequest,
  FinalizeUploadRequest,
  CreateDownloadIntentRequest,
} from "./generated/domain-v1";

interface DomainGateway {
  getIdentityProbe(params: {
    workspaceSlug: string;
  }): Promise<DomainResultFor<"get_identity_probe">>;

  pollOperation(params: {
    workspaceSlug: string;
    operationId: string;
  }): Promise<DomainResultFor<"get_operation">>;

  createUploadIntent(params: {
    workspaceSlug: string;
    body: CreateUploadIntentRequest;
  }): Promise<DomainResultFor<"create_upload_intent">>;

  finalizeUpload(params: {
    workspaceSlug: string;
    intentId: string;
    body: FinalizeUploadRequest;
  }): Promise<DomainResultFor<"finalize_upload">>;

  createDownloadIntent(params: {
    workspaceSlug: string;
    body: CreateDownloadIntentRequest;
  }): Promise<DomainResultFor<"create_download_intent">>;
}
```

The gateway has no access to `req`, `res`, JWT issuers, or server secrets. For each call it:

1. Resolves the BFF path via `buildBffPath(operationId, { slug: params.workspaceSlug, ...pathParams })`.
2. For non-safe methods (POST/PUT/PATCH/DELETE): attaches `X-CSRF-Token` via `CsrfTokenManager.getToken()`. GET/HEAD omit the CSRF token — the BFF gate skips CSRF verification for safe methods (§4.1).
3. For non-safe methods with idempotency: attaches `X-Intent-Id` via `IntentTracker.acquire(actionInstanceId, operationId)`.
4. Attaches `X-Trace-Id` (UUID generated per request for correlation).
5. Sets `Content-Type: application/json` for requests with a body.
6. Performs a same-origin `fetch` to the resolved path.
7. Parses the response into a `DomainResultFor<K>`.

### 6.7 DomainProxy — Server-Only

```typescript
// web/lib/domain-client/proxy.ts — runs on the server only

import type { DomainOperationMap } from "./generated/operation-map";
import type { BodyParam } from "./result-types";
import { OPERATION_REGISTRY } from "./generated/operation-registry";
import { RESPONSE_VALIDATORS } from "./generated/validators";
import { buildBffPath } from "./generated/paths";

export type ValidatedResponse<K extends keyof DomainOperationMap> = {
  status: number;
  headers: Record<string, string>;
  body: unknown;
  validated: true;
};

type DomainProxyConfig = {
  baseUrl: string;
  maxConnections: number;     // DOMAIN_API_MAX_CONNECTIONS, default 20
  maxRetries: number;         // DOMAIN_API_MAX_RETRIES, default 1
  headersTimeoutMs: number;   // DOMAIN_API_HEADERS_TIMEOUT_MS, default 10_000
  bodyTimeoutMs: number;      // DOMAIN_API_BODY_TIMEOUT_MS, default 30_000
};

class DomainProxy {
  private readonly config: DomainProxyConfig;
  private readonly pool: import("undici").Pool;

  constructor(config: DomainProxyConfig) {
    this.config = config;
    this.pool = new (require("undici").Pool)(config.baseUrl, {
      connections: config.maxConnections,
    });
  }

  async forward<K extends keyof DomainOperationMap>(
    operationId: K,
    params: {
      tokenIssuer: DelegatedTokenIssuer;
      req: NextApiRequest;
      res: NextApiResponse;
      slug: string;
      pathParams: DomainOperationMap[K]["pathParams"];
      query: URLSearchParams;
      signal: AbortSignal;
      idempotencyKey: string | undefined;
      requestId: string;
      traceId: string;
      conditionalHeaders: Record<string, string>;
    } & BodyParam<K>,
  ): Promise<ValidatedResponse<K>> {
    const entry = OPERATION_REGISTRY.get(operationId as string);
    if (!entry) {
      throw new Error(`Unknown operationId: ${operationId as string}`);
    }

    // Build FastAPI path from template + pathParams + query
    let path = entry.pathTemplate;
    const pathParams = params.pathParams as Record<string, string>;
    for (const param of entry.pathParams) {
      path = path.replace(`{${param}}`, encodeURIComponent(pathParams[param]));
    }
    const qs = params.query.toString();
    if (qs.length > 0) {
      path += `?${qs}`;
    }

    // Serialize body once — forward() owns serialization
    const serializedBody = "body" in params && params.body !== undefined
      ? JSON.stringify(params.body)
      : undefined;

    // Dispatch with Phase 1/Phase 2 retry logic (see §4.8 for detail).
    // Token is signed per attempt inside dispatchWithRetry — a retry may
    // need a fresh token if the previous one is close to expiry.
    return this.dispatchWithRetry(entry, {
      tokenIssuer: params.tokenIssuer,
      req: params.req,
      res: params.res,
      slug: params.slug,
      path,
      serializedBody,
      signal: params.signal,
      idempotencyKey: params.idempotencyKey,
      requestId: params.requestId,
      traceId: params.traceId,
      conditionalHeaders: params.conditionalHeaders,
    });
  }

  private async validateParsedResponse<K extends keyof DomainOperationMap>(
    operationId: K,
    entry: import("./generated/operation-registry").OperationEntry,
    response: import("undici").Dispatcher.ResponseData,
    raw: string,
    slug: string,
  ): Promise<ValidatedResponse<K>> {
    const status = response.statusCode;
    const responseShape = entry.responses[status];

    // Step 1: Determine response shape.
    // The registry declares per-operation statuses AND shared error statuses
    // (401, 403, 409, 422, 500) via COMMON_ERROR_RESPONSES merged at generation
    // time. An "unknown status" means neither operation-specific nor shared.
    //
    // Fail-closed rule (single canonical statement):
    //   - Declared status → validate per schema (step 3-6 below)
    //   - Undeclared status ≥ 400 with Content-Type: application/problem+json
    //     AND valid Problem Details body → forward to browser (transparent error)
    //   - Undeclared status ≥ 400 without correct Content-Type, or with invalid
    //     or missing Problem Details body → 502 invalid_domain_response
    //   - Undeclared status < 400 → 502 (unexpected success = untrusted)
    //
    // The Content-Type check is mandatory — without it, an attacker-controlled
    // FastAPI response could inject arbitrary JSON under an error status.
    if (!responseShape) {
      if (status >= 400) {
        const contentType = (response.headers["content-type"] as string || "")
          .split(";")[0].trim().toLowerCase();
        if (contentType === "application/problem+json" && raw.length > 0) {
          try {
            const parsed = JSON.parse(raw);
            const problemValidator = RESPONSE_VALIDATORS.get("problem_details");
            if (problemValidator && problemValidator(parsed)) {
              return {
                status,
                headers: this.buildSafeResponseHeaders(response, null),
                body: parsed,
                validated: true as const,
              };
            }
          } catch { /* not valid JSON, fall through to 502 */ }
        }
      }
      throw new DomainProxyError(502, "invalid_domain_response", {
        detail: `Unexpected status ${status} from ${operationId as string}`,
      });
    }

    // Step 3: Content-Type check (skip for bodiless responses)
    if (responseShape.body !== "none") {
      const contentType = (response.headers["content-type"] as string) || "";
      const mediaType = contentType.split(";")[0].trim().toLowerCase();
      if (!responseShape.mediaTypes.includes(mediaType)) {
        throw new DomainProxyError(502, "invalid_domain_response", {
          detail: `Unexpected Content-Type "${mediaType}" from ${operationId as string}`,
        });
      }
    }

    // Step 4: Body validation
    let parsedBody: unknown = undefined;

    if (responseShape.body === "none") {
      if (raw.length > 0) {
        throw new DomainProxyError(502, "invalid_domain_response", {
          detail: `Expected empty body for status ${status} from ${operationId as string}`,
        });
      }
    } else {
      try {
        parsedBody = JSON.parse(raw);
      } catch {
        throw new DomainProxyError(502, "invalid_domain_response", {
          detail: `Invalid JSON from ${operationId as string}`,
        });
      }

      // Step 5: Declared schema validation — identical for success and error
      // statuses. Shared errors point at problem_details; an operation-specific
      // Problem Details subtype points at its own generated validator.
      const validator = RESPONSE_VALIDATORS.get(responseShape.body.validatorId);
      if (!validator) {
        throw new DomainProxyError(502, "invalid_domain_response", {
          detail: `Missing response validator "${responseShape.body.validatorId}" — fail closed`,
        });
      }
      if (!validator(parsedBody)) {
        throw new DomainProxyError(502, "invalid_domain_response", {
          detail: `Response body failed validation for ${operationId as string} status ${status}`,
        });
      }
    }

    // Step 6: Location rewriting — invalid Location is 502, not silent drop
    const responseHeaders = this.buildSafeResponseHeaders(response, responseShape);
    const rawLocation = response.headers["location"] as string | undefined;

    if (rawLocation) {
      const rewritten = this.rewriteLocation(rawLocation, responseShape.locationShapes, slug);
      if (rewritten === null) {
        throw new DomainProxyError(502, "invalid_domain_response", {
          detail: `Invalid Location header from ${operationId as string}`,
        });
      }
      responseHeaders["Location"] = rewritten;
    }

    return {
      status,
      headers: responseHeaders,
      body: parsedBody,
      validated: true as const,
    };
  }

  private buildSafeResponseHeaders(
    response: import("undici").Dispatcher.ResponseData,
    responseShape: unknown,
  ): Record<string, string> {
    const headers: Record<string, string> = {};
    // Only forward safe response headers — NOT upstream content-length
    // (the proxy may re-serialize the body) or x-trace-id (internal).
    // The proxy generates its own Content-Length from the serialized body
    // and sets its own trace header.
    const safeHeaders = ["content-type", "etag", "retry-after"];
    for (const h of safeHeaders) {
      const val = response.headers[h];
      if (typeof val === "string") {
        headers[h] = val;
      }
    }
    // Cache-Control: no-store for all authenticated domain responses
    headers["Cache-Control"] = "no-store";
    return headers;
  }

  private rewriteLocation(
    rawLocation: string,
    shapes: readonly import("./generated/operation-registry").LocationShape[],
    slug: string,
  ): string | null {
    for (const shape of shapes) {
      const match = shape.pattern.exec(rawLocation);
      if (match) {
        const extracted: Record<string, string> = { slug };
        for (const paramName of shape.extractParams) {
          const value = match.groups?.[paramName];
          if (!value) return null;
          extracted[paramName] = value;
        }
        try {
          return buildBffPath(shape.targetOperationId, extracted);
        } catch {
          return null;
        }
      }
    }
    return null;
  }
}

class DomainProxyError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    public readonly details: { detail: string; traceId?: string },
  ) {
    super(details.detail);
    this.name = "DomainProxyError";
  }
}
```

`openapi-fetch` is not used. `undici` is the sole HTTP adapter on the server side, chosen for its timeout control and connection pooling (§4).

### 6.8 Runtime Response Validation Pipeline

For every response from FastAPI, `DomainProxy` validates before returning to the browser:

1. **Status lookup:** Find the HTTP status in `operation.responses[status]`.
2. **Declared status:** Validate its media type, body/no-body rule, schema, and Location shape exactly as registered.
3. **Undeclared success (`< 400`):** Always return 502 `invalid_domain_response`; an unexpected success is untrusted.
4. **Undeclared error (`>= 400`):** Pass through only when `Content-Type` is `application/problem+json` and the body passes the unified Problem Details validator. Otherwise return 502 `invalid_domain_response`.
5. **Typed result:** A validated declared success becomes its generated success variant. Every validated 4xx/5xx, declared or transparent, becomes the generic `problem` variant. Unknown or unvalidated data never reaches the browser.

This is the canonical status rule for the document. Tests cover declared
success, declared Problem Details, transparent undeclared Problem Details,
wrong media type, invalid Problem Details, and undeclared success.

### 6.9 CsrfTokenManager — Browser-Only

```typescript
// web/lib/domain-client/csrf.ts — browser-only

class CsrfTokenManager {
  private cachedToken: string | null = null;
  private cachedAt: number = 0;
  private inflight: Promise<string> | null = null;
  private readonly ttlMs: number;

  constructor(config: { ttlMs: number }) {
    this.ttlMs = config.ttlMs;
  }

  async getToken(): Promise<string> {
    if (this.cachedToken !== null && Date.now() - this.cachedAt < this.ttlMs) {
      return this.cachedToken;
    }

    // Single-flight: concurrent callers coalesce into one fetch
    if (this.inflight !== null) {
      return this.inflight;
    }

    this.inflight = this.fetchToken();
    try {
      const token = await this.inflight;
      this.cachedToken = token;
      this.cachedAt = Date.now();
      return token;
    } finally {
      this.inflight = null;
    }
  }

  clear(): void {
    this.cachedToken = null;
    this.cachedAt = 0;
  }

  private async fetchToken(): Promise<string> {
    const response = await fetch("/api/csrf", {
      method: "GET",
      credentials: "same-origin",
    });
    if (!response.ok) {
      throw new Error(`CSRF token fetch failed: ${response.status}`);
    }
    const data: { token: string } = await response.json();
    return data.token;
  }
}
```

- Server identifies the session from the HttpOnly cookie. The browser does not know or supply the session ID.
- Token cached in memory (not `localStorage`) until TTL expires.
- Cache cleared on: logout, receiving 401 from any BFF call.
- On `csrf_validation_failed` (403): the DomainGateway implementation refreshes the token once, retries the original request with the same intent. Second failure is surfaced to the caller.
- CSRF token is never stored in `localStorage` or forwarded to FastAPI.

### 6.10 IntentTracker — Browser-Only, Persistent Action Identity

```typescript
// web/lib/domain-client/intent.ts — browser-only

type Intent = {
  intentId: string;
  actionInstanceId: string;
};

type ReconciliationRequired = {
  kind: "reconciliation_required";
  actionInstanceId: string;
  staleSince: number;
};

type StoredIntent = {
  intentId: string;
  actionInstanceId: string;
  createdAt: number;
  safeRetryUntil: number;
  localRetentionUntil: number;
  status: "inflight" | "retained";
};

const DEFAULT_SAFE_RETRY_MS = 24 * 60 * 60 * 1000; // 24h — matches IDEMPOTENCY_RETENTION_SECONDS
const DEFAULT_RETENTION_MS = 48 * 60 * 60 * 1000;  // 48h — reconciliation window

type OperationPolicy = {
  safeRetryWindowMs: number;
  localRetentionMs: number;
};

class IntentTracker {
  private readonly storage: Storage;
  private readonly inflightPromises: Map<string, Promise<unknown>> = new Map();
  private readonly operationPolicies: Map<string, OperationPolicy> = new Map();

  constructor(config: { storage: Storage }) {
    this.storage = config.storage;
    this.recoverCrashedInflight();
  }

  registerPolicy(operationId: string, policy: OperationPolicy): void {
    this.operationPolicies.set(operationId, policy);
  }

  acquire(
    actionInstanceId: string,
    operationId: string,
  ): Intent | ReconciliationRequired {
    this.cleanupExpired();

    const key = this.storageKey(actionInstanceId);
    const raw = this.storage.getItem(key);

    if (raw !== null) {
      const stored: StoredIntent = JSON.parse(raw);
      const now = Date.now();

      if (stored.status === "retained" && now > stored.safeRetryUntil) {
        // Past safe retry window with unresolved intent → always reconciliation,
        // regardless of localRetentionUntil. Retained intents are NEVER silently
        // deleted — they stay until explicitly resolved or the user starts a new
        // action via dismissReconciliation().
        return {
          kind: "reconciliation_required",
          actionInstanceId,
          staleSince: stored.safeRetryUntil,
        };
      }

      // Within safe retry window, or still inflight — return same intent
      return {
        intentId: stored.intentId,
        actionInstanceId: stored.actionInstanceId,
      };
    }

    // Create new intent — time bounds from operation policy BEFORE first send
    const policy = this.operationPolicies.get(operationId);
    const now = Date.now();
    const intent: StoredIntent = {
      intentId: crypto.randomUUID(),
      actionInstanceId,
      createdAt: now,
      safeRetryUntil: now + (policy?.safeRetryWindowMs ?? DEFAULT_SAFE_RETRY_MS),
      localRetentionUntil: now + (policy?.localRetentionMs ?? DEFAULT_RETENTION_MS),
      status: "inflight",
    };

    this.storage.setItem(key, JSON.stringify(intent));

    return {
      intentId: intent.intentId,
      actionInstanceId: intent.actionInstanceId,
    };
  }

  resolve(intent: Intent): void {
    this.storage.removeItem(this.storageKey(intent.actionInstanceId));
    this.inflightPromises.delete(intent.actionInstanceId);
  }

  retain(intent: Intent): void {
    const key = this.storageKey(intent.actionInstanceId);
    const raw = this.storage.getItem(key);
    if (raw !== null) {
      const stored: StoredIntent = JSON.parse(raw);
      stored.status = "retained";
      this.storage.setItem(key, JSON.stringify(stored));
    }
    this.inflightPromises.delete(intent.actionInstanceId);
  }

  dismissReconciliation(actionInstanceId: string): void {
    this.storage.removeItem(this.storageKey(actionInstanceId));
  }

  refineTimeBounds(
    actionInstanceId: string,
    bounds: { safeRetryUntil: number; localRetentionUntil: number },
  ): void {
    const key = this.storageKey(actionInstanceId);
    const raw = this.storage.getItem(key);
    if (raw !== null) {
      const stored: StoredIntent = JSON.parse(raw);
      // Only NARROW bounds — never extend the safety window
      stored.safeRetryUntil = Math.min(stored.safeRetryUntil, bounds.safeRetryUntil);
      stored.localRetentionUntil = Math.min(stored.localRetentionUntil, bounds.localRetentionUntil);
      this.storage.setItem(key, JSON.stringify(stored));
    }
  }

  deduplicateInflight<T>(
    actionInstanceId: string,
    work: () => Promise<T>,
  ): Promise<T> {
    const existing = this.inflightPromises.get(actionInstanceId);
    if (existing !== undefined) {
      return existing as Promise<T>;
    }

    const promise = work().finally(() => {
      this.inflightPromises.delete(actionInstanceId);
    });

    this.inflightPromises.set(actionInstanceId, promise);
    return promise;
  }

  private recoverCrashedInflight(): void {
    for (let i = 0; i < this.storage.length; i++) {
      const storageKey = this.storage.key(i);
      if (storageKey === null || !storageKey.startsWith("intent:")) continue;
      try {
        const stored: StoredIntent = JSON.parse(
          this.storage.getItem(storageKey) || "",
        );
        if (stored.status === "inflight") {
          // Page crash/reload: persisted inflight intents have ambiguous outcome.
          // Transition to retained so acquire() returns ReconciliationRequired
          // after safeRetryUntil, rather than treating them as safely retryable
          // indefinitely.
          stored.status = "retained";
          this.storage.setItem(storageKey, JSON.stringify(stored));
        }
      } catch {
        this.storage.removeItem(storageKey);
      }
    }
  }

  private cleanupExpired(): void {
    const now = Date.now();
    for (let i = 0; i < this.storage.length; i++) {
      const storageKey = this.storage.key(i);
      if (storageKey === null || !storageKey.startsWith("intent:")) continue;
      try {
        const stored: StoredIntent = JSON.parse(
          this.storage.getItem(storageKey) || "",
        );
        // Only remove NON-retained intents past local retention.
        // Retained intents are NEVER silently deleted — they produce
        // ReconciliationRequired on the next acquire() and persist until
        // explicitly resolved via resolve() or dismissed via
        // dismissReconciliation().
        if (
          stored.status !== "retained" &&
          now > stored.localRetentionUntil &&
          stored.localRetentionUntil > 0
        ) {
          this.storage.removeItem(storageKey);
        }
      } catch {
        this.storage.removeItem(storageKey);
      }
    }
  }

  private storageKey(actionInstanceId: string): string {
    return `intent:${actionInstanceId}`;
  }
}
```

**Policy registration.** `registerPolicy(operationId, policy)` is called at application startup for each operation that uses intents. The `operationId` matches the OpenAPI operation ID (e.g., `"create_upload_intent"`), not the `actionInstanceId`. `acquire()` receives both `actionInstanceId` (stable user action identifier) and `operationId` (which operation to look up the policy for).

**Page crash recovery.** On construction, `recoverCrashedInflight()` scans persisted intents and transitions any `"inflight"` to `"retained"`. A persisted inflight intent means the page crashed or reloaded before the response arrived — the outcome is ambiguous. By marking them retained, the next `acquire()` will return `ReconciliationRequired` after `safeRetryUntil` instead of silently re-sending a command that may have already executed.

**Dismissing reconciliation.** When the user acknowledges a reconciliation warning (e.g., "This upload may have already completed — check your library"), calling `dismissReconciliation(actionInstanceId)` clears the retained intent, allowing the user to start a new action if desired.

**Action instance identity.** `actionInstanceId` is a stable identifier for a logical user action, **not** randomly generated on each component mount:

- Forms tied to an existing resource: derive from `${formType}:${resourceId}` (for example, `upload:draft_abc123`).
- New-resource forms: generate a `draftId` on first mount, persist it in `sessionStorage` keyed by the form's route, and restore it on remount. The component reads `sessionStorage.getItem(routeKey)` before generating a new ID.

This ensures that refreshing the page recovers the same `actionInstanceId` and therefore the same unresolved intent, rather than orphaning it.

**Two time bounds:**

| Bound | Meaning | Behavior after expiry |
|---|---|---|
| `safeRetryUntil` | No later than server-side idempotency guarantee expiry | After this, `acquire()` never resends the command. Returns `ReconciliationRequired` if the intent was `retained` (ambiguous outcome). |
| `localRetentionUntil` | Reconciliation window. Longer than `safeRetryUntil`. | For non-retained intents, cleanup removes them after this window. **Retained intents are never cleaned up by TTL** — they persist until explicitly resolved or dismissed. |

Both bounds are initialized from `operationPolicies` at `acquire()` time. `refineTimeBounds()` can only **narrow** them (use `Math.min`) — it never extends the safety window beyond what the initial policy set. This prevents a replayed response from inadvertently granting a longer retry window than the server intended.

**Concurrent double-clicks:** `deduplicateInflight()` ensures concurrent calls for the same `actionInstanceId` share a single in-flight promise. The second click does not send a second network request.

### 6.11 Generation Pipeline

| Script | Input | Output | Purpose |
|---|---|---|---|
| `scripts/generate_openapi.py` | Running FastAPI app | `contracts/openapi/marketai-domain-v1.json` | Extract normalized OpenAPI 3.1 spec |
| `npx openapi-typescript` | OpenAPI spec | `web/lib/domain-client/generated/domain-v1.d.ts` | TypeScript type definitions |
| `scripts/generate_operation_map.ts` | OpenAPI spec | `web/lib/domain-client/generated/operation-map.d.ts` | Per-operation typed map |
| `scripts/generate_operation_registry.ts` | OpenAPI spec + policy manifest | `web/lib/domain-client/generated/operation-registry.ts` | Runtime dispatch table with size limits from policy |
| `scripts/generate_response_validators.ts` | OpenAPI spec | `web/lib/domain-client/generated/validators.ts` | Ajv2020 standalone ESM validators |
| `scripts/generate_paths.ts` | OpenAPI spec + policy manifest | `web/lib/domain-client/generated/paths.ts` | `BFF_ROUTES` — browser-safe path builder |
| `scripts/generate_allowlist.ts` | OpenAPI spec + policy manifest | `web/lib/domain-client/generated/allowlist.ts` | `DOMAIN_ALLOWLIST` — server-only allowlist with validators, query schemas, size limits |
| `scripts/generate_allowlist_binding.ts` | `operation-registry.ts` + `allowlist.ts` | `web/lib/domain-client/generated/allowlist-binding.ts` | Startup validation of allowlist ↔ registry consistency |
| `scripts/generate_all.sh` | — | All seven artifacts | Orchestrator; CI runs this + `git diff --exit-code` |

### 6.12 What #17 Delivers

**Tracer milestone** (first commit checkpoint):

- `contracts/openapi/marketai-domain-v1.json` with `GET /api/v1/identity` only.
- All seven generated artifacts for the identity probe operation.
- `DomainGateway` with `getIdentityProbe` method.
- `DomainProxy` with full transport, allowlist, and per-status validation machinery.
- `CsrfTokenManager` unit-tested with `MemoryTransport` adapter (single-flight coalescing, 401 cache clear, csrf_validation_failed refresh-and-retry, second-failure surfacing).
- `IntentTracker` unit-tested with `MemoryStorage` adapter implementing the `Storage` interface (acquire/resolve/retain lifecycle, refresh recovery, dual time bounds with reconciliation signal, concurrent deduplication).
- CI drift check job for all seven generated artifacts.

**#17 final delivery** (same PR, after tracer passes):

- Operation polling: `GET /api/v1/operations/{operation_id}` in OpenAPI spec, registry, allowlist, `DomainGateway.pollOperation()`.
- Idempotency propagation: `IntentTracker` and `CsrfTokenManager` tested end-to-end through a real non-safe endpoint — `create_upload_intent`. No production test-only endpoints.
- File intent endpoints (§7): upload intent, finalize, download intent — in OpenAPI spec, registry, allowlist, and corresponding `DomainGateway` methods.
- Unit tests with `MemoryTransport`/`MemoryStorage` adapters.
- Integration tests against real FastAPI + PostgreSQL.

---

## 7. BlobStore & File Intent Seam

### 7.1 Contract Requirement (§10)

Business metadata and file authorization go through BFF→FastAPI. Binary data never transits Next.js memory. The browser uploads and downloads directly to private object storage via short-lived signed capabilities. FastAPI generates all object keys — the browser cannot supply arbitrary paths. Signed URLs never appear in logs.

### 7.2 Three-Phase File Lifecycle

```
Phase 1: Intent (through BFF)
  Browser → DomainGateway.createUploadIntent(...)
    → BFF → FastAPI
    → Validates Actor, Workspace, purpose, MIME allowlist, size limit, cost policy
    → Generates staging key, creates UploadIntent (pending)
    → BlobStore.issue_upload → UploadGrant
    → Returns 201: UploadIntent + UploadGrant

Phase 2: Direct transfer (browser ↔ object storage, bypasses BFF/FastAPI)
  Browser → POST/PUT to object storage using UploadGrant
    → Storage enforces content-length-range, Content-Type, checksum
    → No Next.js memory transit, no FastAPI proxy

Phase 3: Finalize (through BFF — always async)
  Browser → DomainGateway.finalizeUpload(...)
    → BFF → FastAPI
    → Creates or replays Operation + Job in single transaction
    → Returns 202 + Operation
    → Worker picks up Job:
      → Re-checks Workspace, Actor, authorization_epoch, suspended, purpose policy
      → Acquires verifying lease on UploadIntent (CAS)
      → BlobStore.inspect_upload → InspectedUpload (captures storage_revision)
      → Magic-byte MIME sniffing, size/checksum/MIME validation
      → BlobStore.commit_upload(inspected, expected) → CommittedBlobRef
      → Domain module creates its own resource referencing CommittedBlobRef (unique on intent)
      → Transitions intent to finalized, Operation to succeeded
```

Download creates an auditable intent record and returns a signed capability:

```
Browser → DomainGateway.createDownloadIntent(...)
  → BFF → FastAPI (POST — creates record)
  → Validates Actor, Workspace ownership, asset not expired/deleted
  → BlobStore.issue_download for CommittedBlobRef (exact storage_version_id)
  → Returns 201: DownloadIntent + DownloadGrant
```

### 7.3 BlobStore Interface — Deep Module

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol


@dataclass(frozen=True)
class UploadSpec:
    staging_key: str
    content_type: str
    content_length_min: int
    content_length_max: int
    checksum_sha256_b64: str
    expires_in_seconds: int


@dataclass(frozen=True)
class PresignedPostGrant:
    kind: Literal["presigned_post"]
    url: str
    fields: dict[str, str]          # multipart form fields for POST policy
    expires_at: datetime

@dataclass(frozen=True)
class SignedPutGrant:
    kind: Literal["signed_put"]
    url: str
    headers: dict[str, str]         # HTTP headers for PUT request
    expires_at: datetime

UploadGrant = PresignedPostGrant | SignedPutGrant


@dataclass(frozen=True)
class StagedBlobRef:
    staging_key: str


@dataclass(frozen=True)
class InspectedUpload:
    ref: StagedBlobRef
    storage_revision: str
    size_bytes: int
    checksum_sha256_b64: str
    content_type_declared: str
    magic_bytes: bytes


@dataclass(frozen=True)
class ObjectConstraints:
    expected_size: int
    expected_checksum_sha256_b64: str
    expected_magic_mime: str
    storage_revision: str


@dataclass(frozen=True)
class CommittedBlobRef:
    committed_key: str
    storage_version_id: str
    size_bytes: int
    checksum_sha256_b64: str
    content_type_verified: str


@dataclass(frozen=True)
class DownloadSpec:
    filename: str
    expires_in_seconds: int


@dataclass(frozen=True)
class DownloadGrant:
    url: str
    expires_at: datetime
    content_disposition: str


@dataclass(frozen=True)
class BlobRef:
    key: str
    version_id: str | None = None


class BlobStoreError(Exception):
    pass


class ObjectNotFoundError(BlobStoreError):
    pass


class ObjectChangedError(BlobStoreError):
    pass


class RevisionGoneError(BlobStoreError):
    pass


class BlobStore(Protocol):
    async def issue_upload(self, spec: UploadSpec) -> UploadGrant: ...

    async def inspect_upload(self, ref: StagedBlobRef) -> InspectedUpload: ...

    async def commit_upload(
        self,
        inspected: InspectedUpload,
        expected: ObjectConstraints,
    ) -> CommittedBlobRef: ...

    async def issue_download(
        self,
        ref: CommittedBlobRef,
        spec: DownloadSpec,
    ) -> DownloadGrant: ...

    async def delete(self, ref: BlobRef) -> None: ...
```

### 7.4 Commit Semantics and Overwrite Prevention

`commit_upload` copies the **exact inspected revision**, not the "current" object. S3 `CopyObject` accepts a source `VersionId` — the copy reads the specific version that `inspect_upload` examined, regardless of whether the staging key has been overwritten since. The committed key is in a different key prefix that the original presigned URL cannot write to.

S3 Versioning is a **bucket-level** setting (not prefix-level). The bucket has versioning enabled. Both staging and committed prefixes produce versions. The `storage_revision` is an opaque token — the only correct way to address a specific upload. Never check "is this still the current version?"

| Adapter | `inspect_upload` | `commit_upload` | Overwrite prevention |
|---|---|---|---|
| `S3BlobStore` | Two S3 calls: (1) `HeadObject` on the staging key to get `VersionId` (→ `storage_revision`), `ContentLength`, and `x-amz-checksum-sha256`. (2) Ranged `GetObject` on the same `VersionId` (`Range: bytes=0-8191`) for magic-byte MIME sniffing. Using the exact `VersionId` ensures the ranged read hits the same version that `HeadObject` described. | `CopyObject` from `staging_key?versionId={storage_revision}` to `committed_key`. If the `VersionId` no longer exists (deleted by lifecycle) → `RevisionGoneError`. | The copy targets a distinct committed prefix unreachable by the presigned URL. The committed object gets its own `VersionId` recorded in `CommittedBlobRef.storage_version_id`. Downloads sign against this exact version. |
| `LocalFileBlobStore` | Opens without following symlinks, reads first 8KB for magic bytes, and records `st_dev:st_ino:st_mtime_ns:st_size` as the opaque `storage_revision`. | Acquires the same per-key OS advisory lock used by the transfer process for its final rename, then re-stats without following symlinks. Missing → `RevisionGoneError`; revision mismatch → `ObjectChangedError`; match → atomic rename to the committed path while still holding the lock. | A transfer may stream to an isolated temp file concurrently, but its final staging rename and Worker commit are serialized. The commit either renames the exact inspected generation or fails; it never moves replacement bytes. |

**Interface guarantee:** `commit_upload` either commits the exact bytes
described by `InspectedUpload` or raises a documented `BlobStoreError`. It
never commits a different revision. An Adapter may provide a stronger outcome
(S3 can still address an old VersionId) while callers depend only on this
shared guarantee:

| Condition | S3 | Local |
|---|---|---|
| Object gone before inspect | `NoSuchKey` → `ObjectNotFoundError` | File not found → `ObjectNotFoundError` |
| Revision gone at commit | `InvalidArgument` (deleted VersionId) → `RevisionGoneError` | File missing (already renamed) → `RevisionGoneError` |
| Object replaced between inspect and commit | Commits the inspected VersionId | Raises `ObjectChangedError`; never commits the replacement |

Contract tests are split by observable Interface guarantees rather than using
one assertion that branches on Adapter identity:

1. **Shared Interface suite:** runs unchanged against both Adapters and asserts
   that successful commits reproduce the inspected checksum, no Adapter ever
   commits replacement bytes, disappeared revisions map to
   `RevisionGoneError`, capabilities expire, and keys cannot escape their
   prefix.

2. **Adapter capability suites:** verify stronger, explicitly documented
   behavior without changing the shared caller contract:

| Case | S3 | Local | Reason |
|---|---|---|---|
| Replaced revision at commit | `CommittedBlobRef` (copies exact VersionId) | `ObjectChangedError` (no version history, detects overwrite) | S3 VersionId isolation vs Local filesystem semantics |
| Concurrent commit | Both succeed (each copies independently) | First wins, second gets `RevisionGoneError` (rename is single-winner) | S3 is idempotent per VersionId; Local rename is atomic |

The Worker handles the Interface result, not the Adapter identity.
`ObjectChangedError` is a permanent `object_replaced_during_verification`
rejection that requires a new upload; it is never classified as a transient
storage retry. `RevisionGoneError` is likewise terminal. Network/5xx/timeout
errors remain transient and release the verification lease.

#### Local Blob Transfer Endpoint

The local Adapter exposes capabilities through a dev-only
`LocalBlobTransferApp`, a separate storage ASGI process listening on
`http://localhost:9000`. It is not mounted in the FastAPI Domain API process.
Its route is `/_local_blob/{signature}/{key:path}`; the signature is the
authorization, not a delegated JWT.

**Why a separate process?** ADR-0004 rejects browser access to FastAPI. The
local process occupies the object-storage role that S3 occupies in production;
it has no domain routers, database session, delegated JWT middleware, or domain
state. This preserves the accepted browser → same-origin BFF → private FastAPI
seam while still keeping binary bytes out of Next.js memory.

The browser transfer is intentionally cross-origin (`localhost:3000` →
`localhost:9000`) and governed by the same capability and CORS model as a
browser → S3 transfer. It is never described as same-origin and requires no ADR
exception.

**Method support:** The endpoint handles both `GET` (downloads) and `PUT` (uploads):

| Method | Purpose | Signature binds |
|---|---|---|
| `GET /_local_blob/{signature}/{key:path}` | Download committed blob | `method=GET`, `key`, `expiry`, `storage_revision` |
| `PUT /_local_blob/{signature}/{key:path}` | Upload to staging key | `method=PUT`, `key`, `expiry`, `content_type`, `content_length_min`, `content_length_max`, `checksum_sha256_b64` |

**Signature binding:** The HMAC-SHA256 signature binds not just the key and expiry, but also the **method** and (for uploads) the **content constraints**. This prevents a download URL from being used for uploads, and prevents an upload URL from accepting a file that violates the intent's content constraints.

```python
def _sign_local_url(
    self,
    method: str,
    key: str,
    expiry: int,
    *,
    storage_revision: str | None = None,
    content_type: str | None = None,
    content_length_min: int | None = None,
    content_length_max: int | None = None,
    checksum_sha256_b64: str | None = None,
) -> str:
    message = f"{method}\n{key}\n{expiry}"
    if method == "GET":
        message += f"\n{storage_revision}"
    elif method == "PUT":
        message += f"\n{content_type}\n{content_length_min}\n{content_length_max}\n{checksum_sha256_b64}"
    signature = hmac.new(
        self._signing_secret, message.encode(), hashlib.sha256,
    ).hexdigest()
    query: dict[str, str | int] = {"expires": expiry}
    if method == "GET":
        query["v"] = storage_revision or ""
    elif method == "PUT":
        query.update({
            "ct": content_type or "",
            "cl_min": content_length_min or 0,
            "cl_max": content_length_max or 0,
            "cs": checksum_sha256_b64 or "",
        })
    qs = urllib.parse.urlencode(query)
    return f"{self._public_base_url}/_local_blob/{signature}/{key}?{qs}"
```

`issue_download()` passes `CommittedBlobRef.storage_version_id` as
`storage_revision`; an empty revision is invalid. The transfer parser rejects
duplicate or undeclared query keys, so its canonical HMAC input cannot differ
from the values it enforces.

**Transfer enforcement:** Signature validation is necessary but not sufficient.
For every request, `LocalBlobTransferApp`:

1. Validates the method, expiry, canonical key, exact non-duplicated query
   fields, and HMAC via `hmac.compare_digest()` before opening a source or
   destination.
2. Resolves the key beneath `MARKETAI_BLOB_STORE_LOCAL_ROOT` using a
   no-symlink/no-parent-escape check.
3. For PUT, requires the signed `Content-Type` and
   `x-amz-checksum-sha256`, streams into a same-directory temporary file,
   counts bytes, and updates SHA-256 incrementally. It rejects immediately
   above `cl_max`, rejects at EOF below `cl_min`, and rejects a checksum or
   media-type mismatch.
4. Deletes the temporary file on disconnect or any failure. Only a fully
   verified stream is `fsync`ed. Its final atomic rename to the staging path
   acquires the same per-key OS advisory lock as Worker commit, so those two
   state changes cannot interleave.
5. For GET, requires the signed `v` revision, re-stats without following
   symlinks, and streams only when the current committed generation matches
   that revision. It exposes neither directory listings nor arbitrary ranges.

Thus the signed constraint fields are both immutable and actually enforced at
the transfer seam. Failure responses contain no filesystem paths or signed URL.

**CORS:** The storage process permits exactly
`MARKETAI_BLOB_STORE_LOCAL_CORS_ORIGIN` (default
`http://localhost:3000`), methods `GET, PUT, OPTIONS`, and headers
`Content-Type, x-amz-checksum-sha256`. It emits no wildcard origin and includes
the appropriate `Vary: Origin`; credentialed CORS is disabled because the
signed capability is the credential. This mirrors S3 bucket CORS.

**Process wiring:** `worker/local_blob_transfer.py` exposes the storage-only
ASGI application. The local development orchestration starts it on port 9000
when `MARKETAI_BLOB_STORE_ADAPTER=local`; `app/main.py` never imports or mounts
it. Production refuses to start this process.

### 7.5 MIME Validation — Magic Bytes, Not Metadata

`Content-Type` in storage metadata is whatever the client declared — it is not the actual file type. During `inspect_upload`, the adapter performs a range read of the first 8KB (magic bytes). The finalize Worker passes these to a MIME sniffer (`python-magic`). The purpose policy declares which verified MIME types are allowed:

```python
PURPOSE_POLICIES: dict[str, PurposePolicy] = {
    "generation_output": PurposePolicy(
        allowed_mimes=frozenset({"image/png", "image/jpeg", "image/webp"}),
        max_size_bytes=50 * 1024 * 1024,
        default_ttl_seconds=300,
    ),
    "reference_asset": PurposePolicy(
        allowed_mimes=frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"}),
        max_size_bytes=100 * 1024 * 1024,
        default_ttl_seconds=300,
    ),
    "export_package": PurposePolicy(
        allowed_mimes=frozenset({"application/zip"}),
        max_size_bytes=500 * 1024 * 1024,
        default_ttl_seconds=600,
    ),
}
```

Magic-byte verified MIME not in the purpose allowlist → `rejected` with `mime_type_not_allowed`.

### 7.6 Upload Capability Semantics

Signed upload URLs and presigned POST policies are **short-lived bearer capabilities**, not authenticated sessions. Within their validity window:

- They can be forwarded or replayed — the storage layer cannot distinguish the original browser from another client.
- S3 presigned POST capabilities and local signed PUT capabilities may be replayed against the same staging key within their validity window, replacing the current staging object.

What the system guarantees:

| Guarantee | Mechanism |
|---|---|
| Object scope fixed | Capability writes to exactly one staging key |
| Short-lived | Default 5 minutes, configurable per purpose |
| Size-enforced | Presigned POST policy uses `content-length-range` |
| Checksum-enforced | `x-amz-checksum-sha256` policy (S3) or signed header plus streaming digest comparison (Local) |
| Finalize binds Actor | Only the intent creator (same `sub` claim) can finalize |
| Staging→committed detects replacement | `commit_upload` copies exact `storage_revision` via `VersionId` |
| Cannot extend lifetime | Idempotent replay of `create_upload_intent` returns original `expires_at` |
| Post-expiry | 410 `upload_intent_expired` |

### 7.7 UploadGrant Protocol Variants

```typescript
type UploadGrant =
  | { kind: "presigned_post"; url: string; fields: Record<string, string> }
  | { kind: "signed_put"; url: string; headers: Record<string, string> };

interface StorageOriginPolicy {
  assertAllowed(rawUrl: string): URL;
}

interface BrowserBlobTransferClient {
  upload(grant: UploadGrant, file: Blob): Promise<void>;
  validatedDownloadUrl(grant: DownloadGrant): URL;
}
```

The S3 adapter uses **presigned POST policy** (`kind: "presigned_post"`) because it natively supports `content-length-range` enforcement at the storage layer. The local adapter uses **signed PUT** (`kind: "signed_put"`) because presigned POST policies have no meaning for local filesystem storage — the PUT URL is HMAC-signed with content constraints embedded in the signature (§7.4).

The browser workflow composes `DomainGateway` (intent/finalize metadata calls)
with one `BrowserBlobTransferClient` (binary transfer). The transfer client, not
UI components, discriminates on `grant.kind`: it first calls
`StorageOriginPolicy.assertAllowed()`, then uses `FormData` for presigned POST
or raw `PUT` with exactly the signed headers. Upload fetches use
`credentials: "omit"` and `redirect: "error"`. Download grants are origin-
checked before being handed to a user-initiated navigation and are never
persisted in browser storage. This keeps the BFF gateway free of storage
transport details while giving the direct-transfer boundary one testable
Module.

Checksum encoding: SHA-256 as base64-encoded 32-byte digest, matching the S3 `x-amz-checksum-sha256` header format.

### 7.8 Finalize Is Always Async (202 + Operation)

`finalize_upload` has a single response mode: **202 Accepted + Operation**. There is no synchronous success path.

Rationale:

1. `commit_upload` may involve large-object S3 copy, storage timeouts, or transient 5xx — these belong in the durable Job model, not a synchronous request.
2. Worker restart recovery is automatic via the existing Operation/Job model.
3. The browser already has the Operation polling infrastructure (§5, §6).

In one database transaction, `finalize_upload` creates (or replays via idempotency) an `OperationRecord` + `Job`. The response includes the `operation_id` and `Location` header.

**Worker verification sequence:**

The Worker checks **domain-side projections only** — it never queries the control-plane database. Membership truth belongs to the control plane, but the domain has its own workspace projection that includes `authorization_epoch` and `suspended` status. An epoch mismatch serves as the proxy for membership changes.

1. Re-check domain Workspace projection exists and is not suspended (via `WorkspaceAdmissionReader`).
2. Compare domain projection's `authorization_epoch` against `UploadIntent.authorization_epoch_at_issue`. Mismatch → Operation fails with `authorization_epoch_stale`. An actor removed and re-added changes the epoch — the old intent's epoch no longer matches. This is the domain-side proxy for "membership may have changed" without querying the control plane.
3. Check domain workspace's entitlement lease — if the workspace's entitlement has been suspended or downgraded since intent creation, reject.
4. Re-check purpose policy (MIME allowlist, size limit) — policy may have been tightened between intent creation and finalization.
5. Acquire `verifying` lease on `UploadIntent` via CAS (`state = 'pending'` + set `verification_lease_id` + `verification_lease_expires_at`).
6. `BlobStore.inspect_upload(StagedBlobRef(staging_key))` → `InspectedUpload`.
7. Magic-byte MIME sniff against purpose allowlist.
8. Size and checksum comparison against intent.
9. `BlobStore.commit_upload(inspected, ObjectConstraints(...))` → `CommittedBlobRef`.
10. Record `CommittedBlobRef` on the `UploadIntentRecord`. The base seam produces a `CommittedBlobRef` — **not** an `AssetVersion`. Specific domain modules (Creative Library, Generation, Export) are responsible for creating their own domain references (e.g. `AssetVersion`, `GenerationOutput`, `ExportPackage`) that link to the `CommittedBlobRef`. This keeps the upload infrastructure generic.
11. Transition intent to `finalized`. Operation to `succeeded` with `result_ref` pointing to the committed blob.

### 7.9 UploadIntent State Machine

```
pending → verifying → finalized
                    → rejected
                    → pending (transient storage error, release lease)
pending → expired
```

| From | To | Trigger | Guard |
|---|---|---|---|
| `pending` | `verifying` | Worker Job starts | CAS on `state = 'pending'` + `verification_lease_id` + `verification_lease_expires_at`. Only one Worker acquires. |
| `verifying` | `finalized` | All validations pass, `commit_upload` succeeds | Records `committed_blob_ref_key` and `committed_blob_ref_version` on the intent. Domain modules create their own references (AssetVersion, etc.) in downstream tickets. |
| `verifying` | `rejected` | MIME/size/checksum mismatch, object not found, `ObjectChangedError`, `RevisionGoneError`, authorization epoch stale, Workspace suspended, membership revoked | `rejection_code` recorded. `ObjectChangedError` maps to `object_replaced_during_verification`; a new upload is required. Terminal. |
| `verifying` | `pending` | Transient storage error (5xx, timeout) | Releases lease. Next Job attempt can retry. |
| `verifying` lease timeout | `pending` | Lease exceeds max duration (30 seconds) | Prevents stuck state. |
| `pending` | `expired` | `expires_at < now() - grace_period` | Only by cleanup process. Skips `verifying` intents. |

### 7.10 UploadIntentRecord

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


UploadIntentState = Literal["pending", "verifying", "finalized", "rejected", "expired"]


@dataclass
class UploadIntentRecord:
    id: str                                    # "ui_" + ULID
    workspace_id: str
    initiator_id: str
    authorization_epoch_at_issue: int
    owner_kind: str                            # object binding: "creative_asset", "generation", "export"
    owner_id: str | None                       # FK to owning domain entity (nullable for new resources)
    staging_key: str
    purpose: str
    filename_original: str
    content_type: str
    content_length_min: int
    content_length_max: int
    checksum_sha256_b64: str
    state: UploadIntentState
    verification_lease_id: str | None
    verification_lease_expires_at: datetime | None
    rejection_code: str | None
    committed_blob_ref_key: str | None
    committed_blob_ref_version: str | None
    created_at: datetime
    expires_at: datetime
    finalized_at: datetime | None
```

Invariants:

- CAS guarantees only one Worker holds `verifying` at a time.
- Each intent produces at most one `CommittedBlobRef` — database unique constraint on `upload_intent_id`.
- Already-`finalized` duplicate Operation replay returns the existing result.
- Transient storage errors release the lease back to `pending` — do not permanently reject.
- Cleanup process skips intents in `verifying` state.

### 7.11 Idempotency for Intent Creation — Re-Issuance, Not Reuse

The browser may not have received the original `UploadGrant` (timeout, network error). Idempotent replay of `create_upload_intent` or `create_download_intent`:

1. Returns the same `UploadIntentRecord` / `DownloadIntentRecord`.
2. Does **not** store or return the original signed URL.
3. **Re-issues** a fresh capability for the same staging/committed ref. The new capability's expiry is capped at the intent's original `expires_at` — it cannot extend the intent lifetime.
4. If `intent.expires_at < now()` → 410 `upload_intent_expired` / `download_intent_expired`. The user must start a new action (new `intent_id`, new idempotency key).

For `create_download_intent`, the same rule applies: replay returns the same intent record and re-issues a signed URL against the same `CommittedBlobRef` (exact `storage_version_id`), capped at original `expires_at`.

### 7.12 DownloadIntent — POST, Not GET

`createDownloadIntent` is **POST**, not GET, because it:

1. Creates an immutable `DownloadIntentRecord` (audit log).
2. Requires Origin, CSRF, and session verification (non-safe request).
3. Each user click produces a new intent with a fresh signed capability.

Download intents carry intent-based idempotency: same `intent_id` from the same user action returns the same record with the same `CommittedBlobRef` and original `expires_at`, but a re-issued signed URL. The signed download URL is usable for repeated downloads within its validity window — this is by design (the user may retry a failed download).

```python
@dataclass
class DownloadIntentRecord:
    id: str                        # "di_" + ULID
    workspace_id: str
    initiator_id: str
    owner_kind: str                    # what is being downloaded: "creative_asset", "generation", "export"
    owner_id: str                      # FK to owning domain entity
    committed_blob_ref_key: str
    committed_blob_ref_version: str
    created_at: datetime
    expires_at: datetime
```

### 7.13 Object Key Structure

```
staging/{workspace_id}/{purpose}/{YYYY}/{MM}/{DD}/{ulid}.dat
committed/{workspace_id}/{purpose}/{YYYY}/{MM}/{DD}/{ulid}.{ext}
```

- `workspace_id`: RLS-aligned partition.
- `purpose`: enum — `generation_output`, `reference_asset`, `export_package`, etc.
- Date partition: enables lifecycle policies.
- `ulid`: monotonically sortable, globally unique.
- **Staging key:** uses fixed `.dat` extension. The staging key is generated before the upload — MIME type is unknown at this point (the browser hasn't uploaded yet). Using a fixed opaque suffix avoids depending on unverified metadata.
- **Committed key:** uses `.{ext}` derived from the magic-byte-verified MIME type at commit time. The extension is determined by the Worker during `inspect_upload` → MIME sniff → extension mapping.

**No filename in the key.** The user's original filename is stored only in `UploadIntentRecord.filename_original`. At download time, a safe `Content-Disposition` header is generated from `filename_original` (RFC 6266, UTF-8 `filename*` parameter, stripped of control characters and path separators).

Path traversal protection: each key component is validated against `[a-zA-Z0-9_-]` (plus `.` for ext) before concatenation. No `/`, `..`, `\0`, or control characters pass through.

### 7.14 Object Storage CORS

| Environment | Allowed origins | Protocol |
|---|---|---|
| Production / staging | `APP_URL` only (the configured application origin) | HTTPS required |
| Local development | `http://localhost:{port}` only | HTTP allowed for loopback only |

The browser-only `BrowserBlobTransferClient` receives a `StorageOriginPolicy`
built from the public, non-secret
`NEXT_PUBLIC_MARKETAI_STORAGE_ORIGINS` configuration. It
compares exact normalized origins (scheme, host, effective port), rejects
credentials, fragments, non-HTTPS production URLs, and every non-allowlisted
grant before starting a transfer. The build/startup check rejects an empty or
wildcard production list. Local default is `http://localhost:9000`, matching
`LocalBlobTransferApp`; production values match the origins of the storage
grant URLs. This destination allowlist is distinct from storage CORS: the S3
bucket or local transfer process separately permits `APP_URL` as the browser
request's source origin.

### 7.15 Storage Lifecycle Rules — Versioned Bucket

With S3 Versioning enabled, standard `Expiration` only creates delete markers — noncurrent versions persist as billable storage. The `staging/` prefix requires four lifecycle rules:

| Rule | Target | Action |
|---|---|---|
| Expire current versions | `staging/` prefix, 24 hours after object creation | Creates delete marker |
| Delete noncurrent versions | `staging/` prefix, 1 day after becoming noncurrent | Permanently deletes old versions |
| Remove expired delete markers | `staging/` prefix | Cleans up delete markers with no noncurrent versions |
| Abort incomplete multipart uploads | `staging/` prefix, 1 day | Future-proofing for large file multipart |

S3 lifecycle execution is **asynchronous** — objects become **eligible** for cleanup after the configured period, not guaranteed deleted at that instant. The lifecycle rules are the production safety net. Worker application-level cleanup (downstream ticket) is the timely primary path — it scans expired intent records and calls `BlobStore.delete`.

The `committed/` prefix does **not** have expiration lifecycle rules. Committed objects are managed by domain logic (archive, delete on asset deletion, etc.).

### 7.16 Contract Tests — Both Adapters

Both Adapters run the same shared Interface suite. Stronger storage-specific
capabilities live in separate suites; shared assertions never branch on the
Adapter name:

| Test case | Validates |
|---|---|
| Golden path | `issue_upload` → browser upload → `inspect_upload` → `commit_upload` → `issue_download` |
| Repeated upload before inspect | Both inspect and commit the latest staged bytes |
| Replacement after inspect | Adapter must either commit the originally inspected checksum or raise `ObjectChangedError`; it must never commit replacement bytes |
| Staging deleted/expired | `commit_upload` after staging object removed → `RevisionGoneError` |
| Expired signed URL | Storage rejects the request |
| Path traversal in key components | Rejected at key construction time |
| Download signs exact version | `issue_download` returns URL bound to `CommittedBlobRef.storage_version_id` — returned bytes match committed checksum |
| Concurrent inspect + commit | Every success returns bytes matching its inspected checksum; allowed documented terminal errors are `ObjectChangedError` or `RevisionGoneError` |

The S3 capability suite additionally proves that an old VersionId remains
committable after overwrite and two copies of the same VersionId are safe. The
Local capability suite proves overwrite detection, atomic single-winner rename,
lock enforcement, temporary-file cleanup, and symlink rejection.

### 7.17 OpenAPI Operations Added by #17

| operationId | Method | FastAPI path | Response |
|---|---|---|---|
| `create_upload_intent` | POST | `/api/v1/upload-intents` | 201 `UploadIntentResponse` (includes `UploadGrant`) |
| `finalize_upload` | POST | `/api/v1/upload-intents/{intent_id}/finalize` | 202 `OperationResponse` |
| `create_download_intent` | POST | `/api/v1/download-intents` | 201 `DownloadIntentResponse` (includes `DownloadGrant`) |

### 7.18 Negative Tests

| Scenario | Expected |
|---|---|
| Expired intent | 410 |
| Cross-Workspace intent (created in A, finalize attempted in B) | Rejected |
| Different Actor finalizing (different `sub` claim) | Rejected |
| Actor removed between intent creation and Worker verification (`authorization_epoch` mismatch) | Operation fails with `authorization_epoch_stale` |
| Workspace suspended between intent and verification | Operation fails |
| Wrong checksum at finalize | `rejected` with `checksum_mismatch` |
| Wrong size at finalize | `rejected` with `size_mismatch` |
| Wrong MIME (magic-byte doesn't match purpose allowlist) | `rejected` with `mime_type_not_allowed` |
| Object deleted before commit (`RevisionGoneError`) | `rejected` with `revision_gone` |
| Concurrent finalize (two simultaneous requests) | Only one Worker acquires `verifying` lease |
| Replay `create_upload_intent` after expiry | 410, not a new intent |
| Staging key path traversal attempt | Validation rejects key components |
| Signed URL appears in structured log | Test fails — audit redaction must catch it |
| Storage CORS rejects non-allowlisted origin | Browser request blocked |

---

## 8. Delivery Milestones

All work ships in a single PR on branch `Raybbish/implement-the-delegated-bff-identity-and-openapi`. Two internal milestones gate progress; the second builds on the first without discarding tracer code.

### 8.1 Milestone 1 — Vertical Tracer

The minimal but real end-to-end path proving the identity seam works under production-grade constraints:

```
login → session → membership → ES256 JWT → BFF → FastAPI verify → TenantContext → RLS → identity probe
```

#### Control-plane deliverables

| Deliverable | Detail |
|---|---|
| `delegated_signing_keys` table | Prisma migration. Columns: `id` (text PK, `dsk_` + ULID), `kid` (text, unique), `algorithm` (text, CHECK = `'ES256'`), `public_jwk` (jsonb), `protected_private_key` (jsonb — ciphertext, iv, auth_tag), `public_jwk_thumbprint` (text), `state` (text, CHECK IN PREPUBLISHED/ACTIVE/RETIRING/RETIRED/REVOKED), `created_at`, `activated_at`, `rotation_committed_at`, `verify_until`, `revoked_at`. Partial unique index `WHERE state = 'ACTIVE'`. |
| `PrivateKeyProtector` | AES-256-GCM implementation. `protect(private_key_pem, binding) → ProtectedKey`. `unprotect(protected, binding) → bytes`. `KeyBinding { kid, algorithm, public_jwk_thumbprint }` used as AAD. |
| Key lifecycle state machine | Five states: PREPUBLISHED → ACTIVE → RETIRING → RETIRED, plus emergency REVOKED. `verify_until = rotation_committed_at + max_signer_cache_age + max_token_ttl + allowed_clock_skew`. |
| `DelegatedTokenSigner` | `sign(claims) → str`. Atomically selects ACTIVE key, embeds `kid` in header, decrypts via `PrivateKeyProtector`, signs with ES256. |
| `DelegatedJwksPublisher` | `jwks_response(request_etag) → JwksHttpResponse`. Returns ACTIVE + RETIRING keys. ETag = SHA-256 of canonical response bytes. Cache-Control: `public, max-age={signer_cache_max_age}, must-revalidate`. 304 on ETag match. |
| `DelegatedTokenIssuer` | `issueForRequest({ req, res, workspaceSlug, signal }) → str`. Deep Module: deadline-guarded session → resolve slug → consistent membership/Team snapshot → assemble claims → sign. The BFF never assembles claims. |
| `GET /.well-known/jwks.json` | Endpoint backed by `DelegatedJwksPublisher`. |
| Team model additions | `authorizationEpoch`, `entitlementVersion`, `projectionVersion` (`Int @default(0)`) and `suspended Boolean @default(false)`. Removal/role change increments auth + projection; entitlement change increments entitlement + projection; suspension toggles suspended + auth + projection; member addition increments none. |
| Internal authorization snapshot Interface | `GET /api/internal/domain/workspaces/{workspace_id}/authorization-snapshot`; service-token only, `no-store`, exact six-field response, authoritative 404. `apiServiceAuthenticatedRoutes` is distinct from browser/session routes. |
| CSRF endpoint | `GET /api/csrf` — issues CSRF token bound to session, identified via HttpOnly cookie. |
| `CsrfTokenManager` | Browser-only. `getToken() → Promise<string>`. Single-flight coalescing. Memory cache with TTL. 401 clears cache. `csrf_validation_failed` (403) triggers one refresh + retry. |
| Authenticated route classes | `apiSelfAuthenticatedRoutes`: `['/api/domain/**', '/api/csrf']`; `apiServiceAuthenticatedRoutes`: the internal snapshot route. Browser session and service-token authentication never share a fallback. |
| BFF route handler | `pages/api/domain/v1/w/[slug]/[...path].ts`. Body parser disabled. One total deadline guards session, CSRF, body, token, proxy, validation, and write. No-body operations reject bytes. Gateway errors map to stable Problem Details. |
| `DomainProxy` | Server-only. Typed dispatch via `OPERATION_REGISTRY`; `undici` sole HTTP Adapter. Request headers built from empty set. Declared responses validate per status; valid undeclared Problem Details errors may pass transparently; undeclared success fails closed. |
| `DomainGateway` | Browser-only. `getIdentityProbe({ workspaceSlug })` only. Attaches `X-CSRF-Token` for non-safe methods only (GET omits it), `X-Trace-Id` for all. Uses `buildBffPath()` from generated manifest. |
| `buildBffPath()` | Generated from static allowlist manifest. Single source of truth for server matcher and browser path builder. |
| Generation pipeline | `scripts/generate_openapi.py`, `npx openapi-typescript`, `scripts/generate_operation_map.ts`, `scripts/generate_operation_registry.ts`, `scripts/generate_response_validators.ts`, `scripts/generate_paths.ts`, `scripts/generate_allowlist.ts`, `scripts/generate_allowlist_binding.ts`, `scripts/generate_all.sh`. |
| Seven generated artifacts | `web/lib/domain-client/generated/domain-v1.d.ts`, `operation-map.d.ts`, `operation-registry.ts`, `allowlist-binding.ts`, `validators.ts`, `paths.ts`, `allowlist.ts`. All CI-checked for drift against `contracts/openapi/marketai-domain-v1.json`. |
| `contracts/openapi/marketai-domain-v1.json` | OpenAPI 3.1 spec with `GET /api/v1/identity` only. |

#### FastAPI deliverables

| Deliverable | Detail |
|---|---|
| `DelegatedIdentityMiddleware` | Pure ASGI middleware (not `BaseHTTPMiddleware`). Protects `/api/v1/*` only — legacy `/api/*` routes (health, docs) outside scope. Verifies JWT, builds `TenantContext`, sets `scope["state"]["tenant_context"]`. Uses `scope.setdefault("state", {})`. Repairs claim-ahead projections through `WorkspaceProjectionSyncer`: only authoritative `NOT_FOUND` maps to 401; upstream/store inconsistency maps to retryable 503; a rate-limited repair preserves the epoch/write fail-closed result while entitlement-ahead reads may continue. |
| `JwksKeyStore` | Owns `httpx.AsyncClient`, ETag/key cache and hard-stale state. A 304 counts only with an installed ETag plus non-empty keys; empty/malformed sets raise `JwksRefreshError`, preserve the old cache, and trigger backoff. Unknown kid refresh is single-flighted. |
| `WorkspaceAdmissionReader` | Accepts session factory, hides session lifecycle + `anyio.to_thread.run_sync` internally. Exposes `async read(workspace_id) → WorkspaceAdmission`. Calls `marketai_workspace_admission()` SECURITY DEFINER function. |
| `WorkspaceProjectionSyncer` | Deep Module over `AuthorizationSnapshotFetcher` and `WorkspaceProjectionStore`. Production uses internal HTTP + PostgreSQL function Adapters; tests use memory; SQLite uses direct SQLAlchemy. Rate-limited by middleware. |
| `ControlPlaneAuthorizationSnapshotHttpAdapter` | Only production fetch Adapter. Domain/Worker never read the control-plane database. Uses service token, absolute timeout, exact schema/workspace-ID validation, and maps only 404 to authoritative not-found. |
| Projection SQL functions | `marketai_workspace_admission()` grants read to `marketai_domain_api` and `marketai_domain_worker`. `marketai_apply_workspace_projection(...)` grants write only to `marketai_domain_api`, validates inputs, bypasses RLS narrowly, fills required provisioning columns, and applies only a higher `projection_version`. |
| `workspaces` model/migration | SQLAlchemy + Alembic add `entitlement_version integer NOT NULL DEFAULT 0`, `suspended boolean NOT NULL DEFAULT false`, and `projection_version integer NOT NULL DEFAULT 0` (authorization epoch already exists). Existing required `name/default_currency/created_at` are supplied on initial projection insert. |
| Strict JWT validation | Raw token ≤ 4096 bytes. Algorithm: ES256 only. `typ`: `delegated+jwt`. All 13 claims required, no extras. `jti` format: `jti_<uuid>`. `sub`, `workspace_id`, `membership_id`: non-empty strings, reasonable length. `role`: `owner` or `member`. `authorization_epoch`, `entitlement_version`: non-negative integers. Clock skew: configurable (default 5s). |
| authorization_epoch gate | Fail closed on ANY mismatch (both directions) before domain handler. |
| entitlement_version gate | Reads continue when claim ahead. Non-safe methods (POST/PUT/PATCH/DELETE) blocked centrally by middleware, returning `entitlement_sync_pending`. CI enumerates all routes to verify none bypass. |
| `MARKETAI_LOCAL_IDENTITY_ENABLED` guard | Fails startup if `engine.dialect.name == "postgresql"` (covers all driver variants). Local identity path only valid with SQLite. |
| Readiness check | `/api/health/ready` verifies JWKS reachable + admission function callable. |
| Error responses | `WWW-Authenticate: Bearer` with `error` and `error_description` parameters. Problem Details (RFC 9457) body for all 4xx/5xx. |
| `/api/v1/identity` endpoint | Returns identity probe (workspace, actor, role, epoch, entitlement version) for the verified TenantContext. New v1 router alongside existing unversioned routes. |

#### Tracer negative tests

| Test | Expected outcome |
|---|---|
| Wrong `iss` | 401 |
| Wrong `aud` | 401 |
| Expired token (`exp` in past) | 401 |
| Token not yet valid (`nbf` in future beyond skew) | 401 |
| Unknown `kid` (after single-flight JWKS refresh) | 401 |
| Authorization epoch mismatch — claim ahead | 403 |
| Authorization epoch mismatch — claim behind | 403 |
| Forged `workspace_id` (doesn't match JWT) | Rejected — TenantContext built from JWT, not request |
| Forged `role` (doesn't match JWT) | Rejected — role from JWT, not request |
| Missing required claim | 401 |
| Extra claim in JWT payload | 401 |
| Algorithm downgrade (`RS256`) | 401 |
| `typ` header not `delegated+jwt` | 401 |
| Membership revoked (TeamMember row deleted) | 401 — token not issued |
| Session revoked (Session row deleted) | Token not issued — `requireSession` fails |
| Cross-workspace read via RLS | Empty result / permission denied |
| JWKS hard-stale (no successful refresh within window) | 503 |
| `jti` format invalid (not `jti_<uuid>`) | 401 |
| Raw token > 4096 bytes | 401 |
| Host header mismatch | 400 |
| Origin header mismatch | 403 |
| CSRF token invalid | 403 |
| Request to non-allowlisted BFF path | 404 |
| First JWKS response is empty or 304 without installed ETag/keys | Startup fails |
| Workspace suspension epoch ahead | Projection repairs; request returns 403 `workspace_suspended` before handler |
| Snapshot upstream unavailable | 503 + `Retry-After`, never false 401 |

### 8.2 Milestone 2 — Full #17 Delivery

After the tracer passes, the same PR expands to cover Operations, idempotency, and the file intent seam.

#### Additional deliverables

| Deliverable | Detail |
|---|---|
| `OperationRecord` table | `id` (`op_` + ULID), `workspace_id`, `initiator_id`, `command_type`, `state` (OperationState enum), `idempotency_key_hash`, `result_ref` (jsonb — `resource_type` + `resource_id`), `created_at`, `updated_at`. |
| `OperationStateEvent` table | `id`, `operation_id` (FK), `from_state`, `to_state`, `reason`, `actor`, `created_at`. Append-only audit trail. |
| `IdempotencyRecord` table | `key_hash` (SHA-256), `workspace_id`, `actor_id`, `command_type`, `request_fingerprint_hash`, `state` (`pending` / `committed`), `operation_id` (FK, nullable), `response_resource_type`, `response_resource_id`, `response_status`, `created_at`, `expires_at`. Unique on `(key_hash, workspace_id)`. CHECK constraints: pending rows have no outcome columns; committed rows have a status and bind exactly one of `operation_id` or `resource_type + resource_id`. |
| Operation state machine | `queued → running → succeeded / failed / partially_succeeded / cancelled / uncertain`. `uncertain` is non-terminal — only reconciler exits it. Explicit transition rules, no silent rollback. |
| Idempotency concurrency protocol | **Pending reservation pattern:** claim slot first (`state=pending`), create outcome, UPDATE to `committed`. Loser waits for winner via poll loop. No `ON CONFLICT DO NOTHING` that could create duplicate Operations. Same key + same fingerprint → return existing. Same key + different fingerprint/actor → 409. |
| `GET /api/v1/operations/{operation_id}` | Returns Operation projection with `ETag` and `Retry-After`. |
| `IntentTracker` | Browser-only. `acquire(actionInstanceId, operationId)` — policy looked up by `operationId` (not `actionInstanceId`). `registerPolicy(operationId, policy)` at startup. Page crash recovery: constructor transitions persisted `inflight` → `retained` (ambiguous outcome). `refineTimeBounds()` only narrows (Math.min). Retained intents NEVER silently deleted — persist until `resolve()` or `dismissReconciliation()`. Concurrent dedup into single in-flight promise. |
| `BlobStore` interface | `issue_upload`, `inspect_upload`, `commit_upload`, `issue_download`, `delete`. Value objects: `UploadSpec`, `UploadGrant`, `StagedBlobRef`, `InspectedUpload`, `ObjectConstraints`, `CommittedBlobRef`, `DownloadSpec`, `DownloadGrant`. |
| `S3BlobStore` adapter | Presigned POST policy with `content-length-range`. `inspect_upload` captures `VersionId` as `storage_revision`. `commit_upload` copies exact VersionId to committed prefix. Downloads sign exact `storage_version_id`. Bucket-level versioning. |
| `LocalFileBlobStore` Adapter | HMAC capabilities point to separate dev-only `LocalBlobTransferApp` on port 9000, never FastAPI. Streaming PUT enforces signed type/size/checksum into a temp file before atomic publish. `inspect_upload` records `st_dev:st_ino:st_mtime_ns:st_size`; transfer publish and Worker commit use the same per-key OS advisory lock. Signed GET binds and rechecks the committed revision. |
| `UploadIntentRecord` | State machine: `pending → verifying → finalized / rejected`, `verifying → pending` (transient error), `pending → expired`. CAS lease. `authorization_epoch_at_issue`. `owner_kind` + `owner_id` for object binding. `verification_lease_id` + `verification_lease_expires_at`. `rejection_code`. `committed_blob_ref_key` + `committed_blob_ref_version`. Unique constraint on `upload_intent_id`. |
| `DownloadIntentRecord` | `id` (`di_` + ULID), `workspace_id`, `initiator_id`, `owner_kind`, `owner_id`, `committed_blob_ref_key`, `committed_blob_ref_version`, `created_at`, `expires_at`. |
| `create_upload_intent` endpoint | `POST /api/v1/upload-intents` → 201. Validates Actor, Workspace, purpose, MIME allowlist, size limit, cost policy. Generates staging key. Idempotent replay returns same record with re-issued capability capped at original `expires_at`. Expired → 410. |
| `finalize_upload` endpoint | `POST /api/v1/upload-intents/{intent_id}/finalize` → 202 Operation. Creates Operation + Job in single transaction. Worker does full verification sequence. |
| `create_download_intent` endpoint | `POST /api/v1/download-intents` → 201. Validates Actor, Workspace ownership. Idempotent replay returns same record with re-issued capability. |
| Object key generation | Staging: `staging/{ws}/{purpose}/{YYYY}/{MM}/{DD}/{ulid}.dat` (fixed extension, MIME unknown before upload). Committed: `committed/{ws}/{purpose}/{YYYY}/{MM}/{DD}/{ulid}.{ext}` (extension from verified MIME). No filename in key. Components validated against `[a-zA-Z0-9_-]`. |
| MIME validation | Magic-byte sniffing (first 8KB range read). Purpose policy declares allowed verified MIMEs. `Content-Type` metadata not trusted. |
| Storage lifecycle rules | Staging prefix: 4 rules (expire current versions 24h, delete noncurrent versions 1d, remove expired delete markers, abort incomplete multipart 1d). Objects become eligible, not guaranteed deleted (S3 lifecycle is async). Worker cleanup is timely primary path. Committed prefix: no expiration lifecycle. |
| Object storage CORS/origin policy | Production S3: `APP_URL` only, HTTPS. Local storage app: `http://localhost:3000` only. Browser `StorageOriginPolicy` uses `NEXT_PUBLIC_MARKETAI_STORAGE_ORIGINS`; local default grant origin is `http://localhost:9000`. |
| `BrowserBlobTransferClient` | Browser-only binary-transfer Module. Injected `StorageOriginPolicy` checks each grant destination; presigned POST and signed PUT are handled without credentials or redirect following; download grants are checked before user navigation and never persisted. |
| BlobStore contract tests | Shared Interface suite never branches on Adapter identity and proves no replacement bytes can be committed. Separate S3/Local capability suites verify VersionId strength versus overwrite detection, lock and atomic-rename behavior. |
| DomainGateway expansion | `pollOperation`, `createUploadIntent`, `finalizeUpload`, `createDownloadIntent` methods added. UI workflows compose it with `BrowserBlobTransferClient`; the BFF gateway does not absorb binary transport. |

#### File intent negative tests

| Test | Expected outcome |
|---|---|
| Expired upload intent | 410 `upload_intent_expired` |
| Cross-Workspace upload intent | Rejected |
| Different Actor finalizing | Rejected |
| Authorization epoch stale (membership removed + re-added between intent and verification) | Operation fails `authorization_epoch_stale` |
| Workspace suspended between intent and verification | Operation fails |
| Wrong checksum at finalize | Rejected, `rejection_code` set |
| Wrong size at finalize | Rejected |
| Wrong MIME (magic-byte sniff vs. purpose allowlist) | Rejected, `mime_type_not_allowed` |
| Staging object deleted before commit (`revision_gone`) | Rejected |
| Staging object replaced after inspection (`ObjectChangedError`) | Rejected as `object_replaced_during_verification`; new upload required |
| Concurrent finalize attempts | Only one Worker acquires verifying lease |
| Replay `create_upload_intent` after expiry | 410, not a new intent |
| Staging key path traversal | Validation rejects key components |
| Signed URL not in structured logs | Grep confirms redaction |
| Storage CORS rejects non-allowlisted origin | Browser blocked |
| Idempotent replay with different fingerprint | 409 `idempotency_key_reused` |
| Download intent for asset in different Workspace | Rejected |
| Download intent for deleted/expired asset | Rejected |

---

## 9. Testing Strategy

### 9.1 Unit Tests (no I/O)

Unit tests run without any database, network, or filesystem dependency. They use in-memory adapters where needed.

| Module | Tests |
|---|---|
| `PrivateKeyProtector` | Encrypt → decrypt roundtrip produces original PEM. Binding (AAD) prevents transplant: ciphertext from key A fails `unprotect` with key B's binding. Tampered ciphertext → authentication failure. Tampered auth tag → authentication failure. |
| Key lifecycle state machine | Valid transitions: PREPUBLISHED → ACTIVE, ACTIVE → RETIRING, RETIRING → RETIRED, any → REVOKED. Invalid transitions: RETIRED → ACTIVE, PREPUBLISHED → RETIRING, REVOKED → any. Only one ACTIVE key at a time. |
| JWT claim assembly | All 13 claims present with correct types. No extra claims. `jti` matches `jti_<uuid>` format. `exp - iat <= 60`. `typ` header is `delegated+jwt`. `alg` is `ES256`. `kid` matches active key. |
| `CsrfTokenManager` | Single-flight: two concurrent `getToken()` calls produce one fetch (MemoryTransport). 401 from any BFF call clears cache. `csrf_validation_failed` triggers one refresh + retry; second failure surfaces error. Token cached until TTL. |
| `IntentTracker` | `acquire(actionInstanceId, operationId)` → `resolve` lifecycle (MemoryStorage). `acquire` → `retain` → `acquire` returns same intent (refresh recovery). Same `actionInstanceId` after resolve → new intent. After `safeRetryUntil` with retained intent → `ReconciliationRequired` (never silently deleted). Page crash recovery: persisted inflight → retained on construction. `refineTimeBounds` only narrows (Math.min). `registerPolicy(operationId, ...)` before use. Concurrent `acquire` for same `actionInstanceId` → single in-flight promise. `dismissReconciliation` clears retained intent. |
| `BrowserBlobTransferClient` / `StorageOriginPolicy` | Exact normalized destination origin accepted; wildcard, credentials, fragment, wrong effective port, and non-HTTPS production origin rejected. Presigned POST includes every signed field plus the file; signed PUT forwards exactly signed headers. Both omit credentials and reject redirects. Download grant is checked before navigation and never written to browser storage. |
| `WorkspaceAdmissionReader` | `read()` with valid workspace returns `WorkspaceAdmission`. Missing workspace raises `WorkspaceNotFoundError`. NULL-row (function returns NULL) raises `WorkspaceNotFoundError`. Thread offload: `_read_sync` runs in worker thread (verify `anyio.to_thread.run_sync` called). Session closed in `finally` block even on error. |
| `WorkspaceProjectionSyncer` | The orchestration is tested against in-memory `AuthorizationSnapshotFetcher` and `WorkspaceProjectionStore` Adapters. A higher `projection_version` applies the mutable authorization fields and version; an equal/lower version is a no-op. Every semantic change, including a suspension-only change, advances `projection_version`. A snapshot whose higher `projection_version` attempts to lower either semantic counter is rejected. Initial provisioning supplies snapshot `workspace_name` plus locally configured `default_currency`; those bootstrap fields are not subsequently treated as authorization state. Fetch `NOT_FOUND`, timeout, authentication failure, malformed payload, and store failure remain distinct outcomes. No test or production path reads the control-plane database directly. |
| `DelegatedIdentityMiddleware` repair mapping | On a missing local row or claim-ahead mismatch, `SYNCED` re-reads admission before proceeding. Only authoritative `NOT_FOUND` returns 401. `UPSTREAM_UNAVAILABLE`, store failure, or `SYNCED` followed by an unreadable row returns retryable 503 with `Retry-After`. `RATE_LIMITED` never opens the gate for an epoch mismatch or an entitlement-ahead write; an entitlement-ahead read may use the local projection. No domain handler runs on an epoch mismatch or suspended projection. |
| `JwksCache` | Initial 200 with a non-empty valid set succeeds. Initial 200 with an empty set, initial 304, and 304 without both an existing ETag and non-empty cached keys raise `JwksInitialLoadError`. Refresh 200 with an empty/invalid set raises `JwksRefreshError`, retains the prior cache, and enters backoff. A valid 304 only advances freshness when both preconditions hold. Unknown-`kid` refresh is single-flight. |
| `DomainResult` type discrimination | Compiler enforces: `get_identity_probe` result cannot be `201` or `202`. `create_upload_intent` result includes `201` variant. `finalize_upload` result includes `202` variant. `kind` discrimination is exhaustive per operation. |
| Object key generation | Valid components → well-formed key. Each component validated against `[a-zA-Z0-9_-]`. `..` rejected. `\0` rejected. `/` in component rejected. Control characters rejected. Extension derived from MIME, not user input. |
| Operation state machine | Valid transitions: `queued → running`, `running → succeeded`, `running → failed`, `running → uncertain`, `uncertain → succeeded` (reconciler only). Invalid: `succeeded → running`, `failed → running`, `uncertain → failed` (non-reconciler). Append-only event creation on every transition. |
| Allowlist binding validation | Missing `operationId` → startup error. Method mismatch → startup error. Path param mismatch → startup error. Missing query coverage → startup error. |
| Request/response boundary | Request to FastAPI contains only allowed headers (Authorization, Content-Type, Content-Length, Idempotency-Key, X-Request-Id, X-Trace-Id, Accept). No browser headers are forwarded. Response to browser contains only allowed headers. No-body operations reject a non-empty or streaming body. The single absolute deadline aborts session lookup, CSRF validation, request-body read, token issuance, each proxy attempt, and response-body validation; a late completion is ignored. Declared responses validate against their operation schema. Undeclared success responses fail closed; undeclared error responses pass through only as valid `application/problem+json`. |

### 9.2 Integration Tests (real database)

Integration tests run against real PostgreSQL with RLS and role isolation, and (for BlobStore) against real storage backends.

| Test suite | Tests |
|---|---|
| Delegated signing key CRUD | Create key with encryption → read back → decrypt → verify PEM matches. Key state transitions persist correctly. Partial unique index enforces single ACTIVE key. |
| JWKS endpoint | Returns ACTIVE + RETIRING keys. ETag changes when key set changes. 304 on matching ETag. Cache-Control headers correct. Removing last RETIRING key changes ETag. ACTIVE → RETIRING with new ACTIVE changes key set. |
| Internal authorization-snapshot endpoint | The service credential is required; browser session, CSRF, and CORS are not accepted as substitutes. A successful response contains exactly `workspace_id`, `workspace_name`, `authorization_epoch`, `entitlement_version`, `suspended`, and `projection_version`, with `Cache-Control: no-store`. Unknown Workspace returns 404. Database/transport faults are never translated to 404. The route appears only in `apiServiceAuthenticatedRoutes`. |
| Full identity flow | `requireSession` → `issueForRequest` → FastAPI `DelegatedIdentityMiddleware` → `TenantContext` created → domain handler receives correct `workspace_id`, `role`, `initiator_id`. Two different workspaces → two different TenantContexts → RLS isolates data. |
| WorkspaceAdmissionReader integration | Real database: `read()` returns correct admission for existing workspace. Missing workspace raises `WorkspaceNotFoundError`. Thread offload does not block event loop (concurrent reads complete). Session factory creates independent sessions (no session leaks). PUBLIC/unrelated roles cannot execute the SECURITY DEFINER function; only `marketai_domain_api` and `marketai_domain_worker` can. |
| WorkspaceProjectionSyncer integration | The real HTTP fetch Adapter calls the internal endpoint; it never opens a control-plane database connection. In PostgreSQL, the `marketai_domain_api` role can execute the SECURITY DEFINER apply function but cannot directly bypass tenant RLS; PUBLIC, unrelated roles, and the Worker role cannot execute it. Initial sync inserts snapshot name, configured currency, and all authorization fields. A higher `projection_version` atomically applies `authorization_epoch`, `entitlement_version`, `suspended`, and the version; equal/lower versions are no-ops. Concurrent different versions converge on the highest version. Suspension-only changes apply. A higher version that regresses either semantic counter is rejected. HTTP 404 maps to `NOT_FOUND`; timeout/401/403/5xx/malformed payload map to `UPSTREAM_UNAVAILABLE`. |
| authorization_epoch gate | Initial epoch 0 → token with epoch 0 accepted. Increment epoch to 1 → token with epoch 0 rejected. New token with epoch 1 accepted. Increment to 2 → token with epoch 1 rejected. Both directions: claim ahead also rejected. |
| entitlement_version gate | Claim ahead of local projection → GET succeeds, POST blocked with `entitlement_sync_pending`. Claim behind → local (newer) projection used. Claim matches → all methods allowed. |
| Cross-workspace isolation | User A in Workspace X creates data. User B in Workspace Y cannot read it (RLS). User A with Workspace Y token cannot read Workspace X data. |
| Operation + idempotency | Create Operation with idempotency key. Replay with same key + same fingerprint → same Operation returned. Replay with same key + different fingerprint → 409. Operation state transitions persist events. Concurrent inserts: only one succeeds, other gets existing. |
| Upload intent lifecycle | Create intent → issue capability → finalize → Worker verifies → committed blob → domain resource created via CommittedBlobRef. Expired intent → 410. Cross-workspace → rejected. Different actor → rejected. |
| Download intent lifecycle | Create intent → signed URL returned → download succeeds. Cross-workspace → rejected. Deleted asset → rejected. |
| BlobStore shared Interface contract | Run unchanged against both Adapters: issue → inspect → commit → exact download; commit is either the exact inspected revision or a typed failure. Replacement after inspection produces `ObjectChangedError`; deletion produces `RevisionGoneError`. Verify concurrent inspect/commit behavior and that the Worker branches only on Interface results, never Adapter identity. |
| S3 Adapter capability tests | Presigned POST constraints, bucket/versioning preflight, exact `version_id` copy/download, stale/deleted version behavior, checksum and content-type conditions, and storage CORS. |
| Local Adapter capability tests | The separate localhost transfer process enforces signed method/key/expiry/content type/checksum/min/max size while streaming to a temporary file. Verify too-small/too-large bodies, checksum mismatch, disconnect cleanup, traversal and symlink escape rejection, atomic rename, replacement detection under lock, exact GET, exact-origin CORS, and that the transfer app is never mounted in `app/main.py`. |

### 9.3 Negative and Security Tests

These tests specifically verify fail-closed behavior and attack resistance.

| Category | Tests |
|---|---|
| JWT manipulation | Wrong issuer, wrong audience, expired, not-yet-valid, unknown kid, missing claims, extra claims, algorithm downgrade, invalid `typ`, invalid `jti` format, oversized token, malformed base64, truncated signature. |
| Identity boundary | Membership revoked → token not issued. Session revoked → `requireSession` fails. Cross-workspace read → RLS blocks. Forged claims → TenantContext from JWT, not request. |
| JWKS resilience | First startup with empty 200 or 304 fails closed. Refresh empty 200, malformed set, invalid key, or 304 without valid cache state raises a refresh error, retains any prior good keys, and triggers backoff. JWKS endpoint down uses the prior non-empty cache only until hard-stale; hard-stale rejects all tokens with 503. |
| BFF gate | Host mismatch → 400. Origin mismatch → 403. Missing Origin → 403. CSRF invalid → 403. CSRF missing → 403. Malformed, separator-bearing, or double-encoded path segment → 400. Non-allowlisted path → 404. Body on a no-body operation → 400. Request body larger than its generated limit → 413. A timeout in session/CSRF/body/token/proxy/response validation → 504 with the original trace ID. Undeclared success or malformed/wrong-content-type undeclared error → 502. Valid undeclared Problem Details error → transparent pass-through. Missing generated request validator fails at startup/typecheck and remains a runtime 500 fail-closed guard. |
| Size limits | Request body > `maxRequestBytes` → 413. Response body > `maxResponseBytes` → 502. |
| File intent security | All tests from Section 8.2 file intent negative tests table. |
| Audit safety | Signed URLs not in structured log output (grep verification). JWT not in browser-accessible storage, URL, analytics, or error traces. `DELEGATED_SIGNING_KEY_ENCRYPTION_KEY` not in logs. |

### 9.4 CI Checks

| Check | Mechanism |
|---|---|
| OpenAPI drift | `scripts/generate_all.sh` regenerates all seven artifacts; `git diff --exit-code` on generated files. |
| Breaking change detection | Compare committed `contracts/openapi/marketai-domain-v1.json` against previous version. Reject: field removal, type narrowing, enum value removal, optional → required, endpoint removal. Allow: new optional fields, new endpoints, new enum values (only if field marked extensible). |
| Generated artifact drift | All seven files (`domain-v1.d.ts`, `operation-map.d.ts`, `operation-registry.ts`, `allowlist-binding.ts`, `validators.ts`, `paths.ts`, `allowlist.ts`) match regenerated output. |
| Generated TypeScript compilation | After generation, `npm run typecheck` compiles `allowlist.ts`, `DomainProxy`, and `DomainGateway` against `DomainOperationMap`. This catches invalid relative imports, method/path/request-body discrimination, response unions, and validator-map key drift. |
| Entitlement gate coverage | CI script enumerates all POST/PUT/PATCH/DELETE routes in the v1 router and verifies each has centralized entitlement_version middleware applied. |
| Validator coverage | Every response `validatorId` in `operation-registry.ts` resolves in `RESPONSE_VALIDATORS`. The generated `RequestValidatorMap` has exactly every operation whose `requestBody` is not `never`; `allowlist.ts` references those required properties directly, so omission or an extra/mistyped validator fails generation/typecheck rather than degrading to `null`. |

---

## 10. Configuration

### 10.1 Control Plane (Next.js) — New Environment Variables

| Variable | Type | Required | Default | Description |
|---|---|---|---|---|
| `DELEGATED_SIGNING_KEY_ENCRYPTION_KEY` | string (32 bytes, base64-encoded) | Yes (production) | — | AES-256-GCM encryption key for delegated signing key private key material. Must be supplied from a secret manager. |
| `SIGNER_CACHE_MAX_AGE` | integer (seconds) | No | `600` | `max-age` for JWKS `Cache-Control` header; also used in `verify_until` calculation. |
| `SIGNER_CACHE_REFRESH_INTERVAL_SECONDS` | integer | No | `300` | How often the BFF refreshes the signing key from the database. |
| `SIGNER_CACHE_JITTER_MAX_SECONDS` | integer | No | `30` | Maximum random delay before first key load on startup (prevents thundering herd on multi-instance deploy). |
| `DELEGATED_TOKEN_ISSUER` | string | No | `urn:marketai:control-plane` | JWT `iss` claim value. |
| `DELEGATED_TOKEN_AUDIENCE` | string | No | `urn:marketai:domain-api` | JWT `aud` claim value. |
| `DELEGATED_TOKEN_MAX_TTL_SECONDS` | integer | No | `60` | Maximum JWT lifetime in seconds. `exp - iat` will not exceed this value. |
| `PROJECTION_SYNC_SERVICE_TOKEN` | string | Yes (production) | — | Service credential accepted only by the internal authorization-snapshot route. Must match FastAPI `MARKETAI_PROJECTION_SYNC_SERVICE_TOKEN`. |
| `DOMAIN_API_BASE_URL` | string | Yes | — | FastAPI internal base URL. Never exposed to the browser. Used by `DomainProxy` to construct upstream requests. |
| `DOMAIN_API_MAX_CONNECTIONS` | integer | No | `20` | Maximum connections in the process-local `undici` pool for FastAPI. |
| `DOMAIN_API_HEADERS_TIMEOUT_MS` | integer | No | `10000` | `undici` `headersTimeout` — maximum wait for response headers from FastAPI per attempt. |
| `DOMAIN_API_BODY_TIMEOUT_MS` | integer | No | `30000` | `undici` `bodyTimeout` — maximum stall time (no data received) during response body read per attempt. |
| `DOMAIN_API_MAX_RETRIES` | integer | No | `1` | Maximum retry attempts for retryable failures (network error before response). Per §4.9, only 1 automatic retry (2 total attempts). |
| `NEXT_PUBLIC_MARKETAI_STORAGE_ORIGINS` | string (comma-separated origins) | Yes (production) | `http://localhost:9000` | Public, non-secret exact destination origins accepted by `StorageOriginPolicy`. Production entries must be HTTPS and match the origins emitted in storage grant URLs. |
| `TOTAL_DEADLINE_MS` | integer | No | `55000` | Absolute deadline in milliseconds for the entire BFF handler lifecycle (session, CSRF, body read, token mint, proxy, response validation). Caps all nested `undici` stall timeouts. |
| `ALLOWED_CLOCK_SKEW_SECONDS` | integer | No | `5` | Clock skew tolerance for `verify_until` calculation when issuing delegated tokens. Should match `DELEGATED_JWT_ALLOWED_CLOCK_SKEW_SECONDS` on FastAPI. |
| `CSRF_TOKEN_TTL_SECONDS` | integer | No | `3600` | CSRF token validity period in seconds. |

### 10.2 FastAPI — New Environment Variables

| Variable | Type | Required | Default | Description |
|---|---|---|---|---|
| `DELEGATED_JWKS_URL` | string | Yes (production) | — | Control-plane JWKS endpoint URL (`https://<app>/.well-known/jwks.json`). |
| `DELEGATED_JWKS_REFRESH_INTERVAL_SECONDS` | integer | No | `300` | Periodic JWKS refresh interval. |
| `DELEGATED_JWKS_MAX_STALE_SECONDS` | integer | No | `600` | Hard-stale threshold. If no successful JWKS refresh (including 304) within this window, all delegated tokens are rejected. |
| `DELEGATED_JWT_ISSUER` | string | No | `urn:marketai:control-plane` | Expected JWT `iss` value. Must match control-plane `DELEGATED_TOKEN_ISSUER`. |
| `DELEGATED_JWT_AUDIENCE` | string | No | `urn:marketai:domain-api` | Expected JWT `aud` value. Must match control-plane `DELEGATED_TOKEN_AUDIENCE`. |
| `DELEGATED_JWT_ALLOWED_CLOCK_SKEW_SECONDS` | integer | No | `5` | Maximum tolerated clock difference between control-plane and FastAPI for `iat`, `nbf`, `exp` validation. |
| `DELEGATED_JWT_MAX_TOKEN_BYTES` | integer | No | `4096` | Maximum raw JWT string length. Tokens exceeding this are rejected before parsing. |
| `DELEGATED_TOKEN_MAX_TTL_SECONDS` | integer | No | `60` | Maximum allowed `exp - iat` for delegated tokens. Must match control-plane `DELEGATED_TOKEN_MAX_TTL_SECONDS`. Tokens with a longer TTL are rejected. |
| `MARKETAI_CONTROL_PLANE_INTERNAL_BASE_URL` | string | Yes (production) | — | Private control-plane origin for the authorization-snapshot HTTP Adapter. Domain/Worker database identities must not be able to connect to the control-plane database. |
| `MARKETAI_PROJECTION_SYNC_TIMEOUT_MS` | integer | No | `3000` | Absolute timeout for one authorization-snapshot HTTP request. |
| `MARKETAI_BLOB_STORE_ADAPTER` | string | No | `local` | Storage backend: `s3` (S3-compatible) or `local` (filesystem with HMAC-signed URLs). |
| `MARKETAI_BLOB_STORE_BUCKET` | string | Yes (when `s3`) | — | S3 bucket name. Must have versioning enabled. |
| `MARKETAI_BLOB_STORE_REGION` | string | Yes (when `s3`) | — | AWS region for the S3 bucket. |
| `MARKETAI_BLOB_STORE_ENDPOINT_URL` | string | No | — | S3-compatible endpoint URL (for MinIO or other non-AWS providers). Omit for AWS S3. |
| `MARKETAI_BLOB_STORE_STAGING_PREFIX` | string | No | `staging/` | Object key prefix for staged (unverified) uploads. |
| `MARKETAI_BLOB_STORE_COMMITTED_PREFIX` | string | No | `committed/` | Object key prefix for verified, committed blobs. |
| `MARKETAI_BLOB_STORE_LOCAL_ROOT` | string | No | `./data/blobs` | Root directory for the local filesystem blob adapter. Development and CI only. |
| `MARKETAI_BLOB_STORE_LOCAL_SIGNING_SECRET` | string | Yes (when `local`) | — | HMAC-SHA256 secret for signing local blob transfer URLs. At least 32 bytes. Development only. |
| `MARKETAI_BLOB_STORE_LOCAL_PUBLIC_BASE_URL` | string | No | `http://localhost:9000` | Public base URL for the separate `LocalBlobTransferApp`. Must appear in `NEXT_PUBLIC_MARKETAI_STORAGE_ORIGINS`. |
| `MARKETAI_BLOB_STORE_LOCAL_CORS_ORIGIN` | string | No | `http://localhost:3000` | Allowed CORS origin for the local blob transfer endpoint. Must match the BFF's origin. Analogous to S3 bucket CORS configuration. |
| `MARKETAI_UPLOAD_INTENT_TTL_SECONDS` | integer | No | `300` | Upload intent validity period. Presigned capability expiry is capped to this value. |
| `MARKETAI_DOWNLOAD_INTENT_TTL_SECONDS` | integer | No | `60` | Download intent validity period. |
| `PROJECTION_REPAIR_INTERVAL_SECONDS` | integer | No | `60` | Minimum interval between projection repair triggers per workspace. Rate-limits `WorkspaceProjectionSyncer.sync()` calls. |
| `MARKETAI_PROJECTION_SYNC_SERVICE_TOKEN` | string | Yes (production) | — | Service identity token for authenticating with the control-plane authorization snapshot endpoint. Not required when `MARKETAI_LOCAL_IDENTITY_ENABLED` is true. |
| `MARKETAI_LOCAL_IDENTITY_ENABLED` | boolean | No | `false` | Pre-existing. When true, uses a local identity bypass instead of delegated JWT verification. Must be false when `engine.dialect.name == "postgresql"`. See §3.8. |
| `MARKETAI_IDEMPOTENCY_RETENTION_SECONDS` | integer | No | `86400` | How long idempotency records are retained before becoming eligible for cleanup. `IntentTracker.safeRetryUntil` must not exceed this value. |

S3 credentials (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`) follow the standard AWS SDK credential chain and are not listed here. For MinIO in local development, these are configured in `.env`.

FastAPI application/storage variables use the `MARKETAI_` prefix to align
with existing configuration. Delegated identity variables use the
`DELEGATED_` namespace, and the repair limiter retains the already specified
`PROJECTION_REPAIR_INTERVAL_SECONDS` name. These are the only exceptions.

### 10.3 Relationship Constraints

- `DELEGATED_TOKEN_ISSUER` (control plane) must equal `DELEGATED_JWT_ISSUER` (FastAPI).
- `DELEGATED_TOKEN_AUDIENCE` (control plane) must equal `DELEGATED_JWT_AUDIENCE` (FastAPI).
- `DELEGATED_TOKEN_MAX_TTL_SECONDS` (control plane) must equal `DELEGATED_TOKEN_MAX_TTL_SECONDS` (FastAPI). Both sides enforce the TTL bound.
- `PROJECTION_SYNC_SERVICE_TOKEN` (control plane) must equal `MARKETAI_PROJECTION_SYNC_SERVICE_TOKEN` (FastAPI). It is never exposed through a `NEXT_PUBLIC_` variable or browser response.
- `DELEGATED_TOKEN_MAX_TTL_SECONDS` must not exceed `DELEGATED_JWKS_MAX_STALE_SECONDS` (otherwise tokens could outlive the JWKS staleness window).
- `DOMAIN_API_HEADERS_TIMEOUT_MS` should be less than `DELEGATED_TOKEN_MAX_TTL_SECONDS * 1000` (a single attempt must receive headers before the JWT it carries expires).
- `TOTAL_DEADLINE_MS` encompasses the full BFF handler lifecycle — session verification, CSRF, body read, token mint, proxy dispatch (including retries), response validation. `DOMAIN_API_HEADERS_TIMEOUT_MS` and `DOMAIN_API_BODY_TIMEOUT_MS` are `undici`-level stall timeouts within each proxy attempt. The `AbortSignal.timeout(TOTAL_DEADLINE_MS)` created at handler step 1 is the overall cap; the per-attempt timeouts are nested within it.
- `MARKETAI_IDEMPOTENCY_RETENTION_SECONDS` must be greater than or equal to `IntentTracker.safeRetryUntil` (browser must not retry after server has forgotten the idempotency record).
- `MARKETAI_UPLOAD_INTENT_TTL_SECONDS` must be less than the staging prefix lifecycle expiry (24 hours) minus a safety margin.
- `NEXT_PUBLIC_MARKETAI_STORAGE_ORIGINS` must contain the exact destination origin produced by the configured BlobStore Adapter. Separately, the bucket/local storage CORS `AllowedOrigin` must equal control-plane `APP_URL`, because that is the browser page's source origin. Local destination defaults converge on `http://localhost:9000`, local CORS defaults to `http://localhost:3000`, and production destinations are HTTPS only.

---

## 11. Excluded from #17

The following are explicitly out of scope for this ticket and will be addressed by downstream tickets:

| Exclusion | Rationale |
|---|---|
| Full feature APIs (generate, experiments, localization, scale, library) | #17 delivers infrastructure seams only. Feature endpoints are implemented by their respective tickets per the v9 dependency map (#14). |
| Worker scheduled cleanup job for expired upload intents | #17 delivers the intent state machine including `expired` state and the `BlobStore.delete` interface. The Worker job that scans and cleans up is a separate concern. Storage lifecycle rules provide a 24-hour safety net. |
| Key rotation automation | #17 supports manual key rotation (create PREPUBLISHED → activate → previous becomes RETIRING). Automated scheduled rotation is a future operational concern. |
| WebSocket or SSE for Operation status | #17 uses HTTP polling with `ETag` and `Retry-After`. SSE is only justified after evidence that polling is insufficient (per integration contract §8). |
| Multipart upload support | #17 supports single-object uploads only (presigned POST for S3, signed PUT for local). Multipart/chunked uploads are a future concern. |
| Old route removal | New `/api/v1/` router is added alongside existing unversioned routes. Migration of existing routes to v1 is gradual, handled by subsequent tickets. The two routers coexist without conflict. |
| Full Playwright end-to-end tests | #17 delivers unit tests (MemoryTransport/MemoryStorage adapters), integration tests (real database + storage), and negative/security tests. Full Playwright browser automation across six functional areas is part of the integration contract's acceptance gate (§13), not this ticket. |
