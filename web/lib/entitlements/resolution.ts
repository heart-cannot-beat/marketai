import crypto from 'node:crypto';
import type {
  PlanVersionEntitlements,
  ResolvedEntitlements,
  RestrictionInput,
  SemanticHashInputs,
} from './types';

/**
 * Combines a base PlanVersion's entitlements with zero or more add-on
 * grants' entitlements, then applies operator restrictions. Per spec §1.8,
 * the combination rule is typed and deterministic — boolean keys OR
 * (any active source enabling a feature enables it), numeric keys MAX (the
 * highest cap from any active source wins) — and always runs *before*
 * restrictions, which are the final clamp (spec §1.7 step 1: "base ∪ addons
 * ∪ restrictions ∪ platform clamp").
 *
 * A key present only in an add-on and absent from `base` is carried through
 * as-is: the add-on is still an active source for that key even though the
 * base plan says nothing about it.
 */
export function resolveEntitlements(
  base: PlanVersionEntitlements,
  addOns: PlanVersionEntitlements[],
  restrictions: RestrictionInput[]
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
