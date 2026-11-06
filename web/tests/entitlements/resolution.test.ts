import {
  resolveEntitlements,
  computeSemanticHash,
} from '@/lib/entitlements/resolution';
import type { RestrictionInput } from '@/lib/entitlements/types';

describe('resolveEntitlements', () => {
  it('returns base entitlements when no addons or restrictions', () => {
    const base = { 'module.generate.enabled': true, 'members.max': 5 };
    const result = resolveEntitlements(base, [], []);
    expect(result.entitlements).toEqual(base);
  });

  it('combines boolean addons with OR', () => {
    const base = { 'module.scale.enabled': false };
    const addon = { 'module.scale.enabled': true };
    const result = resolveEntitlements(base, [addon], []);
    expect(result.entitlements['module.scale.enabled']).toBe(true);
  });

  it('combines numeric addons with MAX', () => {
    const base = { 'members.max': 5 };
    const addon = { 'members.max': 10 };
    const result = resolveEntitlements(base, [addon], []);
    expect(result.entitlements['members.max']).toBe(10);
  });

  it('applies DISABLE restriction after combination', () => {
    const base = { 'module.scale.enabled': true };
    const restriction: RestrictionInput = {
      id: 'r1',
      kind: 'ABUSE',
      entitlementKey: 'module.scale.enabled',
      effect: 'DISABLE',
      restrictedValue: false,
      reason: 'abuse',
    };
    const result = resolveEntitlements(base, [], [restriction]);
    expect(result.entitlements['module.scale.enabled']).toBe(false);
  });

  it('applies REDUCE_CAP restriction after combination', () => {
    const base = { 'members.max': 10 };
    const restriction: RestrictionInput = {
      id: 'r1',
      kind: 'BILLING',
      entitlementKey: 'members.max',
      effect: 'REDUCE_CAP',
      restrictedValue: 3,
      reason: 'billing issue',
    };
    const result = resolveEntitlements(base, [], [restriction]);
    expect(result.entitlements['members.max']).toBe(3);
  });
});

describe('computeSemanticHash', () => {
  it('produces stable hash for same inputs', () => {
    const inputs = {
      planVersionId: 'pv_1',
      sourceRefs: [],
      periodStart: '2026-08-01T00:00:00Z',
      periodEnd: '2026-09-01T00:00:00Z',
      periodAnchor: '2026-08-01T00:00:00Z',
      resolvedEntitlements: { 'module.generate.enabled': true },
      restrictionIds: [],
      restrictionEffects: [],
      restrictionValues: [],
      entitlementRegistryVersion: 1,
      resolutionPolicyVersion: 1,
      platformLimitsVersion: 1,
    };
    const h1 = computeSemanticHash(inputs);
    const h2 = computeSemanticHash(inputs);
    expect(h1).toBe(h2);
  });

  it('changes when resolved entitlements change', () => {
    const base = {
      planVersionId: 'pv_1',
      sourceRefs: [],
      periodStart: '2026-08-01T00:00:00Z',
      periodEnd: '2026-09-01T00:00:00Z',
      periodAnchor: '2026-08-01T00:00:00Z',
      resolvedEntitlements: { 'members.max': 5 },
      restrictionIds: [],
      restrictionEffects: [],
      restrictionValues: [],
      entitlementRegistryVersion: 1,
      resolutionPolicyVersion: 1,
      platformLimitsVersion: 1,
    };
    const altered = { ...base, resolvedEntitlements: { 'members.max': 10 } };
    expect(computeSemanticHash(base)).not.toBe(computeSemanticHash(altered));
  });
});
