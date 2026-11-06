// Shared entitlement types for the control plane. Source of truth:
// docs/superpowers/specs/2026-08-31-entitlements-byok-credentials-usage-cost-design.md
// §1.9 (EntitlementSnapshot), §1.14 (Workspace Projection Endpoints).

/**
 * The signed payload carried inside a `SignedEnvelope`. Field names are the
 * exact wire (snake_case) names — this is serialized verbatim into
 * `canonicalEnvelope` and must never be re-keyed on the way out.
 */
export interface SnapshotPayload {
  snapshot_id: string;
  workspace_id: string;
  workspace_name: string;
  authorization_epoch: number;
  entitlement_version: number;
  lease_sequence: number;
  projection_version: number;
  suspended: boolean;
  lifecycle: string;
  entitlement_snapshot: {
    plan_version_id: string;
    source_refs: SourceRef[];
    period_start: string;
    period_end: string;
    period_anchor: string;
    resolved_entitlements: Record<string, boolean | number>;
    restriction_summary: RestrictionSummaryEntry[];
    semantic_hash: string;
    entitlement_registry_version: number;
    resolution_policy_version: number;
    platform_limits_version: number;
    issued_at: string;
    valid_until: string;
    audit_event_id: string;
  };
}

/**
 * Protected header fields carried outside the signed payload.
 */
export interface ProtectedHeader {
  schema_version: 2;
  signature_key_id: string;
  signature_algorithm: 'hmac-sha256';
}

/**
 * The HMAC-signed envelope: a nested `protected_header` containing
 * (`schema_version`, `signature_key_id`, `signature_algorithm`) plus
 * payload plus signature.  The signature covers the protected header and
 * the complete payload; it is never recursively embedded inside the signed
 * payload itself (spec §1.9).
 *
 * Wire format: `{protected_header, payload, signature}`.  The domain
 * projection module (`projection.py`) verifies this exact shape.
 */
export interface SignedEnvelope {
  protected_header: ProtectedHeader;
  payload: SnapshotPayload;
  signature: string;
}

/**
 * The wire shape Push and Pull Repair both send/return (spec §1.14) — the
 * exact bytes stored in `EntitlementSnapshot.canonicalEnvelope`. This is the
 * same shape as `SignedEnvelope`; the alias exists so call sites that mean
 * "the envelope on the wire" per §1.14 can name it that way.
 */
export type WorkspaceProjectionEnvelope = SignedEnvelope;

export interface SourceRef {
  kind: 'ACCESS_GRANT' | 'SUBSCRIPTION';
  id: string;
  plan_version_id: string;
  starts_at: string;
  ends_at: string | null;
}

export interface RestrictionSummaryEntry {
  id: string;
  kind: string;
  key: string;
  effect: string;
  value: boolean | number;
  reason: string;
}

export interface RestrictionInput {
  id: string;
  kind: string;
  entitlementKey: string;
  effect: 'DISABLE' | 'REDUCE_CAP';
  restrictedValue: boolean | number;
  reason: string;
}

export type PlanVersionEntitlements = Record<string, boolean | number>;

export interface ResolvedEntitlements {
  entitlements: Record<string, boolean | number>;
}

export interface SemanticHashInputs {
  planVersionId: string;
  sourceRefs: SourceRef[];
  periodStart: string;
  periodEnd: string;
  periodAnchor: string;
  resolvedEntitlements: Record<string, boolean | number>;
  restrictionIds: string[];
  restrictionEffects: string[];
  restrictionValues: (boolean | number)[];
  entitlementRegistryVersion: number;
  resolutionPolicyVersion: number;
  platformLimitsVersion: number;
}
