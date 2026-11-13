import crypto from 'node:crypto';
import type {
  PlanVersionEntitlements,
  ResolvedEntitlements,
  RestrictionInput,
  SemanticHashInputs,
} from './types';

/**
 * Platform default layer (contract §4.2), holding the Beta limits of
 * contract §8: 5 Owners + Members, 2 concurrent real Provider calls and
 * 10 GiB of private storage. It fills a key only when no source sets it.
 */
export const PLATFORM_ENTITLEMENT_DEFAULTS: Readonly<PlanVersionEntitlements> =
  Object.freeze({
    'members.max': 5,
    'provider_calls.concurrent.max': 2,
    'storage.bytes.max': 10 * 1024 * 1024 * 1024,
  });

/**
 * Resolves a Workspace's entitlements in the contract §4.2 order: base
 * PlanVersion, add-on grants, platform defaults, then restrictions.
 *
 * Per spec §1.8 the combination of sources is typed and deterministic —
 * boolean keys OR (any active source enabling a feature enables it),
 * numeric keys MAX (the highest cap from any active source wins). A key
 * present only in an add-on and absent from `base` is carried through
 * as-is: the add-on is still an active source for that key even though the
 * base plan says nothing about it.
 *
 * `platformDefaults` then fills only the keys no source set; a key any
 * source set keeps the source's value, and a default never joins the MAX.
 * Restrictions run last and are the final clamp, so a REDUCE_CAP can take a
 * defaulted key below its default.
 */
export function resolveEntitlements(
  base: PlanVersionEntitlements,
  addOns: PlanVersionEntitlements[],
  restrictions: RestrictionInput[],
  platformDefaults: Readonly<PlanVersionEntitlements> = PLATFORM_ENTITLEMENT_DEFAULTS
): ResolvedEntitlements {
  const combined: Record<string, boolean | number> = { ...base };

  for (const addon of addOns) {
    for (const [key, value] of Object.entries(addon)) {
      const existing = combined[key];
      if (existing === undefined) {
        combined[key] = value;
      } else if (typeof value === 'boolean' && typeof existing === 'boolean') {
        combined[key] = existing || value;
      } else if (typeof value === 'number' && typeof existing === 'number') {
        combined[key] = Math.max(existing, value);
      }
      // A type mismatch between an existing combined value and an add-on's
      // value for the same key is a registry-configuration error the
      // resolver cannot safely arbitrate; the existing value is kept
      // unchanged rather than guessing.
    }
  }

  for (const [key, value] of Object.entries(platformDefaults)) {
    if (combined[key] === undefined) {
      combined[key] = value;
    }
  }

  for (const r of restrictions) {
    if (r.effect === 'DISABLE') {
      combined[r.entitlementKey] = false;
    } else if (
      r.effect === 'REDUCE_CAP' &&
      typeof r.restrictedValue === 'number'
    ) {
      const current = combined[r.entitlementKey];
      if (typeof current === 'number') {
        combined[r.entitlementKey] = Math.min(current, r.restrictedValue);
      }
    }
  }

  return { entitlements: combined };
}

/**
 * Deterministic SHA-256 over every entitlement-semantic input (spec §1.9):
 * plan/source references including validity windows, periods, restriction
 * IDs/effects/values, resolved entitlements, and the three registry/policy
 * version numbers. Authorization, lifecycle, Workspace display name and
 * lease-only fields are deliberately excluded — they are still signed and
 * ordered by `projectionVersion`/`leaseSequence`, but they must not trigger
 * an `entitlementVersion` bump on their own (publication step 2-3).
 *
 * Keys are explicitly ordered field-by-field (not spread from `inputs`) so
 * `JSON.stringify` produces the same byte sequence for logically identical
 * inputs regardless of property insertion order at the call site.
 */
export function computeSemanticHash(inputs: SemanticHashInputs): string {
  const canonical = JSON.stringify({
    plan_version_id: inputs.planVersionId,
    source_refs: inputs.sourceRefs,
    period_start: inputs.periodStart,
    period_end: inputs.periodEnd,
    period_anchor: inputs.periodAnchor,
    resolved_entitlements: inputs.resolvedEntitlements,
    restriction_ids: inputs.restrictionIds,
    restriction_effects: inputs.restrictionEffects,
    restriction_values: inputs.restrictionValues,
    entitlement_registry_version: inputs.entitlementRegistryVersion,
    resolution_policy_version: inputs.resolutionPolicyVersion,
    platform_limits_version: inputs.platformLimitsVersion,
  });
  return crypto.createHash('sha256').update(canonical).digest('hex');
}
