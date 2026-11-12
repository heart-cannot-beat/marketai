import {
  PLATFORM_ENTITLEMENT_DEFAULTS,
  resolveEntitlements,
  computeSemanticHash,
} from '@/lib/entitlements/resolution';
import type { RestrictionInput } from '@/lib/entitlements/types';

const GIB = 1024 * 1024 * 1024;

function reduceCap(key: string, value: number): RestrictionInput {
  return {
    id: `r_${key}`,
    kind: 'BILLING',
    entitlementKey: key,
    effect: 'REDUCE_CAP',
    restrictedValue: value,
    reason: 'billing issue',
  };
}

describe('resolveEntitlements', () => {
  it('returns base entitlements unchanged when the base sets every limit key', () => {
    const base = {
      'module.generate.enabled': true,
      'members.max': 5,
      'provider_calls.concurrent.max': 2,
      'storage.bytes.max': 10 * GIB,
    };
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

// Contract §4.2 platform default layer, with the §8 Beta values.
describe('resolveEntitlements platform defaults', () => {
  it('holds the contract §8 Beta values', () => {
    expect(PLATFORM_ENTITLEMENT_DEFAULTS).toEqual({
      'members.max': 5,
      'provider_calls.concurrent.max': 2,
      'storage.bytes.max': 10737418240,
    });
    expect(PLATFORM_ENTITLEMENT_DEFAULTS['storage.bytes.max']).toBe(10 * GIB);
    expect(Object.isFrozen(PLATFORM_ENTITLEMENT_DEFAULTS)).toBe(true);
  });

  it('fills every limit key no source sets, and only those', () => {
    // The shape of the known Beta PlanVersions.
    const result = resolveEntitlements({ controlled_mock: true }, [], []);
    expect(result.entitlements).toEqual({
      controlled_mock: true,
      'members.max': 5,
      'provider_calls.concurrent.max': 2,
      'storage.bytes.max': 10737418240,
    });
  });

  it('fills the defaults when there are no sources at all', () => {
    expect(resolveEntitlements({}, [], []).entitlements).toEqual(
      PLATFORM_ENTITLEMENT_DEFAULTS
    );
  });

  it('keeps a base value below or above the default', () => {
    const result = resolveEntitlements(
      { 'members.max': 3, 'storage.bytes.max': 20 * GIB },
      [],
      []
    );
    expect(result.entitlements['members.max']).toBe(3);
    expect(result.entitlements['storage.bytes.max']).toBe(20 * GIB);
    // The key the base left out still gets its default.
    expect(result.entitlements['provider_calls.concurrent.max']).toBe(2);
  });

  it('keeps the MAX across base and add-ons', () => {
    const result = resolveEntitlements(
      { 'members.max': 10, 'provider_calls.concurrent.max': 1 },
      [
        { 'members.max': 7, 'provider_calls.concurrent.max': 4 },
        { 'provider_calls.concurrent.max': 3 },
      ],
      []
    );
    expect(result.entitlements['members.max']).toBe(10);
    expect(result.entitlements['provider_calls.concurrent.max']).toBe(4);
  });

  it('takes an add-on value as the source when the base omits the key', () => {
    // The default only fills a key no source set; it does not join the MAX.
    const result = resolveEntitlements(
      { controlled_mock: true },
      [{ 'members.max': 3 }, { 'storage.bytes.max': 50 * GIB }],
      []
    );
    expect(result.entitlements['members.max']).toBe(3);
    expect(result.entitlements['storage.bytes.max']).toBe(50 * GIB);
    expect(result.entitlements['provider_calls.concurrent.max']).toBe(2);
  });

  it('lets REDUCE_CAP lower a defaulted key below the default', () => {
    const result = resolveEntitlements(
      { controlled_mock: true },
      [],
      [
        reduceCap('members.max', 2),
        reduceCap('provider_calls.concurrent.max', 0),
        reduceCap('storage.bytes.max', GIB),
      ]
    );
    expect(result.entitlements['members.max']).toBe(2);
    expect(result.entitlements['provider_calls.concurrent.max']).toBe(0);
    expect(result.entitlements['storage.bytes.max']).toBe(GIB);
  });

  it('never lets REDUCE_CAP raise a defaulted key', () => {
    const result = resolveEntitlements({}, [], [reduceCap('members.max', 50)]);
    expect(result.entitlements['members.max']).toBe(5);
  });

  it('lets DISABLE switch a defaulted key off', () => {
    const result = resolveEntitlements(
      {},
      [],
      [
        {
          id: 'r_disable',
          kind: 'ABUSE',
          entitlementKey: 'provider_calls.concurrent.max',
          effect: 'DISABLE',
          restrictedValue: false,
          reason: 'abuse',
        },
      ]
    );
    expect(result.entitlements['provider_calls.concurrent.max']).toBe(false);
  });

  it('does not mutate its inputs', () => {
    const base = { controlled_mock: true };
    const addon = { 'members.max': 8 };
    resolveEntitlements(base, [addon], [reduceCap('members.max', 6)]);
    expect(base).toEqual({ controlled_mock: true });
    expect(addon).toEqual({ 'members.max': 8 });
  });

  it('accepts an explicit default layer', () => {
    // An empty layer reproduces the resolution before the platform defaults.
    expect(
      resolveEntitlements({ controlled_mock: true }, [], [], {}).entitlements
    ).toEqual({ controlled_mock: true });
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
