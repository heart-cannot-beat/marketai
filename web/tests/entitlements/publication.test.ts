import {
  buildSnapshotPayload,
  decidePublicationVersions,
  computeCurrentPeriod,
} from '@/lib/entitlements/publication';
import type {
  BuildSnapshotInput,
  PublicationVersionState,
} from '@/lib/entitlements/publication';

function makeInput(
  overrides?: Partial<BuildSnapshotInput>
): BuildSnapshotInput {
  return {
    snapshotId: 'es_1',
    team: {
      id: 'ws_1',
      name: 'Test',
      lifecycle: 'ACTIVE',
      suspended: false,
      authorizationEpoch: 1,
      projectionVersion: 1,
    },
    entitlementVersion: 1,
    leaseSequence: 1,
    planVersionId: 'pv_1',
    sourceRefs: [],
    periodStart: new Date('2026-08-01T00:00:00Z'),
    periodEnd: new Date('2026-09-01T00:00:00Z'),
    periodAnchor: new Date('2026-08-01T00:00:00Z'),
    resolvedEntitlements: { 'module.generate.enabled': true },
    restrictionSummary: [],
    semanticHash: 'abc123',
    registryVersion: 1,
    resolutionPolicyVersion: 1,
    platformLimitsVersion: 1,
    issuedAt: new Date('2026-08-31T10:00:00Z'),
    validUntil: new Date('2026-09-01T00:00:00Z'),
    auditEventId: 'evt_1',
    ...overrides,
  };
}

describe('buildSnapshotPayload', () => {
  it('maps lifecycle to lowercase wire values', () => {
    const payload = buildSnapshotPayload(makeInput());
    expect(payload.lifecycle).toBe('active');
  });

  it('maps PROVISIONING lifecycle', () => {
    const payload = buildSnapshotPayload(
      makeInput({ team: { ...makeInput().team, lifecycle: 'PROVISIONING' } })
    );
    expect(payload.lifecycle).toBe('provisioning');
  });

  it('maps SUSPENDED lifecycle', () => {
    const payload = buildSnapshotPayload(
      makeInput({ team: { ...makeInput().team, lifecycle: 'SUSPENDED' } })
    );
    expect(payload.lifecycle).toBe('suspended');
  });

  it('maps DELETION_PENDING lifecycle', () => {
    const payload = buildSnapshotPayload(
      makeInput({
        team: { ...makeInput().team, lifecycle: 'DELETION_PENDING' },
      })
    );
    expect(payload.lifecycle).toBe('deletion_pending');
  });

  it('maps DELETED lifecycle', () => {
    const payload = buildSnapshotPayload(
      makeInput({ team: { ...makeInput().team, lifecycle: 'DELETED' } })
    );
    expect(payload.lifecycle).toBe('deleted');
  });

  it('throws on unknown lifecycle', () => {
    expect(() =>
      buildSnapshotPayload(
        makeInput({ team: { ...makeInput().team, lifecycle: 'BOGUS' } })
      )
    ).toThrow('Unknown lifecycle');
  });

  it('correctly populates all SnapshotPayload fields', () => {
    const input = makeInput({
      sourceRefs: [
        {
          kind: 'ACCESS_GRANT',
          id: 'ag_1',
          plan_version_id: 'pv_1',
          starts_at: '2026-08-01T00:00:00Z',
          ends_at: null,
        },
      ],
      restrictionSummary: [
        {
          id: 'r_1',
          kind: 'ABUSE',
          key: 'module.scale.enabled',
          effect: 'DISABLE',
          value: false,
          reason: 'abuse',
        },
      ],
    });

    const payload = buildSnapshotPayload(input);

    expect(payload.snapshot_id).toBe('es_1');
    expect(payload.workspace_id).toBe('ws_1');
    expect(payload.workspace_name).toBe('Test');
    expect(payload.authorization_epoch).toBe(1);
    expect(payload.entitlement_version).toBe(1);
    expect(payload.lease_sequence).toBe(1);
    expect(payload.projection_version).toBe(1);
    expect(payload.suspended).toBe(false);
    expect(payload.lifecycle).toBe('active');

    const snap = payload.entitlement_snapshot;
    expect(snap.plan_version_id).toBe('pv_1');
    expect(snap.source_refs).toHaveLength(1);
    expect(snap.source_refs[0].kind).toBe('ACCESS_GRANT');
    expect(snap.period_start).toBe('2026-08-01T00:00:00.000Z');
    expect(snap.period_end).toBe('2026-09-01T00:00:00.000Z');
    expect(snap.period_anchor).toBe('2026-08-01T00:00:00.000Z');
    expect(snap.resolved_entitlements).toEqual({
      'module.generate.enabled': true,
    });
    expect(snap.restriction_summary).toHaveLength(1);
    expect(snap.restriction_summary[0].id).toBe('r_1');
    expect(snap.semantic_hash).toBe('abc123');
    expect(snap.entitlement_registry_version).toBe(1);
    expect(snap.resolution_policy_version).toBe(1);
    expect(snap.platform_limits_version).toBe(1);
    expect(snap.issued_at).toBe('2026-08-31T10:00:00.000Z');
    expect(snap.valid_until).toBe('2026-09-01T00:00:00.000Z');
    expect(snap.audit_event_id).toBe('evt_1');
  });

  it('serializes dates as ISO strings', () => {
    const payload = buildSnapshotPayload(makeInput());
    const snap = payload.entitlement_snapshot;
    expect(typeof snap.period_start).toBe('string');
    expect(typeof snap.period_end).toBe('string');
    expect(typeof snap.period_anchor).toBe('string');
    expect(typeof snap.issued_at).toBe('string');
    expect(typeof snap.valid_until).toBe('string');
  });
});

describe('decidePublicationVersions', () => {
  const base: PublicationVersionState = {
    entitlementVersion: 3,
    leaseSequence: 10,
    semanticHash: 'hash_a',
  };

  it('increments entitlementVersion when semantic hash changes', () => {
    const decision = decidePublicationVersions(base, 'hash_b');
    expect(decision.entitlementVersion).toBe(4);
    expect(decision.semanticVersionChanged).toBe(true);
  });

  it('does not increment entitlementVersion on lease renewal (same hash)', () => {
    const decision = decidePublicationVersions(base, 'hash_a');
    expect(decision.entitlementVersion).toBe(3);
    expect(decision.semanticVersionChanged).toBe(false);
  });

  it('always increments leaseSequence regardless of hash change', () => {
    const unchanged = decidePublicationVersions(base, 'hash_a');
    expect(unchanged.leaseSequence).toBe(11);

    const changed = decidePublicationVersions(base, 'hash_b');
    expect(changed.leaseSequence).toBe(11);
  });

  it('leaseSequence never resets across entitlement version changes', () => {
    const d1 = decidePublicationVersions(base, 'hash_b');
    expect(d1.entitlementVersion).toBe(4);
    expect(d1.leaseSequence).toBe(11);

    const afterBump: PublicationVersionState = {
      entitlementVersion: d1.entitlementVersion,
      leaseSequence: d1.leaseSequence,
      semanticHash: 'hash_b',
    };
    const d2 = decidePublicationVersions(afterBump, 'hash_c');
    expect(d2.entitlementVersion).toBe(5);
    expect(d2.leaseSequence).toBe(12);
  });

  it('handles first publication from zero state', () => {
    const zero: PublicationVersionState = {
      entitlementVersion: 0,
      leaseSequence: 0,
      semanticHash: '',
    };
    const decision = decidePublicationVersions(zero, 'first_hash');
    expect(decision.entitlementVersion).toBe(1);
    expect(decision.leaseSequence).toBe(1);
    expect(decision.semanticVersionChanged).toBe(true);
  });
});

describe('computeCurrentPeriod', () => {
  it('returns a period containing now', () => {
    const anchor = new Date('2026-01-15T00:00:00Z');
    const now = new Date('2026-08-20T12:00:00Z');
    const { periodStart, periodEnd } = computeCurrentPeriod(anchor, now);
    expect(periodStart.getTime()).toBeLessThanOrEqual(now.getTime());
    expect(periodEnd.getTime()).toBeGreaterThan(now.getTime());
  });

  it('is stable across repeated calls within the same period', () => {
    const anchor = new Date('2026-03-01T00:00:00Z');
    const now1 = new Date('2026-08-10T00:00:00Z');
    const now2 = new Date('2026-08-25T23:59:59Z');
    const p1 = computeCurrentPeriod(anchor, now1);
    const p2 = computeCurrentPeriod(anchor, now2);
    expect(p1.periodStart.toISOString()).toBe(p2.periodStart.toISOString());
    expect(p1.periodEnd.toISOString()).toBe(p2.periodEnd.toISOString());
  });
});
