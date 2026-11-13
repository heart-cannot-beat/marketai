/**
 * How a Workspace that already holds a snapshot receives the platform default
 * limits (contract §4.2 and §8, #156).
 *
 * Contract §7: a lease renewal keeps `entitlement_version` and only extends
 * the same key/values. The renewal scan republishes through the Publication
 * Module, which resolves the entitlements again on every run, so the first
 * renewal after the deploy must come out as a new `entitlement_version`
 * carrying the three limit keys, and every renewal after it must leave them
 * alone.
 *
 * "Before the deploy" is the same code with an empty default layer, which is
 * exactly the resolution main ran before #156.
 */
import type { PlanVersionEntitlements } from '@/lib/entitlements/types';

let mockLegacyResolution = false;

jest.mock('@/lib/entitlements/resolution', () => {
  const actual = jest.requireActual('@/lib/entitlements/resolution');
  return {
    ...actual,
    resolveEntitlements: (
      base: PlanVersionEntitlements,
      addOns: PlanVersionEntitlements[],
      restrictions: unknown[]
    ) =>
      actual.resolveEntitlements(
        base,
        addOns,
        restrictions,
        mockLegacyResolution ? {} : actual.PLATFORM_ENTITLEMENT_DEFAULTS
      ),
  };
});

import { publishSnapshot } from '@/lib/entitlements/publication';
import { renewExpiringLeases } from '@/lib/entitlements/renewal';
import { verifyEnvelope } from '@/lib/entitlements/signing';
import type { SignedEnvelope, SnapshotPayload } from '@/lib/entitlements/types';

const KEY_ID = 'beta-v1';
const KEY = Buffer.from('test-signing-key', 'utf8');
const TEAM_ID = 'ws_acme_charge';
const PLAN_VERSION_ID = 'pv_beta_1';
const ANCHOR = new Date('2026-10-01T00:00:00.000Z');
const DEPLOYED_AT = new Date('2026-11-12T08:00:00.000Z');
const HOUR_MS = 60 * 60 * 1000;
const GIB = 1024 * 1024 * 1024;

const LIMIT_DEFAULTS = {
  'members.max': 5,
  'provider_calls.concurrent.max': 2,
  'storage.bytes.max': 10737418240,
};

const at = (hoursFromDeploy: number) =>
  new Date(DEPLOYED_AT.getTime() + hoursFromDeploy * HOUR_MS);

interface ProjectionRow {
  entitlementVersion: number;
  leaseSequence: number;
  semanticHash: string;
  entitlementRegistryVersion: number;
  resolutionPolicyVersion: number;
  platformLimitsVersion: number;
}

/**
 * One ACTIVE Workspace on a base AccessGrant, held in memory with just the
 * Prisma calls the Publication Module and the renewal scan make. Writes land
 * in `state`, and the next publication's `FOR UPDATE` read sees them.
 */
function makeWorkspace(planEntitlements: PlanVersionEntitlements) {
  const state = {
    projection: undefined as ProjectionRow | undefined,
    team: {
      id: TEAM_ID,
      name: 'Acme Charge',
      lifecycle: 'ACTIVE',
      suspended: false,
      authorizationEpoch: 4,
      projectionVersion: 0,
      entitlementVersion: 0,
    },
    snapshots: [] as Array<Record<string, any>>,
    outbox: [] as Array<Record<string, any>>,
    audit: [] as Array<Record<string, any>>,
  };

  const tx = {
    $queryRaw: jest.fn(async () =>
      state.projection ? [{ ...state.projection }] : []
    ),
    team: {
      findUniqueOrThrow: jest.fn(async () => ({ ...state.team })),
      update: jest.fn(async ({ data }: { data: Record<string, number> }) =>
        Object.assign(state.team, data)
      ),
    },
    commercialAccessBinding: {
      findFirst: jest.fn(async () => ({
        sourceKind: 'ACCESS_GRANT',
        planVersionId: PLAN_VERSION_ID,
        effectiveAt: ANCHOR,
        planVersion: { id: PLAN_VERSION_ID, entitlements: planEntitlements },
        accessGrant: {
          id: 'ag_base_1',
          startsAt: ANCHOR,
          endsAt: null,
          periodAnchor: ANCHOR,
        },
        subscription: null,
      })),
    },
    accessGrant: { findMany: jest.fn(async () => []) },
    restriction: { findMany: jest.fn(async () => []) },
    controlPlaneAuditEvent: {
      create: jest.fn(async ({ data }: { data: Record<string, any> }) => {
        state.audit.push(data);
        return { id: `evt_${state.audit.length}` };
      }),
    },
    entitlementSnapshot: {
      create: jest.fn(async ({ data }: { data: Record<string, any> }) => {
        state.snapshots.push(data);
        return data;
      }),
    },
    entitlementSnapshotOutbox: {
      create: jest.fn(async ({ data }: { data: Record<string, any> }) => {
        state.outbox.push(data);
        return data;
      }),
    },
    commercialAccessProjection: {
      upsert: jest.fn(
        async ({
          create,
          update,
        }: {
          create: ProjectionRow;
          update: ProjectionRow;
        }) => {
          const row = state.projection ? update : create;
          state.projection = {
            entitlementVersion: row.entitlementVersion,
            leaseSequence: row.leaseSequence,
            semanticHash: row.semanticHash,
            entitlementRegistryVersion: row.entitlementRegistryVersion,
            resolutionPolicyVersion: row.resolutionPolicyVersion,
            platformLimitsVersion: row.platformLimitsVersion,
          };
        }
      ),
    },
  };

  // The renewal scan reads the due Workspaces from the client, then
  // publishes each one in its own transaction.
  const prisma = {
    $queryRaw: jest.fn(async () => [{ teamId: TEAM_ID }]),
    $transaction: jest.fn(async (fn: (client: typeof tx) => Promise<unknown>) =>
      fn(tx)
    ),
  };

  return { state, prisma };
}

type Workspace = ReturnType<typeof makeWorkspace>;

/** What the dispatcher pushes: the stored bytes, verified as the domain would. */
function pushedPayload(snapshot: Record<string, any>): SnapshotPayload {
  const envelope = JSON.parse(
    Buffer.from(snapshot.canonicalEnvelope).toString('utf-8')
  ) as SignedEnvelope;
  return verifyEnvelope(envelope, new Map([[KEY_ID, KEY]]));
}

async function provisionAt(workspace: Workspace, now: Date) {
  jest.setSystemTime(now);
  await publishSnapshot(workspace.prisma as any, TEAM_ID, KEY_ID, KEY);
  return workspace.state.snapshots.at(-1)!;
}

async function renewAt(workspace: Workspace, now: Date) {
  jest.setSystemTime(now);
  const result = await renewExpiringLeases(workspace.prisma as any, {
    signingKeyId: KEY_ID,
    signingKey: KEY,
    now,
  });
  expect(result).toEqual({
    examined: 1,
    renewed: 1,
    failed: 0,
    failures: [],
  });
  return workspace.state.snapshots.at(-1)!;
}

describe('platform default limits for Workspaces that already hold a snapshot', () => {
  beforeEach(() => {
    jest.useFakeTimers({ now: at(-24) });
    mockLegacyResolution = false;
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it('publishes the first renewal after the deploy as a new entitlement_version carrying the three keys', async () => {
    const workspace = makeWorkspace({ controlled_mock: true });

    // Before the deploy: published and renewed without the limit keys.
    mockLegacyResolution = true;
    const provisioned = await provisionAt(workspace, at(-12));
    const oldRenewal = await renewAt(workspace, at(-6));
    expect(provisioned.resolvedEntitlements).toEqual({ controlled_mock: true });
    expect(oldRenewal.entitlementVersion).toBe(1);
    expect(oldRenewal.leaseSequence).toBe(2);
    expect(oldRenewal.resolvedEntitlements).toEqual({ controlled_mock: true });

    // The deploy.
    mockLegacyResolution = false;
    const first = await renewAt(workspace, at(0));

    expect(first.entitlementVersion).toBe(2);
    expect(first.leaseSequence).toBe(3);
    expect(first.semanticHash).not.toBe(oldRenewal.semanticHash);
    expect(first.resolvedEntitlements).toEqual({
      controlled_mock: true,
      ...LIMIT_DEFAULTS,
    });
    expect(workspace.state.audit.at(-1)!.details).toMatchObject({
      entitlementVersion: 2,
      leaseSequence: 3,
      semanticVersionChanged: true,
    });

    // The signed bytes the dispatcher pushes carry the new version and keys.
    const payload = pushedPayload(first);
    expect(payload.entitlement_version).toBe(2);
    expect(payload.lease_sequence).toBe(3);
    expect(payload.entitlement_snapshot.resolved_entitlements).toEqual({
      controlled_mock: true,
      ...LIMIT_DEFAULTS,
    });
    expect(payload.entitlement_snapshot.semantic_hash).toBe(first.semanticHash);
    expect(workspace.state.outbox.at(-1)).toEqual({
      snapshotId: first.id,
      teamId: TEAM_ID,
      deliveryState: 'PENDING',
    });

    // Projection pointers and Team counters follow the new version.
    expect(workspace.state.projection).toMatchObject({
      entitlementVersion: 2,
      leaseSequence: 3,
      semanticHash: first.semanticHash,
    });
    expect(workspace.state.team.entitlementVersion).toBe(2);
  });

  it('keeps entitlement_version and the key/values on every later renewal', async () => {
    const workspace = makeWorkspace({ controlled_mock: true });
    mockLegacyResolution = true;
    await provisionAt(workspace, at(-12));
    mockLegacyResolution = false;
    const first = await renewAt(workspace, at(0));
    const firstBytes = JSON.stringify(
      pushedPayload(first).entitlement_snapshot.resolved_entitlements
    );

    let leaseSequence = first.leaseSequence;
    for (const hours of [6, 12, 18, 30]) {
      const renewal = await renewAt(workspace, at(hours));
      leaseSequence += 1;

      expect(renewal.entitlementVersion).toBe(2);
      expect(renewal.leaseSequence).toBe(leaseSequence);
      expect(renewal.semanticHash).toBe(first.semanticHash);
      expect(renewal.validUntil.getTime()).toBeGreaterThan(
        first.validUntil.getTime()
      );
      expect(workspace.state.audit.at(-1)!.details).toMatchObject({
        semanticVersionChanged: false,
      });
      // Same keys, same values, same order: the signed content is identical.
      expect(
        JSON.stringify(
          pushedPayload(renewal).entitlement_snapshot.resolved_entitlements
        )
      ).toBe(firstBytes);
    }
  });

  it('leaves a Workspace whose plan already sets the three keys on its entitlement_version', async () => {
    const planEntitlements = {
      controlled_mock: true,
      'members.max': 8,
      'provider_calls.concurrent.max': 3,
      'storage.bytes.max': 20 * GIB,
    };
    const workspace = makeWorkspace(planEntitlements);

    mockLegacyResolution = true;
    const before = await provisionAt(workspace, at(-12));
    mockLegacyResolution = false;
    const after = await renewAt(workspace, at(0));

    expect(after.entitlementVersion).toBe(before.entitlementVersion);
    expect(after.leaseSequence).toBe(before.leaseSequence + 1);
    expect(after.semanticHash).toBe(before.semanticHash);
    expect(after.resolvedEntitlements).toEqual(planEntitlements);
  });

  it('gives a plan that sets one key the other two in a new version, keeping its own value', async () => {
    const workspace = makeWorkspace({
      controlled_mock: true,
      'members.max': 3,
    });

    mockLegacyResolution = true;
    await provisionAt(workspace, at(-12));
    mockLegacyResolution = false;
    const after = await renewAt(workspace, at(0));

    expect(after.entitlementVersion).toBe(2);
    expect(after.resolvedEntitlements).toEqual({
      controlled_mock: true,
      'members.max': 3,
      'provider_calls.concurrent.max': 2,
      'storage.bytes.max': 10737418240,
    });
  });
});
