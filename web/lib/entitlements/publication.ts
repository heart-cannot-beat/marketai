import crypto from 'node:crypto';
import type { PrismaClient } from '@prisma/client';
import { Prisma } from '@prisma/client';

import { signEnvelope } from './signing';
import { resolveEntitlements, computeSemanticHash } from './resolution';
import type {
  PlanVersionEntitlements,
  RestrictionInput,
  RestrictionSummaryEntry,
  SignedEnvelope,
  SnapshotPayload,
  SourceRef,
} from './types';

/**
 * Database (uppercase) -> wire (lowercase) lifecycle mapping (spec §1.13).
 * `Team.lifecycle` is a plain `String` column (not a Prisma enum), so an
 * unrecognized value is a real possibility (bad seed data, a migration gap)
 * rather than something the type system rules out — `buildSnapshotPayload`
 * throws rather than silently publishing an unmapped lifecycle string.
 */
const LIFECYCLE_WIRE_MAP: Record<string, string> = {
  PROVISIONING: 'provisioning',
  ACTIVE: 'active',
  SUSPENDED: 'suspended',
  DELETION_PENDING: 'deletion_pending',
  DELETED: 'deleted',
};

/**
 * Everything `buildSnapshotPayload` needs to build one `SnapshotPayload`.
 * Deliberately flat and Prisma-shape-agnostic: callers (the publication
 * transaction, tests) build this from whatever rows they have in hand, so
 * this function itself never touches Prisma.
 */
export interface BuildSnapshotInput {
  snapshotId: string;
  team: {
    id: string;
    name: string;
    lifecycle: string;
    suspended: boolean;
    authorizationEpoch: number;
    projectionVersion: number;
  };
  entitlementVersion: number;
  leaseSequence: number;
  planVersionId: string;
  sourceRefs: SourceRef[];
  periodStart: Date;
  periodEnd: Date;
  periodAnchor: Date;
  resolvedEntitlements: Record<string, boolean | number>;
  restrictionSummary: RestrictionSummaryEntry[];
  semanticHash: string;
  registryVersion: number;
  resolutionPolicyVersion: number;
  platformLimitsVersion: number;
  issuedAt: Date;
  validUntil: Date;
  auditEventId: string;
}

/**
 * Builds the exact `SnapshotPayload` shape signed and stored verbatim in
 * `canonicalEnvelope` (spec §1.9, §1.14). Built field-by-field (never
 * spread) so key order — and therefore the signed bytes — never depends on
 * caller construction order, matching the canonicalization discipline
 * `signing.ts` and `resolution.ts` already follow.
 */
export function buildSnapshotPayload(
  input: BuildSnapshotInput
): SnapshotPayload {
  const lifecycle = LIFECYCLE_WIRE_MAP[input.team.lifecycle];
  if (!lifecycle) {
    throw new Error(`Unknown lifecycle: ${input.team.lifecycle}`);
  }

  return {
    snapshot_id: input.snapshotId,
    workspace_id: input.team.id,
    workspace_name: input.team.name,
    authorization_epoch: input.team.authorizationEpoch,
    entitlement_version: input.entitlementVersion,
    lease_sequence: input.leaseSequence,
    projection_version: input.team.projectionVersion,
    suspended: input.team.suspended,
    lifecycle,
    entitlement_snapshot: {
      plan_version_id: input.planVersionId,
      source_refs: input.sourceRefs,
      period_start: input.periodStart.toISOString(),
      period_end: input.periodEnd.toISOString(),
      period_anchor: input.periodAnchor.toISOString(),
      resolved_entitlements: input.resolvedEntitlements,
      restriction_summary: input.restrictionSummary,
      semantic_hash: input.semanticHash,
      entitlement_registry_version: input.registryVersion,
      resolution_policy_version: input.resolutionPolicyVersion,
      platform_limits_version: input.platformLimitsVersion,
      issued_at: input.issuedAt.toISOString(),
      valid_until: input.validUntil.toISOString(),
      audit_event_id: input.auditEventId,
    },
  };
}

/** The subset of `CommercialAccessProjection` publication needs to decide versions. */
export interface PublicationVersionState {
  entitlementVersion: number;
  leaseSequence: number;
  semanticHash: string;
}

export interface PublicationVersionDecision {
  entitlementVersion: number;
  leaseSequence: number;
  /** Whether step 3's `semanticHash != projection.semanticHash` fired. */
  semanticVersionChanged: boolean;
}

/**
 * Publication transaction steps 2-4 (spec §1.7), isolated as a pure
 * function so the version-bump invariants are unit-testable without a
 * database: `entitlementVersion` only advances when the semantic hash
 * actually changed; `leaseSequence` advances unconditionally on every
 * publication (renewal or not) and never resets, even across an
 * `entitlementVersion` bump.
 */
export function decidePublicationVersions(
  existing: PublicationVersionState,
  newSemanticHash: string
): PublicationVersionDecision {
  const semanticVersionChanged = existing.semanticHash !== newSemanticHash;
  return {
    entitlementVersion: semanticVersionChanged
      ? existing.entitlementVersion + 1
      : existing.entitlementVersion,
    leaseSequence: existing.leaseSequence + 1,
    semanticVersionChanged,
  };
}

/**
 * Adds `months` whole calendar months to `anchor`, in UTC, clamping the
 * day-of-month to the last day of a shorter target month (e.g. an anchor
 * of the 31st lands on the 30th in a 30-day month) — standard
 * monthly-billing-cycle semantics. `Date.UTC` normalizes an out-of-range
 * month index itself, so `months` may be negative or exceed 12.
 */
function addMonthsUTC(anchor: Date, months: number): Date {
  const targetYear = anchor.getUTCFullYear();
  const targetMonthIndex = anchor.getUTCMonth() + months;
  const daysInTargetMonth = new Date(
    Date.UTC(targetYear, targetMonthIndex + 1, 0)
  ).getUTCDate();
  const day = Math.min(anchor.getUTCDate(), daysInTargetMonth);
  return new Date(
    Date.UTC(
      targetYear,
      targetMonthIndex,
      day,
      anchor.getUTCHours(),
      anchor.getUTCMinutes(),
      anchor.getUTCSeconds(),
      anchor.getUTCMilliseconds()
    )
  );
}

/**
 * The current Beta commercial period: repeats monthly starting at `anchor`
 * (contract §4.3 — "the Beta period repeats monthly starting from the
 * AccessGrant's period_anchor"). Returns the half-open `[periodStart,
 * periodEnd)` window containing `now`. Deliberately deterministic given
 * `(anchor, now)` — unlike a period derived from wall-clock-at-publish-time,
 * this returns the *same* window for repeated calls within one period, so a
 * lease renewal that changes nothing else does not perturb the semantic
 * hash and does not spuriously bump `entitlementVersion`.
 */
export function computeCurrentPeriod(
  anchor: Date,
  now: Date
): { periodStart: Date; periodEnd: Date } {
  let monthsElapsed =
    (now.getUTCFullYear() - anchor.getUTCFullYear()) * 12 +
    (now.getUTCMonth() - anchor.getUTCMonth());

  let periodStart = addMonthsUTC(anchor, monthsElapsed);
  if (periodStart.getTime() > now.getTime()) {
    monthsElapsed -= 1;
    periodStart = addMonthsUTC(anchor, monthsElapsed);
  } else {
    const nextBoundary = addMonthsUTC(anchor, monthsElapsed + 1);
    if (nextBoundary.getTime() <= now.getTime()) {
      monthsElapsed += 1;
      periodStart = addMonthsUTC(anchor, monthsElapsed);
    }
  }

  const periodEnd = addMonthsUTC(anchor, monthsElapsed + 1);
  return { periodStart, periodEnd };
}

export interface PublicationResult {
  snapshotId: string;
  entitlementVersion: number;
  leaseSequence: number;
  projectionVersion: number;
  semanticHash: string;
  semanticVersionChanged: boolean;
  envelope: SignedEnvelope;
}

interface LockedProjectionRow {
  entitlementVersion: number;
  leaseSequence: number;
  semanticHash: string;
  entitlementRegistryVersion: number;
  resolutionPolicyVersion: number;
  platformLimitsVersion: number;
}

/**
 * Publication transaction steps 1-11 (spec §1.7), run against an
 * already-open transaction client. Factored out from `publishSnapshot` so
 * `provisionWorkspace` can run its own grant/binding/projection creation
 * and the initial Snapshot publication inside a single transaction (spec
 * §4.15 steps 6-7), rather than nesting a second `$transaction` call, which
 * Prisma does not support.
 */
export async function publishSnapshotInTx(
  tx: Prisma.TransactionClient,
  teamId: string,
  signingKeyId: string,
  signingKey: Buffer
): Promise<PublicationResult> {
  // Step 1: lock the projection row. No row yet is the first-publication
  // case (spec §4.15) — there is nothing to lock, and the upsert below
  // creates it.
  const locked = await tx.$queryRaw<LockedProjectionRow[]>`
    SELECT "entitlementVersion", "leaseSequence", "semanticHash",
           "entitlementRegistryVersion", "resolutionPolicyVersion",
           "platformLimitsVersion"
    FROM "CommercialAccessProjection"
    WHERE "teamId" = ${teamId}
    FOR UPDATE
  `;
  const existingState: PublicationVersionState = {
    entitlementVersion: locked[0]?.entitlementVersion ?? 0,
    leaseSequence: locked[0]?.leaseSequence ?? 0,
    semanticHash: locked[0]?.semanticHash ?? '',
  };
  // Registry/policy/platform-limits versions are carried forward from the
  // last publication (or default to 1, the schema default, on first
  // publication). A platform-wide entitlement-registry version authority
  // is out of this task's scope; when one exists, its current version
  // replaces this carry-forward read.
  const registryVersion = locked[0]?.entitlementRegistryVersion ?? 1;
  const resolutionPolicyVersion = locked[0]?.resolutionPolicyVersion ?? 1;
  const platformLimitsVersion = locked[0]?.platformLimitsVersion ?? 1;

  const team = await tx.team.findUniqueOrThrow({
    where: { id: teamId },
    select: {
      id: true,
      name: true,
      lifecycle: true,
      suspended: true,
      authorizationEpoch: true,
      projectionVersion: true,
    },
  });

  // Base commercial access source: at most one active binding per
  // Workspace, enforced by a partial unique index on (teamId) WHERE
  // deactivatedAt IS NULL (contract §4.2; migration
  // `commercial_access_binding_one_active`) — so this can only ever find
  // zero or one row, never "base plus its add-ons".
  const binding = await tx.commercialAccessBinding.findFirst({
    where: { teamId, deactivatedAt: null },
    include: { planVersion: true, accessGrant: true, subscription: true },
  });
  if (!binding) {
    throw new Error(
      `Cannot publish an entitlement snapshot for workspace ${teamId}: no active commercial access binding`
    );
  }

  // Add-on AccessGrants are a *separate* source, not reachable through
  // CommercialAccessBinding (that table can only ever hold the one active
  // base source, per the constraint above) — queried directly here.
  const addOnGrants = await tx.accessGrant.findMany({
    where: { teamId, grantKind: 'ADD_ON', revokedAt: null },
    include: { planVersion: true },
  });

  const restrictions = await tx.restriction.findMany({
    where: { teamId, liftedAt: null },
  });
  const restrictionInputs: RestrictionInput[] = restrictions.map((r) => ({
    id: r.id,
    kind: r.kind,
    entitlementKey: r.entitlementKey,
    effect: r.effect,
    restrictedValue: r.restrictedValue as boolean | number,
    reason: r.reason,
  }));
  const restrictionSummary: RestrictionSummaryEntry[] = restrictions.map(
    (r) => ({
      id: r.id,
      kind: r.kind,
      key: r.entitlementKey,
      effect: r.effect,
      value: r.restrictedValue as boolean | number,
      reason: r.reason,
    })
  );

  // Step 1 (resolve): base ∪ addons ∪ restrictions. A distinct
  // platform-clamp input source doesn't exist yet in this task's scope;
  // restrictions already model the "can only lower, never raise" clamp.
  const baseEntitlements = binding.planVersion
    .entitlements as PlanVersionEntitlements;
  const addOnEntitlements = addOnGrants.map(
    (g) => g.planVersion.entitlements as PlanVersionEntitlements
  );
  const resolved = resolveEntitlements(
    baseEntitlements,
    addOnEntitlements,
    restrictionInputs
  );

  // Deterministic source-ref order: base first, then add-ons in the order
  // returned — the semantic hash depends on this order being stable.
  const sourceRefs: SourceRef[] = [];
  if (binding.sourceKind === 'ACCESS_GRANT' && binding.accessGrant) {
    sourceRefs.push({
      kind: 'ACCESS_GRANT',
      id: binding.accessGrant.id,
      plan_version_id: binding.planVersionId,
      starts_at: binding.accessGrant.startsAt.toISOString(),
      ends_at: binding.accessGrant.endsAt
        ? binding.accessGrant.endsAt.toISOString()
        : null,
    });
  } else if (binding.sourceKind === 'SUBSCRIPTION' && binding.subscription) {
    sourceRefs.push({
      kind: 'SUBSCRIPTION',
      id: binding.subscription.id,
      plan_version_id: binding.planVersionId,
      starts_at: binding.effectiveAt.toISOString(),
      ends_at: null,
    });
  } else {
    throw new Error(
      `CommercialAccessBinding for workspace ${teamId} is missing its ${binding.sourceKind} source row`
    );
  }
  for (const grant of addOnGrants) {
    sourceRefs.push({
      kind: 'ACCESS_GRANT',
      id: grant.id,
      plan_version_id: grant.planVersionId,
      starts_at: grant.startsAt.toISOString(),
      ends_at: grant.endsAt ? grant.endsAt.toISOString() : null,
    });
  }

  // Period (contract §4.3): a Subscription carries its confirmed billing
  // period directly; a Beta AccessGrant's period repeats monthly from its
  // periodAnchor, recomputed deterministically from (anchor, now) so an
  // unrelated republish doesn't perturb it.
  const now = new Date();
  let periodStart: Date;
  let periodEnd: Date;
  let periodAnchor: Date;
  if (binding.sourceKind === 'SUBSCRIPTION' && binding.subscription) {
    periodStart = binding.subscription.periodStart;
    periodEnd = binding.subscription.periodEnd;
    periodAnchor = binding.subscription.periodAnchor;
  } else if (binding.accessGrant?.periodAnchor) {
    periodAnchor = binding.accessGrant.periodAnchor;
    ({ periodStart, periodEnd } = computeCurrentPeriod(periodAnchor, now));
  } else {
    throw new Error(
      `Active binding for workspace ${teamId} has neither a Subscription period nor an AccessGrant periodAnchor`
    );
  }

  const semanticHash = computeSemanticHash({
    planVersionId: binding.planVersionId,
    sourceRefs,
    periodStart: periodStart.toISOString(),
    periodEnd: periodEnd.toISOString(),
    periodAnchor: periodAnchor.toISOString(),
    resolvedEntitlements: resolved.entitlements,
    restrictionIds: restrictions.map((r) => r.id),
    restrictionEffects: restrictions.map((r) => r.effect),
    restrictionValues: restrictions.map(
      (r) => r.restrictedValue as boolean | number
    ),
    entitlementRegistryVersion: registryVersion,
    resolutionPolicyVersion,
    platformLimitsVersion,
  });

  // Steps 2-4.
  const decision = decidePublicationVersions(existingState, semanticHash);
  const newProjectionVersion = team.projectionVersion + 1;

  // Step 6: valid_until = min(issuedAt + 24h, every active source
  // deadline, periodEnd, graceDeadline).
  const issuedAt = now;
  const validUntilCandidates: Date[] = [
    new Date(issuedAt.getTime() + 24 * 60 * 60 * 1000),
    periodEnd,
  ];
  for (const ref of sourceRefs) {
    if (ref.ends_at) {
      validUntilCandidates.push(new Date(ref.ends_at));
    }
  }
  if (binding.subscription?.graceEndsAt) {
    validUntilCandidates.push(binding.subscription.graceEndsAt);
  }
  const validUntil = new Date(
    Math.min(...validUntilCandidates.map((d) => d.getTime()))
  );

  // Snapshot id is minted application-side (rather than left to Prisma's
  // schema default) because the signed payload must carry it — it cannot
  // be known only after the row exists.
  const snapshotId = crypto.randomUUID();

  // Step 7: durable audit event for this publication. `resourceId` can be
  // set to the real snapshot id up front (unlike a "create then patch"
  // approach) because the id above is minted before either row exists —
  // and ControlPlaneAuditEvent is append-only (a DB trigger rejects UPDATE
  // and DELETE), so a later patch wouldn't have been possible anyway.
  const auditEvent = await tx.controlPlaneAuditEvent.create({
    data: {
      scopeKind: 'WORKSPACE',
      teamId,
      actorKind: 'SYSTEM',
      actorId: 'entitlement-publication-module',
      executor: 'control-plane',
      eventType: 'entitlement_snapshot.published',
      resourceType: 'EntitlementSnapshot',
      resourceId: snapshotId,
      details: {
        semanticHash,
        entitlementVersion: decision.entitlementVersion,
        leaseSequence: decision.leaseSequence,
        semanticVersionChanged: decision.semanticVersionChanged,
      },
    },
  });

  // Step 8: build and sign the canonical envelope.
  const payload = buildSnapshotPayload({
    snapshotId,
    team: {
      id: team.id,
      name: team.name,
      lifecycle: team.lifecycle,
      suspended: team.suspended,
      authorizationEpoch: team.authorizationEpoch,
      projectionVersion: newProjectionVersion,
    },
    entitlementVersion: decision.entitlementVersion,
    leaseSequence: decision.leaseSequence,
    planVersionId: binding.planVersionId,
    sourceRefs,
    periodStart,
    periodEnd,
    periodAnchor,
    resolvedEntitlements: resolved.entitlements,
    restrictionSummary,
    semanticHash,
    registryVersion,
    resolutionPolicyVersion,
    platformLimitsVersion,
    issuedAt,
    validUntil,
    auditEventId: auditEvent.id,
  });
  const envelope = signEnvelope(payload, signingKeyId, signingKey);
  // The exact immutable bytes both push and pull-repair send: `signEnvelope`
  // builds its returned object field-by-field in a fixed key order, so
  // `JSON.stringify` here is deterministic for a given logical envelope.
  const canonicalEnvelope = Buffer.from(JSON.stringify(envelope), 'utf-8');

  // Step 8 (insert): EntitlementSnapshot with the exact bytes.
  const snapshot = await tx.entitlementSnapshot.create({
    data: {
      id: snapshotId,
      teamId,
      schemaVersion: envelope.protected_header.schema_version,
      workspaceName: team.name,
      authorizationEpoch: team.authorizationEpoch,
      entitlementVersion: decision.entitlementVersion,
      leaseSequence: decision.leaseSequence,
      projectionVersion: newProjectionVersion,
      lifecycle: team.lifecycle,
      suspended: team.suspended,
      planVersionId: binding.planVersionId,
      sourceRefs: sourceRefs as unknown as Prisma.InputJsonValue,
      periodStart,
      periodEnd,
      periodAnchor,
      resolvedEntitlements:
        resolved.entitlements as unknown as Prisma.InputJsonValue,
      restrictionSummary:
        restrictionSummary as unknown as Prisma.InputJsonValue,
      semanticHash,
      entitlementRegistryVersion: registryVersion,
      resolutionPolicyVersion,
      platformLimitsVersion,
      issuedAt,
      validUntil,
      auditEventId: auditEvent.id,
      signatureHmac: envelope.signature,
      signatureKeyId: signingKeyId,
      signatureAlgorithm: envelope.protected_header.signature_algorithm,
      canonicalEnvelope,
    },
  });

  // Step 9: transactional outbox, PENDING.
  await tx.entitlementSnapshotOutbox.create({
    data: {
      snapshotId: snapshot.id,
      teamId,
      deliveryState: 'PENDING',
    },
  });

  // Step 10: projection pointers. `upsert` handles the first-publication
  // case (no row locked in step 1) by creating the row here.
  await tx.commercialAccessProjection.upsert({
    where: { teamId },
    create: {
      teamId,
      currentSnapshotId: snapshot.id,
      entitlementVersion: decision.entitlementVersion,
      leaseSequence: decision.leaseSequence,
      semanticHash,
      entitlementRegistryVersion: registryVersion,
      resolutionPolicyVersion,
      platformLimitsVersion,
    },
    update: {
      currentSnapshotId: snapshot.id,
      entitlementVersion: decision.entitlementVersion,
      leaseSequence: decision.leaseSequence,
      semanticHash,
      entitlementRegistryVersion: registryVersion,
      resolutionPolicyVersion,
      platformLimitsVersion,
    },
  });

  // Step 11: Team counters.
  await tx.team.update({
    where: { id: teamId },
    data: {
      entitlementVersion: decision.entitlementVersion,
      projectionVersion: newProjectionVersion,
    },
  });

  return {
    snapshotId: snapshot.id,
    entitlementVersion: decision.entitlementVersion,
    leaseSequence: decision.leaseSequence,
    projectionVersion: newProjectionVersion,
    semanticHash,
    semanticVersionChanged: decision.semanticVersionChanged,
    envelope,
  };
}

/**
 * Resolves, signs and publishes one `WorkspaceProjectionEnvelope` for
 * `teamId` (spec §1.7 publication transaction, steps 1-11), atomically
 * updating `CommercialAccessProjection` and `Team` counters and enqueueing
 * an outbox entry for delivery. Runs at `Serializable` isolation so two
 * concurrent publications for the same Workspace cannot both observe the
 * pre-publication version state — one wins, the other's transaction fails
 * and the caller retries.
 */
export async function publishSnapshot(
  prisma: PrismaClient,
  teamId: string,
  signingKeyId: string,
  signingKey: Buffer
): Promise<PublicationResult> {
  return prisma.$transaction(
    (tx) => publishSnapshotInTx(tx, teamId, signingKeyId, signingKey),
    { isolationLevel: Prisma.TransactionIsolationLevel.Serializable }
  );
}

/**
 * Workspace provisioning (spec §4.15), steps 3-7. Steps 1-2 (creating the
 * Team with `lifecycle=PROVISIONING` and its first Owner membership) are
 * the caller's responsibility and must already be committed before this
 * runs — this function starts from "ensure a valid Beta PlanVersion
 * exists" and ends with the initial `provisioning`-lifecycle Snapshot
 * publication and its Outbox entry, all as one transaction.
 *
 * The domain side applies this first snapshot and creates the Workspace
 * projection and SpendGuardrail (Task 10's `EntitlementProjectionModule`).
 * On that confirmation, a *separate* later call transitions
 * `Team.lifecycle` to `ACTIVE` and publishes a second `active`-lifecycle
 * Snapshot — not part of this function, since it depends on an external
 * confirmation this function cannot itself wait for.
 */
export async function provisionWorkspace(
  prisma: PrismaClient,
  teamId: string,
  ownerId: string,
  betaPlanVersionId: string,
  signingKeyId: string,
  signingKey: Buffer
): Promise<void> {
  await prisma.$transaction(
    async (tx) => {
      // Step 3: verify preconditions — Team must be PROVISIONING, Beta
      // PlanVersion must exist and be EFFECTIVE.
      const team = await tx.team.findUniqueOrThrow({
        where: { id: teamId },
        select: { lifecycle: true },
      });
      if (team.lifecycle !== 'PROVISIONING') {
        throw new Error(
          `Cannot provision workspace ${teamId}: lifecycle is ${team.lifecycle}, not PROVISIONING`
        );
      }

      const planVersion = await tx.planVersion.findUniqueOrThrow({
        where: { id: betaPlanVersionId },
      });
      if (planVersion.state !== 'EFFECTIVE') {
        throw new Error(
          `Cannot provision workspace ${teamId}: PlanVersion ${betaPlanVersionId} is ${planVersion.state}, not EFFECTIVE`
        );
      }

      // Step 4: create the BASE AccessGrant.
      const now = new Date();
      const accessGrant = await tx.accessGrant.create({
        data: {
          teamId,
          planVersionId: betaPlanVersionId,
          grantKind: 'BASE',
          source: 'beta_provisioning',
          reason: 'Initial Beta workspace provisioning',
          periodAnchor: now,
          startsAt: now,
          grantedBy: ownerId,
        },
      });
      await tx.commercialAccessBinding.create({
        data: {
          teamId,
          sourceKind: 'ACCESS_GRANT',
          accessGrantId: accessGrant.id,
          planVersionId: betaPlanVersionId,
          effectiveAt: now,
        },
      });

      // Step 5: create the CommercialAccessProjection row.
      await tx.commercialAccessProjection.create({ data: { teamId } });

      // Steps 6-7: publish the initial `provisioning`-lifecycle Snapshot
      // and its Outbox entry, in the same transaction as the grant/binding
      // creation above — `Team.lifecycle` is already `PROVISIONING` from
      // the caller's step 1, so `publishSnapshotInTx` naturally publishes
      // that lifecycle without a separate parameter.
      await publishSnapshotInTx(tx, teamId, signingKeyId, signingKey);
    },
    { isolationLevel: Prisma.TransactionIsolationLevel.Serializable }
  );
}
