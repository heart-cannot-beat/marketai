import type { Page } from '@playwright/test';

// ---------------------------------------------------------------------------
// Branded opaque IDs
// ---------------------------------------------------------------------------

declare const brand: unique symbol;
export type BrandedOpaqueId<K extends EntityKind> = string & {
  readonly [brand]: K;
};

// ---------------------------------------------------------------------------
// Core types
// ---------------------------------------------------------------------------

export type Area =
  | 'library'
  | 'generation'
  | 'assets'
  | 'experiments'
  | 'localization'
  | 'scale';

export type EntityKind =
  | 'product'
  | 'product_version'
  | 'offer_version'
  | 'persona_version'
  | 'template_version'
  | 'prompt_policy_version'
  | 'data_source_version'
  | 'generation_run'
  | 'asset_version'
  | 'experiment'
  | 'winner_decision'
  | 'localization_batch'
  | 'scale_enrollment'
  | 'advice_thread'
  | 'generation_draft'
  | 'campaign'
  | 'ad_set'
  | 'ad';

export interface EntityRef<K extends EntityKind> {
  readonly kind: K;
  readonly runtimeId: BrandedOpaqueId<K>;
  readonly observedVia: 'dom' | 'same_origin_response';
}

export interface EvidenceEnvelope<A extends Area, C extends string> {
  readonly schemaVersion: 1;
  readonly area: A;
  readonly claims: readonly {
    code: C;
    status: 'passed';
  }[];
}

// ---------------------------------------------------------------------------
// Claim code unions
// ---------------------------------------------------------------------------

export type LibraryClaimCode =
  | 'product_created'
  | 'offer_created'
  | 'persona_created'
  | 'template_applied'
  | 'prompt_policy_created'
  | 'data_source_created';

export type GenerationClaimCode =
  | 'generation_submitted'
  | 'generation_completed'
  | 'asset_versions_created';

export type AssetsClaimCode =
  | 'assets_approved'
  | 'asset_needs_changes'
  | 'asset_re_approved'
  | 'export_completed';

export type ExperimentClaimCode =
  | 'experiment_frozen'
  | 'experiment_launched'
  | 'experiment_inconclusive'
  | 'experiment_winner_confirmed';

export type LocalizationClaimCode =
  | 'localization_batch_submitted'
  | 'localization_outcomes_verified'
  | 'localization_retry_succeeded';

export type ScaleClaimCode =
  | 'scale_enrollment_created'
  | 'scale_advice_received'
  | 'scale_draft_created';

// ---------------------------------------------------------------------------
// Area-specific evidence types
// ---------------------------------------------------------------------------

export interface LibraryEvidence extends EvidenceEnvelope<
  'library',
  LibraryClaimCode
> {
  product: EntityRef<'product'>;
  productVersion: EntityRef<'product_version'>;
  offerVersion: EntityRef<'offer_version'>;
  personaVersion: EntityRef<'persona_version'>;
  templateVersion: EntityRef<'template_version'>;
  promptPolicyVersion: EntityRef<'prompt_policy_version'>;
  dataSourceVersion: EntityRef<'data_source_version'>;
}

export interface GenerationSubmittedEvidence extends EvidenceEnvelope<
  'generation',
  GenerationClaimCode
> {
  run: EntityRef<'generation_run'>;
  expectedItemCount: number;
}

export interface GenerationCompletedEvidence extends EvidenceEnvelope<
  'generation',
  GenerationClaimCode
> {
  run: EntityRef<'generation_run'>;
  assetVersions: EntityRef<'asset_version'>[];
}

export interface AssetsEvidence extends EvidenceEnvelope<
  'assets',
  AssetsClaimCode
> {
  approvedRefs: EntityRef<'asset_version'>[];
  reApprovedRef: EntityRef<'asset_version'>;
  originalNeedsChangesRef: EntityRef<'asset_version'>;
  exportClaims: readonly { code: string; status: 'passed' }[];
}

export interface ExperimentFrozenEvidence extends EvidenceEnvelope<
  'experiments',
  ExperimentClaimCode
> {
  experiment: EntityRef<'experiment'>;
  candidateRefs: EntityRef<'asset_version'>[];
}

export interface ExperimentLaunchedEvidence extends EvidenceEnvelope<
  'experiments',
  ExperimentClaimCode
> {
  experiment: EntityRef<'experiment'>;
  campaignRef: EntityRef<'campaign'>;
  adSetRefs: EntityRef<'ad_set'>[];
  adRefs: EntityRef<'ad'>[];
  candidateDeliveries: readonly {
    candidateRef: EntityRef<'asset_version'>;
    adSetRef: EntityRef<'ad_set'>;
    adRef: EntityRef<'ad'>;
  }[];
}

export interface ExperimentInconclusiveEvidence extends EvidenceEnvelope<
  'experiments',
  ExperimentClaimCode
> {
  experiment: EntityRef<'experiment'>;
}

export interface ExperimentWinnerEvidence extends EvidenceEnvelope<
  'experiments',
  ExperimentClaimCode
> {
  experiment: EntityRef<'experiment'>;
  winnerAssetVersion: EntityRef<'asset_version'>;
  winnerDecision: EntityRef<'winner_decision'>;
}

export interface LocalizationBatchEvidence extends EvidenceEnvelope<
  'localization',
  LocalizationClaimCode
> {
  batch: EntityRef<'localization_batch'>;
  itemOutcomes: Record<
    string,
    {
      status: string;
      localizedAssetVersion?: EntityRef<'asset_version'>;
    }
  >;
}

export interface LocalizationOutcomesEvidence extends EvidenceEnvelope<
  'localization',
  LocalizationClaimCode
> {
  batch: EntityRef<'localization_batch'>;
  retriedMarket: string;
  retriedAttemptCount: number;
  localizedAssetVersions: EntityRef<'asset_version'>[];
}

export interface ScaleEnrollmentEvidence extends EvidenceEnvelope<
  'scale',
  ScaleClaimCode
> {
  enrollment: EntityRef<'scale_enrollment'>;
}

export interface ScaleEvidence extends EvidenceEnvelope<
  'scale',
  ScaleClaimCode
> {
  enrollment: EntityRef<'scale_enrollment'>;
  adviceThread: EntityRef<'advice_thread'>;
  generationDraft: EntityRef<'generation_draft'>;
}

// ---------------------------------------------------------------------------
// Fixture-support types
// ---------------------------------------------------------------------------

export interface MemberSessionCapability {
  page(): Page;
  close(): Promise<void>;
}

export interface BarrierRef {
  readonly name: string;
  readonly namespace: string;
}

export interface TerminalCheckpointState {
  phase: 'after_remote_commit';
  remote_effect_committed: true;
  response_received_by_adapter: true;
  worker_barrier_waiting: true;
}

export interface SanitizedEffectSummary {
  idempotencyKeyHash: string;
  effectRef: string;
  effectKind: string;
  namespace: string;
}

export interface RequestAttemptSummary {
  sequence: number;
  idempotencyKeyHash: string;
  requestKind: 'generate' | 'reconcile';
  requestFingerprint: string;
  receivedAt: string;
}

export type MetaScenarioId =
  | 'insufficient_metrics'
  | 'decisive_winner'
  | 'scale_warming'
  | 'scale_declining';

export interface MetaObservationAdvancedResult {
  scenarioApplied: MetaScenarioId;
  observationCount: number;
  provenance: 'customer_sandbox';
}

// ---------------------------------------------------------------------------
// Report types (match golden-path-report.schema.json)
// ---------------------------------------------------------------------------

export type CompletionStatus = 'passed' | 'failed' | 'interrupted';
export type StepOutcome = 'passed' | 'failed' | 'skipped';

export interface AssertionRecord {
  code: string;
  passed: boolean;
  observed: boolean | number | string;
  expected: boolean | number | string;
}

export interface StepRecord {
  step: number;
  label: string;
  outcome: StepOutcome;
  duration_ms: number;
  failure_code: string | null;
  assertions: AssertionRecord[];
}

export interface ConfigAttestation {
  service: string;
  captured_after_step: number;
  generation_adapter: string;
  meta_adapter: string;
  meta_write_enabled: boolean;
  meta_write_token_present: boolean;
  meta_write_ready: boolean;
}

export interface GoldenPathReport {
  schema_version: '1.0';
  run_id: string;
  commit_sha: string;
  started_at: string;
  completed_at: string;
  completion_status: CompletionStatus;
  last_completed_step: number;
  failure_code: string | null;
  steps: StepRecord[];
  config_attestations: ConfigAttestation[];
}
